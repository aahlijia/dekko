"""B7: interactive HTML export — document, escaping, size guard, CLI."""

import json
import re
from pathlib import Path

import pytest

from conftest import RepoFactory

from dekko import cli, mapfile, render_html
from dekko.model import CallGraph, Edge, FileMap, Symbol

_ISLAND = re.compile(
    r'<script type="application/json" id="dekko-map">(.*?)</script>',
    re.DOTALL,
)


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


def _index() -> mapfile.MapIndex:
    fa = FileMap("a.py", "python", symbols=[_sym("a.py", "f")])
    fb = FileMap("b.py", "python", symbols=[_sym("b.py", "g")])
    graph = CallGraph(
        edges=[Edge(caller="b.py::g", callee="a.py::f", lines=[2])],
        calls_out={"b.py::g": ["a.py::f"]},
        calls_in={"a.py::f": ["b.py::g"]},
    )
    return mapfile.index_from_maps([fa, fb], graph, "demo")


def _parse_island(page: str) -> dict:
    m = _ISLAND.search(page)
    assert m, "no JSON island found"
    return json.loads(m.group(1))


def test_document_matches_index() -> None:
    index = _index()
    doc = render_html.build_document(index)
    assert doc["root"] == "demo"
    assert len(doc["symbols"]) == len(index.symbols_by_id)
    assert doc["stats"]["files"] == 2
    # The call edge is carried both ways, with its call-site line.
    callee = doc["symbols"]["b.py::g"]["callees"][0]
    assert callee == {"id": "a.py::f", "lines": [2]}
    caller = doc["symbols"]["a.py::f"]["callers"][0]
    assert caller == {"id": "b.py::g", "lines": [2]}


def test_island_parses_and_is_valid_json() -> None:
    page = render_html.render(render_html.build_document(_index()))
    parsed = _parse_island(page)
    assert set(parsed["symbols"]) == {"a.py::f", "b.py::g"}
    # Page carries the interactive shell.
    assert 'id="search"' in page
    assert 'id="tree"' in page


def test_script_named_symbol_does_not_break_island() -> None:
    fm = FileMap("x.py", "python", symbols=[_sym("x.py", "</script>")])
    index = mapfile.index_from_maps([fm], CallGraph(), "demo")
    page = render_html.render(render_html.build_document(index))
    island = _ISLAND.search(page).group(1)
    # The island must not be terminated early by the literal sequence.
    assert "</script>" not in island
    # …yet it round-trips: JSON.parse would decode \\u003c back to '<'.
    parsed = json.loads(island)
    assert parsed["symbols"]["x.py::</script>"]["name"] == "</script>"


def test_size_guard_refuses_oversized_map(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(render_html, "HTML_MAX_BYTES", 10)
    out = tmp_path / "map.html"
    assert render_html.run(_index(), out) == render_html.EXIT_TOO_BIG
    assert not out.exists()


def test_run_writes_file(tmp_path: Path) -> None:
    out = tmp_path / "sub" / "map.html"
    assert render_html.run(_index(), out) == render_html.EXIT_OK
    page = out.read_text()
    assert page.startswith("<!doctype html>")
    assert len(_parse_island(page)["symbols"]) == 2


def test_cli_export_html_default_path(make_mapped_repo: RepoFactory) -> None:
    root = make_mapped_repo(
        {
            "a.py": "def f() -> int:\n    return 1\n",
            "b.py": "from a import f\n\n\ndef g() -> int:\n    return f()\n",
        }
    )
    assert cli.main(["export", "--format", "html", "--root", str(root)]) == 0
    page = (root / ".dekko" / "map.html").read_text()
    parsed = _parse_island(page)
    assert any(s["name"] == "g" for s in parsed["symbols"].values())


def test_cli_export_mermaid_output_file(
    make_mapped_repo: RepoFactory, tmp_path: Path
) -> None:
    root = make_mapped_repo({"a.py": "def f() -> int:\n    return 1\n"})
    out = tmp_path / "graph.mmd"
    code = cli.main(
        [
            "export",
            "--format",
            "mermaid",
            "--root",
            str(root),
            "--output",
            str(out),
        ]
    )
    assert code == 0
    assert out.read_text().startswith("flowchart LR")
