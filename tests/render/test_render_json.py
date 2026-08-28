"""render_json.py: id-interning, compact output, backward-read compat
(round-15 plan: map.json size at scale)."""

import json
from pathlib import Path

from dekko.render import mapfile
from dekko.render.render_json import render_json
from dekko.core.model import (
    CallGraph,
    Edge,
    ExternalCall,
    FileMap,
    HeritageEdge,
    ModuleEdge,
    ModuleGraph,
    Symbol,
)


def _sym(path: str, name: str, kind: str = "function") -> Symbol:
    return Symbol(
        id=f"{path}::{name}",
        name=name,
        qualname=name,
        kind=kind,
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
        FileMap(
            path="d.py",
            language="python",
            symbols=[
                _sym("d.py", "Dog", kind="class"),
                _sym("d.py", "Animal", kind="class"),
            ],
        ),
        FileMap(
            path="e.py",
            language="python",
            symbols=[
                _sym("e.py", "Base1", kind="class"),
                _sym("e.py", "Base2", kind="class"),
            ],
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
        heritage=[
            HeritageEdge(
                subtype="d.py::Dog",
                supertype="d.py::Animal",
                relation="extends",
                lines=[1],
            )
        ],
        heritage_ambiguous=[
            (
                "d.py::Dog",
                "Base",
                ["e.py::Base1", "e.py::Base2"],
            )
        ],
        heritage_external=[
            ExternalCall(
                caller="d.py::Dog", callee="pydantic.BaseModel", lines=[2]
            )
        ],
        modules=ModuleGraph(
            edges=[
                ModuleEdge(importer="a.py", imported="b.py", names=["helper"])
            ],
            deps_out={"a.py": ["b.py"]},
            deps_in={"b.py": ["a.py"]},
            external={"a.py": ["os"]},
        ),
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

    heritage = doc["heritage"][0]
    assert ids[heritage["subtype"]] == "d.py::Dog"
    assert ids[heritage["supertype"]] == "d.py::Animal"
    assert heritage["relation"] == "extends"
    assert heritage["lines"] == [1]

    h_amb = doc["heritage_ambiguous"][0]
    assert ids[h_amb["subtype"]] == "d.py::Dog"
    assert h_amb["name"] == "Base"
    assert [ids[c] for c in h_amb["candidates"]] == [
        "e.py::Base1",
        "e.py::Base2",
    ]

    h_ext = doc["heritage_external"][0]
    assert ids[h_ext["caller"]] == "d.py::Dog"
    assert ids[h_ext["callee"]] == "pydantic.BaseModel"

    mg_edge = doc["module_graph"]["edges"][0]
    assert ids[mg_edge["importer"]] == "a.py"
    assert ids[mg_edge["imported"]] == "b.py"
    assert mg_edge["names"] == ["helper"]

    mg_ext = doc["module_graph"]["external"][0]
    assert ids[mg_ext["path"]] == "a.py"
    assert mg_ext["sources"] == ["os"]


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
    assert loaded.heritage_out == expected.heritage_out
    assert loaded.heritage_in == expected.heritage_in
    assert loaded.heritage_lines == expected.heritage_lines
    assert loaded.heritage_relation == expected.heritage_relation
    assert loaded.heritage_ambiguous_in == expected.heritage_ambiguous_in
    assert loaded.heritage_ambiguous_out == expected.heritage_ambiguous_out
    assert loaded.heritage_external_out == expected.heritage_external_out
    assert loaded.module_deps_out == expected.module_deps_out
    assert loaded.module_deps_in == expected.module_deps_in
    assert loaded.module_edge_names == expected.module_edge_names
    assert loaded.module_external == expected.module_external
    assert (
        loaded.heritage_synthetic_tiebreak_count
        == expected.heritage_synthetic_tiebreak_count
    )


def test_heritage_synthetic_tiebreak_count_round_trips(
    tmp_path: Path,
) -> None:
    # Round 24 (``.features/plans/round24/
    # 03-heritage-crate-decoy-tiebreak.md``): a nonzero count must
    # survive a real write-then-load round trip, not just default to 0
    # by coincidence.
    files, graph = _sample_graph()
    graph.heritage_synthetic_tiebreak_count = 3
    map_dir = tmp_path / ".dekko"
    map_dir.mkdir()
    (map_dir / "map.json").write_bytes(render_json(files, graph, "demo"))

    doc = json.loads((map_dir / "map.json").read_bytes())
    assert doc["heritage_synthetic_tiebreak_count"] == 3

    loaded = mapfile.load_map(tmp_path)
    assert loaded is not None
    assert loaded.heritage_synthetic_tiebreak_count == 3


def test_heritage_synthetic_tiebreak_count_defaults_to_0_pre_v11(
    tmp_path: Path,
) -> None:
    # A document written before doc version 11 has no
    # "heritage_synthetic_tiebreak_count" key at all -- load_map()
    # must default to 0 rather than crashing.
    files, graph = _sample_graph()
    doc = json.loads(render_json(files, graph, "demo"))
    doc["version"] = 10
    del doc["heritage_synthetic_tiebreak_count"]
    map_dir = tmp_path / ".dekko"
    map_dir.mkdir()
    (map_dir / "map.json").write_text(json.dumps(doc))

    loaded = mapfile.load_map(tmp_path)
    assert loaded is not None
    assert loaded.heritage_synthetic_tiebreak_count == 0


def test_map_doc_version_is_11() -> None:
    # Bumped 10 -> 11 for heritage_synthetic_tiebreak_count (round 24
    # heritage crate-decoy tiebreak disclosure, ``.features/plans/
    # round24/03-heritage-crate-decoy-tiebreak.md``).
    assert mapfile.MAP_DOC_VERSION == 11


def test_backward_read_v5_document_without_heritage_sections(
    tmp_path: Path,
) -> None:
    # A v5 document (id-interning present, but written before heritage
    # existed) has no "heritage"/"heritage_ambiguous"/"heritage_
    # external" keys at all. load_map() must not crash and must
    # produce empty heritage tables rather than assuming those keys
    # are always present.
    files, graph = _sample_graph()
    graph.heritage = []
    graph.heritage_ambiguous = []
    graph.heritage_external = []
    doc = json.loads(render_json(files, graph, "demo"))
    doc["version"] = 5
    del doc["heritage"]
    del doc["heritage_ambiguous"]
    del doc["heritage_external"]
    map_dir = tmp_path / ".dekko"
    map_dir.mkdir()
    (map_dir / "map.json").write_text(json.dumps(doc))

    index = mapfile.load_map(tmp_path)
    assert index is not None
    assert index.heritage_out == {}
    assert index.heritage_in == {}
    assert index.heritage_ambiguous_in == {}
    assert index.heritage_ambiguous_out == {}
    assert index.heritage_external_out == {}
    # The rest of the document still reads correctly — a heritage-less
    # v5 document is not otherwise degraded.
    assert index.calls_out["a.py::main"] == ["b.py::helper"]


def test_backward_read_v6_document_without_module_graph_section(
    tmp_path: Path,
) -> None:
    # A v6 document (heritage present, but written before the module
    # dependency graph existed) has no "module_graph" key at all.
    # load_map() must not crash and must produce empty module-graph
    # tables rather than assuming the key is always present.
    files, graph = _sample_graph()
    doc = json.loads(render_json(files, graph, "demo"))
    doc["version"] = 6
    del doc["module_graph"]
    map_dir = tmp_path / ".dekko"
    map_dir.mkdir()
    (map_dir / "map.json").write_text(json.dumps(doc))

    index = mapfile.load_map(tmp_path)
    assert index is not None
    assert index.module_deps_out == {}
    assert index.module_deps_in == {}
    assert index.module_edge_names == {}
    assert index.module_external == {}
    # The rest of the document still reads correctly — a module-
    # graph-less v6 document is not otherwise degraded.
    assert index.heritage_out["d.py::Dog"] == ["d.py::Animal"]
    assert index.calls_out["a.py::main"] == ["b.py::helper"]


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
