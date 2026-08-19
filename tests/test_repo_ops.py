"""Tests for repo_ops.py's shared discover/extract/resolve/render
pipeline -- currently just round 17's process-pool retry behavior on
``_extract_misses``. No existing test exercised this function or a
broken process pool directly before this pass; see
``.features/plans/round17/round17-mcp-process-pool-concurrent-load-plan.md``
for the investigation and design behind it.
"""

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

import pytest

from dekko import repo_ops

FIXTURES = Path(__file__).parent / "fixtures"


def _flaky_pool_factory(fail_times: int) -> type:
    """Build a ``ProcessPoolExecutor``-shaped fake that raises
    ``BrokenProcessPool`` on ``__enter__`` for its first ``fail_times``
    constructions (counted across every instance built from this one
    factory call), then delegates to ``ThreadPoolExecutor`` -- same
    ``pool.map``/context-manager interface, but in-process, so
    ``extract_one`` needs no pickling across a real subprocess just to
    prove the retry wiring. See ``tests/core/test_resolver.py``'s
    identical helper (kept as a separate copy rather than a shared
    test util, since it's small and each module's real pool site has
    its own call shape: ``pool.map`` here vs. ``pool.submit`` there).
    """
    state = {"calls": 0}

    class _FlakyPool:
        def __init__(self, max_workers: int | None = None) -> None:
            self._max_workers = max_workers
            self._real: ThreadPoolExecutor | None = None

        def __enter__(self) -> "_FlakyPool | ThreadPoolExecutor":
            state["calls"] += 1
            if state["calls"] <= fail_times:
                raise BrokenProcessPool("simulated: process pool broken")
            self._real = ThreadPoolExecutor(max_workers=self._max_workers)
            return self._real.__enter__()

        def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
            if self._real is not None:
                return bool(self._real.__exit__(exc_type, exc, tb))
            return False

    return _FlakyPool


def test_extract_misses_retries_once_on_broken_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(repo_ops, "_PARALLEL_MIN", 0)
    monkeypatch.setattr(
        repo_ops, "ProcessPoolExecutor", _flaky_pool_factory(1)
    )
    root = FIXTURES / "python"
    misses = ["main.py", "util.py"]

    result = repo_ops._extract_misses(root, misses, workers=4)

    assert set(result) == set(misses)
    assert all(fm is not None for fm in result.values())


def test_extract_misses_prints_disclosure_note_on_retry(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(repo_ops, "_PARALLEL_MIN", 0)
    monkeypatch.setattr(
        repo_ops, "ProcessPoolExecutor", _flaky_pool_factory(1)
    )
    root = FIXTURES / "python"
    misses = ["main.py", "util.py"]

    repo_ops._extract_misses(root, misses, workers=4)

    err = capsys.readouterr().err
    assert "note:" in err
    assert "file extraction" in err


def test_extract_misses_propagates_when_retry_also_broken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(repo_ops, "_PARALLEL_MIN", 0)
    monkeypatch.setattr(
        repo_ops, "ProcessPoolExecutor", _flaky_pool_factory(99)
    )
    root = FIXTURES / "python"
    misses = ["main.py", "util.py"]

    with pytest.raises(BrokenProcessPool):
        repo_ops._extract_misses(root, misses, workers=4)


def test_extract_misses_below_parallel_min_never_touches_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sequential path (the default for a small miss list) must not
    construct a pool at all -- unchanged baseline behavior."""

    def _boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("pool should not be constructed")

    monkeypatch.setattr(repo_ops, "ProcessPoolExecutor", _boom)
    root = FIXTURES / "python"
    misses = ["main.py", "util.py"]

    result = repo_ops._extract_misses(root, misses, workers=4)

    assert set(result) == set(misses)
