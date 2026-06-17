"""Outline subcommand: structure, nesting, size framing, budget."""

import json

import pytest

from dekko import cli

from conftest import RepoFactory

PY = {
    "a.py": (
        '"""Module A does things."""\n'
        "def helper(x: int) -> int:\n"
        '    """Add one."""\n'
        "    return x + 1\n"
        "\n"
        "\n"
        "class Thing:\n"
        '    """A thing."""\n'
        "    def go(self) -> None:\n"
        "        helper(1)\n"
    ),
    "b.py": "def lone() -> None:\n    pass\n",
}


def test_outline_renders_structure(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY)
    code = cli.main(["outline", "a.py", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "outline: a.py  [python]" in out
    assert "Module A does things" in out
    assert "helper(x: int) -> int" in out
    assert "Add one" in out
    assert "class Thing" in out


def test_members_indented_and_bare_named(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY)
    cli.main(["outline", "a.py", "--root", str(root)])
    out = capsys.readouterr().out
    assert "go(self) -> None" in out
    # Nesting is shown by indent, so the container prefix is dropped.
    assert "Thing.go" not in out
    go_row = next(ln for ln in out.splitlines() if "go(self)" in ln)
    assert go_row.startswith("    ")


def test_size_framing_present(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY)
    cli.main(["outline", "a.py", "--root", str(root)])
    out = capsys.readouterr().out
    assert "full ≈" in out
    assert "outline ≈" in out


def test_docless_file_has_no_emdash(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY)
    cli.main(["outline", "b.py", "--root", str(root)])
    out = capsys.readouterr().out
    assert "lone() -> None" in out
    assert "—" not in out


def test_budget_trims_and_footers(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY)
    code = cli.main(["outline", "a.py", "--root", str(root), "--budget", "1"])
    assert code == 0
    out = capsys.readouterr().out
    assert "omitted" in out
    assert "raise --budget" in out


def test_directory_rollup(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY)
    code = cli.main(["outline", ".", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "2 files" in out
    assert "helper(x: int) -> int" in out
    assert "lone() -> None" in out


def test_outline_json(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY)
    code = cli.main(["outline", "a.py", "--root", str(root), "--json"])
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["files"][0]["path"] == "a.py"
    assert doc["files"][0]["full_tokens"] > 0
    sigs = [s["signature"] for s in doc["files"][0]["symbols"]]
    assert "helper(x: int) -> int" in sigs
    assert doc["meta"]["total"] == 3


def test_outline_not_found(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY)
    code = cli.main(["outline", "zzz.py", "--root", str(root)])
    assert code == 3
    assert "no mapped file or directory" in capsys.readouterr().err
