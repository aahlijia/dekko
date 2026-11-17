"""The weighted file graph communities are built from."""

from pathlib import Path

import pytest
from conftest import RepoFactory

from dekko.analysis.communities import file_graph
from dekko.render import mapfile

SOURCES = {
    "pkg/__init__.py": "",
    "pkg/base.py": "class Base:\n    pass\n\n\ndef helper():\n    return 1\n",
    "pkg/child.py": (
        "from pkg.base import Base, helper\n\n\n"
        "class Child(Base):\n"
        "    def go(self):\n"
        "        helper()\n"
        "        helper()\n"
        "        return helper\n"
    ),
    "pkg/run.py": "from pkg.base import helper\n\nhelper()\n",
    "pkg/alone.py": "def solo():\n    return 0\n",
    "tests/test_child.py": (
        "from pkg.child import Child\n\n\ndef test_go():\n    Child().go()\n"
    ),
}


@pytest.fixture
def index(make_mapped_repo: RepoFactory) -> mapfile.MapIndex:
    root: Path = make_mapped_repo(SOURCES)
    loaded = mapfile.load_map(root)
    assert loaded is not None
    return loaded


def test_weights_add_call_sites_references_imports_and_heritage(
    index: mapfile.MapIndex,
) -> None:
    _, edges = file_graph(index, tests=False)

    # Two helper() calls, references to Base and helper, one import,
    # one heritage clause.
    assert edges[("pkg/base.py", "pkg/child.py")] == 6


def test_a_module_level_call_counts_for_its_file(
    index: mapfile.MapIndex,
) -> None:
    _, edges = file_graph(index, tests=False)

    assert edges[("pkg/base.py", "pkg/run.py")] == 2


def test_default_view_leaves_test_files_out(index: mapfile.MapIndex) -> None:
    nodes, edges = file_graph(index, tests=False)

    assert nodes == [
        "pkg/__init__.py",
        "pkg/alone.py",
        "pkg/base.py",
        "pkg/child.py",
        "pkg/run.py",
    ]
    assert set(edges) == {
        ("pkg/base.py", "pkg/child.py"),
        ("pkg/base.py", "pkg/run.py"),
    }


def test_tests_view_adds_test_files_and_their_edges(
    index: mapfile.MapIndex,
) -> None:
    nodes, edges = file_graph(index, tests=True)

    assert "tests/test_child.py" in nodes
    assert edges[("pkg/child.py", "tests/test_child.py")] == 3
