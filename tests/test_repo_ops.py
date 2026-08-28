"""Tests for repo_ops.py's shared discover/extract/resolve/render
pipeline -- currently just round 17's process-pool retry behavior on
``_extract_misses``. No existing test exercised this function or a
broken process pool directly before this pass; see
``.features/plans/round17/round17-mcp-process-pool-concurrent-load-plan.md``
for the investigation and design behind it.
"""

import json
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as PoolTimeoutError
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

import pytest

from dekko import repo_ops
from dekko.core import languages
from dekko.core import resolver
from dekko.render import mapfile

from conftest import RepoFactory

FIXTURES = Path(__file__).parent / "fixtures"


def _flaky_pool_factory(fail_times: int) -> type:
    """Build a ``ProcessPoolExecutor``-shaped fake that raises
    ``BrokenProcessPool`` on construction for its first ``fail_times``
    constructions (counted across every instance built from this one
    factory call), then delegates to ``ThreadPoolExecutor`` -- same
    ``submit``/``shutdown`` interface, but in-process, so
    ``extract_one`` needs no pickling across a real subprocess just to
    prove the retry wiring. See ``tests/core/test_resolver.py``'s
    identical helper (kept as a separate copy rather than a shared
    test util, since it's small and each module's real pool site has
    its own call shape).

    Round 22: ``_extract_misses`` now owns its pool via
    ``pool = ProcessPoolExecutor(...)`` / ``try``/``finally:
    pool.shutdown(wait=False)`` instead of ``with ProcessPoolExecutor(
    ...) as pool:`` (see ``resolver._run_pool_bounded``'s docstring
    for why) -- so the failure trigger point moves from ``__enter__``
    to ``__init__``, and ``submit``/``shutdown`` delegate straight to
    the real ``ThreadPoolExecutor`` instead of relying on
    context-manager protocol.
    """
    state = {"calls": 0}

    class _FlakyPool:
        def __init__(self, max_workers: int | None = None) -> None:
            state["calls"] += 1
            if state["calls"] <= fail_times:
                raise BrokenProcessPool("simulated: process pool broken")
            self._real = ThreadPoolExecutor(max_workers=max_workers)

        def submit(self, fn: object, *args: object) -> object:
            return self._real.submit(fn, *args)

        def shutdown(
            self, wait: bool = True, *, cancel_futures: bool = False
        ) -> None:
            self._real.shutdown(wait=wait, cancel_futures=cancel_futures)

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


# Round 21 Track A: cline reproduced a spawned extraction worker
# hanging indefinitely at 0% CPU (a worker that resolved a completely
# different Python interpreter than its own parent and never came
# up). ``_extract_misses`` now submits each file individually and
# bounds each future's ``.result()`` with
# ``resolver.POOL_RESULT_TIMEOUT_S``, so a worker that never returns a
# result surfaces as ``resolver.PoolStalledError`` instead of hanging
# forever -- see ``tests/core/test_resolver.py``'s equivalent coverage
# for ``resolve()``'s own pool call sites.


def test_extract_misses_raises_pool_stalled_error_on_stalled_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _StalledFuture:
        def result(self, timeout: float | None = None) -> object:
            raise PoolTimeoutError("simulated: worker never returned")

    class _StalledPool:
        def __init__(self, max_workers: int | None = None) -> None:
            # ``_run_pool_bounded`` reads the private
            # ``_processes`` attribute (dict of pid -> Process) to
            # force-kill any still-wedged worker after a timeout --
            # empty here since this fake never launches a real
            # subprocess.
            self._processes: dict = {}

        def __enter__(self) -> "_StalledPool":
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

        def submit(self, fn: object, *args: object) -> _StalledFuture:
            return _StalledFuture()

        def shutdown(
            self, wait: bool = True, *, cancel_futures: bool = False
        ) -> None:
            pass

    monkeypatch.setattr(repo_ops, "_PARALLEL_MIN", 0)
    monkeypatch.setattr(repo_ops, "ProcessPoolExecutor", _StalledPool)
    root = FIXTURES / "python"
    misses = ["main.py", "util.py"]

    with pytest.raises(resolver.PoolStalledError):
        repo_ops._extract_misses(root, misses, workers=4)


