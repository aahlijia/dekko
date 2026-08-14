"""Cross-platform advisory file lock for the ``.dekko/`` regen critical
section.

POSIX uses ``fcntl.flock(LOCK_EX | LOCK_NB)``, so acquisition never
blocks; Windows uses ``msvcrt.locking``. Mirrors
``daemon_transport.py``'s convention of being the *one* place that
branches on ``sys.platform`` for its own narrow concern, rather than
scattering platform checks through ``cli.py``.

Round-12 §4.1b: multiple independent processes (bare CLI, a
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


def _open_lock_file(root: Path) -> int:
    """Open (creating if needed) the lock file under ``root/.dekko/``.

    Args:
        root: Repository root containing (or about to contain) the
            ``.dekko/`` directory.

    Returns:
        A raw file descriptor for the lock file, opened for writing.
    """
    lock_dir = root / ".dekko"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / LOCK_NAME
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
def try_regen_lock(root: Path) -> Iterator[bool]:
    """Best-effort advisory lock around a ``.dekko/`` regen.

    Yields ``True`` if the lock was acquired (caller should proceed
    with its own regen), ``False`` if another process already holds it
    (caller should wait briefly and re-check freshness rather than
    redundantly regenerating -- the other process's regen will make
    this one unnecessary). Never raises: any locking-primitive failure
    (a filesystem that doesn't support locks, a permissions error, an
    unsupported platform) yields ``True``, matching this project's
    fail-open philosophy elsewhere (daemon lifecycle, atomic writes) --
    a lock that can't be acquired reliably must never *block* a regen
    from happening, only deduplicate it when coordination is cheaply
    possible.

    The lock is released automatically when the ``with`` block exits,
    including on an unhandled exception -- a crashed holder never
    permanently wedges future regens, since a non-blocking acquisition
    attempt from a subsequent process simply succeeds once the OS
    reclaims the crashed process's file descriptors.

    Args:
        root: Repository root whose ``.dekko/`` regen is being
            coordinated.

    Yields:
        ``True`` if this call acquired the lock (or locking isn't
        available/failed, in which case proceeding is always safe
        by design); ``False`` if another process currently holds it.
    """
    try:
        fd = _open_lock_file(root)
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
