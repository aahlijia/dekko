"""Tests for the OS-independent daemon transport abstraction.

Per the daemon-mode-cli-workflow.md's cross-cutting test-scoping rule
(its §7): most of this feature's tests should run unconditionally on
every platform, since ``default_transport_for()`` is the only
``sys.platform`` branch anything downstream needs to know about. A
test only gets a ``skipif`` when the code path it exercises is itself
``sys.platform``-gated inside ``daemon_transport.py`` -- i.e. the
other branch genuinely never runs on this OS.
"""

import json
import shutil
import socket
import stat
import sys
import tempfile
import threading
from pathlib import Path

import pytest

from dekko import daemon_transport as dt


@pytest.fixture
def short_root() -> Path:
    """A short-path temp dir for tests that bind a real AF_UNIX socket.

    pytest's own ``tmp_path`` fixture routes through a path like
    ``/private/var/folders/.../pytest-of-<user>/pytest-<n>/
    <test-name>0/`` on macOS, which alone can already exceed (or come
    close to) the ~104-byte ``sun_path`` limit before ``.dekko/
    daemon.sock`` is even appended -- confirmed by hand: a bare
    ``socket.bind()`` at a 136-byte path already raises "AF_UNIX path
    too long" on this platform. Tests that need a real, reliably-
    short bindable path use ``tempfile.mkdtemp()`` (``/tmp/tmpXXXXXXXX``,
    well under the limit) instead.
    """
    d = Path(tempfile.mkdtemp(prefix="dkd"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _serve_one_line(
    listener: socket.socket,
    transport: dt.DaemonTransport,
    require_auth: bool,
    ready: threading.Event,
) -> list[str]:
    """Accept one connection, optionally authenticate, echo one line.

    Runs in a background thread. Returns (via the mutable ``result``
    list appended in-place) the received line, or nothing if
    authentication failed and the connection was closed unanswered.
    """
    result: list[str] = []
    listener.settimeout(5.0)
    ready.set()
    try:
        conn, _addr = listener.accept()
    except OSError:
        return result
    with conn:
        if require_auth and not transport.authenticate(conn):
            return result
        conn.settimeout(5.0)
        reader = conn.makefile("r", encoding="utf-8", newline="\n")
        line = reader.readline()
        if not line:
            return result
        result.append(line.strip())
        conn.sendall(f"echo:{line.strip()}\n".encode("utf-8"))
    return result


def test_default_transport_for_returns_daemon_transport(
    tmp_path: Path,
) -> None:
    transport = dt.default_transport_for(tmp_path)
    assert isinstance(transport, dt.DaemonTransport)


def test_platform_default_round_trip(short_root: Path) -> None:
    """Bind, connect, send/receive one framed line, cleanup.

    Runs against whatever default_transport_for() returns for the
    platform actually running this test -- the one test proving "the
    platform's own default transport works," per the workflow doc.
    """
    transport = dt.default_transport_for(short_root)
    assert not transport.exists()

    listener = transport.bind_and_listen()
    assert transport.exists()

    ready = threading.Event()
    received: list[str] = []

    def _serve() -> None:
        received.extend(_serve_one_line(listener, transport, True, ready))

    server_thread = threading.Thread(target=_serve, daemon=True)
    server_thread.start()
    ready.wait(timeout=5.0)

    try:
        sock = transport.client_connect(timeout=5.0)
        transport.send_auth_preamble(sock)
        sock.sendall(b"hello daemon\n")
        reply = sock.makefile("r", encoding="utf-8", newline="\n").readline()
        sock.close()
    finally:
        server_thread.join(timeout=5.0)
        listener.close()
        transport.cleanup()

    assert received == ["hello daemon"]
    assert reply.strip() == "echo:hello daemon"
    assert not transport.exists()


# ---------------------------------------------------------------------
# UnixSocketTransport-specific
# ---------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="AF_UNIX is POSIX-only")
def test_unix_socket_created_with_owner_only_mode(short_root: Path) -> None:
    transport = dt.UnixSocketTransport(short_root)
    listener = transport.bind_and_listen()
    try:
        mode = stat.S_IMODE(transport.socket_path.stat().st_mode)
        assert mode == 0o600
    finally:
        listener.close()
        transport.cleanup()


@pytest.mark.skipif(sys.platform == "win32", reason="AF_UNIX is POSIX-only")
def test_unix_socket_default_on_posix(tmp_path: Path) -> None:
    transport = dt.default_transport_for(tmp_path)
    assert isinstance(transport, dt.UnixSocketTransport)


@pytest.mark.skipif(sys.platform == "win32", reason="AF_UNIX is POSIX-only")
def test_unix_socket_sun_path_too_long_raises_transport_unavailable(
    short_root: Path,
) -> None:
    # Build a root path deep enough that
    # "<root>/.dekko/daemon.sock" exceeds the sun_path limit, starting
    # from a known-short base so the result is deterministic
    # regardless of how long the platform's own temp-dir prefix is.
    deep = short_root
    segment = "x" * 40
    while len(str(deep / ".dekko" / "daemon.sock")) < dt._SUN_PATH_LIMIT + 20:
        deep = deep / segment
    deep.mkdir(parents=True)

    transport = dt.UnixSocketTransport(deep)
    with pytest.raises(dt.TransportUnavailable):
        transport.bind_and_listen()

    # The CLI's fail-open path treats this identically to "no
    # daemon present" -- exists()/client_connect() must not raise.
    assert not transport.exists()
    with pytest.raises(dt.DaemonUnavailableError):
        transport.client_connect(timeout=1.0)


# ---------------------------------------------------------------------
# TcpLoopbackTransport-specific (unconditional -- usable everywhere)
# ---------------------------------------------------------------------


def test_tcp_loopback_port_file_contents(tmp_path: Path) -> None:
    transport = dt.TcpLoopbackTransport(tmp_path)
    listener = transport.bind_and_listen()
    try:
        data = json.loads(transport.port_file.read_text())
        assert isinstance(data["port"], int)
        assert len(data["token"]) == dt._TOKEN_BYTES * 2
        int(data["token"], 16)  # valid hex

        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.settimeout(2.0)
        probe.connect(("127.0.0.1", data["port"]))
        probe.close()
    finally:
        listener.close()
        transport.cleanup()


def test_tcp_loopback_rejects_missing_token(tmp_path: Path) -> None:
    transport = dt.TcpLoopbackTransport(tmp_path)
    listener = transport.bind_and_listen()

    ready = threading.Event()
    received: list[str] = []

    def _serve() -> None:
        received.extend(_serve_one_line(listener, transport, True, ready))

    server_thread = threading.Thread(target=_serve, daemon=True)
    server_thread.start()
    ready.wait(timeout=5.0)

    try:
        port, _token = transport._read_port_file()
        raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw.settimeout(3.0)
        raw.connect(("127.0.0.1", port))
        # No auth preamble sent -- the request line looks like a
        # real request, but authenticate() should read it as (and
        # reject) a missing/mismatched token.
        raw.sendall(b"hello daemon\n")
        # authenticate() rejects the (mis-typed) token and the server
        # closes the connection -- recv() returns b"" (EOF), not a
        # response line.
        reply = raw.recv(4096)
        assert reply == b""
    finally:
        server_thread.join(timeout=5.0)
        listener.close()
        transport.cleanup()
        raw.close()

    # authenticate() consumed "hello daemon" as the (wrong) token and
    # closed the connection -- the echo handler never ran.
    assert received == []


def test_tcp_loopback_rejects_mismatched_token(tmp_path: Path) -> None:
    transport = dt.TcpLoopbackTransport(tmp_path)
    listener = transport.bind_and_listen()

    ready = threading.Event()
    received: list[str] = []

    def _serve() -> None:
        received.extend(_serve_one_line(listener, transport, True, ready))

    server_thread = threading.Thread(target=_serve, daemon=True)
    server_thread.start()
    ready.wait(timeout=5.0)

    try:
        port, _token = transport._read_port_file()
        raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw.settimeout(3.0)
        raw.connect(("127.0.0.1", port))
        raw.sendall(b"not-the-real-token\n")
    finally:
        server_thread.join(timeout=5.0)
        listener.close()
        transport.cleanup()
        raw.close()

    assert received == []


def test_tcp_loopback_accepts_correct_token(tmp_path: Path) -> None:
    transport = dt.TcpLoopbackTransport(tmp_path)
    listener = transport.bind_and_listen()

    ready = threading.Event()
    received: list[str] = []

    def _serve() -> None:
        received.extend(_serve_one_line(listener, transport, True, ready))

    server_thread = threading.Thread(target=_serve, daemon=True)
    server_thread.start()
    ready.wait(timeout=5.0)

    try:
        sock = transport.client_connect(timeout=5.0)
        transport.send_auth_preamble(sock)
        sock.sendall(b"hello daemon\n")
        reply = sock.makefile("r", encoding="utf-8", newline="\n").readline()
        sock.close()
    finally:
        server_thread.join(timeout=5.0)
        listener.close()
        transport.cleanup()

    assert received == ["hello daemon"]
    assert reply.strip() == "echo:hello daemon"


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="only asserting Windows' actual default here",
)
def test_tcp_loopback_default_on_windows(tmp_path: Path) -> None:
    transport = dt.default_transport_for(tmp_path)
    assert isinstance(transport, dt.TcpLoopbackTransport)


# ---------------------------------------------------------------------
# spawn_detached: pure kwarg-construction checks against a mocked
# Popen -- both branches run on every platform, nothing OS-specific
# about *running* these tests, only about which branch they exercise.
# ---------------------------------------------------------------------


def test_spawn_detached_posix_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dt.sys, "platform", "darwin")
    captured = {}

    def _fake_popen(cmd, **kwargs):  # noqa: ANN001, ANN003, ANN202
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return "fake-popen"

    monkeypatch.setattr(dt.subprocess, "Popen", _fake_popen)

    result = dt.spawn_detached(["dekko", "daemon", "start"])

    assert result == "fake-popen"
    assert captured["cmd"] == ["dekko", "daemon", "start"]
    assert captured["kwargs"] == {"start_new_session": True}


