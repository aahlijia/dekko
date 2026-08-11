"""The dekko daemon: accept loop, request routing, explicit lifecycle.

Phase 2 of ``.features/daemon-mode/``: a per-repo background process
the bare CLI can talk to instead of paying a full ``map.json``
reload on every invocation. This module owns:

- The daemon-side accept loop (``serve_daemon``), built on
  ``daemon_transport.DaemonTransport`` so it never branches on
  ``sys.platform`` itself.
- Request/response framing: newline-delimited JSON, one request line
  in, one response line out, then the connection closes -- the same
  shape ``server.py``'s stdio transport already uses (see that
  module's docstring), reused rather than reinvented.
- Routing: reusing ``cli.py``'s *exact* ``run_*`` functions
  (``args.func`` would have called directly) for every daemon-
  eligible subcommand, so this module can never drift from direct-
  execution behavior for any flag it covers.
- The client-side helper (``try_daemon``) ``cli.py``'s ``main()``
  calls before falling back to direct execution, and the lifecycle
  helpers (``start``/``stop``/``status``) backing ``dekko daemon
  start/stop/status``.

Phase 3 (``.features/daemon-mode/TRACKER.md``) adds a single-slot warm
``MapIndex`` cache (``_WarmCache``) that ``serve_daemon`` installs into
``cli.py``'s ``_load_or_regen`` via ``cli.set_daemon_cache_hook`` at
startup, re-validated on every access via ``mapfile.check_freshness``
-- the same freshness oracle ``server.py``'s ``Context.index_cache``
already uses for the MCP server's analogous cache. A daemon serving
repeated requests against an unchanged map skips the JSON-parse/
index-rebuild step entirely after the first request; a working-tree
change (or an out-of-band ``dekko map`` regen) is picked up on the
next request, never served stale.
"""

import argparse
import json
import os
import socket
import sys
import time
from collections.abc import Callable
from pathlib import Path

from . import mapfile
from .daemon_transport import (
    DaemonTransport,
    DaemonUnavailableError,
    TransportUnavailable,
    default_transport_for,
    force_stop,
    is_daemon_reachable,
    spawn_detached,
)

# Read-only subcommands eligible for daemon routing (design doc §2.3).
# Write-path commands -- map (regen), note add/rm, hooks
# install/uninstall/run -- always run directly, regardless of whether
# a daemon is running, sidestepping write-concurrency questions
# entirely for v1.
_DAEMON_ELIGIBLE = frozenset(
    {
        "query",
        "search",
        "workset",
        "diff",
        "affected",
        "outline",
        "context",
        "trace",
        "stats",
        "summary",
        "lean",
        "unused",
        "status",
        "ledger",
        "note",
        "export",
    }
)

# Reserved protocol verbs, distinct from any real subcommand name (no
# entry in SUBCOMMANDS starts with "_") so they can never collide with
# a routed command.
_SHUTDOWN_CMD = "_shutdown"
_STATUS_CMD = "_status"

# Default self-shutdown window: 30 minutes with no requests (design
# doc §2.1).
DEFAULT_IDLE_TIMEOUT = 1800.0

# Per-request socket timeout once a connection has been accepted --
# generous relative to even a full map reload, tight enough that one
# wedged client can't hang the accept loop indefinitely.
_REQUEST_TIMEOUT = 30.0

