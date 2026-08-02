"""map.json round-trip, provenance, and freshness checks."""

import json
from pathlib import Path

from dekko import mapfile
from dekko.model import CallGraph, Edge, FileMap, Symbol

from conftest import RepoFactory

CHAIN = {
    "a.py": (
        "def helper(x: int) -> int:\n"
        "    return x + 1\n"
        "\n"
        "\n"
        "def main() -> None:\n"
        "    helper(1)\n"
    )
}


def test_load_round_trip(make_mapped_repo: RepoFactory) -> None:
    root = make_mapped_repo(CHAIN)
    index = mapfile.load_map(root)
    assert index is not None
    helper = index.symbols_by_qualname["helper"][0]
    assert helper.path == "a.py"
    assert helper.params[0].type == "int"
    main_id = index.symbols_by_qualname["main"][0].id
    assert helper.id in index.calls_out[main_id]
    assert main_id in index.calls_in[helper.id]


def test_provenance_written(make_mapped_repo: RepoFactory) -> None:
    root = make_mapped_repo(CHAIN)
    doc = json.loads((root / ".dekko" / "map.json").read_text())
    assert doc["version"] == 4
    prov = doc["provenance"]
    assert prov["tool_version"]
    assert prov["spec_hash"]
    assert set(prov["files"]) == {"a.py"}


def test_freshness_transitions(make_mapped_repo: RepoFactory) -> None:
    root = make_mapped_repo(CHAIN)
    index = mapfile.load_map(root)
    assert mapfile.check_freshness(root, index).fresh

    (root / "a.py").write_text(CHAIN["a.py"] + "\n\nX = 1\n")
    fresh = mapfile.check_freshness(root, index)
    assert not fresh.fresh
    assert fresh.changed == ["a.py"]

    (root / "b.py").write_text("def extra() -> None:\n    pass\n")
    fresh = mapfile.check_freshness(root, index)
    assert fresh.added == ["b.py"]


def test_removed_file_detected(make_mapped_repo: RepoFactory) -> None:
    root = make_mapped_repo(
        dict(CHAIN, **{"b.py": "def extra() -> None:\n    pass\n"})
    )
    index = mapfile.load_map(root)
    (root / "b.py").unlink()
    fresh = mapfile.check_freshness(root, index)
    assert not fresh.fresh
    assert fresh.removed == ["b.py"]