# ``_resolve_header_spec``: the C/C++ ``.h`` dispatch decision itself,
# in isolation from a full ``extract_one``/``map_repository`` run --
# see ``tests/core/test_languages.py::
# test_cpp_header_dispatch_by_content_not_extension`` for the
# end-to-end pipeline coverage of the same fix, and
# ``tests/core/test_extractor_cpp_header_dispatch.py`` for the
# underlying ``looks_like_cpp_header`` heuristic's own unit tests.


def test_resolve_header_spec_cpp_content_swaps_to_cpp_spec(
    tmp_path: Path,
) -> None:
    (tmp_path / "widget.h").write_text(
        "namespace demo {\nclass Widget {};\n}\n"
    )
    resolved = repo_ops._resolve_header_spec(tmp_path, "widget.h", languages.C)
    assert resolved is languages.CPP


def test_resolve_header_spec_plain_c_content_keeps_c_spec(
    tmp_path: Path,
) -> None:
    (tmp_path / "plain.h").write_text(
        "struct Point {\n  int x;\n  int y;\n};\n"
    )
    resolved = repo_ops._resolve_header_spec(tmp_path, "plain.h", languages.C)
    assert resolved is languages.C


def test_resolve_header_spec_dot_c_files_never_content_sniffed(
    tmp_path: Path,
) -> None:
    # `.c` also resolves to the C spec (languages.EXTENSION_MAP), but
    # is never ambiguous the way `.h` is -- a `.c` file must not be
    # reclassified even if its content happens to contain what would
    # otherwise read as a C++ construct.
    (tmp_path / "odd.c").write_text("namespace demo {\nclass Widget {};\n}\n")
    resolved = repo_ops._resolve_header_spec(tmp_path, "odd.c", languages.C)
    assert resolved is languages.C


def test_resolve_header_spec_non_c_spec_passes_through_unchanged(
    tmp_path: Path,
) -> None:
    # Only a `.h` file resolved to the C spec is ever a candidate for
    # this dispatch -- any other spec (e.g. a `.py` file's resolved
    # Python spec) must be returned untouched.
    py_spec = languages.SPEC_BY_NAME["python"]
    resolved = repo_ops._resolve_header_spec(tmp_path, "mod.py", py_spec)
    assert resolved is py_spec


def test_resolve_header_spec_unreadable_header_keeps_original_spec(
    tmp_path: Path,
) -> None:
    # No such file: the read raises OSError, which this function must
    # swallow and return the original spec unchanged -- extract_file's
    # own read right after this call will surface the same failure
    # through the normal FileMap.error path.
    resolved = repo_ops._resolve_header_spec(
        tmp_path, "missing.h", languages.C
    )
    assert resolved is languages.C


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


# --- provenance: standing ambiguous-rate stamp (round 23) -----------------


def test_map_run_stamps_ambiguous_rate_into_provenance(
    make_mapped_repo: RepoFactory,
) -> None:
    """End-to-end ``dekko map`` stamps ``provenance.ambiguous_rate`` /
    ``ambiguous_sites`` at write time, and ``load_provenance()`` -- the
    cheap sidecar-only path ``dekko doctor`` uses -- surfaces both
    without needing a full ``load_map()`` (round 23's standing
    high-ambiguous-rate flag design doc).
    """
    files = {
        "a.py": "def target() -> int:\n    return 1\n",
        "b.py": "def target() -> int:\n    return 2\n",
        "c.py": "def caller() -> int:\n    return target()\n",
    }
    root = make_mapped_repo(files)

    doc = json.loads((root / ".dekko" / "map.json").read_text())
    prov = doc["provenance"]
    assert prov["ambiguous_sites"] == 1
    assert prov["ambiguous_rate"] == 1.0

    loaded = mapfile.load_provenance(root)
    assert loaded is not None
    assert loaded["ambiguous_sites"] == 1
    assert loaded["ambiguous_rate"] == 1.0
