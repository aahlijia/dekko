"""Trace subcommand: shortest call paths, exit codes, JSON shape."""

import json

import pytest

from dekko.integrations import cli

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

# A bare call to a same-named free function defined in two other files
# (neither the caller's own) resolves ambiguously rather than to either
# candidate — same shape as
# ``test_resolver.test_bare_call_stays_ambiguous_among_multiple_non_methods``,
# but built as real source so the CLI-level ``trace`` command can be
# exercised against it, not just ``resolve()`` directly.
AMBIGUOUS_HOP = {
    "a.py": "def helper():\n    pass\n",
    "b.py": "def helper():\n    pass\n",
    "caller.py": "def entry():\n    helper()\n",
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


def test_no_path_through_ambiguous_hop_discloses_it(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # round-13 spring-boot.md: when the only route to the destination
    # runs through a call that resolved ambiguously (not absent, just
    # unresolved), a bare "no call path" reads as a false ground-truth
    # negative — `query callees` on the same edge honestly discloses
    # it as ambiguous rather than hiding it, and `trace`'s own message
    # should say the same thing, not contradict it.
    root = make_mapped_repo(AMBIGUOUS_HOP)
    code = cli.main(["trace", "entry", "a.py:helper", "--root", str(root)])
    assert code == 1
    err = capsys.readouterr().err
    assert "no *resolved* call path" in err
    assert "ambiguously-resolved calls" in err
    assert "query callees" in err


def test_no_path_through_ambiguous_hop_json(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(AMBIGUOUS_HOP)
    code = cli.main(
        ["trace", "entry", "a.py:helper", "--root", str(root), "--json"]
    )
    assert code == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["paths"] == []
    assert doc["ambiguous_path_exists"] is True


def test_no_path_is_clean_stays_unaffected_by_ambiguity_check(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # Regression guard: a genuinely unreachable pair with *no*
    # ambiguous edges anywhere along the way must keep the plain
    # message, not the new "may exist through ambiguous calls" one.
    root = make_mapped_repo(CHAIN)
    code = cli.main(["trace", "c", "a", "--root", str(root), "--json"])
    assert code == 1
    doc = json.loads(capsys.readouterr().out)
    assert "ambiguous_path_exists" not in doc


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
