"""``sanity --all``'s planned sweep returns ``_run_grep``'s rows.

The sweep walks the repo once, then greps each name over only the files
that can hold it. These tests pin that the result is the same one the
single-target grep gives: same rows, same order, same cap and
pathological-line counts.
"""

import subprocess
from pathlib import Path
from typing import Any

import pytest

from dekko.analysis import sanity
from dekko.render import mapfile
from conftest import RepoFactory


def _write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def _planned(root: Path, name: str) -> sanity.GrepSweepResult:
    plan = sanity._plan_sweep(root, [name])
    assert plan.error is None
    return sanity._run_grep_planned(
        root, name, plan.files_by_name.get(name, [])
    )


def _assert_same(root: Path, name: str) -> sanity.GrepSweepResult:
    want = sanity._run_grep(root, name)
    got = _planned(root, name)
    assert got == want
    return got


def test_name_in_two_files_comes_back_in_walk_order(tmp_path: Path) -> None:
    root = _write(
        tmp_path,
        {
            "b/two.py": "target()\nx = 1\ntarget()\n",
            "a/one.py": "def target():\n    pass\n",
            "c.py": "targeted = 1\n",
        },
    )
    got = _assert_same(root, "target")
    assert {h.path for h in got.hits} == {"b/two.py", "a/one.py"}


def test_name_only_in_an_excluded_directory_has_no_rows(
    tmp_path: Path,
) -> None:
    root = _write(
        tmp_path,
        {
            "node_modules/lib/x.js": "target();\n",
            "src/y.py": "other()\n",
        },
    )
    assert _assert_same(root, "target").hits == []


def test_line_cap_cuts_at_the_same_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sanity, "_MAX_GREP_LINES", 5)
    root = _write(
        tmp_path,
        {f"f{i}.py": "target()\ntarget()\n" for i in range(4)},
    )
    got = _assert_same(root, "target")
    assert got.truncated is True
    assert len(got.hits) == 5


def test_long_line_file_keeps_short_rows_and_counts_the_long_one(
    tmp_path: Path,
) -> None:
    long_line = "x" * 6000 + " target " + "y" * 6000
    root = _write(
        tmp_path,
        {
            "data.json": f"target\n{long_line}\nnot here\ntarget again\n",
            "code.py": "target()\n",
        },
    )
    got = _assert_same(root, "target")
    assert got.skipped_pathological == 1
    assert [(h.path, h.line) for h in got.hits if h.path == "data.json"] == [
        ("data.json", 1),
        ("data.json", 4),
    ]


def test_non_identifier_name_still_sweeps(tmp_path: Path) -> None:
    root = _write(
        tmp_path,
        {
            "a.cpp": "bool operator==(A a, A b);\n",
            "b.cpp": "int x = 1;\n",
        },
    )
    got = _assert_same(root, "operator==")
    assert [h.path for h in got.hits] == ["a.cpp"]


def test_name_with_no_files_runs_no_grep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _write(tmp_path, {"a.py": "other()\n"})
    plan = sanity._plan_sweep(root, ["target"])
    assert "target" not in plan.files_by_name

    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("grep ran for a name with no files")

    monkeypatch.setattr(sanity.subprocess, "run", boom)
    got = sanity._run_grep_planned(root, "target", [])
    assert got.hits == []
    assert got.error is None


def test_walk_failure_exits_grep_failed(
    make_mapped_repo: RepoFactory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    root = make_mapped_repo(
        {"a.py": "def helper():\n    return 1\n\n\ndef c():\n    helper()\n"}
    )
    real_run = subprocess.run

    def failing_walk(cmd: list[str], *args: Any, **kwargs: Any) -> Any:
        if "-rlI" in cmd:
            raise FileNotFoundError("grep")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(sanity.subprocess, "run", failing_walk)
    index = mapfile.load_map(root)
    assert index is not None
    assert sanity.run_all(index, root, jobs=1) == sanity.EXIT_GREP_FAILED
    assert "'grep' not found" in capsys.readouterr().err
