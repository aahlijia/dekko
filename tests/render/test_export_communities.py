"""``dekko export`` groups files by community at file granularity."""

from pathlib import Path

import pytest
from conftest import RepoFactory

from dekko.integrations import cli
from dekko.render import mapfile, render_html

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
    "tests/test_all.py": "from app.a import a\n\n\ndef test_a():\n    a()\n",
}


@pytest.fixture
def root(make_mapped_repo: RepoFactory) -> Path:
    return make_mapped_repo(SOURCES)


def _export(root: Path, *args: str) -> int:
    return cli.main(["export", "--root", str(root), *args])


def test_mermaid_wraps_each_community_in_a_subgraph(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _export(root, "--format", "mermaid", "--granularity", "file") == 0
    out = capsys.readouterr().out

    assert '  subgraph c1["community 1: lib"]' in out
    assert '  subgraph c2["community 2: app"]' in out
    assert out.count("  end") == 2
    lib = out[out.index("subgraph c1") : out.index("subgraph c2")]
    assert '"lib/x.py"' in lib
    assert '"app/a.py"' not in lib


def test_files_outside_communities_stay_at_top_level(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _export(root, "--format", "mermaid", "--granularity", "file") == 0
    out = capsys.readouterr().out

    test_node = next(
        line for line in out.splitlines() if '"tests/test_all.py"' in line
    )
    assert test_node.startswith("  n") and not test_node.startswith("    ")


def test_dot_draws_communities_as_clusters(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _export(root, "--format", "dot", "--granularity", "file") == 0
    out = capsys.readouterr().out

    assert "  subgraph cluster_1 {" in out
    assert '    label="community 1: lib";' in out


def test_symbol_granularity_has_no_subgraphs(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _export(root, "--format", "mermaid") == 0

    assert "subgraph" not in capsys.readouterr().out


def test_html_files_carry_their_community(root: Path) -> None:
    index = mapfile.load_map(root)
    assert index is not None

    files = {f["path"]: f for f in render_html.build_document(index)["files"]}

    assert files["lib/x.py"]["community"] == "community 1: lib"
    assert files["tests/test_all.py"]["community"] == ""
