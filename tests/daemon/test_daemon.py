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
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from dekko.integrations import cli
from dekko.daemon import daemon
from dekko.daemon import daemon_transport as dt
from dekko.render import mapfile
from dekko.integrations.server import _capture

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
    transport = dt.default_transport_for(short_root)
    sock_dir = short_root / ".dekko"
    sock_dir.mkdir(parents=True)
    # Empty bytes are simultaneously "not a real AF_UNIX socket" (Unix)
    # and "not valid JSON" (TCP loopback's port file) -- a genuinely
    # unusable artifact on whichever transport this platform selects,
    # per this module's own stated convention (see module docstring)
    # of driving everything through default_transport_for() rather
    # than hardcoding one platform's artifact name.
    artifact = (
        transport.socket_path
        if isinstance(transport, dt.UnixSocketTransport)
        else transport.port_file
    )
    artifact.write_bytes(b"")

    args = cli.build_subcommand_parser().parse_args(
        ["status", "--root", str(short_root)]
    )
    assert daemon.try_daemon(args) is None
    # try_daemon's failure path best-effort unlinks the stale artifact
    # (via client_connect's own cleanup on a genuinely unusable
    # artifact -- a failed connect or, for TCP loopback, a failed port-
    # file read), so a later daemon start doesn't trip over it.
    assert not artifact.exists()


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
# Abandoned requests (round-12 master report §3.8): a client-side
# timeout after a request has already been sent to the daemon must
# not be treated like "no daemon reachable" -- silently falling back
# to a local re-run would duplicate the (possibly still-running)
# daemon-side work.
# ---------------------------------------------------------------------


def test_main_reports_abandoned_daemon_request_without_local_fallback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """``cli.main()`` must not call ``args.func`` when ``try_daemon``
    raises ``DaemonRequestAbandonedError`` -- that would duplicate
    whatever the daemon may still be computing. It should surface the
    distinct exit code and a clear message instead."""
    calls: list[str] = []

    def _spy_run_status(args: object) -> int:
        calls.append("run_status")
        return 0

    def _raise(args: object) -> tuple[int, str, str] | None:
        raise daemon.DaemonRequestAbandonedError("simulated timeout")

    monkeypatch.setattr(cli, "run_status", _spy_run_status)
    monkeypatch.setattr(cli.daemon_mod, "try_daemon", _raise)

    code = cli.main(["status", "--root", "/nonexistent"])

    assert code == daemon.EXIT_DAEMON_ABANDONED
    assert calls == []  # args.func must never have run
    err = capsys.readouterr().err
    assert "did not respond in time" in err
    assert "--no-daemon" in err


def test_no_daemon_flag_bypasses_abandoned_request_handling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--no-daemon`` must skip ``try_daemon`` entirely, so it can
    never observe (or be blocked by) an abandoned-request signal."""

    def _raise(args: object) -> tuple[int, str, str] | None:
        raise daemon.DaemonRequestAbandonedError("should never be called")

    monkeypatch.setattr(cli.daemon_mod, "try_daemon", _raise)

    code = cli.main(["status", "--root", "/nonexistent", "--no-daemon"])
    assert code in (0, 1)


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


def test_stop_blocks_until_artifacts_are_gone_before_returning(
    short_root: Path,
) -> None:
    """Round-14 master report ("daemon stop returns success ~1s
    before the process actually dies"): ``stop()`` used to report
    success as soon as the daemon's graceful-shutdown ack arrived,
    which is *before* ``serve_daemon()``'s own teardown (joining the
    status thread, closing both sockets, unlinking transport
    artifacts) actually runs -- see ``daemon._wait_for_teardown``'s
    docstring for the full root cause. Unlike
    ``test_stop_shuts_down_a_reachable_daemon`` above (which joins the
    daemon thread directly -- something only a white-box, in-thread
    test can do), this checks exactly what a real CLI client can
    observe: ``transport.exists()`` must already be ``False`` the
    instant ``daemon.stop()`` returns, with no extra wait of any kind
    on this test's own side."""
    transport = dt.default_transport_for(short_root)
    thread = threading.Thread(
        target=daemon.serve_daemon,
        kwargs={"root": short_root, "idle_timeout": 30.0},
        daemon=True,
    )
    thread.start()
    try:
        assert _wait_until(transport.exists)
        assert dt.is_daemon_reachable(transport)

        assert daemon.stop(short_root) == 0
        assert not transport.exists()
    finally:
        thread.join(timeout=_POLL_DEADLINE)


