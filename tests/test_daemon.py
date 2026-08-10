"""The dekko daemon: accept loop, request routing, explicit lifecycle.

Per the daemon-mode-cli-workflow.md's cross-cutting test-scoping rule
(its §7): this module is built entirely on ``default_transport_for()``
and ``DaemonTransport``'s interface, so almost none of it needs a
``skipif`` -- the one platform-specific test explicitly exercises the
TCP loopback transport's accept-loop parity, which is meaningful (and
runs) on every platform, not just Windows.

Tests that spawn a genuine background daemon process are wrapped in a
try/finally teardown that force-stops the daemon even if assertions
fail partway through -- GitHub Actions' windows-latest runners have a
known history of hanging on orphaned child processes left behind by a
test that didn't clean up (workflow doc §3, point 3).
"""

import json as _json
import shutil
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from dekko import cli
from dekko import daemon
from dekko import daemon_transport as dt
from dekko import mapfile
from dekko.server import _capture

_POLL_DEADLINE = 5.0
_POLL_INTERVAL = 0.02

_CACHE_SRC = {
    "a.py": "def f() -> int:\n    return 1\n",
    "b.py": "from a import f\n\n\ndef g() -> int:\n    return f()\n",
}


@pytest.fixture
def short_root() -> Path:
    """A short-path temp dir, safe for a real ``AF_UNIX`` bind.

    Mirrors ``tests/test_daemon_transport.py``'s fixture of the same
    name/reasoning: pytest's own ``tmp_path`` can already be close to
    or past the ``sun_path`` length limit on some machines before
    ``.dekko/daemon.sock`` is even appended.
    """
    root = Path(tempfile.mkdtemp(prefix="dkd"))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _wait_until(
    predicate: Callable[[], bool], deadline: float = _POLL_DEADLINE
) -> bool:
    """Poll ``predicate`` until true or ``deadline`` seconds pass."""
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(_POLL_INTERVAL)
    return predicate()


@pytest.fixture
def daemon_thread_root(short_root: Path) -> Iterator[Path]:
    """A repo root with an in-thread daemon already serving it.

    An in-process ``threading.Thread`` (not a real subprocess) so
    routing/fallback/protocol tests run fast; the one real-subprocess
    lifecycle test lives separately below.
    """
    root = short_root
    thread = threading.Thread(
        target=daemon.serve_daemon,
        kwargs={"root": root, "idle_timeout": 30.0},
        daemon=True,
    )
    thread.start()
    transport = dt.default_transport_for(root)
    assert _wait_until(transport.exists), "daemon did not bind in time"
    try:
        yield root
    finally:
        daemon.stop(root)
        thread.join(timeout=_POLL_DEADLINE)


# ---------------------------------------------------------------------
# Fallback: no daemon present at all (the common case)
# ---------------------------------------------------------------------


def test_try_daemon_no_socket_returns_none(short_root: Path) -> None:
    args = cli.build_subcommand_parser().parse_args(
        ["status", "--root", str(short_root)]
    )
    assert daemon.try_daemon(args) is None


def test_try_daemon_ineligible_command_returns_none(
    daemon_thread_root: Path,
) -> None:
    # "map" is a write-path command -- never routed, even with a live
    # daemon actually reachable for this root.
    args = cli.build_subcommand_parser().parse_args(
        ["map", str(daemon_thread_root), "--quiet"]
    )
    assert daemon.try_daemon(args) is None


def test_try_daemon_note_add_not_eligible(
    daemon_thread_root: Path,
) -> None:
    args = cli.build_subcommand_parser().parse_args(
        [
            "note",
            "add",
            "somesymbol",
            "text",
            "--root",
            str(daemon_thread_root),
        ]
    )
    assert daemon.try_daemon(args) is None


def test_try_daemon_note_list_is_eligible(
    daemon_thread_root: Path,
) -> None:
    args = cli.build_subcommand_parser().parse_args(
        ["note", "list", "--root", str(daemon_thread_root)]
    )
    assert daemon.try_daemon(args) is not None


def test_stale_socket_falls_open(short_root: Path) -> None:
    """A dead transport artifact (no listener) must fall open, silently."""
    sock_dir = short_root / ".dekko"
    sock_dir.mkdir(parents=True)
    (sock_dir / "daemon.sock").write_bytes(b"")  # not a real socket

    args = cli.build_subcommand_parser().parse_args(
        ["status", "--root", str(short_root)]
    )
    assert daemon.try_daemon(args) is None
    # try_daemon's failure path best-effort unlinks the stale artifact
    # (via client_connect's own cleanup on OSError), so a later
    # daemon start doesn't trip over it.
    assert not (sock_dir / "daemon.sock").exists()


