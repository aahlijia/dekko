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
``repo_ops.py``'s ``load_or_regen`` via ``repo_ops.set_daemon_cache_hook``
at startup, re-validated on every access via ``mapfile.check_freshness``
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
import threading
import time
from collections.abc import Callable
from pathlib import Path

from dekko import repo_ops
from dekko.render import mapfile
from dekko.storage import cache as cache_mod
from dekko.daemon.daemon_transport import (
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
        "ambiguous",
        "status",
        "ledger",
        "note",
        "export",
        "deps",
    }
)

# Reserved protocol verbs, distinct from any real subcommand name (no
# entry in SUBCOMMANDS starts with "_") so they can never collide with
# a routed command.
_SHUTDOWN_CMD = "_shutdown"
_STATUS_CMD = "_status"

# cli.py's main() returns this when a daemon-routed request was sent
# but abandoned (see DaemonRequestAbandonedError below) -- distinct from
# every other exit code already in use across the CLI (0-6; see
# query.py/outline.py's EXIT_NOT_FOUND/EXIT_AMBIGUOUS, ledger.py's
# EXIT_NO_TRANSCRIPT=6, cli.py's own literal 5 for --no-regen
# staleness) so a caller can distinguish "the daemon may still be
# working on this in the background" from every other failure shape.
EXIT_DAEMON_ABANDONED = 7

# stop()'s dedicated exit code for the one branch that is not "gone"
# in any sense -- the daemon was confirmed alive and busy and
# deliberately left untouched (see stop()'s own docstring/body).
# Distinct from every code already in use (0-7 above) so a caller
# checking stop()'s exit code programmatically can't mistake "still
# running" for "stopped."
EXIT_DAEMON_STILL_RUNNING = 8

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

# Round-15 finding: ``_CLIENT_TIMEOUT``'s fixed 30s budget covers the
# entire connect-send-recv round trip (see above), but an edit-
# triggered auto-regen on the fleet's largest repos can legitimately
# exceed it -- not a hang, just genuinely slow work. Isolated,
# contention-free benchmarks (round-15 daemon-large-repo-timeout-plan
# investigation) of a single-file-change auto-regen (map_repository's
# extraction + resolve(), both already run with regen_map's hardcoded
# jobs=0/all-cores) measured ~21s on spring-boot (853 MB map.json)
# and ~209s on tensorflow (1.1 GB map.json) -- a sharply super-linear
# jump for only a ~1.35x size increase, since resolve()'s call-graph
# cost scales with the graph's size, not just file count/bytes. A
# straight-line fit through both points would either badly
# under-provision tensorflow-scale repos or badly over-provision
# spring-boot-scale ones, so this is fit to the slower (tensorflow)
# measurement alone -- 1,212,389,046 bytes in 209.08s, rounded down
# to ~5.5 MB/s for margin -- so every smaller repo gets *more*
# headroom than it measured needing, never less.
_TIMEOUT_BYTES_PER_SECOND = 5_500_000

# Upper bound on the size-scaled client timeout: keeps a hypothetical
# multi-GB map.json from making a routed request's client wait
# effectively unboundedly -- matches this module's "generous but
# bounded" convention elsewhere (_STOP_TEARDOWN_TIMEOUT,
# repo_ops._REGEN_LOCK_WAIT_CAP).
_SCALED_CLIENT_TIMEOUT_CAP = 300.0


def _scaled_client_timeout(root: Path) -> float:
    """Repo-size-aware client timeout for a routed daemon request.

    Scales with ``root``'s on-disk ``.dekko/map.json`` size (readable
    with a single ``stat()``, no daemon round trip needed) rather than
    a guessed multiplier -- see ``_TIMEOUT_BYTES_PER_SECOND`` for the
    measurements this is fit to.

    Deliberately scoped to ``try_daemon()``'s routed-command connect
    only -- not ``stop()``'s shutdown handshake, which has its own
    fast fallback to ``force_stop`` and should stay bounded by the
    fixed ``_CLIENT_TIMEOUT`` rather than grow with repo size (a
    slower shutdown handshake would only delay that fallback), and
    not the status-listener probes, already governed by the separate,
    always-fast ``_STATUS_PROBE_TIMEOUT``. The server's own per-
    connection ``_REQUEST_TIMEOUT`` (``_handle_connection``'s
    ``conn.settimeout``) doesn't need to scale either: it bounds
    socket I/O against a slow/wedged *client*, not the time a routed
    command's own computation takes between the request being read
    and its response being sent, so a slow regen was never actually
    constrained by it in the first place.

    Args:
        root: Repo root whose ``.dekko/map.json`` size (if any) to
            scale from.

    Returns:
        ``_CLIENT_TIMEOUT`` when ``map.json`` is missing/unreadable
        or small enough that the scaled value wouldn't exceed it;
        otherwise a larger budget, capped at
        ``_SCALED_CLIENT_TIMEOUT_CAP``.
    """
    try:
        size = (root / cache_mod.CACHE_DIR / "map.json").stat().st_size
    except OSError:
        return _CLIENT_TIMEOUT
    scaled = size / _TIMEOUT_BYTES_PER_SECOND
    return min(max(_CLIENT_TIMEOUT, scaled), _SCALED_CLIENT_TIMEOUT_CAP)


