"""The doc index survives map.json: written by ``dekko map``, loaded back."""

import json
from pathlib import Path

from conftest import RepoFactory

from dekko.core.model import CallGraph
from dekko.render import mapfile
from dekko.core.resolver import resolve
from dekko.render.mapfile import load_map

SOURCES = {
    "src/app/walker.py": "def discover(root):\n    return root\n",
    "src/app/index.py": (
        "class MapIndex:\n    def load(self):\n        return 1\n"
    ),
    "tests/test_walker.py": "def test_discover():\n    assert True\n",
    "README.md": (
        "# App\n\nUse `MapIndex.load` and `discover`.\n\n"
        "## Layout\n\nSee [walker](src/app/walker.py#L1).\n"
        "Old: [gone](src/app/gone.py).\n"
    ),
    "tests/README.md": "# Tests\n\nRun `test_discover`.\n",
}


def test_map_writes_and_loads_the_doc_index(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(SOURCES)

    index = load_map(root)

    assert index is not None
    assert index.has_doc_index
    headings = {(s.path, s.heading) for s in index.doc_sections}
    assert ("README.md", "App") in headings
    assert ("README.md", "Layout") in headings
    targets = {link.target for link in index.doc_links}
    assert "src/app/index.py::MapIndex.load" in targets
    assert "src/app/walker.py::discover" in targets
    assert [d.text for d in index.doc_dangling] == ["src/app/gone.py"]


def test_links_are_looked_up_by_target_and_by_doc(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(SOURCES)

    index = load_map(root)

    by_target = index.doc_links_by_target["src/app/walker.py::discover"]
    assert {index.doc_links[i].text for i in by_target} == {
        "discover",
        "src/app/walker.py:1",
    }
    readme = {
        index.doc_links[i].text for i in index.doc_links_by_doc["README.md"]
    }
    assert "MapIndex.load" in readme


def test_a_repo_without_docs_still_has_a_doc_index(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo({"a.py": "def f():\n    return 1\n"})

    index = load_map(root)

    assert index.has_doc_index
    assert index.doc_sections == []


def test_a_map_written_before_docs_were_indexed(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(SOURCES)
    path = root / ".dekko" / "map.json"
    doc = json.loads(path.read_text())
    for key in ("doc_sections", "doc_links", "doc_dangling"):
        del doc[key]
    path.write_text(json.dumps(doc))

    index = load_map(root)

    assert index is not None
    assert not index.has_doc_index
    assert index.doc_links == []


def test_without_tests_drops_test_docs_and_test_targets(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(SOURCES)

    full = load_map(root)
    view = full.without_tests()

    assert any(
        full.doc_sections[link.section].path == "tests/README.md"
        for link in full.doc_links
    )
    assert "tests/README.md" not in view.doc_links_by_doc
    assert (
        "tests/test_walker.py::test_discover" not in view.doc_links_by_target
    )
    assert all(
        not view.doc_sections[view.doc_links[i].section].path.startswith(
            "tests/"
        )
        for ids in view.doc_links_by_doc.values()
        for i in ids
    )
    assert view.has_doc_index


def test_an_in_memory_index_has_no_doc_index(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(SOURCES)
    index = load_map(root)

    rebuilt = mapfile.index_from_maps([], _empty_graph(), "x")

    assert index.has_doc_index
    assert not rebuilt.has_doc_index


def _empty_graph() -> CallGraph:
    return resolve([])


def test_doc_paths_are_relative_to_the_root(
    make_mapped_repo: RepoFactory, tmp_path: Path
) -> None:
    root = make_mapped_repo(SOURCES)

    index = load_map(root)

    assert all(not Path(s.path).is_absolute() for s in index.doc_sections)
    assert root == tmp_path