# ---------------------------------------------------------------------
# Routing correctness: byte-identical output, with vs. without daemon
# ---------------------------------------------------------------------


@pytest.mark.parametrize("as_json", [False, True])
def test_routing_byte_identical_output(
    daemon_thread_root: Path, as_json: bool
) -> None:
    argv = ["status", "--root", str(daemon_thread_root)]
    if as_json:
        argv.append("--json")

    parser = cli.build_subcommand_parser()
    direct_args = parser.parse_args(argv)
    direct_code, direct_out, direct_err = _capture(
        lambda: cli.run_status(direct_args)
    )

    routed_args = parser.parse_args(argv)
    routed = daemon.try_daemon(routed_args)

    assert routed == (direct_code, direct_out, direct_err)


def test_routing_search_over_real_repo(short_root: Path) -> None:
    """Routing correctness against an actual mapped repo, not just
    the map-less ``status`` fast path used by the other tests."""
    (short_root / "a.py").write_text(
        "def sandbox_escape() -> None:\n    pass\n"
    )
    assert cli.main(["map", str(short_root), "--quiet"]) == 0

    thread = threading.Thread(
        target=daemon.serve_daemon,
        kwargs={"root": short_root, "idle_timeout": 30.0},
        daemon=True,
    )
    thread.start()
    transport = dt.default_transport_for(short_root)
    assert _wait_until(transport.exists)

    try:
        argv = [
            "search",
            "sandbox",
            "escape",
            "--root",
            str(short_root),
            "--json",
        ]
        parser = cli.build_subcommand_parser()
        direct_code, direct_out, direct_err = _capture(
            lambda: cli.run_search(parser.parse_args(argv))
        )
        routed = daemon.try_daemon(parser.parse_args(argv))
        assert routed == (direct_code, direct_out, direct_err)
        assert "sandbox_escape" in direct_out
    finally:
        daemon.stop(short_root)
        thread.join(timeout=_POLL_DEADLINE)


def test_unknown_command_returns_error_and_daemon_keeps_serving(
    daemon_thread_root: Path,
) -> None:
    transport = dt.default_transport_for(daemon_thread_root)
    sock = transport.client_connect(2.0)
    try:
        transport.send_auth_preamble(sock)
        daemon._send_line(sock, {"cmd": "not-a-real-command"})
        raw = daemon._recv_line(sock)
    finally:
        sock.close()
    assert raw is not None

    response = _json.loads(raw)
    assert response["exit_code"] != 0

    # One bad request must not have taken the accept loop down.
    args = cli.build_subcommand_parser().parse_args(
        ["status", "--root", str(daemon_thread_root)]
    )
    assert daemon.try_daemon(args) is not None


# ---------------------------------------------------------------------
# --no-daemon
# ---------------------------------------------------------------------


