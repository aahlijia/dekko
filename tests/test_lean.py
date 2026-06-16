"""FR1 file backbone: floor guarantee, dense encoding, determinism."""

from pathlib import Path

from dekko import render_lean
from dekko.mapfile import load_map

from conftest import RepoFactory

# A repo with production files in nested dirs plus a tests/ tree, so the
# floor-vs-demotable partition and directory grouping both have teeth.
FILES = {
    "src/pkg/core.py": (
        '"""Core engine: orchestrates the pipeline end to end."""\n'
        "def run() -> None:\n"
        "    pass\n"
    ),
    "src/pkg/util.py": (
        '"""Small helpers shared across the package."""\n'
        "def helper() -> int:\n"
        "    return 1\n"
    ),
    "src/pkg/nodoc.py": "def bare() -> None:\n    pass\n",
    "tests/test_core.py": "def test_run() -> None:\n    pass\n",
    "tests/test_util.py": "def test_helper() -> None:\n    pass\n",
}


def _backbone(make_mapped_repo: RepoFactory) -> tuple[Path, list]:
    root = make_mapped_repo(FILES)
    index = load_map(root)
    assert index is not None
    return root, render_lean.compute_backbone(index)


def test_every_production_file_present_with_purpose(
    make_mapped_repo: RepoFactory,
) -> None:
    _, groups = _backbone(make_mapped_repo)
    by_path = {
        row.path: row for g in groups for row in g.rows
    }
    assert "src/pkg/core.py" in by_path
    assert "src/pkg/util.py" in by_path
    assert by_path["src/pkg/core.py"].purpose.startswith("Core engine")
    # Production files are the floor: never demotable.
    assert by_path["src/pkg/core.py"].demotable is False
    assert by_path["src/pkg/util.py"].demotable is False


def test_test_files_are_demotable(make_mapped_repo: RepoFactory) -> None:
    _, groups = _backbone(make_mapped_repo)
    tests = next(g for g in groups if g.directory == "tests")
    assert tests.demotable is True
    assert all(r.demotable for r in tests.rows)


def test_docless_file_has_empty_purpose(
    make_mapped_repo: RepoFactory,
) -> None:
    _, groups = _backbone(make_mapped_repo)
    by_path = {row.path: row for g in groups for row in g.rows}
    assert by_path["src/pkg/nodoc.py"].purpose == ""


def test_groups_and_rows_sorted(make_mapped_repo: RepoFactory) -> None:
    _, groups = _backbone(make_mapped_repo)
    dirs = [g.directory for g in groups]
    assert dirs == sorted(dirs)
    for g in groups:
        paths = [r.path for r in g.rows]
        assert paths == sorted(paths)


def test_determinism_byte_identical(
    make_mapped_repo: RepoFactory,
) -> None:
    _, groups = _backbone(make_mapped_repo)
    first = render_lean.render_backbone(groups)
    second = render_lean.render_backbone(groups)
    assert first == second


def test_dense_encoding_amortizes_dir_prefix(
    make_mapped_repo: RepoFactory,
) -> None:
    _, groups = _backbone(make_mapped_repo)
    lines = render_lean.render_backbone(groups)
    # Directory header carries the path once...
    assert "src/pkg/" in lines
    # ...and file rows are basename-only, indented, no repeated prefix.
    core = next(ln for ln in lines if "core.py" in ln)
    assert core.startswith("  core.py")
    assert "src/pkg/core.py" not in core


def test_docless_row_has_no_separator(
    make_mapped_repo: RepoFactory,
) -> None:
    _, groups = _backbone(make_mapped_repo)
    lines = render_lean.render_backbone(groups)
    row = next(ln for ln in lines if "nodoc.py" in ln)
    assert row == "  nodoc.py"


def test_width_zero_drops_purpose_keeps_path(
    make_mapped_repo: RepoFactory,
) -> None:
    _, groups = _backbone(make_mapped_repo)
    lines = render_lean.render_backbone(groups, width=0)
    row = next(ln for ln in lines if "core.py" in ln)
    # Floor's narrowest rung: path survives, purpose is gone.
    assert row == "  core.py"
    assert "Core engine" not in "\n".join(lines)


def test_narrowing_width_truncates_purpose(
    make_mapped_repo: RepoFactory,
) -> None:
    _, groups = _backbone(make_mapped_repo)
    wide = render_lean.render_backbone(groups, width=72)
    narrow = render_lean.render_backbone(groups, width=12)
    wide_core = next(ln for ln in wide if "core.py" in ln)
    narrow_core = next(ln for ln in narrow if "core.py" in ln)
    assert len(narrow_core) < len(wide_core)
    assert narrow_core.startswith("  core.py")


def test_collapse_demotable_folds_test_dir(
    make_mapped_repo: RepoFactory,
) -> None:
    _, groups = _backbone(make_mapped_repo)
    lines = render_lean.render_backbone(groups, collapse_demotable=True)
    assert "tests/  (2 files)" in lines
    # Production dir stays expanded even when collapsing is on.
    assert "src/pkg/" in lines
    assert any("core.py" in ln for ln in lines)
    # No individual test file leaked through the collapse.
    assert not any("test_core.py" in ln for ln in lines)


def test_purpose_truncated_to_width_cap(
    make_mapped_repo: RepoFactory,
) -> None:
    long_doc = "x" * 200
    root = make_mapped_repo(
        {"src/long.py": f'"""{long_doc}"""\ndef f() -> None:\n    pass\n'}
    )
    index = load_map(root)
    assert index is not None
    groups = render_lean.compute_backbone(index)
    row = next(r for g in groups for r in g.rows if r.path == "src/long.py")
    assert len(row.purpose) <= render_lean.LEAN_PURPOSE_WIDTH
