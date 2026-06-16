"""Regression guard for the context-layer benchmark (design §7, step 3).

Keeps G★ falsifiable on every test run: against a synthetic repo, the
dekko strategy must cost strictly less than the whole-file baseline for
the comparative tasks, the report must render, and the live session-cost
bridge to the ledger must work.
"""

import sys
from pathlib import Path

from dekko.mapfile import load_map

from conftest import RepoFactory

# benchmarks/ is intentionally outside the package; reach it via path.
_BENCH = Path(__file__).parent.parent / "benchmarks"
if str(_BENCH) not in sys.path:
    sys.path.insert(0, str(_BENCH))

import measure  # noqa: E402

_FIXTURE = (
    Path(__file__).parent / "fixtures" / "transcripts" / "session_basic.jsonl"
)

# A file with a called symbol plus many unrelated bodies. The whole-file
# baseline grows with every body; target's neighborhood pack does not, so
# both outline (drops bodies) and context (neighbourhood vs whole file)
# have clear headroom to win — the realistic shape, not a toy.
def _build_module() -> str:
    head = (
        '"""Module with internal calls and plenty of unrelated code."""\n'
        "def target() -> None:\n"
        '    """Do the main thing across a few lines of body."""\n'
        "    _helper()\n"
        "    _helper()\n"
        "\n"
        "def _helper() -> None:\n"
        '    """A helper with a body of its own."""\n'
        "    x = 1\n"
        "    y = 2\n"
        "    del x, y\n"
        "\n"
        "def caller_one() -> None:\n"
        "    target()\n"
        "\n"
        "def caller_two() -> None:\n"
        "    target()\n"
    )
    # Unrelated filler: inflates the whole-file read, not target's pack.
    filler = "".join(
        f"\n\ndef filler_{i}(a: int, b: int) -> int:\n"
        f'    """Unrelated helper number {i} that does arithmetic."""\n'
        "    total = a + b\n"
        "    scaled = total * 2\n"
        "    return scaled - 1\n"
        for i in range(12)
    )
    return head + filler


_FILES = {"src/mod.py": _build_module()}

_TASKS = (
    measure.Task("outline", "src/mod.py", "outline mod.py"),
    measure.Task("context", "target", "context target"),
    measure.Task("lean", "", "lean"),
)


def test_dekko_beats_whole_file_baseline(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(_FILES)
    results = measure.run_all(root, _TASKS)
    by_kind = {r.task.kind: r for r in results}
    # The whole point: the structural tool is cheaper than reading source.
    assert by_kind["outline"].dekko < by_kind["outline"].baseline
    assert by_kind["context"].dekko < by_kind["context"].baseline
    assert by_kind["outline"].reduction > 0
    assert by_kind["context"].reduction > 0


def test_lean_is_coverage_only(make_mapped_repo: RepoFactory) -> None:
    root = make_mapped_repo(_FILES)
    lean = next(
        r for r in measure.run_all(root, _TASKS) if r.task.kind == "lean"
    )
    assert lean.baseline == 0          # no naive baseline
    assert lean.dekko > 0
    assert "symbols" in lean.covers


def test_report_renders_with_aggregate(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(_FILES)
    lines = measure.render_report(measure.run_all(root, _TASKS))
    assert lines[0].startswith("dekko context-layer benchmark")
    assert any(line.startswith("overall:") for line in lines)


def test_run_all_without_map_raises(tmp_path: Path) -> None:
    try:
        measure.run_all(tmp_path, _TASKS)
    except RuntimeError as exc:
        assert "no dekko map" in str(exc)
    else:
        raise AssertionError("expected RuntimeError for an unmapped repo")


def test_session_cost_bridges_to_ledger(
    make_mapped_repo: RepoFactory,
) -> None:
    # The future on/off half: read a session's real token tally back out.
    make_mapped_repo(_FILES)
    doc = measure.session_cost(_FIXTURE, Path("/repo"))
    assert doc["consumed_tokens"] == 2500
    assert doc["turns"] == 6


def test_result_as_dict_round_trips() -> None:
    task = measure.Task("outline", "f.py", "x")
    row = measure.Result(task, baseline=1000, dekko=100).as_dict()
    assert row["saved"] == 900
    assert row["reduction"] == 0.9


def test_map_load_smoke(make_mapped_repo: RepoFactory) -> None:
    # Sanity: the harness's map is the same one dekko loads.
    root = make_mapped_repo(_FILES)
    assert load_map(root) is not None
