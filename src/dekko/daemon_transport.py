"""OS-independent transport abstraction for the dekko daemon.

The bare CLI's daemon mode (see ``.features/daemon-mode/``) needs a
socket-like channel between a short-lived ``dekko <cmd>`` client
process and a long-lived per-repo daemon process. Unix domain sockets
are the natural choice on macOS/Linux but don't exist in a usable,
battle-tested form on Windows, so every daemon-facing caller
(a later phase's accept loop, the CLI's daemon-routing check) is
written against the ``DaemonTransport`` interface here rather than
against ``socket.AF_UNIX`` directly. ``default_transport_for()`` is
the *only* place that branches on ``sys.platform`` -- everything above
it only ever sees a ``socket.socket``, since Python's ``socket``
module presents ``AF_UNIX`` and ``AF_INET`` through the identical
``accept()``/``connect()``/``send()``/``recv()`` API.

This module is transport plumbing plus the OS-conditional process
primitives (detached spawn, forced stop, connect-based liveness) that
are equally platform-sensitive. It does *not* contain a daemon accept
loop, request/response JSON framing, or CLI wiring -- those land in a
later phase (see ``.features/daemon-mode/TRACKER.md``); this module
only needs to bind, connect, authenticate, and clean up a transport.
"""

import json
import os
import secrets
import signal
import socket
import subprocess
import sys
from abc import ABC, abstractmethod
from pathlib import Path

# Socket file name inside a repo's ``.dekko/`` directory (POSIX).
_SOCKET_NAME = "daemon.sock"
# Port-file name inside a repo's ``.dekko/`` directory (Windows/TCP).
_PORT_FILE_NAME = "daemon.port"
# Token length in hex chars written to the port file (16 bytes -> 32
# hex chars via secrets.token_hex).
_TOKEN_BYTES = 16
# sun_path's length limit is platform-specific (~104 bytes on
# macOS/BSD including the null terminator, ~108 on Linux) -- both far
# shorter than typical filesystem path limits. Guard against the
# stricter of the two (with a little headroom) rather than branching
# on the exact platform for a few bytes of difference.
_SUN_PATH_LIMIT = 100
# Hard cap on a single auth-preamble line read by ``_recv_line`` --
# a real token line is ~34 bytes (32 hex chars + newline); this is
# just an upper bound against a misbehaving/malicious client sending
# an unterminated stream.
_MAX_PREAMBLE_LINE = 4096

# subprocess.CREATE_NEW_PROCESS_GROUP / DETACHED_PROCESS are Windows-
# only module attributes -- referencing them by bare attribute access
# raises AttributeError on a POSIX build of Python, which would break
# both spawn_detached()'s Windows branch under test (monkeypatching
# sys.platform to exercise that branch's kwargs on non-Windows CI, per
# the workflow doc's cross-platform kwarg-construction tests) and any
# accidental import-time evaluation. Fall back to the documented
# literal Win32 API flag values so this module imports and the
# Windows branch's kwargs can be constructed identically on every
# platform; on real Windows, getattr simply returns the native
# constants.
_CREATE_NEW_PROCESS_GROUP = getattr(
    subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200
)
_DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)


def _recv_line(sock: socket.socket) -> str | None:
    """Read one newline-terminated line from ``sock``, byte at a time.

    Deliberately avoids ``socket.makefile()``: its internal buffering
    reads ahead past the line boundary, and any bytes it buffers but
    doesn't return are lost once that file object is garbage
    collected -- silently swallowing the start of whatever a
    subsequent ``makefile()`` call on the same socket (the actual
    request line, read by the daemon's dispatch loop after
    authentication) would otherwise see. A byte-at-a-time read is
    slower but consumes exactly the auth line and nothing past it.

    Returns:
        The line (including trailing newline, if present) decoded as
        UTF-8, or ``None`` if the connection closed before a newline
        was seen.
    """
    chunks = bytearray()
    while len(chunks) < _MAX_PREAMBLE_LINE:
        byte = sock.recv(1)
        if not byte:
            return None
        chunks += byte
        if byte == b"\n":
            return chunks.decode("utf-8", errors="replace")
    return None