# Client-side connect/round-trip timeout (design doc §2.5: "generous
# relative to the reload times this whole feature exists to avoid,
# tight enough that a genuinely hung daemon doesn't make every
# subsequent `dekko` call visibly slower than not having a daemon at
# all"). Originally a separate, much tighter 2.0s constant -- round-12
# master report §3.5: ``socket.settimeout()`` covers the *entire*
# connect-send-recv cycle, not just connection setup, and the
# single-threaded accept loop (see ``serve_daemon``'s docstring) means
# a concurrent request can't even be accepted, let alone answered,
# until whatever the daemon is currently servicing finishes. 2.0s was
# tighter than a routine cold-cache reload on a large repo (round 11's
# own numbers: 6-8s just to reload map.json on tensorflow), so both
# ``status()`` and ``try_daemon()`` would misreport/time out on
# entirely ordinary requests, not just pathological ones --
# ``status()`` printed a false "not running" while the daemon was
# alive and busy, and ``try_daemon()`` abandoned the original slow
# request mid-flight and silently duplicated the work locally, while
# the daemon kept computing the orphaned request in the background.
# Matching the server's own per-request budget (``_REQUEST_TIMEOUT``)
# means a client never gives up before the daemon itself would --
# turning the false-negative into an honest wait when the daemon is
# genuinely busy, not fixing the daemon's single-request-at-a-time
# design (a separate, larger architectural change; see that
# docstring for why it's single-threaded on purpose).
_CLIENT_TIMEOUT = _REQUEST_TIMEOUT

# Bootstrap script used to spawn the detached daemon process. There is
# no ``src/dekko/__main__.py`` (the packaged entry point is the
# ``dekko`` console script, ``dekko.cli:main``, per pyproject.toml),
# so ``python -m dekko`` isn't available; ``python -c <bootstrap>
# daemon _serve ...`` instead guarantees the spawned daemon imports
# the *same* dekko package on the *same* interpreter running the
# parent ``dekko daemon start`` invocation, rather than depending on
# whatever ``dekko`` happens to resolve to first on PATH (which may be
# a differently-versioned install -- see project memory on
# ``~/.local/bin/dekko`` being a separate frozen install from a dev
# checkout).
_SERVE_BOOTSTRAP = "import sys; from dekko.cli import main; sys.exit(main())"


def _dispatch_table() -> dict[str, Callable[[argparse.Namespace], int]]:
    """Build the daemon's ``{command name -> cli.py function}`` map.

    A manual mapping, not argparse introspection (deferred import to
    avoid a circular import with ``cli.py``, which imports this
    module at load time for ``try_daemon``/``daemon start/stop/
    status``): every value here is the *exact* function object
    ``args.func`` would have called directly, so there is no second
    copy of dispatch or argument-handling logic to drift out of sync
    with ``cli.py``'s real subparser wiring. If ``cli.py`` ever
    renames or removes one of these, this import breaks loudly at
    daemon startup rather than silently routing to something stale.
    """
    from . import cli

    return {
        "query": cli.run_query,
        "search": cli.run_search,
        "workset": cli.run_workset,
        "diff": cli.run_diff,
        "affected": cli.run_affected,
        "outline": cli.run_outline,
        "context": cli.run_context,
        "trace": cli.run_trace,
        "stats": cli.run_stats,
        "summary": cli.run_summary,
        "lean": cli.run_lean,
        "unused": cli.run_unused,
        "status": cli.run_status,
        "ledger": cli.run_ledger,
        "note": cli.run_note,
        "export": cli.run_export,
    }


class _WarmCache:
    """Single-slot warm ``MapIndex`` cache for one daemon process.

    One daemon serves exactly one repo root (its transport artifact
    lives inside that root's own ``.dekko/``), so this doesn't need
    ``server.Context.index_cache``'s dict-keyed shape the way a
    multi-root MCP server session does -- a single slot is enough,
    re-validated via ``mapfile.check_freshness`` on every access
    exactly as that cache is (design doc §2.4). Installed into
    ``cli._load_or_regen`` via ``cli.set_daemon_cache_hook`` for the
    lifetime of ``serve_daemon``'s accept loop.
    """

    def __init__(self) -> None:
        self._index: mapfile.MapIndex | None = None
        self._root: Path | None = None
        self.hits = 0
        self.misses = 0

    def get(self, root: Path) -> mapfile.MapIndex | None:
        """Return the cached index for ``root`` if still fresh, else
        ``None``.

        Every call that doesn't return a fresh hit counts as a miss --
        including the very first call before anything has ever been
        cached -- so ``snapshot()``'s hit/miss counters describe every
        cache-check ``_load_or_regen`` made, not just the ones that
        found something to check.
        """
        if self._index is not None and self._root == root:
            if mapfile.check_freshness(root, self._index).fresh:
                self.hits += 1
                return self._index
        self.misses += 1
        return None

    def put(self, root: Path, index: mapfile.MapIndex) -> None:
        """Record a freshly loaded ``index`` for ``root``."""
        self._root = root
        self._index = index

    def snapshot(self) -> dict | None:
        """Status-reportable cache state, or ``None`` before any
        request has populated the cache."""
        if self._index is None or self._root is None:
            return None
        fresh = mapfile.check_freshness(self._root, self._index).fresh
        return {
            "cached_root": str(self._root),
            "fresh": fresh,
            "hits": self.hits,
            "misses": self.misses,
        }