# Round-14 master report §"Daemon-lifecycle investigation": bound on
# how long stop() will poll for the daemon's transport artifacts to
# actually disappear before giving up and reporting success anyway
# (this command's contract is "always returns 0" -- see stop()'s own
# docstring). Comfortably above the ~1.0-1.1s worst-case teardown lag
# that motivated the poll in the first place (bounded by
# _serve_status_loop's own 1.0s accept() timeout, the slowest single
# step in serve_daemon()'s shutdown finally-block), with real margin
# for a slower machine.
_STOP_TEARDOWN_TIMEOUT = 5.0
_STOP_TEARDOWN_POLL_INTERVAL = 0.02

# Budget for the liveness/status *round trip* specifically (status(),
# _query_pid(), and stop()'s forced-fallback reachability probe) --
# distinct from _CLIENT_TIMEOUT (stop()'s shutdown handshake stays a
# fixed 30s; a routed command's own reply gets _scaled_client_timeout,
# which can grow past 30s on the largest repos -- see that function).
# Round-14 daemon-status-contention-plan.md §2:
# round-12's reason for making the old, single liveness timeout
# generous (a short probe timeout used to mean "lie and say not
# running") no longer applies once a probe timeout produces an honest
# degraded report instead of a false negative -- see status()'s own
# handling of a post-connect TimeoutError. Matches is_daemon_
# reachable's own existing default (daemon_transport.py) for
# consistency between the two liveness checks.
_STATUS_PROBE_TIMEOUT = 2.0

# Bootstrap script used to spawn the detached daemon process. There is
# no ``src/dekko/__main__.py`` (the packaged entry point is the
# ``dekko`` console script, ``dekko.integrations.cli:main``, per
# pyproject.toml),
# so ``python -m dekko`` isn't available; ``python -c <bootstrap>
# daemon _serve ...`` instead guarantees the spawned daemon imports
# the *same* dekko package on the *same* interpreter running the
# parent ``dekko daemon start`` invocation, rather than depending on
# whatever ``dekko`` happens to resolve to first on PATH (which may be
# a differently-versioned install -- see project memory on
# ``~/.local/bin/dekko`` being a separate frozen install from a dev
# checkout).
_SERVE_BOOTSTRAP = (
    "import sys; from dekko.integrations.cli import main; sys.exit(main())"
)


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
    from dekko.integrations import cli

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
        "ambiguous": cli.run_ambiguous,
        "status": cli.run_status,
        "ledger": cli.run_ledger,
        "note": cli.run_note,
        "export": cli.run_export,
        "deps": cli.run_deps,
    }


