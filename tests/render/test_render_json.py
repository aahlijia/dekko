"""render_json.py: id-interning, compact output, backward-read compat
(round-15 plan: map.json size at scale)."""

import json
from pathlib import Path

from dekko.render import mapfile
from dekko.render.render_json import render_json
from dekko.core.model import CallGraph, Edge, ExternalCall, FileMap, Symbol


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


def _sample_graph() -> tuple[list[FileMap], CallGraph]:
    files = [
        FileMap(
            path="a.py", language="python", symbols=[_sym("a.py", "main")]
        ),
        FileMap(
            path="b.py", language="python", symbols=[_sym("b.py", "helper")]
        ),
        FileMap(
            path="c.py", language="python", symbols=[_sym("c.py", "helper")]
        ),
    ]
    graph = CallGraph(
        edges=[Edge(caller="a.py::main", callee="b.py::helper", lines=[3])],
        ambiguous=[("a.py::main", "helper", ["b.py::helper", "c.py::helper"])],
        external=[
            ExternalCall(
                caller="a.py::main", callee="subprocess.run", lines=[5]
            )
        ],
        referenced=[
            Edge(caller="a.py::main", callee="c.py::helper", lines=[7])
        ],
    )
    return files, graph


def test_render_json_interns_repeated_ids() -> None:
    # "a.py::main" and "b.py::helper" each occur in more than one of
    # edges/ambiguous/external/referenced; the shared "ids" table must
    # write each distinct id string exactly once regardless of how
    # many sections reference it (round-15 plan's core fix).
    files, graph = _sample_graph()
    doc = json.loads(render_json(files, graph, "demo"))
    assert doc["version"] == mapfile.MAP_DOC_VERSION
    ids = doc["ids"]
    assert ids.count("a.py::main") == 1
    assert ids.count("b.py::helper") == 1

    edge = doc["edges"][0]
    assert ids[edge["caller"]] == "a.py::main"
    assert ids[edge["callee"]] == "b.py::helper"

    amb = doc["ambiguous"][0]
    assert ids[amb["caller"]] == "a.py::main"
    assert [ids[c] for c in amb["candidates"]] == [
        "b.py::helper",
        "c.py::helper",
    ]

    ext = doc["external"][0]
    assert ids[ext["caller"]] == "a.py::main"
    assert ids[ext["callee"]] == "subprocess.run"

    ref = doc["referenced"][0]
    assert ids[ref["caller"]] == "a.py::main"
    assert ids[ref["callee"]] == "c.py::helper"


def test_render_json_is_compact_not_pretty_printed() -> None:
    files, graph = _sample_graph()
    text = render_json(files, graph, "demo").decode("utf-8")
    # indent=2 pretty-printing puts each top-level key on its own
    # indented line; compact output has no such padding anywhere.
    assert "\n  " not in text


def test_round_trip_matches_index_from_maps(tmp_path: Path) -> None:
    # The acceptance bar the round-15 plan asks for: not just "loads
    # without crashing" but "produces the same answers" as the
    # in-process index built directly from the same graph.
    files, graph = _sample_graph()
    map_dir = tmp_path / ".dekko"
    map_dir.mkdir()
    (map_dir / "map.json").write_bytes(render_json(files, graph, "demo"))

    loaded = mapfile.load_map(tmp_path)
    expected = mapfile.index_from_maps(files, graph, "demo")

    assert loaded is not None
    assert loaded.calls_in == expected.calls_in
    assert loaded.calls_out == expected.calls_out
    assert loaded.ambiguous_in == expected.ambiguous_in
    assert loaded.ambiguous_out == expected.ambiguous_out
    assert loaded.externals_by_name == expected.externals_by_name
    assert loaded.referenced_in == expected.referenced_in
    assert loaded.referenced_out == expected.referenced_out


def test_backward_read_v4_document_without_ids_table(
    tmp_path: Path,
) -> None:
    # A fixture in the pre-v5 shape: raw id strings, no "ids" key,
    # version stamped 4. load_map() must still resolve it correctly —
    # proof the version branch actually guards old documents rather
    # than assuming every file on disk is already v5.
    doc = {
        "generator": "dekko",
        "version": 4,
        "root": "demo",
        "generated_at": "2020-01-01T00:00:00+00:00",
        "provenance": None,
        "files": [
            {
                "path": "a.py",
                "language": "python",
                "error": None,
                "doc": None,
                "imports": [],
            },
            {
                "path": "b.py",
                "language": "python",
                "error": None,
                "doc": None,
                "imports": [],
            },
        ],
        "symbols": [
            {
                "id": "a.py::main",
                "name": "main",
                "qualname": "main",
                "kind": "function",
                "path": "a.py",
                "language": "python",
            },
            {
                "id": "b.py::helper",
                "name": "helper",
                "qualname": "helper",
                "kind": "function",
                "path": "b.py",
                "language": "python",
            },
        ],
        "edges": [
            {"caller": "a.py::main", "callee": "b.py::helper", "lines": [3]}
        ],
        "ambiguous": [
            {
                "caller": "a.py::main",
                "name": "helper",
                "candidates": ["b.py::helper", "c.py::helper"],
            }
        ],
        "external": [
            {
                "caller": "a.py::main",
                "callee": "subprocess.run",
                "lines": [5],
            }
        ],
        "referenced": [
            {"caller": "a.py::main", "callee": "b.py::helper", "lines": [7]}
        ],
    }
    map_dir = tmp_path / ".dekko"
    map_dir.mkdir()
    (map_dir / "map.json").write_text(json.dumps(doc))

    index = mapfile.load_map(tmp_path)
    assert index is not None
    assert index.calls_out["a.py::main"] == ["b.py::helper"]
    assert index.calls_in["b.py::helper"] == ["a.py::main"]
    assert index.ambiguous_out["a.py::main"] == ["helper"]
    assert index.ambiguous_in["b.py::helper"] == [("a.py::main", "helper")]
    assert index.ambiguous_in["c.py::helper"] == [("a.py::main", "helper")]
    assert index.externals_by_name["run"][0].callee == "subprocess.run"
    assert index.referenced_out["a.py::main"] == ["b.py::helper"]
    assert index.referenced_in["b.py::helper"] == ["a.py::main"]
