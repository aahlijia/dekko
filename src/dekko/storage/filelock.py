"""Cross-platform advisory file lock for the ``.dekko/`` regen critical
section.

POSIX uses ``fcntl.flock(LOCK_EX | LOCK_NB)``, so acquisition never
blocks; Windows uses ``msvcrt.locking``. Mirrors
``daemon_transport.py``'s convention of being the *one* place that
branches on ``sys.platform`` for its own narrow concern, rather than
scattering platform checks through ``cli.py``.

Multiple independent processes (bare CLI, a
daemon-triggered auto-regen, the MCP server) can each trigger a full
``.dekko/`` regen against the same root with zero coordination. This
module doesn't prevent that outright -- it's advisory and best-effort,
never a hard mutex -- it only lets a caller *detect* that another
process already holds the lock, so it can wait briefly and re-check
freshness instead of redundantly repeating the same expensive work.
"""

import contextlib
import os
import sys
from collections.abc import Iterator
from pathlib import Path

LOCK_NAME = "regen.lock"


def _open_lock_file(root: Path, name: str = LOCK_NAME) -> int:
    """Open (creating if needed) the lock file under ``root/.dekko/``.

    Args:
        root: Repository root containing (or about to contain) the
            ``.dekko/`` directory.
        name: Lock file name (and, for a name containing ``/``, its
            path) relative to ``root/.dekko/``. Defaults to the
            regen-lock's own name for backward compatibility.

    Returns:
        A raw file descriptor for the lock file, opened for writing.
    """
    lock_path = root / ".dekko" / name
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    return os.open(str(lock_path), os.O_CREAT | os.O_RDWR)


def _try_acquire_posix(fd: int) -> bool:
    """POSIX: non-blocking exclusive ``flock``.

    Args:
        fd: Open file descriptor for the lock file.

    Returns:
        True if the lock was acquired, False if another process
        already holds it.
    """
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _release_posix(fd: int) -> None:
    import fcntl

    with contextlib.suppress(OSError):
        fcntl.flock(fd, fcntl.LOCK_UN)


def _try_acquire_windows(fd: int) -> bool:
    """Windows: non-blocking exclusive ``msvcrt.locking``.

    ``msvcrt.locking`` requires a nonzero byte region to lock; one
    byte at offset 0 is enough since this lock only ever gates
    presence/absence, never protects file content.

    Args:
        fd: Open file descriptor for the lock file.

    Returns:
        True if the lock was acquired, False if another process
        already holds it.
    """
    import msvcrt

    try:
        os.write(fd, b"\0")
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    except OSError:
        return False
    return True


def _release_windows(fd: int) -> None:
    import msvcrt

    with contextlib.suppress(OSError):
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


@contextlib.contextmanager
def try_named_lock(root: Path, name: str) -> Iterator[bool]:
    """Best-effort advisory lock around an arbitrary named critical
    section under ``root/.dekko/``.

    Generalizes :func:`try_regen_lock`'s mechanism (originally
    hardcoded to the single ``.dekko/`` regen lock) to any named lock
    file, so other critical sections (e.g. per-SHA rev-cache builds)
    can reuse the identical acquire/wait/fail-open shape
    without duplicating the platform-branching logic.

    Yields ``True`` if the lock was acquired (caller should proceed
    with its own work), ``False`` if another process already holds it
    (caller should wait briefly and re-check for the other process's
    result rather than redundantly repeating the same work). Never
    raises: any locking-primitive failure (a filesystem that doesn't
    support locks, a permissions error, an unsupported platform)
    yields ``True``, matching this project's fail-open philosophy
    elsewhere (daemon lifecycle, atomic writes) -- a lock that can't be
    acquired reliably must never *block* the work from happening, only
    deduplicate it when coordination is cheaply possible.

    The lock is released automatically when the ``with`` block exits,
    including on an unhandled exception -- a crashed holder never
    permanently wedges future work, since a non-blocking acquisition
    attempt from a subsequent process simply succeeds once the OS
    reclaims the crashed process's file descriptors.

    Args:
        root: Repository root under whose ``.dekko/`` the lock file
            lives.
        name: Lock file name (may include ``/`` to nest under a
            subdirectory of ``.dekko/``, e.g. ``"rev-cache/<sha>
            .lock"``).

    Yields:
        ``True`` if this call acquired the lock (or locking isn't
        available/failed, in which case proceeding is always safe
        by design); ``False`` if another process currently holds it.
    """
    try:
        fd = _open_lock_file(root, name)
    except OSError:
        yield True
        return

    acquired = True
    try:
        try:
            if sys.platform == "win32":
                acquired = _try_acquire_windows(fd)
            else:
                acquired = _try_acquire_posix(fd)
        except (OSError, ImportError):
            # Any other locking-primitive failure (missing module,
            # unsupported filesystem, ...) -- fail open.
            acquired = True

        yield acquired
    finally:
        if acquired:
            if sys.platform == "win32":
                _release_windows(fd)
            else:
                _release_posix(fd)
        with contextlib.suppress(OSError):
            os.close(fd)


def try_regen_lock(root: Path) -> "contextlib.AbstractContextManager[bool]":
    """Best-effort advisory lock around a ``.dekko/`` regen.

    Thin wrapper over :func:`try_named_lock` for the regen critical
    section specifically -- kept as its own name since every existing
    caller (``repo_ops._locked_regen``) already depends on this exact
    signature.

    Args:
        root: Repository root whose ``.dekko/`` regen is being
            coordinated.

    Returns:
        A context manager yielding ``True``/``False`` exactly as
        documented on :func:`try_named_lock`.
    """
    return try_named_lock(root, LOCK_NAME)