class _WarmCache:
    """Single-slot warm ``MapIndex`` cache for one daemon process.

    One daemon serves exactly one repo root (its transport artifact
    lives inside that root's own ``.dekko/``), so this doesn't need
    ``server.Context.index_cache``'s dict-keyed shape the way a
    multi-root MCP server session does -- a single slot is enough,
    re-validated via ``mapfile.check_freshness`` on every access
    exactly as that cache is (design doc §2.4). Installed into
    ``repo_ops.load_or_regen`` via ``repo_ops.set_daemon_cache_hook``
    for the lifetime of ``serve_daemon``'s accept loop.

    ``get``/``put`` are only ever called from the main accept loop's
    thread (single-threaded by design). ``snapshot()`` is also called
    from the dedicated status-listener thread (round-13 master report
    §2), which runs concurrently with the main loop -- ``_lock`` guards
    the ``(root, index)`` pair so a snapshot can never observe a torn
    read (a new root paired with a stale index, or vice versa) from a
    ``put()`` happening mid-read. The (potentially slower)
    ``mapfile.check_freshness`` stat call itself runs outside the lock
    in ``snapshot()`` so a status probe never blocks behind it.
    """

    def __init__(self) -> None:
        self._index: mapfile.MapIndex | None = None
        self._root: Path | None = None
        self.hits = 0
        self.misses = 0
        self._lock = threading.Lock()

    def get(self, root: Path) -> mapfile.MapIndex | None:
        """Return the cached index for ``root`` if still fresh, else
        ``None``.

        Every call that doesn't return a fresh hit counts as a miss --
        including the very first call before anything has ever been
        cached -- so ``snapshot()``'s hit/miss counters describe every
        cache-check ``_load_or_regen`` made, not just the ones that
        found something to check.
        """
        with self._lock:
            if self._index is not None and self._root == root:
                if mapfile.check_freshness(root, self._index).fresh:
                    self.hits += 1
                    return self._index
            self.misses += 1
            return None

    def put(self, root: Path, index: mapfile.MapIndex) -> None:
        """Record a freshly loaded ``index`` for ``root``."""
        with self._lock:
            self._root = root
            self._index = index

    def snapshot(self) -> dict | None:
        """Status-reportable cache state, or ``None`` before any
        request has populated the cache."""
        with self._lock:
            if self._index is None or self._root is None:
                return None
            root, index = self._root, self._index
        fresh = mapfile.check_freshness(root, index).fresh
        return {
            "cached_root": str(root),
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
    transport: DaemonTransport,
    start_time: float,
    cache: "_WarmCache",
    busy: bool,
) -> dict:
    """Build the response body for a ``_status`` protocol request.

    Args:
        transport: The transport being probed (for its ``describe()``).
        start_time: ``time.monotonic()`` value at daemon startup.
        cache: This daemon's warm cache, for its snapshot.
        busy: Whether the main accept loop is currently mid-request
            (round-13 master report §2) -- always ``False`` when this
            is built from inside the main loop's own ``_status``
            handling (it can't be handling two requests at once by
            definition), meaningfully ``True``/``False`` when built by
            the independent status-listener thread while the main loop
            may be busy elsewhere.
    """
    return {
        "running": True,
        "pid": os.getpid(),
        "uptime_seconds": round(time.monotonic() - start_time, 3),
        "busy": busy,
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
    busy_event: threading.Event,
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
        busy_event: Set immediately before a routed command runs and
            cleared immediately after, so the independent status-
            listener thread (round-13 master report §2) can report an
            honest ``busy`` flag while this connection is in flight.

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
            _send_line(
                conn,
                _status_payload(
                    transport, start_time, cache, busy_event.is_set()
                ),
            )
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
        busy_event.set()
        try:
            exit_code, out, err = _run_captured(func, args)
        finally:
            busy_event.clear()
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
    from dekko.integrations.server import _capture

    try:
        return _capture(lambda: func(args))
    except Exception as exc:  # see docstring: must not crash the loop
        return 1, "", f"dekko daemon: internal error: {exc}"


def _serve_status_connection(
    conn: socket.socket,
    transport: DaemonTransport,
    start_time: float,
    cache: "_WarmCache",
    busy_event: threading.Event,
) -> None:
    """Handle exactly one connection on the status-only listener.

    Deliberately minimal (round-13 master report §2): authenticates,
    expects a single ``_status`` request, and replies -- anything else
    (a malformed line, an unexpected command) gets an error envelope
    or is simply dropped, never dispatched to a routed ``cli.py``
    function. This listener exists solely so ``daemon status``/
    ``is_daemon_reachable`` stay fast and honest while the main accept
    loop is busy on a slow routed request; it must never grow a second
    copy of real command routing -- that would reopen exactly the
    concurrent-shared-state questions ``serve_daemon``'s single-
    threaded design exists to sidestep.
    """
    conn.settimeout(_REQUEST_TIMEOUT)
    try:
        if not transport.authenticate(conn):
            return
        raw = _recv_line(conn)
        if raw is None:
            return
        try:
            request = json.loads(raw)
        except json.JSONDecodeError:
            return
        if request.get("cmd") != _STATUS_CMD:
            _send_line(
                conn,
                {
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": (
                        "dekko daemon: status listener only answers "
                        "status probes"
                    ),
                },
            )
            return
        payload = _status_payload(
            transport, start_time, cache, busy_event.is_set()
        )
        _send_line(conn, payload)
    except OSError:
        # Mirrors _handle_connection: a transient socket error here
        # must not take this thread's loop down.
        pass
    finally:
        conn.close()


