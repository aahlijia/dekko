"""MAP.md Overview section: rollup table, hotspots, anchor links."""

import re

from conftest import RepoFactory

from dekko import mapfile, summary
from dekko.model import CallGraph, Edge, FileMap, Symbol
from dekko.render_md import render_markdown


def _sym(path: str, name: str) -> Symbol:
    return Symbol(
        id=f"{path}::{name}",
        name=name,
        qualname=name,
        kind="function",
        path=path,
        language="python",
        start_line=1,
        end_line=2,
    )


def test_cross_dir_edge_counts() -> None:
    fa = FileMap(
        path="a/x.py", language="python", symbols=[_sym("a/x.py", "f")]
    )
    fb = FileMap(
        path="b/y.py", language="python", symbols=[_sym("b/y.py", "g")]
    )
    graph = CallGraph(
        edges=[Edge(caller="a/x.py::f", callee="b/y.py::g", lines=[2])]
    )
    index = mapfile.index_from_maps([fa, fb], graph, "demo")
    dirs = {d["path"]: d for d in summary.compute(index)["directories"]}
    assert dirs["a"]["cross_edges"] == 1
    assert dirs["b"]["cross_edges"] == 1
    assert dirs["a"]["internal_edges"] == 0


def test_internal_edge_counts() -> None:
    fm = FileMap(
        path="p/m.py",
        language="python",
        symbols=[_sym("p/m.py", "f"), _sym("p/m.py", "g")],
    )
    graph = CallGraph(
        edges=[Edge(caller="p/m.py::f", callee="p/m.py::g", lines=[2])]
    )
    index = mapfile.index_from_maps([fm], graph, "demo")
    row = summary.compute(index)["directories"][0]
    assert row["internal_edges"] == 1
    assert row["cross_edges"] == 0


def test_overview_present_and_table_well_formed(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo({"a.py": "def f():\n    return 1\n"})
    text = (root / ".dekko" / "MAP.md").read_text()
    assert "## Overview" in text
    assert (
        "| Directory | Files | Symbols | Internal | Cross-dir | Purpose |"
        in text
    )


def test_overview_links_resolve_to_anchors() -> None:
    fm = FileMap(
        path="p/m.py",
        language="python",
        symbols=[_sym("p/m.py", "f"), _sym("p/m.py", "g")],
    )
    graph = CallGraph(
        edges=[Edge(caller="p/m.py::f", callee="p/m.py::g", lines=[2])]
    )
    text = render_markdown([fm], graph, "demo")
    overview = text.split("## Contents")[0]
    hrefs = re.findall(r"\]\(#([a-z0-9-]+)\)", overview)
    assert hrefs, "overview produced no links"
    ids = set(re.findall(r'<a id="([a-z0-9-]+)">', text))
    for href in hrefs:
        assert href in ids, f"dangling overview link: #{href}"
