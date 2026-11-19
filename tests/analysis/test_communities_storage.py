"""Communities are computed by ``dekko map`` and stored in map.json."""

import json
from pathlib import Path

import pytest
from conftest import RepoFactory

from dekko.integrations import cli
from dekko.render import mapfile

SOURCES = {
    "app/one.py": "from app.two import two\n\n\ndef one():\n    two()\n",
    "app/two.py": "def two():\n    return 2\n",
    "app/__init__.py": "",
    "lib/a.py": "from lib.b import b\n\n\ndef a():\n    b()\n",
    "lib/b.py": "def b():\n    return 1\n",
    "lib/__init__.py": "",
    "tests/test_app.py": (
        "from app.one import one\nfrom lib.a import a\n\n\n"
        "def test_both():\n    one()\n    a()\n"
    ),
}


@pytest.fixture
def root(make_mapped_repo: RepoFactory) -> Path:
    return make_mapped_repo(SOURCES)


def _doc(root: Path) -> dict:
    return json.loads((root / ".dekko" / "map.json").read_text())


def test_map_json_stores_both_views_as_file_indexes(root: Path) -> None:
    doc = _doc(root)
    paths = [f["path"] for f in doc["files"]]

    stored = doc["communities"]
    default = [[paths[i] for i in group] for group in stored["default"]]
    with_tests = [[paths[i] for i in g] for g in stored["with_tests"]]

    assert ["app/one.py", "app/two.py"] in default
    assert ["lib/a.py", "lib/b.py"] in default
    assert not any("tests/test_app.py" in g for g in default)
    assert any("tests/test_app.py" in g for g in with_tests)


def test_load_map_reads_communities(root: Path) -> None:
    index = mapfile.load_map(root)
    assert index is not None

    assert index.has_communities
    assert ["app/one.py", "app/two.py"] in index.communities
    assert any("tests/test_app.py" in g for g in index.communities_with_tests)


def test_a_map_without_communities_says_so(root: Path) -> None:
    doc = _doc(root)
    del doc["communities"]
    (root / ".dekko" / "map.json").write_text(json.dumps(doc))

    index = mapfile.load_map(root)
    assert index is not None

    assert not index.has_communities
    assert index.communities == []


def test_the_test_free_view_keeps_the_default_partition(root: Path) -> None:
    index = mapfile.load_map(root)
    assert index is not None

    view = index.without_tests()

    assert view.has_communities
    assert view.communities == index.communities
    assert view.communities_with_tests == []


def test_remapping_an_unchanged_tree_adds_missing_communities(
    root: Path,
) -> None:
    doc = _doc(root)
    del doc["communities"]
    (root / ".dekko" / "map.json").write_text(json.dumps(doc))

    assert cli.main(["map", str(root), "--quiet"]) == 0

    assert "communities" in _doc(root)