def _serve_status_loop(
    status_sock: socket.socket,
    transport: DaemonTransport,
    start_time: float,
    cache: "_WarmCache",
    busy_event: threading.Event,
    stop_event: threading.Event,
) -> None:
    """Dedicated accept loop for the status-only listener.

    Runs in a background thread for the lifetime of ``serve_daemon``'s
    main accept loop, on a socket the main loop never touches. A short
    (1s) socket timeout on ``status_sock`` lets this loop wake
    periodically to check ``stop_event`` rather than blocking forever
    in ``accept()`` past the point ``serve_daemon`` wants to shut down.
    """
    status_sock.settimeout(1.0)
    while not stop_event.is_set():
        try:
            conn, _addr = status_sock.accept()
        except socket.timeout:
            continue
        except OSError:
            # status_sock was closed out from under this thread during
            # shutdown -- exit the loop rather than spin on the error.
            break
        _serve_status_connection(
            conn, transport, start_time, cache, busy_event
        )


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

    Round-13 master report §2: alongside the main command socket, this
    also binds and serves a second, status-only listener
    (``DaemonTransport.bind_status_listener``) on a dedicated
    background thread (``_serve_status_loop``). That thread's contract
    is deliberately narrow -- it only ever answers ``_status`` probes,
    reading nothing but already-lock-guarded state
    (``_WarmCache.snapshot``) and a ``threading.Event`` the main loop
    flips before/after each routed command -- so liveness/status
    queries (``dekko daemon status``, ``is_daemon_reachable()``) stay
    fast and honest even while the main loop is mid-request. This adds
    exactly one narrowly-scoped thread; it does not make the main
    command loop itself concurrent, and does not touch the concerns
    that loop's single-threaded design was protecting (no second
    thread ever dispatches a routed command or mutates the cache).

    Args:
        root: Repo root this daemon serves.
        idle_timeout: Self-shutdown window, in seconds, with no
            requests.
        transport: Transport to bind, or ``None`` to use
            ``default_transport_for(root)``.

    Returns:
        ``0`` on a clean shutdown (explicit or idle timeout), ``1``
        if either listener could not be bound.
    """
    transport = transport or default_transport_for(root)
    try:
        sock = transport.bind_and_listen()
    except TransportUnavailable as exc:
        print(f"dekko daemon: cannot start: {exc}", file=sys.stderr)
        return 1

    try:
        status_sock = transport.bind_status_listener()
    except TransportUnavailable as exc:
        print(
            f"dekko daemon: cannot start status listener: {exc}",
            file=sys.stderr,
        )
        sock.close()
        transport.cleanup()
        return 1

    dispatch = _dispatch_table()
    cache = _WarmCache()
    repo_ops.set_daemon_cache_hook(cache.get, cache.put)
    start_time = time.monotonic()
    busy_event = threading.Event()
    status_stop = threading.Event()
    status_thread = threading.Thread(
        target=_serve_status_loop,
        args=(
            status_sock,
            transport,
            start_time,
            cache,
            busy_event,
            status_stop,
        ),
        daemon=True,
    )
    status_thread.start()

    sock.settimeout(idle_timeout)
    try:
        while True:
            try:
                conn, _addr = sock.accept()
            except socket.timeout:
                break
            if not _handle_connection(
                conn, transport, dispatch, start_time, cache, busy_event
            ):
                break
    finally:
        status_stop.set()
        status_sock.close()
        status_thread.join(timeout=2.0)
        sock.close()
        transport.cleanup()
        # Uninstall so this process-global hook never leaks past this
        # daemon's lifetime -- matters for tests running serve_daemon
        # in-thread (same process, same repo_ops module) across several
        # daemon instances, and simply keeps a stopped daemon's stale
        # cache from being reachable through repo_ops.py by anything
        # else.
        repo_ops.set_daemon_cache_hook(None, None)
    return 0


class DaemonRequestAbandonedError(Exception):
    """A request reached the daemon, but its response never arrived.

    Round-12 master report §3.8: ``serve_daemon``'s accept loop is
    single-threaded (see its own docstring), so once the daemon has
    started ``_run_captured(func, args)`` for a dispatched request, it
    runs to completion regardless of whether the client is still
    listening -- there is no cancellation. If the client's own
    ``_CLIENT_TIMEOUT`` expires first (a timeout, a dropped
    connection, or a malformed reply), the daemon may still be
    computing that abandoned request in the background. Falling back
    to a local re-run in that situation duplicates the expensive work
    and contends with the orphaned daemon-side copy for CPU -- which
    is why this is raised instead of returned as another ``None``:
    ``None`` means "the daemon was never reached, a local fallback is
    free," this means "the daemon *was* reached and may still be
    working, a local fallback is not free." ``cli.py``'s ``main()``
    must not treat the two the same way.
    """


def _send_daemon_request(
    sock: socket.socket,
    transport: DaemonTransport,
    command: str,
    args: argparse.Namespace,
) -> bool:
    """Authenticate and send one request line on an open ``sock``.

    Returns:
        ``True`` once the request has been fully sent. ``False`` if a
        socket error occurred during authentication or the send
        itself -- safe to treat as "the daemon never started work on
        this request," since nothing was dispatched for it to act on.
    """
    try:
        transport.send_auth_preamble(sock)
        payload = {k: v for k, v in vars(args).items() if k != "func"}
        _send_line(sock, {"cmd": command, "args": payload})
    except OSError:
        return False
    return True


def _recv_daemon_response(sock: socket.socket) -> tuple[int, str, str]:
    """Read and decode the daemon's response line on ``sock``.

    Raises:
        DaemonRequestAbandonedError: on a timeout, a dropped connection, or
            a malformed reply -- always *after* a request was already
            sent, so see that exception's docstring for why this must
            not be swallowed into a plain ``None`` return the way a
            pre-send failure is.
    """
    try:
        raw = _recv_line(sock)
    except OSError as exc:
        raise DaemonRequestAbandonedError(str(exc)) from exc
    if raw is None:
        raise DaemonRequestAbandonedError(
            "connection closed before a response arrived"
        )
    try:
        response = json.loads(raw)
        return (
            int(response["exit_code"]),
            str(response.get("stdout", "")),
            str(response.get("stderr", "")),
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise DaemonRequestAbandonedError(
            f"malformed daemon response: {exc}"
        ) from exc


def try_daemon(args: argparse.Namespace) -> tuple[int, str, str] | None:
    """Attempt to route a parsed CLI invocation through a live daemon.

    Returns ``None`` on every "the daemon was never actually reached
    for this request" condition (design doc §2.5) so ``cli.py``'s
    ``main()`` integration can treat those as "fall back to direct
    execution, silently." Once a request has been sent, though, a
    failure to get its response back is raised as
    :class:`DaemonRequestAbandonedError` rather than folded into the same
    ``None`` return -- round-12 master report §3.8 traced a silent
    local fallback in that specific case to a duplicate-execution bug
    (the daemon keeps computing the abandoned request in the
    background while the client redoes the same work locally,
    contending for the same CPU). ``main()`` must let that exception
    propagate to a clear message and a distinct exit code instead of
    catching it here.

    Args:
        args: The already-parsed ``argparse.Namespace`` for a
            daemon-eligible subcommand (i.e. what ``args.func(args)``
            would otherwise be called with directly).

    Returns:
        ``(exit_code, stdout, stderr)`` from the daemon, or ``None``
        to signal "run directly instead."

    Raises:
        DaemonRequestAbandonedError: if a request was sent to a live
            daemon but no usable response came back.
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
        sock = transport.client_connect(_scaled_client_timeout(root))
    except DaemonUnavailableError:
        return None

    try:
        if not _send_daemon_request(sock, transport, command, args):
            return None
        return _recv_daemon_response(sock)
    finally:
        sock.close()