def _recv_line(sock: socket.socket) -> str | None:
    """Read one newline-terminated line from ``sock``.

    Deliberately avoids ``socket.makefile()`` (see
    ``daemon_transport._recv_line``'s docstring for the read-ahead
    buffering bug that caused). Unlike that helper, this one has no
    small fixed size cap -- a captured ``stdout``/``stderr`` response
    can legitimately run to tens of KB (a budget-capped search/
    workset result), so it accumulates in ``recv(65536)`` chunks
    instead of byte-at-a-time.

    Each connection carries exactly one request line and one response
    line (no streaming, per the design doc's §2.2 framing), so any
    bytes received past the first newline in the same chunk are
    discarded rather than buffered for a next call -- there never is
    a next call on the same connection.

    Returns:
        The decoded line, without its trailing newline, or ``None``
        if the connection closed before a complete line arrived.
    """
    chunks = bytearray()
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            return None
        newline_at = chunk.find(b"\n")
        if newline_at == -1:
            chunks += chunk
            continue
        chunks += chunk[:newline_at]
        return chunks.decode("utf-8", errors="replace")


def _send_line(sock: socket.socket, payload: dict) -> None:
    """Write one JSON-encoded line (newline-terminated) to ``sock``."""
    sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))


def _status_payload(
    transport: DaemonTransport, start_time: float, cache: "_WarmCache"
) -> dict:
    """Build the response body for a ``_status`` protocol request."""
    return {
        "running": True,
        "pid": os.getpid(),
        "uptime_seconds": round(time.monotonic() - start_time, 3),
        "transport": transport.describe(),
        # None until the first daemon-routed read populates the
        # cache; a dict with the cached root, its current freshness,
        # and cumulative hit/miss counts afterward.
        "cache": cache.snapshot(),
    }


def _handle_connection(
    conn: socket.socket,
    transport: DaemonTransport,
    dispatch: dict[str, Callable[[argparse.Namespace], int]],
    start_time: float,
    cache: "_WarmCache",
) -> bool:
    """Handle exactly one accepted connection.

    Args:
        conn: The freshly accepted socket.
        transport: The transport that accepted it (for
            authentication and the status payload's ``describe()``).
        dispatch: ``{command -> cli.py function}``, built once at
            daemon startup.
        start_time: ``time.monotonic()`` value at daemon startup.
        cache: This daemon's warm single-slot index cache, for the
            ``_status`` payload's cache-state report.

    Returns:
        ``False`` if the accept loop should stop after this
        connection (a ``_shutdown`` request was received), ``True``
        otherwise.
    """
    keep_serving = True
    conn.settimeout(_REQUEST_TIMEOUT)
    try:
        if not transport.authenticate(conn):
            return True

        raw = _recv_line(conn)
        if raw is None:
            return True

        try:
            request = json.loads(raw)
        except json.JSONDecodeError:
            _send_line(
                conn,
                {
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": "dekko daemon: malformed request",
                },
            )
            return True

        cmd = request.get("cmd")
        if cmd == _SHUTDOWN_CMD:
            _send_line(conn, {"exit_code": 0, "stdout": "", "stderr": ""})
            return False
        if cmd == _STATUS_CMD:
            _send_line(conn, _status_payload(transport, start_time, cache))
            return True

        func = dispatch.get(cmd)
        if func is None:
            _send_line(
                conn,
                {
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": (
                        f"dekko daemon: unknown or ineligible command {cmd!r}"
                    ),
                },
            )
            return True

        args = argparse.Namespace(**(request.get("args") or {}))
        exit_code, out, err = _run_captured(func, args)
        _send_line(
            conn, {"exit_code": exit_code, "stdout": out, "stderr": err}
        )
    except OSError:
        # A client that disconnects mid-request (or a transient
        # socket error) must not take the whole accept loop down --
        # this connection is simply abandoned.
        pass
    finally:
        conn.close()
    return keep_serving