# Name kept as specified: the workflow doc (§2.1) names this
# exception "TransportUnavailable" explicitly ("raise a dedicated
# TransportUnavailable exception rather than letting the underlying
# OSError ... propagate"), so it isn't renamed to satisfy N818's
# Error-suffix convention.
class TransportUnavailable(Exception):  # noqa: N818
    """This transport cannot be used in the current environment.

    Raised instead of letting an OS-level error (e.g. ``OSError:
    AF_UNIX path too long``) propagate. Callers treat this identically
    to "no daemon present" -- never a crash.
    """


class DaemonUnavailableError(Exception):
    """No live daemon could be reached through this transport.

    Covers every fail-open case: no socket/port-file artifact, a
    stale one left behind by an ungracefully-killed daemon, connection
    refused, and connect timeouts. Callers must fall back to direct
    execution on this exception, never surface it as a CLI error.
    """


class DaemonTransport(ABC):
    """OS-specific channel between the CLI client and the daemon.

    Concrete implementations: ``UnixSocketTransport`` (macOS/Linux),
    ``TcpLoopbackTransport`` (Windows default, also usable on POSIX
    for testing/parity). Use ``default_transport_for()`` to pick the
    right one for the current platform.
    """

    @abstractmethod
    def exists(self) -> bool:
        """Cheap presence check, no connection attempt.

        Used by the CLI's fail-open logic before attempting a real
        connect -- "no transport artifact" is the common case for
        installs that never ran ``dekko daemon start``.
        """

    @abstractmethod
    def bind_and_listen(self) -> socket.socket:
        """Daemon-side: create and return a listening socket.

        Raises:
            TransportUnavailable: If this transport cannot be bound
                in the current environment (e.g. a too-long
                ``sun_path``).
        """

    @abstractmethod
    def preflight_check(self) -> None:
        """Parent-process-side: cheaply predict a ``bind_and_listen()``
        failure before spawning the detached daemon.

        Exists so ``daemon.start()`` can fail fast, in the foreground,
        with a real non-zero exit code -- rather than reporting false
        success because the actual bind happens in a detached child
        process whose stdio the parent never reads. Must only check
        conditions knowable without a real bind attempt (e.g. a path
        length limit); it is not a substitute for ``bind_and_listen()``
        itself, and a transport with nothing cheap to pre-check may
        simply no-op here.

        Raises:
            TransportUnavailable: If this transport is already known
                to be unbindable in the current environment.
        """

    @abstractmethod
    def client_connect(self, timeout: float) -> socket.socket:
        """CLI-side: connect to a live daemon.

        Args:
            timeout: Socket connect/operation timeout in seconds.

        Raises:
            DaemonUnavailableError: On any failure to reach a live daemon
                (no transport artifact, stale artifact, connection
                refused, or timeout). Callers fail open to direct
                execution on this exception.
        """

    @abstractmethod
    def authenticate(self, conn: socket.socket) -> bool:
        """Daemon-side: validate a freshly accepted connection.

        Reads and checks whatever auth preamble this transport
        requires. ``UnixSocketTransport`` requires none (filesystem
        permissions already gate access) and always returns ``True``.
        ``TcpLoopbackTransport`` reads one line containing the token
        and compares it against the port file's token, since a
        loopback TCP socket has no filesystem-permission equivalent.

        Returns:
            ``True`` if the connection may proceed, ``False`` if the
            caller should close it without sending a response.
        """

    @abstractmethod
    def send_auth_preamble(self, sock: socket.socket) -> None:
        """CLI-side: write this transport's auth preamble, if any.

        Must be called (and will no-op harmlessly if not needed)
        immediately after ``client_connect()`` succeeds, before
        sending the first real request line.
        """

    @abstractmethod
    def cleanup(self) -> None:
        """Remove the on-disk artifact for this transport, best-effort.

        Called on graceful daemon exit and on stale-transport
        detection. Never raises -- failures are swallowed, since
        cleanup is advisory (a missing file is not an error).
        """

    @abstractmethod
    def describe(self) -> str:
        """Human-readable summary for ``dekko daemon status``."""