def _query_pid(transport: DaemonTransport) -> int | None:
    """Best-effort PID lookup via a ``_status`` round-trip.

    Used only by ``stop()``'s forced-fallback path, when a graceful
    shutdown request didn't get an answer -- prefers the status-only
    listener (fast even if the main loop is still busy) and falls back
    to the main socket for a daemon started before that listener
    existed. Returns ``None`` on any failure -- the caller already
    treats a missing PID as "can't force-stop," not as an error to
    surface. Uses ``_STATUS_PROBE_TIMEOUT`` (not ``_CLIENT_TIMEOUT``):
    this is a liveness probe, not a routed command, so it should give
    up quickly rather than tie up ``stop()`` for up to 30s (round-14
    daemon-status-contention-plan.md §2) -- ``stop()``'s own call site
    treats a timeout here the same as any other failure to confirm a
    PID, and falls back to ``is_daemon_reachable`` as a second opinion
    before deciding whether it's safe to unlink the transport (§3).
    """
    sock = _status_connect(transport, _STATUS_PROBE_TIMEOUT)
    if sock is None:
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


def _wait_for_teardown(
    transport: DaemonTransport, timeout: float = _STOP_TEARDOWN_TIMEOUT
) -> None:
    """Block until a gracefully-shutting-down daemon's artifacts are gone.

    Round-14 master report ("Daemon-lifecycle investigation"): three
    independent evaluators (``cline.md`` §5.2, ``claude-buddy.md``
    §3.3, ``claude-code.md`` §1) found ``dekko daemon stop`` printing
    "stopped" and returning success up to ~1.0-1.1s *before* the
    daemon process actually exits. Root cause: ``_handle_connection``
    acks a ``_shutdown`` request (and returns) the moment it's
    received, but ``serve_daemon()``'s own teardown -- joining the
    status-listener thread (bounded by that thread's 1.0s ``accept()``
    timeout, the dominant term in the measured ~1s lag),
    closing both sockets, then unlinking their on-disk artifacts via
    ``DaemonTransport.cleanup()`` as the very last step -- all happens
    *after* that ack is already on the wire. A command issued in that
    window either connects to a listening-but-about-to-vanish main
    socket and gets its connection reset mid-request (misread by the
    client as "still busy," not "already gone" -- exit 7, violating
    the documented fail-open contract), or races a concurrent
    ``daemon start`` into spawning a genuine duplicate live process
    before the old one has actually released its transport artifacts.

    Polling ``transport.exists()`` (the main socket/port file,
    unlinked only as literally the last act of ``serve_daemon()``'s
    shutdown) is a race-free, filesystem-only signal that teardown has
    actually finished -- unlike a network probe, it can't be fooled by
    a listening socket that's still accepting into the OS backlog even
    though nothing will ever call ``accept()`` on it again.

    Args:
        transport: The transport whose artifacts to poll.
        timeout: Maximum time to wait before giving up. ``stop()``
            reports success either way once this returns -- matching
            this command's existing "always returns 0" contract, this
            is a best-effort wait to close the race, not a new failure
            mode of its own.
    """
    deadline = time.monotonic() + timeout
    while transport.exists() and time.monotonic() < deadline:
        time.sleep(_STOP_TEARDOWN_POLL_INTERVAL)


