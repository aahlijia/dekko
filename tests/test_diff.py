"""The diff subcommand: added/removed/changed symbols and exit codes."""

import json
import subprocess
from pathlib import Path

import pytest

from dekko import cli, diff

BASE = {
    "a.py": "def f() -> int:\n    return 1\n",
    "b.py": "from a import f\n\n\ndef g() -> int:\n    return f()\n",
}


def _git(root: Path, *args: str) -> None:
    """Run a git command in ``root``, raising on failure."""
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
    )


def _commit_all(root: Path, message: str) -> None:
    """Stage and commit everything currently in the tree."""
    _git(root, "add", "-A")
    _git(
        root,
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "commit",
        "-m",
        message,
    )


def _repo(root: Path, files: dict[str, str]) -> Path:
    """Create a committed git repo and map it."""
    _git(root, "init", "-q")
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    _commit_all(root, "base")
    assert cli.main(["map", str(root), "--quiet"]) == 0
    return root


def test_diff_clean_tree_is_empty(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    root = _repo(tmp_path, BASE)
    assert cli.main(["diff", "--root", str(root)]) == 0
    assert "no symbol changes" in capsys.readouterr().out


def test_diff_detects_added_removed_changed(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    root = _repo(tmp_path, BASE)
    # change f, add h, remove g (by replacing b.py's body)
    (root / "a.py").write_text("def f() -> int:\n    return 2\n")
    (root / "c.py").write_text("def h() -> int:\n    return 3\n")
    (root / "b.py").write_text("X = 1\n")

    assert cli.main(["diff", "--root", str(root)]) == 1
    out = capsys.readouterr().out
    assert "1 changed, 1 added, 1 removed" in out
    assert "~ a.py:1" in out  # f changed
    assert "+ c.py:1" in out  # h added
    assert "- b.py:4" in out  # g removed


def test_diff_reports_impacted_callers(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    root = _repo(tmp_path, BASE)
    (root / "a.py").write_text("def f() -> int:\n    return 42\n")

    assert cli.main(["diff", "--root", str(root)]) == 1
    out = capsys.readouterr().out
    assert "called by: b.py:4 g" in out


def test_diff_json(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    root = _repo(tmp_path, BASE)
    (root / "a.py").write_text("def f() -> int:\n    return 2\n")

    assert cli.main(["diff", "--root", str(root), "--json"]) == 1
    doc = json.loads(capsys.readouterr().out)
    assert [d["id"] for d in doc["changed"]] == ["a.py::f"]
    assert doc["changed"][0]["callers"] == ["b.py:4 g"]
    assert doc["added"] == []
    assert doc["removed"] == []


def test_diff_explicit_rev(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    root = _repo(tmp_path, BASE)
    (root / "a.py").write_text("def f() -> int:\n    return 2\n")
    _commit_all(root, "change f")

    # worktree now matches HEAD, but differs from HEAD~1
    assert cli.main(["diff", "HEAD", "--root", str(root)]) == 0
    capsys.readouterr()
    assert cli.main(["diff", "HEAD~1", "--root", str(root)]) == 1
    assert "~ a.py:1" in capsys.readouterr().out


def test_diff_bad_rev(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    root = _repo(tmp_path, BASE)
    assert cli.main(["diff", "nope-not-a-rev", "--root", str(root)]) == 2
    assert "cannot export git rev" in capsys.readouterr().err


def test_diff_uses_no_tar_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """export_rev extracts via stdlib tarfile, never the tar binary."""
    root = _repo(tmp_path, BASE)
    (root / "a.py").write_text("def f() -> int:\n    return 2\n")

    real_run = subprocess.run
    calls: list[list[str]] = []

    def spy(cmd: list[str], *args: object, **kwargs: object):  # noqa: ANN202
        calls.append(list(cmd))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(diff.subprocess, "run", spy)
    assert cli.main(["diff", "--root", str(root)]) == 1
    # No `tar` subprocess; the snapshot still comes from `git archive`.
    assert not any(c and c[0] == "tar" for c in calls)
    assert any(c[:2] == ["git", "-C"] and "archive" in c for c in calls)
