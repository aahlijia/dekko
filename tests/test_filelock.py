"""Cross-platform advisory regen lock (round-12 §4.1b)."""

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from dekko import filelock


def test_first_acquire_succeeds(tmp_path: Path) -> None:
    with filelock.try_regen_lock(tmp_path) as acquired:
        assert acquired is True


def test_lock_file_created_under_dekko_dir(tmp_path: Path) -> None:
    with filelock.try_regen_lock(tmp_path):
        pass
    assert (tmp_path / ".dekko" / filelock.LOCK_NAME).exists()


def test_second_acquire_while_first_held_reports_false(
    tmp_path: Path,
) -> None:
    # Each call opens its own file descriptor (a distinct open file
    # description), so this genuinely exercises OS-level lock
    # contention -- not just a Python-level flag -- the same
    # contention two separate processes would see.
    with filelock.try_regen_lock(tmp_path) as first:
        assert first is True
        with filelock.try_regen_lock(tmp_path) as second:
            assert second is False


def test_lock_released_after_block_exits(tmp_path: Path) -> None:
    with filelock.try_regen_lock(tmp_path) as first:
        assert first is True
    with filelock.try_regen_lock(tmp_path) as second:
        assert second is True


def test_lock_released_even_on_exception(tmp_path: Path) -> None:
    class _ProbeError(Exception):
        pass

    try:
        with filelock.try_regen_lock(tmp_path) as acquired:
            assert acquired is True
            raise _ProbeError
    except _ProbeError:
        pass

    with filelock.try_regen_lock(tmp_path) as second:
        assert second is True


def test_fails_open_when_lock_file_cannot_be_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A filesystem that can't support the lock file at all (permissions,
    # read-only mount, ...) must never block a regen -- fail open.
    def raise_oserror(root: Path) -> int:
        raise OSError("simulated: cannot open lock file")

    monkeypatch.setattr(filelock, "_open_lock_file", raise_oserror)
    with filelock.try_regen_lock(tmp_path) as acquired:
        assert acquired is True


def test_lock_blocks_across_real_processes(tmp_path: Path) -> None:
    # A genuine second-process test, not just an in-process simulation
    # -- the whole point of this lock is cross-process coordination.
    # dekko is installed into the test venv (conftest.py's own note),
    # so a subprocess using the same interpreter can import it
    # directly with no path hacks.
    script = textwrap.dedent(
        f"""
        import sys
        import time
        from pathlib import Path
        from dekko import filelock

        with filelock.try_regen_lock(Path({str(tmp_path)!r})) as ok:
            assert ok is True
            print("locked", flush=True)
            time.sleep(1.5)
        """
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        line = proc.stdout.readline()
        assert line.strip() == "locked"

        with filelock.try_regen_lock(tmp_path) as acquired_while_held:
            assert acquired_while_held is False

        assert proc.wait(timeout=5.0) == 0

        with filelock.try_regen_lock(tmp_path) as acquired_after:
            assert acquired_after is True
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5.0)