class UnixSocketTransport(DaemonTransport):
    """Unix domain socket at ``<root>/.dekko/daemon.sock`` (POSIX only).

    Selected by ``default_transport_for()`` whenever ``sys.platform
    != "win32"``. Created with mode ``0600`` -- filesystem permissions
    are the only access control this transport needs; there is no
    independent authentication, matching how ``map.json``/
    ``notes.json`` are already protected the same way.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.socket_path = root / ".dekko" / _SOCKET_NAME

    def exists(self) -> bool:
        return self.socket_path.exists()

    def preflight_check(self) -> None:
        encoded = os.fsencode(str(self.socket_path))
        if len(encoded) > _SUN_PATH_LIMIT:
            raise TransportUnavailable(
                f"socket path too long for AF_UNIX "
                f"({len(encoded)} bytes > {_SUN_PATH_LIMIT}): "
                f"{self.socket_path}"
            )

    def bind_and_listen(self) -> socket.socket:
        self.preflight_check()

        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        # A stale socket file left behind by an ungracefully-killed
        # daemon (kill -9, crash, reboot without a cleanup hook)
        # blocks bind() with "address already in use". Best-effort
        # remove it first -- whether an existing daemon is actually
        # still live is the caller's responsibility to check before
        # calling bind_and_listen() at all.
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(str(self.socket_path))
        except OSError as exc:
            sock.close()
            raise TransportUnavailable(
                f"could not bind unix socket at {self.socket_path}: {exc}"
            ) from exc
        os.chmod(self.socket_path, 0o600)
        sock.listen()
        return sock

    def client_connect(self, timeout: float) -> socket.socket:
        if not self.socket_path.exists():
            raise DaemonUnavailableError(f"no socket at {self.socket_path}")

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect(str(self.socket_path))
        except OSError as exc:
            sock.close()
            self.cleanup()
            raise DaemonUnavailableError(
                f"could not connect to {self.socket_path}: {exc}"
            ) from exc
        return sock

    def authenticate(self, conn: socket.socket) -> bool:
        return True

    def send_auth_preamble(self, sock: socket.socket) -> None:
        pass

    def cleanup(self) -> None:
        try:
            self.socket_path.unlink()
        except OSError:
            pass

    def describe(self) -> str:
        return f"unix socket: {self.socket_path}"


class TcpLoopbackTransport(DaemonTransport):
    """Token-authenticated TCP loopback socket (Windows default).

    Binds an OS-assigned ephemeral port on ``127.0.0.1`` and writes
    ``<root>/.dekko/daemon.port`` containing ``{"port": N, "token":
    "<32 hex chars>"}``. A loopback TCP socket has no filesystem-
    permission equivalent to the Unix socket's ``0600`` mode -- any
    local process on the machine can connect to ``127.0.0.1:N``
    regardless of file permissions -- so the token is a hard
    requirement, not optional: every accepted connection must present
    it via ``send_auth_preamble``/``authenticate`` before being
    treated as a real client, or it is closed without a response.

    Also constructible (and fully usable) on POSIX for testing/parity
    even though ``default_transport_for()`` only selects it by default
    on Windows.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.port_file = root / ".dekko" / _PORT_FILE_NAME
        self._token: str | None = None

    def exists(self) -> bool:
        return self.port_file.exists()

    def preflight_check(self) -> None:
        # Nothing cheaply knowable ahead of a real bind for a TCP
        # loopback socket -- an OS-assigned ephemeral port has no
        # length-style limit analogous to AF_UNIX's sun_path, and any
        # other bind failure (e.g. no loopback interface) is rare
        # enough that a real bind_and_listen() attempt is the only
        # meaningful check.
        pass

    def _read_port_file(self) -> tuple[int, str]:
        try:
            data = json.loads(self.port_file.read_text())
            return int(data["port"]), str(data["token"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise DaemonUnavailableError(
                f"could not read port file {self.port_file}: {exc}"
            ) from exc

    def bind_and_listen(self) -> socket.socket:
        self.port_file.parent.mkdir(parents=True, exist_ok=True)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        token = secrets.token_hex(_TOKEN_BYTES)
        self._token = token
        self.port_file.write_text(json.dumps({"port": port, "token": token}))
        sock.listen()
        return sock

    def client_connect(self, timeout: float) -> socket.socket:
        if not self.port_file.exists():
            raise DaemonUnavailableError(f"no port file at {self.port_file}")

        port, token = self._read_port_file()
        self._token = token
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect(("127.0.0.1", port))
        except OSError as exc:
            sock.close()
            self.cleanup()
            raise DaemonUnavailableError(
                f"could not connect to 127.0.0.1:{port}: {exc}"
            ) from exc
        return sock

    def authenticate(self, conn: socket.socket) -> bool:
        if self._token is None:
            try:
                _, self._token = self._read_port_file()
            except DaemonUnavailableError:
                return False

        previous_timeout = conn.gettimeout()
        conn.settimeout(2.0)
        try:
            line = _recv_line(conn)
        except OSError:
            return False
        finally:
            conn.settimeout(previous_timeout)

        if line is None:
            return False
        return line.strip() == self._token

    def send_auth_preamble(self, sock: socket.socket) -> None:
        if self._token is None:
            _, self._token = self._read_port_file()
        sock.sendall(f"{self._token}\n".encode("utf-8"))

    def cleanup(self) -> None:
        try:
            self.port_file.unlink()
        except OSError:
            pass

    def describe(self) -> str:
        if self.port_file.exists():
            try:
                port, _ = self._read_port_file()
                return f"tcp loopback: 127.0.0.1:{port}"
            except DaemonUnavailableError:
                pass
        return f"tcp loopback: {self.port_file} (not bound)"


def default_transport_for(root: Path) -> DaemonTransport:
    """Pick the right transport for the current platform.

    The only ``sys.platform`` branch the rest of the daemon subsystem
    should ever need -- everything built on ``DaemonTransport``
    operates on the returned ``socket.socket`` the same way regardless
    of address family.

    Args:
        root: Resolved repo root the daemon serves.

    Returns:
        ``TcpLoopbackTransport`` on Windows, ``UnixSocketTransport``
        everywhere else.
    """
    if sys.platform == "win32":
        return TcpLoopbackTransport(root)
    return UnixSocketTransport(root)


def spawn_detached(cmd: list[str]) -> subprocess.Popen:
    """Launch ``cmd`` as a detached background process.

    POSIX: ``start_new_session=True`` puts the child in its own
    session (via ``setsid()``) so it survives the parent shell/process
    exiting. That kwarg is POSIX-only, so the Windows branch instead
    passes ``CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS`` creation
    flags, which detach the child from the parent's console and
    process group the equivalent way on that platform.

    Args:
        cmd: Full argv for the detached process.

    Returns:
        The ``subprocess.Popen`` handle for the spawned process.
    """
    if sys.platform == "win32":
        creationflags = _CREATE_NEW_PROCESS_GROUP | _DETACHED_PROCESS
        return subprocess.Popen(cmd, creationflags=creationflags)
    return subprocess.Popen(cmd, start_new_session=True)


def force_stop(pid: int) -> None:
    """Send an unconditional stop signal to the process at ``pid``.

    ``signal.SIGTERM`` is used on both platforms: on POSIX it's a
    catchable terminate request; Python's ``os.kill()`` maps any
    signal other than ``CTRL_C_EVENT``/``CTRL_BREAK_EVENT`` to an
    unconditional ``TerminateProcess`` call on Windows, so it is *not*
    catchable there -- no last-chance socket cleanup runs on that
    platform. That asymmetry is acceptable: this module's stale-
    transport cleanup (``bind_and_listen()`` removing a stale socket
    file, ``client_connect()`` removing a stale artifact on connection
    failure) already handles "the daemon died without cleaning up"
    regardless of which platform killed it, so no second recovery
    mechanism is needed for Windows specifically.

    Args:
        pid: Process ID to stop.

    Raises:
        ProcessLookupError: If no process with this PID exists.
    """
    os.kill(pid, signal.SIGTERM)


def is_daemon_reachable(
    transport: DaemonTransport, timeout: float = 2.0
) -> bool:
    """Best-effort liveness check: does a connect actually succeed?

    Deliberately *not* PID-based: ``os.kill(pid, 0)`` is a POSIX-only
    idiom that doesn't probe liveness the same way, or at all, on
    Windows. A real connect is also a strictly stronger signal than
    PID liveness anyway -- a PID can be alive while the daemon itself
    is wedged. A later phase's ``dekko daemon status`` upgrades this
    into a full protocol round-trip that asks the daemon to report its
    own state; this transport-layer version only proves a listener is
    present and accepting connections.

    Args:
        transport: The transport to probe.
        timeout: Connect timeout in seconds.

    Returns:
        ``True`` if a connection could be established, ``False``
        otherwise (including "no transport artifact present").
    """
    if not transport.exists():
        return False
    try:
        sock = transport.client_connect(timeout)
    except DaemonUnavailableError:
        return False
    sock.close()
    return True