def stop(root: Path) -> int:
    """Handle ``dekko daemon stop``.

    A no-op (not an error) if no daemon is running for this root.
    Attempts a graceful shutdown first; if that doesn't get an answer
    (a wedged daemon, not merely "none running"), falls back to
    ``force_stop`` via a PID obtained through a best-effort ``status``
    round-trip. Either way, blocks (bounded by
    ``_STOP_TEARDOWN_TIMEOUT``) until the daemon's transport artifacts
    are actually gone before reporting success -- see
    ``_wait_for_teardown`` for why a graceful shutdown's own ack
    arrives before the daemon has actually finished tearing down.

    The forced-fallback branch only unlinks the daemon's transport
    artifacts when there is *positive* evidence it's actually gone --
    either a confirmed PID it just force-stopped, or a final
    reachability probe that itself fails (round-14 daemon-status-
    contention-plan.md §3: under sustained CPU contention, both the
    graceful-ack wait and the PID lookup can time out without
    confirming anything even though the daemon is genuinely still
    alive and listening; unlinking unconditionally in that case
    stranded a live, unreachable daemon process -- see that document
    for the full root cause).

    Args:
        root: Resolved repo root whose daemon to stop.

    Returns:
        ``0`` for "no daemon running" or "stopped" -- both are
        successful outcomes of this command. ``EXIT_DAEMON_STILL_
        RUNNING`` for the one remaining branch: the daemon was
        confirmed alive and busy and deliberately left untouched (see
        the forced-fallback branch below) -- this is neither a
        "stopped" nor a "no daemon running" outcome, so it gets its
        own honest message and a distinct, non-zero exit code rather
        than reusing the "stopped" success message for a daemon that
        is, in fact, still running.
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
        elif not is_daemon_reachable(transport, timeout=_STATUS_PROBE_TIMEOUT):
            # Neither the shutdown ack nor a pid lookup confirmed
            # anything, AND a final direct reachability probe also
            # fails -- now there's real (if not airtight) evidence
            # nothing is listening. Safe to clean up.
            transport.cleanup()
        else:
            # pid lookup failed but the daemon still answers a plain
            # reachability probe -- it's alive, just hasn't replied to
            # either of our two attempts. Leave its transport alone; a
            # subsequent stop() (or the daemon's own idle-timeout) can
            # retry. This is the one branch that did not stop
            # anything, so it gets its own honest message/exit code
            # below instead of the unconditional "stopped" success
            # every other branch reports.
            print(
                f"dekko daemon: still running and busy for {root}; "
                "could not confirm stop -- it may be mid-request. Try "
                "again once it's idle, or run 'dekko daemon stop' "
                "again to retry the PID/reachability check."
            )
            return EXIT_DAEMON_STILL_RUNNING
    else:
        _wait_for_teardown(transport)

    print(f"dekko daemon: stopped for {root}")
    return 0


def _status_connect(
    transport: DaemonTransport, timeout: float
) -> socket.socket | None:
    """Connect for a ``_status`` round-trip, preferring the status-only
    listener.

    Round-13 master report §2: the dedicated status-only listener
    stays fast and honest even while the daemon is mid-request on the
    main command socket. Falls back to the main socket only for a
    daemon started before that listener existed (an in-place upgrade
    with the old daemon still running). Returns ``None`` on total
    failure -- callers treat that identically to "not running."
    """
    try:
        return transport.status_client_connect(timeout)
    except DaemonUnavailableError:
        pass
    try:
        return transport.client_connect(timeout)
    except DaemonUnavailableError:
        return None


def _probe_status(transport: DaemonTransport) -> tuple[dict | None, bool]:
    """Run the ``_status`` round-trip, distinguishing a timeout from
    every other failure.

    Round-14 daemon-status-contention-plan.md §1-2: a connect to a
    genuinely dead/absent daemon fails immediately at
    ``_status_connect()`` (``ConnectionRefusedError``, not a timeout)
    -- reaching the ``TimeoutError`` branch below at all means a live
    listener already accepted the connection and just hasn't replied
    yet, almost certainly GIL/OS-scheduling starvation on the status
    thread under sustained CPU contention, not a dead daemon.
    ``status()`` reports that honestly instead of folding it into the
    same "not running" outcome as a genuine absence.

    Returns:
        A ``(data, timed_out)`` pair: ``data`` is the parsed response
        (or ``None`` on any failure), ``timed_out`` is ``True`` only
        for the post-connect-timeout case described above.
    """
    if not transport.exists():
        return None, False
    sock = _status_connect(transport, _STATUS_PROBE_TIMEOUT)
    if sock is None:
        return None, False
    try:
        transport.send_auth_preamble(sock)
        _send_line(sock, {"cmd": _STATUS_CMD})
        raw = _recv_line(sock)
        data = json.loads(raw) if raw is not None else None
        return data, False
    except TimeoutError:
        return None, True
    except (OSError, ValueError):
        return None, False
    finally:
        sock.close()


def _print_unconfirmed_status(root: Path, as_json: bool) -> None:
    """Report the "connected, but didn't reply in time" degraded state.

    Distinct from the plain "not running" report: ``busy``/``uptime``/
    ``cache`` are genuinely unknown here (the round trip that would
    report them is exactly what didn't complete), so they're reported
    as absent rather than guessed, matching this codebase's existing
    "ambiguous rather than guessed" philosophy.
    """
    payload = {
        "running": True,
        "confirmed": False,
        "root": str(root),
        "note": (
            "connected to a live daemon but it did not reply within "
            f"{_STATUS_PROBE_TIMEOUT}s -- likely busy under CPU "
            "contention; state below is unknown"
        ),
    }
    if as_json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"dekko daemon: running for {root} (unconfirmed)")
        print(f"  note: {payload['note']}")


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
    data, timed_out = _probe_status(transport)

    if data is None:
        if timed_out:
            _print_unconfirmed_status(root, as_json)
            return 0
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
    if "busy" in data:
        print(f"  busy: {data.get('busy')}")
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
