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


LINKED = {
    "app.py": "def main():\n    return 0\n",
    "README.md": (
        "# App\n\nStart with [setup](docs/setup.md#install) and [[faq]].\n"
    ),
    "docs/setup.md": "# Setup\n\n## Install {#install}\n\nRun it.\n",
    "docs/faq.md": "# FAQ\n\nBack to [the readme](../README.md).\n",
    "tests/notes.md": "# Notes\n\nSee [setup](../docs/setup.md).\n",
}


def test_links_between_docs_survive_the_map(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(LINKED)

    index = load_map(root)

    pairs = {
        (index.doc_sections[e.section].path, e.target) for e in index.doc_edges
    }
    assert pairs == {
        ("README.md", "docs/setup.md"),
        ("README.md", "docs/faq.md"),
        ("docs/faq.md", "README.md"),
        ("tests/notes.md", "docs/setup.md"),
    }
    anchored = next(e for e in index.doc_edges if e.target_section is not None)
    assert index.doc_sections[anchored.target_section].heading == "Install"
    assert index.doc_sections[anchored.target_section].anchor == "install"


def test_doc_links_are_looked_up_both_ways(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(LINKED)

    index = load_map(root)

    out = {index.doc_edges[i].target for i in index.doc_edges_out["README.md"]}
    assert out == {"docs/setup.md", "docs/faq.md"}
    into = {
        index.doc_sections[index.doc_edges[i].section].path
        for i in index.doc_edges_in["docs/setup.md"]
    }
    assert into == {"README.md", "tests/notes.md"}


def test_a_map_without_doc_links_loads_with_none(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(LINKED)
    path = root / ".dekko" / "map.json"
    doc = json.loads(path.read_text())
    del doc["doc_edges"]
    path.write_text(json.dumps(doc))

    index = load_map(root)

    assert index.has_doc_index
    assert index.doc_edges == []
    assert index.doc_edges_in == {}


def test_without_tests_drops_doc_links_from_test_docs(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(LINKED)

    view = load_map(root).without_tests()

    assert "tests/notes.md" not in view.doc_edges_out
    sources = {
        view.doc_sections[view.doc_edges[i].section].path
        for i in view.doc_edges_in["docs/setup.md"]
    }
    assert sources == {"README.md"}


def test_a_wikilink_to_a_code_file_still_links_the_code(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(
        {
            "src/app.py": "def main():\n    return 0\n",
            "notes.md": "# Notes\n\nThe entry point is [[src/app.py]].\n",
        }
    )

    index = load_map(root)

    assert "src/app.py" in {link.target for link in index.doc_links}
    assert index.doc_edges == []