def test_no_daemon_flag_skips_routing(
    daemon_thread_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    original = daemon.try_daemon

    def _spy(args):  # noqa: ANN001, ANN202
        calls.append(getattr(args, "command", None))
        return original(args)

    monkeypatch.setattr(cli.daemon_mod, "try_daemon", _spy)

    code = cli.main(
        ["status", "--root", str(daemon_thread_root), "--no-daemon"]
    )
    assert code in (0, 1)
    assert calls == []

    code = cli.main(["status", "--root", str(daemon_thread_root)])
    assert code in (0, 1)
    assert calls == ["status"]


# ---------------------------------------------------------------------
# Idle-timeout self-shutdown
# ---------------------------------------------------------------------


def test_idle_timeout_self_shutdown(short_root: Path) -> None:
    transport = dt.default_transport_for(short_root)
    thread = threading.Thread(
        target=daemon.serve_daemon,
        kwargs={"root": short_root, "idle_timeout": 0.3},
        daemon=True,
    )
    thread.start()
    assert _wait_until(transport.exists)

    thread.join(timeout=_POLL_DEADLINE)
    assert not thread.is_alive()
    assert not transport.exists()


# ---------------------------------------------------------------------
# Explicit shutdown via daemon.stop()
# ---------------------------------------------------------------------


def test_stop_shuts_down_a_reachable_daemon(short_root: Path) -> None:
    transport = dt.default_transport_for(short_root)
    thread = threading.Thread(
        target=daemon.serve_daemon,
        kwargs={"root": short_root, "idle_timeout": 30.0},
        daemon=True,
    )
    thread.start()
    assert _wait_until(transport.exists)
    assert dt.is_daemon_reachable(transport)

    assert daemon.stop(short_root) == 0
    thread.join(timeout=_POLL_DEADLINE)
    assert not thread.is_alive()
    assert not transport.exists()


def test_stop_is_a_noop_when_nothing_is_running(short_root: Path) -> None:
    assert daemon.stop(short_root) == 0


# ---------------------------------------------------------------------
# Status: text and JSON shape
# ---------------------------------------------------------------------


def test_status_reports_not_running(short_root: Path) -> None:
    assert daemon.status(short_root, as_json=False) == 0
    assert daemon.status(short_root, as_json=True) == 0


def test_status_json_shape_when_running(
    daemon_thread_root: Path, capsys: pytest.CaptureFixture
) -> None:
    code = daemon.status(daemon_thread_root, as_json=True)
    assert code == 0
    out = capsys.readouterr().out

    data = _json.loads(out)
    assert data["running"] is True
    assert isinstance(data["pid"], int)
    assert "uptime_seconds" in data
    assert data["cache"] is None


# ---------------------------------------------------------------------
# Warm cache (Phase 3): repeated daemon-routed reads against an
# unchanged map skip mapfile.load_map entirely, and a working-tree
# change between requests is picked up on the next one, never served
# stale. Mirrors tests/test_server.py's Context.index_cache tests for
# the MCP server's analogous cache.
# ---------------------------------------------------------------------


@pytest.fixture
def daemon_thread_cached_root(short_root: Path) -> Iterator[Path]:
    """A mapped repo (``_CACHE_SRC``) served by an in-thread daemon."""
    for name, text in _CACHE_SRC.items():
        (short_root / name).write_text(text)
    assert cli.main(["map", str(short_root), "--quiet"]) == 0

    thread = threading.Thread(
        target=daemon.serve_daemon,
        kwargs={"root": short_root, "idle_timeout": 30.0},
        daemon=True,
    )
    thread.start()
    transport = dt.default_transport_for(short_root)
    assert _wait_until(transport.exists), "daemon did not bind in time"
    try:
        yield short_root
    finally:
        daemon.stop(short_root)
        thread.join(timeout=_POLL_DEADLINE)


def _query_symbol_args(root: Path, symbol: str) -> object:
    return cli.build_subcommand_parser().parse_args(
        ["query", "symbol", symbol, "--root", str(root)]
    )


def test_daemon_cache_hits_across_repeated_requests(
    daemon_thread_cached_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Several daemon-routed reads against an unchanged map must make
    exactly one real ``mapfile.load_map`` call -- mirrors
    ``test_server.py``'s
    ``test_index_for_caches_across_calls_when_unchanged``."""
    root = daemon_thread_cached_root
    calls: list[Path] = []
    real_load_map = mapfile.load_map

    def spy(root_arg: Path) -> mapfile.MapIndex | None:
        calls.append(root_arg)
        return real_load_map(root_arg)

    monkeypatch.setattr(mapfile, "load_map", spy)

    result1 = daemon.try_daemon(_query_symbol_args(root, "f"))
    assert result1 is not None
    assert result1[0] == 0
    assert len(calls) == 1  # cache miss: first request loads the map

    result2 = daemon.try_daemon(_query_symbol_args(root, "g"))
    assert result2 is not None
    assert result2[0] == 0
    assert len(calls) == 1  # cache hit: no second load_map call

    result3 = daemon.try_daemon(_query_symbol_args(root, "f"))
    assert result3 is not None
    assert result3[0] == 0
    assert len(calls) == 1  # still cached
    assert "g() -> int" in result2[1]


def test_daemon_cache_busts_on_working_tree_change(
    daemon_thread_cached_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A working-tree edit between daemon-routed requests must not be
    served stale -- correctness comes from re-checking
    ``mapfile.check_freshness`` on every access, not from observing an
    invalidation event. Mirrors ``test_server.py``'s
    ``test_index_for_busts_cache_when_map_goes_stale``."""
    root = daemon_thread_cached_root
    calls: list[Path] = []
    real_load_map = mapfile.load_map

    def spy(root_arg: Path) -> mapfile.MapIndex | None:
        calls.append(root_arg)
        return real_load_map(root_arg)

    monkeypatch.setattr(mapfile, "load_map", spy)

    result1 = daemon.try_daemon(_query_symbol_args(root, "f"))
    assert result1 is not None and result1[0] == 0
    assert len(calls) == 1  # cache miss: populates the cache

    not_yet = daemon.try_daemon(_query_symbol_args(root, "brand_new"))
    assert not_yet is not None
    assert not_yet[0] != 0  # cache still serving the old (pre-edit) index
    assert len(calls) == 1  # served from cache, no reload triggered

    (root / "a.py").write_text(
        "def f() -> int:\n    return 1\n\n\ndef brand_new() -> int:\n"
        "    return 2\n"
    )
    before_refresh = len(calls)
    result2 = daemon.try_daemon(_query_symbol_args(root, "brand_new"))
    assert result2 is not None
    assert result2[0] == 0  # staleness detected, cache refreshed
    assert len(calls) > before_refresh  # a reload actually happened
    assert "brand_new() -> int" in result2[1]

    after_refresh = len(calls)
    result3 = daemon.try_daemon(_query_symbol_args(root, "brand_new"))
    assert result3 is not None and result3[0] == 0
    assert len(calls) == after_refresh  # re-cached after the refresh


def test_daemon_status_reports_cache_state_after_a_request(
    daemon_thread_cached_root: Path, capsys: pytest.CaptureFixture
) -> None:
    """``dekko daemon status``'s cache field is ``None`` until the
    first daemon-routed read populates it, then reports the cached
    root, its current freshness, and cumulative hit/miss counts."""
    root = daemon_thread_cached_root
    result = daemon.try_daemon(_query_symbol_args(root, "f"))
    assert result is not None and result[0] == 0

    capsys.readouterr()  # discard anything printed so far, if any
    code = daemon.status(root, as_json=True)
    assert code == 0
    data = _json.loads(capsys.readouterr().out)
    cache = data["cache"]
    assert cache is not None
    # The daemon-side handler resolves args.root itself (mirroring
    # every other subcommand's Path(args.root).resolve()), which can
    # differ textually from the fixture's own root on platforms where
    # the temp dir sits behind a symlink (macOS: /var -> /private/var)
    # -- compare against the same resolution, not raw text.
    assert cache["cached_root"] == str(root.resolve())
    assert cache["fresh"] is True
    assert cache["hits"] + cache["misses"] >= 1


# ---------------------------------------------------------------------
# Cross-transport parity: the TCP loopback transport's accept loop
# ---------------------------------------------------------------------


def test_tcp_loopback_transport_accept_loop_parity(
    short_root: Path,
) -> None:
    """serve_daemon()'s accept loop is transport-agnostic -- prove it
    against TcpLoopbackTransport explicitly, not just whatever
    default_transport_for() happens to pick on this platform."""
    transport = dt.TcpLoopbackTransport(short_root)
    thread = threading.Thread(
        target=daemon.serve_daemon,
        kwargs={
            "root": short_root,
            "idle_timeout": 30.0,
            "transport": transport,
        },
        daemon=True,
    )
    thread.start()
    assert _wait_until(transport.exists)

    try:
        sock = transport.client_connect(2.0)
        try:
            transport.send_auth_preamble(sock)
            daemon._send_line(sock, {"cmd": daemon._STATUS_CMD})
            raw = daemon._recv_line(sock)
        finally:
            sock.close()
        assert raw is not None

        assert _json.loads(raw)["running"] is True

        # A connection presenting no token must be closed, unanswered
        # -- _recv_line() sees a clean EOF (None), not an error.
        bad_sock = transport.client_connect(2.0)
        try:
            bad_sock.sendall(b"not-the-token\n")
            bad_sock.settimeout(1.0)
            assert daemon._recv_line(bad_sock) is None
        finally:
            bad_sock.close()
    finally:
        sock2 = transport.client_connect(2.0)
        try:
            transport.send_auth_preamble(sock2)
            daemon._send_line(sock2, {"cmd": daemon._SHUTDOWN_CMD})
            daemon._recv_line(sock2)
        finally:
            sock2.close()
        thread.join(timeout=_POLL_DEADLINE)


# ---------------------------------------------------------------------
# Real subprocess lifecycle: dekko daemon start/stop/status
# ---------------------------------------------------------------------


def test_real_process_start_stop_status_roundtrip(
    short_root: Path,
) -> None:
    transport = dt.default_transport_for(short_root)
    try:
        code = cli.main(
            [
                "daemon",
                "start",
                "--root",
                str(short_root),
                "--idle-timeout",
                "60",
            ]
        )
        assert code == 0
        assert _wait_until(lambda: dt.is_daemon_reachable(transport))

        status_code = cli.main(
            ["daemon", "status", "--root", str(short_root), "--json"]
        )
        assert status_code == 0

        # A second `start` against an already-running daemon is a
        # no-op, not an error.
        assert cli.main(["daemon", "start", "--root", str(short_root)]) == 0
    finally:
        cli.main(["daemon", "stop", "--root", str(short_root)])
        assert _wait_until(lambda: not transport.exists())