def test_stop_does_not_unlink_live_daemon_when_ack_and_pid_query_both_fail(
    short_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-14 daemon-status-contention-plan.md §3: ``stop()``'s
    forced-fallback branch used to call ``transport.cleanup()``
    unconditionally whenever neither the shutdown-ack round trip nor a
    ``_query_pid`` lookup confirmed anything -- even when the daemon
    was still genuinely alive and listening. This reproduced
    tensorflow.md §4.3's exact symptom (``ps``/``lsof`` confirm a
    live, listening process; the directory entries for its bound
    socket paths are gone) *without* needing the ``start``-races-
    self-cleanup timing window a different item in this round's plan
    doc describes -- a single ``stop()`` call against a busy daemon is
    sufficient.

    Forces ``graceful = False`` for real: a sleep-based slow routed
    command occupies the single-threaded main accept loop long enough
    that a shortened ``_CLIENT_TIMEOUT`` genuinely lapses on the
    shutdown-ack wait, exactly as a wedged daemon would. ``_query_pid``
    is monkeypatched to also return ``None``: a real daemon's
    dedicated status listener stays responsive under a sleep-based
    busy double (it's a separate thread untouched by the main loop
    sleeping -- see ``test_status_true_positive_while_daemon_busy_
    on_slow_request``), so making it *also* time out for real needs
    the CPU-bound GIL-starvation double this round's status-probe
    fix is about, not this test's own charter; monkeypatching isolates
    *this* fix's new conditional logic in ``stop()`` -- whether to
    unlink once both signals have failed to confirm anything -- from
    that separate concern.

    Confirms the final, unmocked ``is_daemon_reachable`` probe still
    finds the daemon alive, and that ``stop()`` leaves its transport
    artifacts (and the process itself) alone as a result, rather than
    orphaning a live, now-unreachable-by-path daemon.

    Round 21 (tensorflow.md §3, Track C): this branch used to print
    the same unconditional "stopped" success message and return ``0``
    even here -- the one branch that did not stop anything. Now
    returns the distinct ``EXIT_DAEMON_STILL_RUNNING`` exit code
    instead (asserted below). The new message's own text is *not*
    asserted here: this test's busy double runs its 2-second sleep
    inside the daemon thread's own request-capture window, and
    ``contextlib.redirect_stdout``-style capture swaps ``sys.stdout``
    process-globally, not per-thread -- so this test's own
    ``daemon.stop()`` call on the main thread can race that window and
    have its ``print()`` land in the *daemon's own* captured response
    buffer instead of real stdout, making a ``capsys`` assertion here
    inherently flaky. See
    ``test_stop_reports_still_running_message_and_exit_code`` below
    for a race-free, non-threaded check of the new message's exact
    text.
    """
    started = threading.Event()
    real_run_stats = cli.run_stats

    def slow_run_stats(args: object) -> int:
        started.set()
        time.sleep(2.0)
        return real_run_stats(args)

    monkeypatch.setattr(cli, "run_stats", slow_run_stats)
    monkeypatch.setattr(daemon, "_query_pid", lambda transport: None)

    root = short_root
    (root / "a.py").write_text("def f() -> int:\n    return 1\n")
    assert cli.main(["map", str(root), "--quiet"]) == 0

    transport = dt.default_transport_for(root)
    thread = threading.Thread(
        target=daemon.serve_daemon,
        kwargs={"root": root, "idle_timeout": 30.0},
        daemon=True,
    )
    thread.start()
    original_timeout = daemon._CLIENT_TIMEOUT
    try:
        assert _wait_until(transport.exists)

        args = cli.build_subcommand_parser().parse_args(
            ["stats", "--root", str(root)]
        )
        sock = transport.client_connect(30.0)
        try:
            daemon._send_daemon_request(sock, transport, "stats", args)
        finally:
            sock.close()
        assert started.wait(timeout=5.0), "slow request never started"

        daemon._CLIENT_TIMEOUT = 0.3
        assert daemon.stop(root) == daemon.EXIT_DAEMON_STILL_RUNNING

        assert transport.exists()
        assert dt.is_daemon_reachable(transport, timeout=2.0)
        assert thread.is_alive()
    finally:
        daemon._CLIENT_TIMEOUT = original_timeout
        daemon.stop(root)
        thread.join(timeout=_POLL_DEADLINE)


def test_stop_reports_still_running_message_and_exit_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """Round 21 (tensorflow.md §3, Track C), text/exit-code companion
    to ``test_stop_does_not_unlink_live_daemon_when_ack_and_pid_query_
    both_fail`` above, which can't safely assert ``print()`` output
    itself (see that test's docstring for why). Exercises the exact
    same "still running and busy, left alone" branch in ``stop()``
    without a real daemon thread or its busy-request stdout-capture
    race at all: a ``socketpair()`` stands in for the client
    connection (a short ``settimeout`` deterministically fails the
    graceful-shutdown-ack read, no sleep-based busy double needed),
    and a minimal fake transport skips real auth/socket-file handling
    so ``stop()`` runs single-threaded, synchronously, start to finish.

    Confirms the new honest message and ``EXIT_DAEMON_STILL_RUNNING``
    exit code, and that ``cleanup()`` -- which would strand a live
    daemon's transport in this branch, the exact bug round 14 already
    fixed once -- is never called here.
    """
    client_sock, server_sock = socket.socketpair()
    client_sock.settimeout(0.2)

    class _FakeTransport:
        def exists(self) -> bool:
            return True

        def client_connect(self, timeout: float) -> socket.socket:
            return client_sock

        def send_auth_preamble(self, sock: socket.socket) -> None:
            pass

        def cleanup(self) -> None:
            raise AssertionError(
                "cleanup() must not run on the still-running branch"
            )

    transport = _FakeTransport()
    monkeypatch.setattr(
        daemon, "default_transport_for", lambda root: transport
    )
    monkeypatch.setattr(daemon, "_query_pid", lambda t: None)
    monkeypatch.setattr(
        daemon, "is_daemon_reachable", lambda t, timeout=0.0: True
    )

    try:
        code = daemon.stop(tmp_path)
        out = capsys.readouterr().out
    finally:
        client_sock.close()
        server_sock.close()

    assert code == daemon.EXIT_DAEMON_STILL_RUNNING
    assert "still running and busy" in out
    assert "stopped" not in out


def test_stop_then_immediate_command_falls_back_not_abandoned(
    short_root: Path,
) -> None:
    """Round-14 master report: three independent evaluators
    (``cline.md`` §5.2, ``claude-buddy.md`` §3.3, ``claude-code.md``
    §1) found a command issued within ~1s of ``daemon stop`` hard-
    failing with exit 7 ("a daemon-routed request did not respond in
    time") instead of silently falling back to direct-process mode,
    violating the documented §3.3 fail-open contract for an entirely
    ordinary stop-then-continue-working sequence. Uses a real
    subprocess daemon (not the in-thread fixture used elsewhere in
    this module) since the bug is specifically about what a separate
    client process observes -- an in-thread test can't reproduce the
    client-side race a real ``dekko daemon stop`` process leaves
    behind."""
    root = short_root
    (root / "a.py").write_text("def f() -> int:\n    return 1\n")
    assert cli.main(["map", str(root), "--quiet"]) == 0

    transport = dt.default_transport_for(root)
    assert (
        cli.main(
            [
                "daemon",
                "start",
                "--root",
                str(root),
                "--idle-timeout",
                "60",
            ]
        )
        == 0
    )
    try:
        assert _wait_until(lambda: dt.is_daemon_reachable(transport))

        assert cli.main(["daemon", "stop", "--root", str(root)]) == 0

        code = cli.main(["query", "symbol", "f", "--root", str(root)])
        assert code != daemon.EXIT_DAEMON_ABANDONED
        assert code == 0
    finally:
        cli.main(["daemon", "stop", "--root", str(root)])


class _FakeArtifactTransport:
    """Minimal ``exists()``-only stand-in for ``_wait_for_teardown``'s
    polling contract, exercised in isolation from any real socket."""

    def __init__(self, false_after: int) -> None:
        self.calls = 0
        self._false_after = false_after

    def exists(self) -> bool:
        self.calls += 1
        return self.calls <= self._false_after


def test_wait_for_teardown_returns_once_artifacts_disappear() -> None:
    transport = _FakeArtifactTransport(false_after=3)
    daemon._wait_for_teardown(transport, timeout=2.0)
    assert not transport.exists()


def test_wait_for_teardown_gives_up_after_its_own_timeout() -> None:
    transport = _FakeArtifactTransport(false_after=10_000)
    started = time.monotonic()
    daemon._wait_for_teardown(transport, timeout=0.1)
    assert time.monotonic() - started < 1.0


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
    assert data["busy"] is False


def test_status_probe_timeout_reports_confirmed_false_not_not_running(
    short_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """Round-14 daemon-status-contention-plan.md §1-2: before this fix,
    a post-connect ``TimeoutError`` in ``status()``'s ``_recv_line``
    call (raised only *after* a live listener already accepted the
    connection -- see ``_status_connect``'s docstring for why a
    genuinely dead/absent daemon can never reach this point, since a
    Unix-socket connect to a path with no listener behind it fails
    immediately with ``ConnectionRefusedError``, not a timeout) used
    to be caught by the same ``except (OSError, ValueError): data =
    None`` clause as every other failure, folding "alive but hasn't
    replied yet" into the identical "not running" outcome as a
    genuine absence -- tensorflow.md §4.2's exact symptom (~30.0s
    calls returning ``{"running": false}`` for a confirmed-alive
    daemon).

    Reproduces the mechanism deterministically with a raw listener
    that accepts the status connection and then simply never replies,
    standing in for a status thread starved of CPU under sustained
    GIL/OS-scheduling contention -- this is the *same* code path a
    real starved status thread would trigger (a connected-but-
    unanswered socket, timing out in ``_recv_line``), without needing
    genuine contention itself, which this project's own daemon tests
    otherwise reproduce with sleep-based slow commands that a
    dedicated status listener stays responsive under (see
    ``test_status_true_positive_while_daemon_busy_on_slow_request``)
    -- only real CPU-bound work reaches the exposure this fix covers,
    which a raw non-replying listener triggers directly instead of
    needing to construct.
    """
    monkeypatch.setattr(daemon, "_STATUS_PROBE_TIMEOUT", 0.3)
    root = short_root
    transport = dt.default_transport_for(root)
    main_sock = transport.bind_and_listen()
    status_sock = transport.bind_status_listener()

    def accept_and_hang() -> None:
        conn, _ = status_sock.accept()
        time.sleep(2.0)
        conn.close()

    server_thread = threading.Thread(target=accept_and_hang, daemon=True)
    server_thread.start()
    try:
        code = daemon.status(root, as_json=True)
    finally:
        server_thread.join(timeout=3.0)
        main_sock.close()
        status_sock.close()
        transport.cleanup()

    assert code == 0
    data = _json.loads(capsys.readouterr().out)
    assert data["running"] is True
    assert data["confirmed"] is False
    assert "note" in data


def test_query_pid_timeout_returns_none_same_as_daemon_absent(
    short_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_query_pid``'s contract intentionally stays "``None`` on any
    failure, including a timeout" (round-14 daemon-status-contention-
    plan.md §3's "open question": the extra confirmation ``stop()``
    needs when a pid lookup fails to confirm anything is added at the
    call site via ``is_daemon_reachable``, not by growing this
    function's own return type into a three-state contract). Confirms
    a post-connect timeout here still returns ``None`` rather than
    raising -- exactly like every other failure mode it already
    covers -- using the same raw-non-replying-listener technique as
    the ``status()`` probe-timeout test above.
    """
    monkeypatch.setattr(daemon, "_STATUS_PROBE_TIMEOUT", 0.3)
    root = short_root
    transport = dt.default_transport_for(root)
    main_sock = transport.bind_and_listen()
    status_sock = transport.bind_status_listener()

    def accept_and_hang() -> None:
        conn, _ = status_sock.accept()
        time.sleep(2.0)
        conn.close()

    server_thread = threading.Thread(target=accept_and_hang, daemon=True)
    server_thread.start()
    try:
        assert daemon._query_pid(transport) is None
    finally:
        server_thread.join(timeout=3.0)
        main_sock.close()
        status_sock.close()
        transport.cleanup()


def test_client_timeout_matches_request_timeout() -> None:
    """Round-12 master report §3.5: ``_CLIENT_TIMEOUT`` used to be a
    separate, much tighter 2.0s constant covering the client's entire
    connect+send+recv cycle -- shorter than the server's own
    per-request budget (``_REQUEST_TIMEOUT``), so a client could give
    up on a request the daemon was still legitimately servicing. It
    must never again drift below what the server itself allows a
    single request."""
    assert daemon._CLIENT_TIMEOUT == daemon._REQUEST_TIMEOUT


def test_scaled_client_timeout_floors_at_client_timeout_when_no_map(
    short_root: Path,
) -> None:
    """No ``map.json`` at all (or an unreadable one) -- the floor."""
    assert daemon._scaled_client_timeout(short_root) == daemon._CLIENT_TIMEOUT


def test_scaled_client_timeout_floors_for_a_small_map(
    short_root: Path,
) -> None:
    """A small map.json's scaled budget never drops below the floor."""
    dekko_dir = short_root / ".dekko"
    dekko_dir.mkdir()
    (dekko_dir / "map.json").write_bytes(b"{}")
    assert daemon._scaled_client_timeout(short_root) == daemon._CLIENT_TIMEOUT


def test_scaled_client_timeout_scales_past_the_floor_for_a_large_map(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-15 finding: a large enough map.json widens the budget.

    Uses a tiny ``_TIMEOUT_BYTES_PER_SECOND`` rather than writing a
    genuinely large file to disk, so the test stays fast -- the real
    constant is derived from round-15's own measurements (see that
    constant's docstring), not re-derived here.
    """
    monkeypatch.setattr(daemon, "_TIMEOUT_BYTES_PER_SECOND", 100)
    dekko_dir = short_root / ".dekko"
    dekko_dir.mkdir()
    (dekko_dir / "map.json").write_bytes(b"x" * 10_000)
    scaled = daemon._scaled_client_timeout(short_root)
    assert scaled == pytest.approx(100.0)
    assert scaled > daemon._CLIENT_TIMEOUT


def test_scaled_client_timeout_is_capped(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pathologically large map.json is capped, not unbounded."""
    monkeypatch.setattr(daemon, "_TIMEOUT_BYTES_PER_SECOND", 1)
    dekko_dir = short_root / ".dekko"
    dekko_dir.mkdir()
    (dekko_dir / "map.json").write_bytes(b"x" * 10_000)
    assert (
        daemon._scaled_client_timeout(short_root)
        == daemon._SCALED_CLIENT_TIMEOUT_CAP
    )


def test_timeout_bytes_per_second_reflects_round24_recalibration() -> None:
    """Round-24 finding: the pre-recalibration 5.5 MB/s fit was made
    against a pre-symbol-interning map.json and stayed stale after
    ``1f06c44e`` shrank map.json's on-disk size ~5.15x without
    changing build cost, under-provisioning every non-rev-cache-miss
    daemon-routed command on large repos by roughly that same factor.
    Asserts bounds, not an exact pin -- this is explicitly a
    single-repo fit (tensorflow) per the constant's own comment and
    may reasonably move again with a second large-repo data point, but
    it must never silently regress back toward the old, stale value.
    """
    assert daemon._TIMEOUT_BYTES_PER_SECOND < 5_500_000
    assert 900_000 <= daemon._TIMEOUT_BYTES_PER_SECOND <= 1_500_000


def test_try_daemon_uses_scaled_client_timeout(
    daemon_thread_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``try_daemon()`` connects with the size-aware budget, not the
    fixed ``_CLIENT_TIMEOUT`` -- the actual round-15 fix, not just the
    helper function it's built on."""
    root = daemon_thread_root
    seen: list[Path] = []
    sentinel = 12345.0

    def _fake_scaled(r: Path) -> float:
        seen.append(r)
        return sentinel

    monkeypatch.setattr(daemon, "_scaled_client_timeout", _fake_scaled)

    # DaemonTransport is abstract; the concrete class in play depends
    # on the platform (Unix socket vs. TCP loopback), so patch
    # whichever one default_transport_for() actually picked here --
    # patching the abstract base wouldn't reach either subclass's own
    # override.
    transport_cls = type(dt.default_transport_for(root))
    original_connect = transport_cls.client_connect
    seen_timeouts: list[float] = []

    def _spy_connect(
        self: dt.DaemonTransport, timeout: float
    ) -> socket.socket:
        seen_timeouts.append(timeout)
        return original_connect(self, timeout)

    monkeypatch.setattr(transport_cls, "client_connect", _spy_connect)

    args = cli.build_subcommand_parser().parse_args(
        ["status", "--root", str(root), "--json"]
    )
    result = daemon.try_daemon(args)

    assert result is not None
    assert seen == [root.resolve()]
    assert seen_timeouts == [sentinel]


# ---------------------------------------------------------------------
# Round-24 §2 fix: rev-cache-miss-aware timeout for diff/affected/
# workset, scaled by tracked-file count instead of map.json size.
# ---------------------------------------------------------------------


def test_scaled_client_timeout_for_revcache_miss_floors_when_untrackable(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unresolvable rev (``tracked_at_rev`` returns ``None``) -- the
    floor, same failure-mode contract as ``_scaled_client_timeout``
    with no ``map.json``."""
    monkeypatch.setattr(daemon.diff_mod, "tracked_at_rev", lambda r, rev: None)
    assert (
        daemon._scaled_client_timeout_for_revcache_miss(short_root, "HEAD")
        == daemon._CLIENT_TIMEOUT
    )


def test_scaled_client_timeout_for_revcache_miss_floors_for_few_files(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A small tracked-file count never drops the budget below the
    floor."""
    monkeypatch.setattr(
        daemon.diff_mod, "tracked_at_rev", lambda r, rev: ["a.py", "b.py"]
    )
    assert (
        daemon._scaled_client_timeout_for_revcache_miss(short_root, "HEAD")
        == daemon._CLIENT_TIMEOUT
    )


def test_scaled_client_timeout_for_revcache_miss_scales_past_the_floor(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-24 §2 fix: a large tracked-file count widens the budget,
    scaled by ``_TIMEOUT_SECONDS_PER_TRACKED_FILE`` rather than
    ``map.json`` size -- the tensorflow repro this fix targets never
    even reaches a ``map.json`` read on this path.

    Uses a tiny ``_TIMEOUT_SECONDS_PER_TRACKED_FILE`` rather than a
    genuinely large file list, matching
    ``test_scaled_client_timeout_scales_past_the_floor_for_a_large_map``'s
    own convention for its byte-size sibling.
    """
    monkeypatch.setattr(daemon, "_TIMEOUT_SECONDS_PER_TRACKED_FILE", 1.0)
    candidates = [f"f{i}.py" for i in range(100)]
    monkeypatch.setattr(
        daemon.diff_mod, "tracked_at_rev", lambda r, rev: candidates
    )
    scaled = daemon._scaled_client_timeout_for_revcache_miss(
        short_root, "HEAD"
    )
    assert scaled == pytest.approx(100.0)
    assert scaled > daemon._CLIENT_TIMEOUT


def test_scaled_client_timeout_for_revcache_miss_is_capped(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pathologically large tracked-file count is capped, not
    unbounded -- same cap ``_scaled_client_timeout`` shares."""
    monkeypatch.setattr(daemon, "_TIMEOUT_SECONDS_PER_TRACKED_FILE", 1.0)
    candidates = [f"f{i}.py" for i in range(10_000)]
    monkeypatch.setattr(
        daemon.diff_mod, "tracked_at_rev", lambda r, rev: candidates
    )
    assert (
        daemon._scaled_client_timeout_for_revcache_miss(short_root, "HEAD")
        == daemon._SCALED_CLIENT_TIMEOUT_CAP
    )


def test_target_rev_for_workset_symbol_seed_is_none(
    short_root: Path,
) -> None:
    """A ``workset --symbol`` seed never reaches ``old_snapshot`` -- no
    rev exists to scale a timeout by."""
    args = cli.build_subcommand_parser().parse_args(
        ["workset", "--symbol", "foo", "--root", str(short_root)]
    )
    assert daemon._target_rev_for("workset", args) is None


def test_target_rev_for_explicit_rev_wins(short_root: Path) -> None:
    """An explicit positional ``REV`` is used as-is -- no provenance
    sidecar read needed (and none exists here)."""
    args = cli.build_subcommand_parser().parse_args(
        ["affected", "deadbeef", "--root", str(short_root)]
    )
    assert daemon._target_rev_for("affected", args) == "deadbeef"


def test_target_rev_for_defaults_to_provenance_git_commit(
    short_root: Path,
) -> None:
    """No ``REV`` given -- falls back to the map's own recorded
    ``git_commit`` provenance, matching ``diff.run``'s default chain
    (``rev or prov.get("git_commit") or "HEAD"``)."""
    dekko_dir = short_root / ".dekko"
    dekko_dir.mkdir()
    (dekko_dir / "map.json").write_bytes(
        _json.dumps({"provenance": {"git_commit": "c" * 40}}).encode()
    )
    args = cli.build_subcommand_parser().parse_args(
        ["diff", "--root", str(short_root)]
    )
    assert daemon._target_rev_for("diff", args) == "c" * 40


def test_target_rev_for_defaults_to_head_with_no_provenance(
    short_root: Path,
) -> None:
    """Neither a ``REV`` nor a recorded ``git_commit`` -- falls back to
    ``"HEAD"``, matching ``diff.run``'s own final default."""
    args = cli.build_subcommand_parser().parse_args(
        ["diff", "--root", str(short_root)]
    )
    assert daemon._target_rev_for("diff", args) == "HEAD"


def test_try_daemon_uses_revcache_miss_timeout_for_a_genuine_miss(
    daemon_thread_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``affected``/``diff``/``workset`` route through the tracked-
    file-count-scaled timeout, not the ``map.json``-size-scaled one,
    when the target rev has no rev-cache entry yet."""
    root = daemon_thread_root
    sentinel = 54321.0

    monkeypatch.setattr(daemon.revcache, "has_entry", lambda r, rev: False)
    monkeypatch.setattr(
        daemon,
        "_scaled_client_timeout_for_revcache_miss",
        lambda r, rev: sentinel,
    )
    # A wrong value here would mean the test passed for the wrong
    # reason (the miss-aware path never actually got picked).
    monkeypatch.setattr(daemon, "_scaled_client_timeout", lambda r: 999999.0)

    transport_cls = type(dt.default_transport_for(root))
    original_connect = transport_cls.client_connect
    seen_timeouts: list[float] = []

    def _spy_connect(
        self: dt.DaemonTransport, timeout: float
    ) -> socket.socket:
        seen_timeouts.append(timeout)
        return original_connect(self, timeout)

    monkeypatch.setattr(transport_cls, "client_connect", _spy_connect)

    args = cli.build_subcommand_parser().parse_args(
        ["affected", "--root", str(root)]
    )
    daemon.try_daemon(args)

    assert seen_timeouts == [sentinel]


def test_try_daemon_uses_ordinary_timeout_on_a_revcache_hit(
    daemon_thread_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rev-cache *hit* is a fast disk read regardless of repo size --
    ``affected``/``diff``/``workset`` keep using the ordinary
    ``map.json``-size-scaled budget, unchanged, exactly as they did
    before this fix."""
    root = daemon_thread_root
    sentinel = 13579.0

    monkeypatch.setattr(daemon.revcache, "has_entry", lambda r, rev: True)
    monkeypatch.setattr(daemon, "_scaled_client_timeout", lambda r: sentinel)
    # A wrong value here would mean the test passed for the wrong
    # reason (the miss-aware path got picked despite the hit).
    monkeypatch.setattr(
        daemon,
        "_scaled_client_timeout_for_revcache_miss",
        lambda r, rev: 999999.0,
    )

    transport_cls = type(dt.default_transport_for(root))
    original_connect = transport_cls.client_connect
    seen_timeouts: list[float] = []

    def _spy_connect(
        self: dt.DaemonTransport, timeout: float
    ) -> socket.socket:
        seen_timeouts.append(timeout)
        return original_connect(self, timeout)

    monkeypatch.setattr(transport_cls, "client_connect", _spy_connect)

    args = cli.build_subcommand_parser().parse_args(
        ["affected", "--root", str(root)]
    )
    daemon.try_daemon(args)

    assert seen_timeouts == [sentinel]


def test_try_daemon_other_commands_never_use_revcache_miss_timeout(
    daemon_thread_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression check: this fix must not touch behavior for every
    daemon-routed command outside ``diff``/``affected``/``workset`` --
    ``query``/``search``/``outline``/etc. keep using
    ``_scaled_client_timeout(root)`` exactly as before, never the new
    rev-cache-miss branch at all."""
    root = daemon_thread_root
    called = False

    def _fail_if_called(r: Path, rev: str) -> float:
        nonlocal called
        called = True
        return 1.0

    monkeypatch.setattr(
        daemon, "_scaled_client_timeout_for_revcache_miss", _fail_if_called
    )

    args = cli.build_subcommand_parser().parse_args(
        ["status", "--root", str(root), "--json"]
    )
    daemon.try_daemon(args)

    assert called is False


def test_status_true_positive_while_daemon_busy_on_slow_request(
    short_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-13 master report §2: the main accept loop is deliberately
    single-threaded, so before this fix a concurrent ``daemon status``
    request couldn't be accepted -- let alone answered -- until
    whatever the daemon was currently servicing finished, no matter
    how generous the client timeout. The fix is a dedicated status-
    only listener (``DaemonTransport.bind_status_listener`` +
    ``daemon._serve_status_loop``), serviced by its own thread, that
    never blocks behind the main loop. This deliberately slows one
    dispatched command to 3s and confirms a status probe made *while
    it's in flight* still reports the daemon as running and busy.

    Queries the status listener directly over its own socket rather
    than through ``daemon.status()``'s ``print()``-based CLI wrapper:
    that wrapper's ``print()`` would land inside the busy request's
    own ``_capture()``-redirected stdout buffer in this *same-process*
    test (``contextlib.redirect_stdout`` is process-global, not
    per-thread) -- an artifact of exercising ``daemon.status()``
    in-thread rather than as the separate OS process a real ``dekko
    daemon status`` invocation always is, where no such collision
    exists. The raw socket round trip below is exactly what that
    separate process would see on the wire, so it's an equally
    faithful check of the actual fix."""
    started = threading.Event()
    real_run_stats = cli.run_stats

    def slow_run_stats(args: object) -> int:
        started.set()
        time.sleep(3.0)
        return real_run_stats(args)

    monkeypatch.setattr(cli, "run_stats", slow_run_stats)

    root = short_root
    (root / "a.py").write_text("def f() -> int:\n    return 1\n")
    assert cli.main(["map", str(root), "--quiet"]) == 0

    transport = dt.default_transport_for(root)
    thread = threading.Thread(
        target=daemon.serve_daemon,
        kwargs={"root": root, "idle_timeout": 30.0},
        daemon=True,
    )
    thread.start()
    try:
        assert _wait_until(transport.exists)

        def send_slow_request() -> None:
            args = cli.build_subcommand_parser().parse_args(
                ["stats", "--root", str(root)]
            )
            daemon.try_daemon(args)

        requester = threading.Thread(target=send_slow_request, daemon=True)
        requester.start()
        try:
            assert started.wait(timeout=5.0), "slow request never started"

            assert dt.is_daemon_reachable(transport, timeout=5.0)
            sock = transport.status_client_connect(5.0)
            try:
                transport.send_auth_preamble(sock)
                daemon._send_line(sock, {"cmd": daemon._STATUS_CMD})
                raw = daemon._recv_line(sock)
            finally:
                sock.close()
            assert raw is not None
            data = _json.loads(raw)
            assert data["running"] is True
            assert data["busy"] is True
        finally:
            requester.join(timeout=6.0)
    finally:
        daemon.stop(root)
        thread.join(timeout=_POLL_DEADLINE)


def test_start_does_not_spawn_duplicate_while_daemon_busy(
    short_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-13 master report §2: ``daemon start``'s own liveness probe
    (``is_daemon_reachable``, called with its default 2.0s timeout)
    used to target the main command socket, which can't answer while
    busy on a slow routed request -- making a live, busy daemon look
    unreachable and causing ``start()`` to spawn a duplicate process
    for the same root (the exact bug caught live in claude-code.md,
    two PIDs for one root). Confirms ``start()`` now returns success
    (the "already running" branch) with no duplicate spawn, even while
    the daemon is mid-request -- the fix (probing the dedicated
    status-only listener instead of the busy main socket) closes this
    without any timeout tuning at this call site.

    Doesn't assert on ``start()``'s printed output: like the busy-
    status test above, calling a ``print()``-based function directly
    in this same-process test while the busy request's own
    ``_capture()`` (process-global ``contextlib.redirect_stdout``) is
    active would swallow that output into the *other* thread's
    captured buffer -- a same-process testing artifact with no
    production equivalent, not something to route around when a
    behavioral assertion (return code, no spawn) already proves the
    fix. See that test's docstring for the full explanation."""
    started = threading.Event()
    real_run_stats = cli.run_stats

    def slow_run_stats(args: object) -> int:
        started.set()
        time.sleep(3.0)
        return real_run_stats(args)

    monkeypatch.setattr(cli, "run_stats", slow_run_stats)

    root = short_root
    (root / "a.py").write_text("def f() -> int:\n    return 1\n")
    assert cli.main(["map", str(root), "--quiet"]) == 0

    transport = dt.default_transport_for(root)
    thread = threading.Thread(
        target=daemon.serve_daemon,
        kwargs={"root": root, "idle_timeout": 30.0},
        daemon=True,
    )
    thread.start()
    try:
        assert _wait_until(transport.exists)

        def send_slow_request() -> None:
            args = cli.build_subcommand_parser().parse_args(
                ["stats", "--root", str(root)]
            )
            daemon.try_daemon(args)

        requester = threading.Thread(target=send_slow_request, daemon=True)
        requester.start()
        try:
            assert started.wait(timeout=5.0), "slow request never started"

            spawned: list[list[str]] = []
            monkeypatch.setattr(
                daemon, "spawn_detached", lambda cmd: spawned.append(cmd)
            )
            code = daemon.start(root)
            assert code == 0
            assert spawned == []  # no duplicate process spawned
        finally:
            requester.join(timeout=6.0)
    finally:
        daemon.stop(root)
        thread.join(timeout=_POLL_DEADLINE)


def test_try_daemon_raises_abandoned_error_on_client_timeout(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-12 master report §3.8: once a request has actually been
    sent to the daemon, a client-side timeout must surface as
    ``DaemonRequestAbandonedError``, not a plain ``None`` return --
    the daemon (single-threaded, no cancellation, see
    ``_handle_connection``'s docstring) may still be computing the
    abandoned request in the background, so the caller must not treat
    this the same as "no daemon reachable, a free fallback." Shortens
    ``_CLIENT_TIMEOUT`` well below a deliberately slowed dispatched
    command's duration to force exactly that race."""
    started = threading.Event()
    finished = threading.Event()
    real_run_stats = cli.run_stats

    def slow_run_stats(args: object) -> int:
        started.set()
        time.sleep(1.5)
        finished.set()
        return real_run_stats(args)

    monkeypatch.setattr(cli, "run_stats", slow_run_stats)
    original_timeout = daemon._CLIENT_TIMEOUT
    monkeypatch.setattr(daemon, "_CLIENT_TIMEOUT", 0.3)

    root = short_root
    (root / "a.py").write_text("def f() -> int:\n    return 1\n")
    daemon._CLIENT_TIMEOUT = original_timeout
    assert cli.main(["map", str(root), "--quiet"]) == 0
    daemon._CLIENT_TIMEOUT = 0.3

    transport = dt.default_transport_for(root)
    thread = threading.Thread(
        target=daemon.serve_daemon,
        kwargs={"root": root, "idle_timeout": 30.0},
        daemon=True,
    )
    thread.start()
    try:
        assert _wait_until(transport.exists)

        args = cli.build_subcommand_parser().parse_args(
            ["stats", "--root", str(root)]
        )
        with pytest.raises(daemon.DaemonRequestAbandonedError):
            daemon.try_daemon(args)
        assert started.is_set()

        # Let the daemon-side slow command actually finish (and
        # restore a sane timeout) before teardown, so `stop()` below
        # doesn't itself race the daemon's single-threaded accept
        # loop while it's still busy with the abandoned request.
        assert finished.wait(timeout=5.0)
    finally:
        daemon._CLIENT_TIMEOUT = original_timeout
        daemon.stop(root)
        thread.join(timeout=_POLL_DEADLINE)


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
# Phase 4: diff/affected's current-tree load shares the warm cache.
#
# diff.run/affected.changes used to call mapfile.load_map(root)
# directly, bypassing load_or_regen (and therefore the daemon's
# warm-cache hook) entirely -- the "partial exception" the design doc
# flagged at §2.4's last bullet. repo_ops.load_current_index_no_regen()
# closes that gap: it checks the same _daemon_cache_get/_put hooks
# load_or_regen uses, without adopting its regen-on-stale side
# effect (diff/affected never wrote map.json as a side effect before,
# and still don't). These tests prove the cache is now genuinely
# shared -- a query warms it for diff/affected and vice versa -- and
# that a working-tree edit is still never served stale.
# ---------------------------------------------------------------------


def _git(root: Path, *args: str) -> None:
    """Run a git command in ``root``, raising on failure."""
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def daemon_thread_git_root(short_root: Path) -> Iterator[Path]:
    """A committed git repo (``_CACHE_SRC``), mapped, served by an
    in-thread daemon -- diff/affected need real git history for their
    old-side snapshot, unlike ``daemon_thread_cached_root``."""
    root = short_root
    _git(root, "init", "-q")
    for name, text in _CACHE_SRC.items():
        (root / name).write_text(text)
    _git(root, "add", "-A")
    _git(
        root,
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "commit",
        "-m",
        "base",
    )
    assert cli.main(["map", str(root), "--quiet"]) == 0

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


def _diff_args(root: Path) -> object:
    return cli.build_subcommand_parser().parse_args(
        ["diff", "--root", str(root)]
    )


def _affected_args(root: Path) -> object:
    return cli.build_subcommand_parser().parse_args(
        ["affected", "--root", str(root)]
    )


def test_daemon_diff_and_affected_reuse_query_warmed_cache(
    daemon_thread_git_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``diff``/``affected`` request's current-tree load reuses a
    cache a prior ``query`` request already warmed -- the fix for
    Phase 4's partial-exception investigation."""
    root = daemon_thread_git_root
    calls: list[Path] = []
    real_load_map = mapfile.load_map

    def spy(root_arg: Path) -> mapfile.MapIndex | None:
        calls.append(root_arg)
        return real_load_map(root_arg)

    monkeypatch.setattr(mapfile, "load_map", spy)

    result1 = daemon.try_daemon(_query_symbol_args(root, "f"))
    assert result1 is not None and result1[0] == 0
    assert len(calls) == 1  # cache miss: query populates the cache

    result2 = daemon.try_daemon(_diff_args(root))
    assert result2 is not None
    assert result2[0] == 0  # clean tree vs the committed base
    assert len(calls) == 1  # diff reused query's warm cache

    result3 = daemon.try_daemon(_affected_args(root))
    assert result3 is not None
    assert result3[0] == 0  # no impacted tests, clean tree
    assert len(calls) == 1  # affected reused the same warm cache


def test_daemon_diff_populates_cache_for_a_later_query(
    daemon_thread_git_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reverse direction: ``diff`` runs first and its own
    current-tree load populates the cache; a later ``query`` reuses
    it -- proves the seam is a genuine two-way shared cache, not a
    one-directional special case."""
    root = daemon_thread_git_root
    calls: list[Path] = []
    real_load_map = mapfile.load_map

    def spy(root_arg: Path) -> mapfile.MapIndex | None:
        calls.append(root_arg)
        return real_load_map(root_arg)

    monkeypatch.setattr(mapfile, "load_map", spy)

    result1 = daemon.try_daemon(_diff_args(root))
    assert result1 is not None and result1[0] == 0
    assert len(calls) == 1  # diff's own current-tree load populates it

    result2 = daemon.try_daemon(_query_symbol_args(root, "g"))
    assert result2 is not None and result2[0] == 0
    assert len(calls) == 1  # query reused diff's warm cache
    assert "g() -> int" in result2[1]


def test_daemon_diff_cache_miss_on_edit_still_reports_correctly(
    daemon_thread_git_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A working-tree edit between a warming ``query`` and a following
    ``diff`` must not be served a stale answer: the shared cache
    reports a miss (forcing a fresh ``mapfile.load_map``), and
    ``diff.run``'s own stale-index fallback (an in-memory re-parse via
    ``diff.snapshot()``, never touching ``map.json``) still reports
    the edit correctly -- correctness is unchanged by this seam, only
    the cache-hit rate is."""
    root = daemon_thread_git_root
    calls: list[Path] = []
    real_load_map = mapfile.load_map

    def spy(root_arg: Path) -> mapfile.MapIndex | None:
        calls.append(root_arg)
        return real_load_map(root_arg)

    monkeypatch.setattr(mapfile, "load_map", spy)

    result1 = daemon.try_daemon(_query_symbol_args(root, "f"))
    assert result1 is not None and result1[0] == 0
    assert len(calls) == 1  # cache miss: query populates the cache

    (root / "a.py").write_text("def f() -> int:\n    return 2\n")

    result2 = daemon.try_daemon(_diff_args(root))
    assert result2 is not None
    assert result2[0] == 1  # a real change detected (f's body changed)
    assert "~ a.py:1" in result2[1]
    assert len(calls) == 2  # cache miss on the now-stale index: reloaded


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


def _deep_root(short_root: Path) -> Path:
    """Build a root deep enough that ``<root>/.dekko/daemon.sock``
    exceeds the ``AF_UNIX`` ``sun_path`` limit -- mirrors
    ``tests/test_daemon_transport.py``'s helper of the same shape."""
    deep = short_root
    segment = "x" * 40
    while len(str(deep / ".dekko" / "daemon.sock")) < dt._SUN_PATH_LIMIT + 20:
        deep = deep / segment
    deep.mkdir(parents=True)
    return deep


@pytest.mark.skipif(sys.platform == "win32", reason="AF_UNIX is POSIX-only")
class _FakeStartTransport:
    """Minimal stub for exercising ``start()``'s bind-confirmation poll.

    ``exists_after`` is the number of ``exists()`` calls that return
    ``False`` before the transition to ``True`` (``None`` means it
    never confirms, simulating a child that never finishes binding
    within the poll cap).
    """

    def __init__(self, exists_after: int | None) -> None:
        self._calls = 0
        self._exists_after = exists_after

    def preflight_check(self) -> None:
        return None

    def cleanup(self) -> None:
        return None

    def describe(self) -> str:
        return "fake transport"

    def exists(self) -> bool:
        self._calls += 1
        if self._exists_after is None:
            return False
        return self._calls > self._exists_after


def test_start_waits_for_bind_confirmation_before_reporting_started(
    short_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """Round-23 §13: ``start()`` used to return (and print "started")
    the instant ``spawn_detached`` launched the child, before the
    child had necessarily finished binding -- an immediate ``daemon
    status`` call could race that and see ``transport.exists() ==
    False``, an honest-but-wrong "not running" (observed ~1/6 in
    testing). Confirms ``start()`` now polls
    ``transport.exists()`` and only reports "started" once it
    transitions to ``True``, not before."""
    transport = _FakeStartTransport(exists_after=3)
    monkeypatch.setattr(
        daemon, "default_transport_for", lambda root: transport
    )
    monkeypatch.setattr(daemon, "is_daemon_reachable", lambda t: False)
    monkeypatch.setattr(daemon, "_START_CONFIRM_POLL_INTERVAL", 0.001)
    spawned: list[list[str]] = []
    monkeypatch.setattr(
        daemon, "spawn_detached", lambda cmd: spawned.append(cmd)
    )

    code = daemon.start(short_root)

    assert code == 0
    assert spawned  # the spawn itself happened
    assert transport._calls > 3  # polled past the False->True transition
    out = capsys.readouterr().out
    assert f"dekko daemon: started for {short_root}" in out
    assert "didn't confirm" not in out


def test_start_reports_unconfirmed_when_bind_poll_times_out(
    short_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """Complements the confirmation test above: when the poll cap is
    hit without ``transport.exists()`` ever going ``True``, ``start()``
    must not lie and print "started" -- it prints a distinct,
    honest "spawned but unconfirmed" message and still returns ``0``
    (the spawn genuinely succeeded; slow-to-bind is not the same as
    failed -- a new non-zero exit code here would be a breaking change
    for scripts already gating on this command's exit code)."""
    transport = _FakeStartTransport(exists_after=None)
    monkeypatch.setattr(
        daemon, "default_transport_for", lambda root: transport
    )
    monkeypatch.setattr(daemon, "is_daemon_reachable", lambda t: False)
    monkeypatch.setattr(daemon, "_START_CONFIRM_TIMEOUT", 0.05)
    monkeypatch.setattr(daemon, "_START_CONFIRM_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(daemon, "spawn_detached", lambda cmd: None)

    code = daemon.start(short_root)

    assert code == 0
    out = capsys.readouterr().out
    assert f"dekko daemon: started for {short_root}" not in out
    assert "didn't confirm" in out
    assert "dekko daemon status" in out


def test_start_immediately_followed_by_status_never_false_negative(
    short_root: Path,
) -> None:
    """The actual regression test for the reported bug (round-23 §13,
    awesome-go.md §2.2): a real ``daemon start`` followed *immediately*
    (no artificial wait) by ``daemon status`` must never report "not
    running" once ``start()`` itself has returned -- run several
    real start/stop cycles for a reasonable chance of statistically
    catching the pre-fix ~1/6 failure rate if this fix were reverted."""
    transport = dt.default_transport_for(short_root)
    for _ in range(6):
        try:
            code = daemon.start(short_root)
            assert code == 0
            # No _wait_until here on purpose -- start() itself is now
            # responsible for not returning until confirmed (or having
            # honestly said it couldn't confirm).
            assert transport.exists()
            assert dt.is_daemon_reachable(transport)
        finally:
            daemon.stop(short_root)
            assert _wait_until(lambda: not transport.exists())


def test_start_fails_fast_on_unbindable_socket_path(
    short_root: Path, capsys: pytest.CaptureFixture
) -> None:
    """Round-10's headline daemon-mode finding: ``dekko daemon start``
    used to print "started" and exit 0 even though the detached
    child's own ``bind_and_listen()`` silently failed on a too-long
    ``AF_UNIX`` ``sun_path`` -- the failure only surfaced later, on a
    subsequent ``daemon status`` call, because the parent never waited
    for or captured the child's stderr. ``start()`` now runs
    ``transport.preflight_check()`` in the foreground before spawning
    anything, so this case is caught immediately with a non-zero exit
    code and a clear message, and no detached process is ever
    spawned."""
    deep = _deep_root(short_root)

    code = daemon.start(deep)
    assert code == 1

    captured = capsys.readouterr()
    assert "started" not in captured.out
    assert "cannot start" in captured.err
    assert "too long" in captured.err

    transport = dt.default_transport_for(deep)
    assert not transport.exists()

    # A second attempt against the same broken root must fail exactly
    # the same deterministic way -- not the flaky "sometimes prints
    # only 'started' with no error at all" behavior the round-10
    # report flagged for the old race between the child's async
    # stderr write and the parent's own stdout flush.
    code2 = daemon.start(deep)
    assert code2 == 1
    assert "started" not in capsys.readouterr().out


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