def test_v1_map_is_always_stale(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(CHAIN)
    doc = json.loads((root / ".dekko" / "map.json").read_text())
    doc["version"] = 1
    del doc["provenance"]
    (root / ".dekko" / "map.json").write_text(json.dumps(doc))

    index = mapfile.load_map(root)
    assert index is not None
    assert not mapfile.check_freshness(root, index).fresh


def test_missing_map_loads_none(tmp_path: Path) -> None:
    assert mapfile.load_map(tmp_path) is None


def test_provenance_records_unsupported_files(
    make_mapped_repo: RepoFactory,
) -> None:
    # A repo with an unparseable file type (Astro, the confirmed
    # 2026-07-31 eval gap) must not map "clean" — the skip has to
    # survive into map.json so read commands can warn about it.
    root = make_mapped_repo(
        dict(CHAIN, **{"Card.astro": "---\nconst x = 1;\n---\n<div/>\n"})
    )
    index = mapfile.load_map(root)
    assert index is not None
    assert index.provenance is not None
    unsupported = index.provenance["unsupported"]
    assert unsupported == {"count": 1, "languages": {"astro": 1}}
    assert mapfile.format_unsupported(index.provenance) == (
        "1 files unparsed — no parser for: astro (1)"
    )


def test_format_unsupported_none_when_fully_covered(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(CHAIN)
    index = mapfile.load_map(root)
    assert index is not None
    assert index.provenance["unsupported"] is None
    assert mapfile.format_unsupported(index.provenance) is None


def test_version_stamp_stale_even_with_unchanged_source(
    make_mapped_repo: RepoFactory,
) -> None:
    # Bug #1: a map whose provenance predates the running dekko build
    # must read as stale even when every file's content hash still
    # matches — content-only diffing can never catch an extractor
    # change (or a real version bump) on an untouched source tree.
    root = make_mapped_repo(CHAIN)
    map_path = root / ".dekko" / "map.json"
    doc = json.loads(map_path.read_text())
    doc["provenance"]["tool_version"] = "0.0.0-stale"
    map_path.write_text(json.dumps(doc))

    index = mapfile.load_map(root)
    assert index is not None
    fresh = mapfile.check_freshness(root, index)
    assert not fresh.fresh
    assert fresh.reason == "version"
    # No file-hash diff was attempted for a version mismatch.
    assert fresh.added == fresh.removed == fresh.changed == []


def test_spec_hash_stale_even_with_matching_tool_version(
    make_mapped_repo: RepoFactory,
) -> None:
    # The finer-grained half of bug #1: an extraction-logic change
    # under an unchanged (or unreleased) version string must still
    # invalidate, not just a released version bump.
    root = make_mapped_repo(CHAIN)
    map_path = root / ".dekko" / "map.json"
    doc = json.loads(map_path.read_text())
    doc["provenance"]["spec_hash"] = "deadbeef"
    map_path.write_text(json.dumps(doc))

    index = mapfile.load_map(root)
    assert index is not None
    fresh = mapfile.check_freshness(root, index)
    assert not fresh.fresh
    assert fresh.reason == "version"


def test_freshness_reason_missing_for_v1_map(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(CHAIN)
    doc = json.loads((root / ".dekko" / "map.json").read_text())
    doc["version"] = 1
    del doc["provenance"]
    (root / ".dekko" / "map.json").write_text(json.dumps(doc))

    index = mapfile.load_map(root)
    assert index is not None
    fresh = mapfile.check_freshness(root, index)
    assert not fresh.fresh
    assert fresh.reason == "missing"


def test_freshness_reason_content_on_change(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(CHAIN)
    index = mapfile.load_map(root)
    (root / "a.py").write_text(CHAIN["a.py"] + "\n\nX = 1\n")
    fresh = mapfile.check_freshness(root, index)
    assert not fresh.fresh
    assert fresh.reason == "content"


def test_freshness_reason_none_when_fresh(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(CHAIN)
    index = mapfile.load_map(root)
    fresh = mapfile.check_freshness(root, index)
    assert fresh.fresh
    assert fresh.reason is None


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


def test_load_map_reads_referenced_edge_lines(
    make_mapped_repo: RepoFactory,
) -> None:
    # Package B: the reference-site lines already round-trip through
    # map.json's "referenced" edges (render_json.py already writes
    # them); load_map() must not silently drop them on the read side.
    root = make_mapped_repo(CHAIN)
    map_path = root / ".dekko" / "map.json"
    doc = json.loads(map_path.read_text())
    doc["referenced"] = [
        {"caller": "a.py::main", "callee": "a.py::helper", "lines": [42]}
    ]
    map_path.write_text(json.dumps(doc))

    index = mapfile.load_map(root)
    assert index is not None
    key = ("a.py::main", "a.py::helper")
    assert index.ref_lines[key] == [42]
    assert index.referenced_out["a.py::main"] == ["a.py::helper"]
    assert index.referenced_in["a.py::helper"] == ["a.py::main"]


def test_index_from_maps_reads_referenced_edge_lines() -> None:
    files = [
        FileMap(path="a.py", language="python", symbols=[_sym("a.py", "f")]),
        FileMap(path="b.py", language="python", symbols=[_sym("b.py", "g")]),
    ]
    graph = CallGraph(
        referenced=[Edge(caller="a.py::f", callee="b.py::g", lines=[42])]
    )
    index = mapfile.index_from_maps(files, graph, "demo")
    assert index.ref_lines[("a.py::f", "b.py::g")] == [42]
    assert index.referenced_out["a.py::f"] == ["b.py::g"]
    assert index.referenced_in["b.py::g"] == ["a.py::f"]


def test_without_tests_drops_ref_lines_touching_test_paths() -> None:
    files = [
        FileMap(path="a.py", language="python", symbols=[_sym("a.py", "f")]),
        FileMap(path="b.py", language="python", symbols=[_sym("b.py", "g")]),
        FileMap(
            path="tests/test_a.py",
            language="python",
            symbols=[_sym("tests/test_a.py", "h")],
        ),
    ]
    graph = CallGraph(
        referenced=[
            Edge(caller="a.py::f", callee="b.py::g", lines=[10]),
            Edge(caller="tests/test_a.py::h", callee="b.py::g", lines=[20]),
        ]
    )
    index = mapfile.index_from_maps(files, graph, "demo")
    filtered = index.without_tests()
    assert filtered.ref_lines == {("a.py::f", "b.py::g"): [10]}
