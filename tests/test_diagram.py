"""MAP.md embedded mermaid diagram: scale-guard tiers and block syntax."""

import re

import pytest

from dekko import export, mapfile
from dekko.model import CallGraph, Edge, FileMap, Symbol
from dekko.render_md import _overview_diagram


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


def _index(edges: list[Edge]) -> mapfile.MapIndex:
    files = [
        FileMap(
            path="a/x.py", language="python", symbols=[_sym("a/x.py", "f")]
        ),
        FileMap(
            path="a/z.py", language="python", symbols=[_sym("a/z.py", "h")]
        ),
        FileMap(
            path="b/y.py", language="python", symbols=[_sym("b/y.py", "g")]
        ),
    ]
    return mapfile.index_from_maps(files, CallGraph(edges=edges), "demo")


_EDGES = [
    Edge(caller="a/x.py::f", callee="b/y.py::g", lines=[2]),
    Edge(caller="a/x.py::f", callee="a/z.py::h", lines=[3]),
]


def test_empty_graph_has_no_diagram() -> None:
    _, _, status = export.overview_graph(_index([]), export.DEFAULT_MAX_NODES)
    assert status == "empty"
    assert _overview_diagram(_index([])) == []


def test_file_tier_when_under_cap() -> None:
    labels, _, status = export.overview_graph(_index(_EDGES), max_nodes=10)
    assert status == "file"
    assert len(labels) == 3  # three files carry edges


def test_dir_tier_when_files_exceed_cap() -> None:
    labels, _, status = export.overview_graph(_index(_EDGES), max_nodes=2)
    assert status == "dir"
    assert set(labels) == {"a", "b"}


def test_omitted_when_dirs_exceed_cap() -> None:
    labels, _, status = export.overview_graph(_index(_EDGES), max_nodes=1)
    assert status == "too_big"
    assert len(labels) == 2


def test_rendered_block_is_valid_mermaid() -> None:
    block = _overview_diagram(_index(_EDGES))
    assert block[0] == "```mermaid"
    assert block[-2] == "```"
    body = block[1].splitlines()
    assert body[0] == "flowchart LR"
    node_re = re.compile(r'^  n\d+\["[^"]*"\]$')
    edge_re = re.compile(r"^  n\d+ --> n\d+$")
    nodes = [ln for ln in body[1:] if node_re.match(ln)]
    edges = [ln for ln in body[1:] if edge_re.match(ln)]
    assert len(nodes) == 3
    assert len(edges) == 2
    # every line after the header is a node or an edge
    assert len(nodes) + len(edges) == len(body) - 1


def test_too_big_renders_pointer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(export, "DEFAULT_MAX_NODES", 1)
    block = _overview_diagram(_index(_EDGES))
    text = "\n".join(block)
    assert "Architecture diagram omitted" in text
    assert "dekko export --format mermaid" in text
    assert "```mermaid" not in text
