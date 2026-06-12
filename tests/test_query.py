"""Query subcommand: actions, target syntax, exit codes."""

import json

import pytest

from lidar_map import cli

from conftest import RepoFactory

TWO_FILES = {
    "a.py": (
        "def helper(x: int) -> int:\n"
        "    return x + 1\n"
        "\n"
        "\n"
        "def entry() -> None:\n"
        "    helper(1)\n"
    ),
    "b.py": "def helper(x: int) -> int:\n    return x - 1\n",
}


def test_callers(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(TWO_FILES)
    code = cli.main(["query", "callers", "a.py:helper", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "entry() -> None" in out


def test_callees(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(TWO_FILES)
    assert cli.main(["query", "callees", "entry", "--root", str(root)]) == 0
    assert "helper(x: int) -> int" in capsys.readouterr().out


def test_ambiguous_bare_name(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(TWO_FILES)
    code = cli.main(["query", "symbol", "helper", "--root", str(root)])
    assert code == 4
    err = capsys.readouterr().err
    assert "a.py:helper" in err
    assert "b.py:helper" in err


def test_not_found(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(TWO_FILES)
    code = cli.main(["query", "symbol", "nope", "--root", str(root)])
    assert code == 3
    assert "no symbol" in capsys.readouterr().err


def test_symbol_card_json(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(TWO_FILES)
    code = cli.main(
        ["query", "symbol", "entry", "--root", str(root), "--json"]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["path"] == "a.py"
    assert doc["fan_out"] == 1
    assert doc["signature"] == "entry() -> None"


def test_file_action_and_limit(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(TWO_FILES)
    code = cli.main(
        ["query", "file", "a.py", "--root", str(root), "--limit", "1"]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "helper" in out
    assert "and 1 more" in out


def test_file_not_found(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(TWO_FILES)
    code = cli.main(["query", "file", "zzz.py", "--root", str(root)])
    assert code == 3