def _run_captured(
    func: Callable[[argparse.Namespace], int], args: argparse.Namespace
) -> tuple[int, str, str]:
    """Run a routed ``cli.py`` function, capturing stdout/stderr.

    Reuses ``server.py``'s ``_capture`` (same stdout/stderr
    redirection pattern MCP tool calls already rely on) rather than a
    second copy of it. A bare ``except Exception`` around the call
    itself is deliberate and additional to what ``_capture`` provides:
    one malformed or unlucky request (e.g. a reconstructed
    ``Namespace`` missing a field a code path assumed) must return an
    error envelope to its caller, never crash the daemon process out
    from under every other client.
    """
    from .server import _capture

    try:
        return _capture(lambda: func(args))
    except Exception as exc:  # see docstring: must not crash the loop
        return 1, "", f"dekko daemon: internal error: {exc}"


def serve_daemon(
    root: Path,
    idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
    transport: DaemonTransport | None = None,
) -> int:
    """Run the daemon's accept loop for ``root``.

    Serves requests one at a time (design doc §2.6: a single-threaded
    accept loop, not thread-per-connection -- the realistic CLI
    invocation pattern doesn't need true parallelism, and this
    sidesteps every question about locking shared state around
    concurrent access). Returns when a ``_shutdown`` request is
    received or when ``idle_timeout`` seconds pass with no new
    connection since the last one was handled.

    Args:
        root: Repo root this daemon serves.
        idle_timeout: Self-shutdown window, in seconds, with no
            requests.
        transport: Transport to bind, or ``None`` to use
            ``default_transport_for(root)``.

    Returns:
        ``0`` on a clean shutdown (explicit or idle timeout), ``1``
        if the transport could not be bound at all.
    """
    transport = transport or default_transport_for(root)
    try:
        sock = transport.bind_and_listen()
    except TransportUnavailable as exc:
        print(f"dekko daemon: cannot start: {exc}", file=sys.stderr)
        return 1

    # Deferred import: avoids a circular import at module load time
    # with cli.py, which imports this module (see _dispatch_table's
    # docstring for the same reasoning).
    from . import cli

    dispatch = _dispatch_table()
    cache = _WarmCache()
    cli.set_daemon_cache_hook(cache.get, cache.put)
    start_time = time.monotonic()
    sock.settimeout(idle_timeout)
    try:
        while True:
            try:
                conn, _addr = sock.accept()
            except socket.timeout:
                break
            if not _handle_connection(
                conn, transport, dispatch, start_time, cache
            ):
                break
    finally:
        sock.close()
        transport.cleanup()
        # Uninstall so this process-global hook never leaks past this
        # daemon's lifetime -- matters for tests running serve_daemon
        # in-thread (same process, same cli module) across several
        # daemon instances, and simply keeps a stopped daemon's stale
        # cache from being reachable through cli.py by anything else.
        cli.set_daemon_cache_hook(None, None)
    return 0


