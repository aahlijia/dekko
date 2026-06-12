"""Trace subcommand: shortest call paths, exit codes, JSON shape."""

import json

import pytest

from lidar_map import cli

from conftest import RepoFactory

CHAIN = {
    "m.py": (
        "def c():\n    pass\n\n\ndef b():\n    c()\n\n\ndef a():\n    b()\n"
    )
}

DIAMOND = {
    "m.py": (
        "def c():\n"
        "    pass\n"
        "\n"
        "\n"
        "def b1():\n"
        "    c()\n"
        "\n"
        "\n"
        "def b2():\n"
        "    c()\n"
        "\n"
        "\n"
        "def a():\n"
        "    b1()\n"
        "    b2()\n"
    )
}

TWO_HELPERS = {
    "a.py": "def helper():\n    pass\n",
    "b.py": "def helper():\n    pass\n",
}


def test_linear_path(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(CHAIN)
    code = cli.main(["trace", "a", "c", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out.strip()
    assert out == "m.py:9 a -> m.py:5 b -> m.py:1 c"


def test_multiple_shortest_paths(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(DIAMOND)
    code = cli.main(["trace", "a", "c", "--root", str(root)])
    assert code == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 2
    assert any("b1" in line for line in lines)
    assert any("b2" in line for line in lines)


def test_max_paths_caps_results(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(DIAMOND)
    code = cli.main(
        ["trace", "a", "c", "--max-paths", "1", "--root", str(root)]
    )
    assert code == 0
    assert len(capsys.readouterr().out.strip().splitlines()) == 1


def test_no_path_is_clean(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(CHAIN)
    code = cli.main(["trace", "c", "a", "--root", str(root)])
    assert code == 1
    assert "no call path" in capsys.readouterr().err


def test_endpoint_not_found(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(CHAIN)
    code = cli.main(["trace", "a", "nope", "--root", str(root)])
    assert code == 3
    assert "no symbol" in capsys.readouterr().err


def test_endpoint_ambiguous(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(TWO_HELPERS)
    code = cli.main(["trace", "helper", "helper", "--root", str(root)])
    assert code == 4
    err = capsys.readouterr().err
    assert "ambiguous" in err


def test_json_shape(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(CHAIN)
    code = cli.main(["trace", "a", "c", "--root", str(root), "--json"])
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["from"] == "m.py::a"
    assert doc["to"] == "m.py::c"
    assert len(doc["paths"]) == 1
    ids = [hop["id"] for hop in doc["paths"][0]]
    assert ids == ["m.py::a", "m.py::b", "m.py::c"]


def test_no_path_json_exit_code(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(CHAIN)
    code = cli.main(["trace", "c", "a", "--root", str(root), "--json"])
    assert code == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["paths"] == []