def test_spawn_detached_windows_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dt.sys, "platform", "win32")
    captured = {}

    def _fake_popen(cmd, **kwargs):  # noqa: ANN001, ANN003, ANN202
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return "fake-popen"

    monkeypatch.setattr(dt.subprocess, "Popen", _fake_popen)

    result = dt.spawn_detached(["dekko", "daemon", "start"])

    assert result == "fake-popen"
    assert captured["cmd"] == ["dekko", "daemon", "start"]
    # dt._CREATE_NEW_PROCESS_GROUP/_DETACHED_PROCESS fall back to the
    # documented Win32 literal flag values on a non-Windows Python
    # build (subprocess doesn't define these attributes there) -- see
    # the module-level comment in daemon_transport.py.
    expected_flags = dt._CREATE_NEW_PROCESS_GROUP | dt._DETACHED_PROCESS
    assert captured["kwargs"] == {"creationflags": expected_flags}


# ---------------------------------------------------------------------
# force_stop / is_daemon_reachable
# ---------------------------------------------------------------------


def test_force_stop_sends_sigterm(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(
        dt.os, "kill", lambda pid, sig: calls.append((pid, sig))
    )

    dt.force_stop(1234)

    assert calls == [(1234, dt.signal.SIGTERM)]


def test_force_stop_missing_pid_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(pid, sig):  # noqa: ANN001, ANN202
        raise ProcessLookupError(pid)

    monkeypatch.setattr(dt.os, "kill", _raise)

    with pytest.raises(ProcessLookupError):
        dt.force_stop(999999)


def test_is_daemon_reachable_false_when_no_artifact(tmp_path: Path) -> None:
    transport = dt.default_transport_for(tmp_path)
    assert dt.is_daemon_reachable(transport, timeout=1.0) is False


def test_is_daemon_reachable_true_when_listening(short_root: Path) -> None:
    transport = dt.default_transport_for(short_root)
    listener = transport.bind_and_listen()
    listener.settimeout(5.0)

    def _accept_once() -> None:
        try:
            conn, _addr = listener.accept()
        except OSError:
            return
        conn.close()

    accept_thread = threading.Thread(target=_accept_once, daemon=True)
    accept_thread.start()

    try:
        assert dt.is_daemon_reachable(transport, timeout=3.0) is True
    finally:
        accept_thread.join(timeout=5.0)
        listener.close()
        transport.cleanup()