def try_daemon(args: argparse.Namespace) -> tuple[int, str, str] | None:
    """Attempt to route a parsed CLI invocation through a live daemon.

    Returns ``None`` on *every* absence/error condition (design doc
    §2.5) so ``cli.py``'s ``main()`` integration is the one-line
    ``if result is None: return args.func(args)`` the design doc
    calls for. Never raises -- every failure path here means "fall
    back to direct execution, silently," not "surface an error the
    caller never asked for."

    Args:
        args: The already-parsed ``argparse.Namespace`` for a
            daemon-eligible subcommand (i.e. what ``args.func(args)``
            would otherwise be called with directly).

    Returns:
        ``(exit_code, stdout, stderr)`` from the daemon, or ``None``
        to signal "run directly instead."
    """
    command = getattr(args, "command", None)
    if command not in _DAEMON_ELIGIBLE:
        return None
    # "note" is eligible only for its read-only "list" action --
    # "note add"/"note rm" are write-path and always run directly.
    if command == "note" and getattr(args, "note_action", None) != "list":
        return None

    root_value = getattr(args, "root", None)
    if not root_value:
        return None
    root = Path(root_value).resolve()

    transport = default_transport_for(root)
    if not transport.exists():
        return None

    try:
        sock = transport.client_connect(_CLIENT_TIMEOUT)
    except DaemonUnavailableError:
        return None

    try:
        transport.send_auth_preamble(sock)
        payload = {k: v for k, v in vars(args).items() if k != "func"}
        _send_line(sock, {"cmd": command, "args": payload})
        raw = _recv_line(sock)
        if raw is None:
            return None
        response = json.loads(raw)
        return (
            int(response["exit_code"]),
            str(response.get("stdout", "")),
            str(response.get("stderr", "")),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None
    finally:
        sock.close()


def _query_pid(transport: DaemonTransport) -> int | None:
    """Best-effort PID lookup via a ``_status`` round-trip.

    Used only by ``stop()``'s forced-fallback path, when a graceful
    shutdown request didn't get an answer. Returns ``None`` on any
    failure -- the caller already treats a missing PID as "can't
    force-stop," not as an error to surface.
    """
    try:
        sock = transport.client_connect(_CLIENT_TIMEOUT)
    except DaemonUnavailableError:
        return None
    try:
        transport.send_auth_preamble(sock)
        _send_line(sock, {"cmd": _STATUS_CMD})
        raw = _recv_line(sock)
        if raw is None:
            return None
        data = json.loads(raw)
        pid = data.get("pid")
        return int(pid) if pid is not None else None
    except (OSError, ValueError, TypeError):
        return None
    finally:
        sock.close()


def start(root: Path, idle_timeout: float = DEFAULT_IDLE_TIMEOUT) -> int:
    """Handle ``dekko daemon start``.

    No-op (not an error) if a live daemon is already reachable for
    this root. Spawns a detached background process and returns
    immediately -- it does not wait for the daemon to finish binding
    (design doc §2.1: "returns immediately, does not block").

    Before spawning, runs ``transport.preflight_check()`` in the
    foreground and fails fast with a non-zero exit code if it raises.
    This exists specifically to catch the ``AF_UNIX`` ``sun_path``-
    length case (and any other cheaply-predictable bind failure) here
    rather than only in the detached child: without it, the child's
    own ``bind_and_listen()`` failure is written to stdio nobody reads
    (see ``daemon_transport.spawn_detached``), so ``start()`` used to
    print "started" and exit 0 even though no daemon ever came up --
    the failure only surfaced later, on a subsequent ``daemon
    status``/routed call. This does not close every possible bind
    failure (a real ``bind()`` can still fail for reasons the
    preflight check can't predict, e.g. a permissions error), but it
    closes the one this round's testing actually hit, deterministically,
    every time -- including on a second ``start`` attempt against the
    same broken root, which used to sometimes print the bare "started"
    line with no error at all (an async stderr write from the child
    racing past the parent's own flush).

    Args:
        root: Resolved repo root to serve.
        idle_timeout: Self-shutdown window to pass to the daemon.

    Returns:
        ``0`` on a successful spawn (or an already-running daemon),
        ``1`` if the preflight check or the spawn itself failed.
    """
    transport = default_transport_for(root)
    if is_daemon_reachable(transport):
        print(
            f"dekko daemon: already running for {root} "
            f"({transport.describe()})"
        )
        return 0

    try:
        transport.preflight_check()
    except TransportUnavailable as exc:
        print(f"dekko daemon: cannot start: {exc}", file=sys.stderr)
        return 1

    # A transport artifact can exist without a live daemon behind it
    # (an ungracefully-killed process) -- best-effort clear it before
    # spawning so a `status` immediately after `start` doesn't race a
    # lingering stale file. bind_and_listen() would self-heal this
    # too, but doing it here means `is_daemon_reachable` above and any
    # concurrent `status` call see a consistent "not yet started"
    # state rather than a stale-but-present artifact.
    if transport.exists():
        transport.cleanup()

    cmd = [
        sys.executable,
        "-c",
        _SERVE_BOOTSTRAP,
        "daemon",
        "_serve",
        "--root",
        str(root),
        "--idle-timeout",
        str(idle_timeout),
    ]
    try:
        spawn_detached(cmd)
    except OSError as exc:
        print(f"dekko daemon: failed to start: {exc}", file=sys.stderr)
        return 1

    print(f"dekko daemon: started for {root}")
    return 0


def stop(root: Path) -> int:
    """Handle ``dekko daemon stop``.

    A no-op (not an error) if no daemon is running for this root.
    Attempts a graceful shutdown first; if that doesn't get an answer
    (a wedged daemon, not merely "none running"), falls back to
    ``force_stop`` via a PID obtained through a best-effort ``status``
    round-trip.

    Args:
        root: Resolved repo root whose daemon to stop.

    Returns:
        ``0`` in every case -- "no daemon running" and "stopped" are
        both successful outcomes of this command.
    """
    transport = default_transport_for(root)
    if not transport.exists():
        print(f"dekko daemon: no daemon running for {root}")
        return 0

    try:
        sock = transport.client_connect(_CLIENT_TIMEOUT)
    except DaemonUnavailableError:
        print(f"dekko daemon: no daemon running for {root}")
        return 0

    graceful = False
    try:
        transport.send_auth_preamble(sock)
        _send_line(sock, {"cmd": _SHUTDOWN_CMD})
        graceful = _recv_line(sock) is not None
    except OSError:
        graceful = False
    finally:
        sock.close()

    if not graceful:
        pid = _query_pid(transport)
        if pid is not None:
            try:
                force_stop(pid)
            except ProcessLookupError:
                pass
        transport.cleanup()

    print(f"dekko daemon: stopped for {root}")
    return 0


def status(root: Path, as_json: bool = False) -> int:
    """Handle ``dekko daemon status``.

    Args:
        root: Resolved repo root to check.
        as_json: Emit structured JSON instead of the text summary.

    Returns:
        ``0`` in every case -- "not running" is a successful report,
        not a failure.
    """
    transport = default_transport_for(root)
    data: dict | None = None
    if transport.exists():
        try:
            sock = transport.client_connect(_CLIENT_TIMEOUT)
        except DaemonUnavailableError:
            sock = None
        if sock is not None:
            try:
                transport.send_auth_preamble(sock)
                _send_line(sock, {"cmd": _STATUS_CMD})
                raw = _recv_line(sock)
                data = json.loads(raw) if raw is not None else None
            except (OSError, ValueError):
                data = None
            finally:
                sock.close()

    if data is None:
        if as_json:
            print(json.dumps({"running": False, "root": str(root)}))
        else:
            print(f"dekko daemon: no daemon running for {root}")
        return 0

    data["root"] = str(root)
    if as_json:
        print(json.dumps(data, indent=2))
        return 0

    print(f"dekko daemon: running for {root}")
    print(f"  pid: {data.get('pid')}")
    print(f"  uptime: {data.get('uptime_seconds', 0):.1f}s")
    print(f"  transport: {data.get('transport')}")
    cache = data.get("cache")
    if cache is None:
        print("  cache: empty (no daemon-routed read yet)")
    else:
        state = "fresh" if cache.get("fresh") else "stale-pending-refresh"
        print(
            f"  cache: {state} "
            f"(hits={cache.get('hits', 0)}, misses={cache.get('misses', 0)})"
        )
    return 0
