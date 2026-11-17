"""``dekko communities``: the listing, one file's place, and edge cases."""

import json
from pathlib import Path

import pytest
from conftest import RepoFactory

from dekko.integrations import cli

SOURCES = {
    "app/__init__.py": "",
    "app/a.py": (
        "from app.b import b\nfrom lib.x import x\n\n\n"
        "def a():\n    b()\n    x()\n"
    ),
    "app/b.py": "from app.c import c\n\n\ndef b():\n    c()\n    c()\n",
    "app/c.py": "def c():\n    return 1\n",
    "lib/__init__.py": "",
    "lib/x.py": "from lib.y import y\n\n\ndef x():\n    y()\n    y()\n",
    "lib/y.py": "from lib.z import z\n\n\ndef y():\n    z()\n    z()\n",
    "lib/z.py": "def z():\n    return 2\n",
    "tests/test_all.py": (
        "from app.a import a\n\n\ndef test_a():\n    a()\n    a()\n"
    ),
}


@pytest.fixture
def root(make_mapped_repo: RepoFactory) -> Path:
    return make_mapped_repo(SOURCES)


def _run(root: Path, *args: str) -> int:
    return cli.main(["communities", "--root", str(root), *args])


def test_listing_shows_communities_and_bridges(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(root) == 0
    out = capsys.readouterr().out

    assert out.startswith(
        "dekko: 2 communities over 8 files (2 unconnected, 0 small groups)"
    )
    assert "#1 lib  3 files, 3 symbols, cohesion" in out
    assert "#2 app  3 files, 3 symbols, cohesion" in out
    assert "bridges:\n  #1 <-> #2" in out


def test_limit_caps_the_communities_listed(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(root, "--limit", "1") == 0
    out = capsys.readouterr().out

    assert "#1 lib" in out
    assert "#2 app" not in out


def test_a_small_budget_sheds_the_trailing_sections(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(root, "--budget", "40") == 0
    out = capsys.readouterr().out

    assert "bridges:" not in out
    assert "omitted" in out


def test_json_lists_every_community(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(root, "--json") == 0
    doc = json.loads(capsys.readouterr().out)

    assert doc["view"] == "default"
    assert [c["label"] for c in doc["communities"]] == ["lib", "app"]
    assert doc["bridges"][0]["first"] == 1


def test_a_file_target_shows_its_community_and_links(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(root, "app/a.py") == 0
    out = capsys.readouterr().out

    assert out.startswith("app/a.py is in:\n#2 app  3 files")
    assert "#1 lib  2" in out


def test_a_symbol_target_uses_its_file(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(root, "lib/z.py::z") == 0

    assert capsys.readouterr().out.startswith("lib/z.py is in:\n#1 lib")


def test_an_unknown_target_exits_3(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(root, "nowhere.py") == 3
    assert "no symbol matches 'nowhere.py'" in capsys.readouterr().err


def test_tests_flag_reads_the_partition_with_tests(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(root, "--tests", "--json") == 0
    doc = json.loads(capsys.readouterr().out)

    assert doc["view"] == "with_tests"
    assert doc["files"] == 9


def test_a_repo_without_cross_file_edges_prints_one_line(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_mapped_repo({"solo.py": "def f():\n    return 1\n"})

    assert _run(root) == 0

    assert capsys.readouterr().out == (
        "dekko: no communities (1 file, no cross-file edges)\n"
    )


def test_a_map_from_before_communities_says_to_remap(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = root / ".dekko" / "map.json"
    doc = json.loads(path.read_text())
    del doc["communities"]
    path.write_text(json.dumps(doc))

    assert _run(root, "--no-regen") == 2
    assert "run dekko map" in capsys.readouterr().err


def test_an_ambiguous_symbol_lists_its_candidates(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_mapped_repo(
        {**SOURCES, "lib/dup.py": "def c():\n    return 3\n"}
    )

    assert _run(root, "c") == 4

    err = capsys.readouterr().err
    assert "'c' is ambiguous" in err
    assert "lib/dup.py" in err


def test_a_test_file_in_the_default_view_points_at_tests_flag(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(root, "tests/test_all.py") == 0

    assert "left out of the default view; pass --tests" in (
        capsys.readouterr().out
    )


def test_a_file_target_shows_its_whole_community_row(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(root, "lib/y.py") == 0
    out = capsys.readouterr().out

    assert "   top: " in out
    assert "   key: " in out


def test_limit_zero_prints_counts_only(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(root, "--limit", "0", "--budget", "0") == 0

    assert capsys.readouterr().out == (
        "dekko: 2 communities over 8 files (2 unconnected, 0 small groups)\n"
    )


def test_bridges_under_a_limit_name_only_listed_communities(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(root, "--limit", "1") == 0

    assert "<->" not in capsys.readouterr().out
