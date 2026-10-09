"""Record constructions resolve through the canonical constructor.

``new Point(1, 2)`` from another file reaches the sole-candidate rung,
which counts arguments against the candidate's parameters. A record's
own symbol has none, so before the canonical constructor was a symbol
the construction was dropped as external. Now the record stands for
its constructors the way a class that declares them does.
"""

import json
from pathlib import Path

from dekko.core.model import CallGraph
from dekko.core.resolver import resolve
from dekko.integrations import cli
from dekko.render.mapfile import load_map
from dekko.repo_ops import map_repository

_PKG = "src/main/java/app"


def _graph(root: Path, sources: dict[str, str]) -> CallGraph:
    for rel, text in sources.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    files, _ = map_repository(
        root,
        subpath=None,
        excludes=(),
        max_file_size=1_000_000,
    )
    return resolve(files)


def _callees(graph: CallGraph, caller: str) -> set[str]:
    return {e.callee for e in graph.edges if e.caller == caller}


def test_a_construction_from_another_file_reaches_the_record(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            f"{_PKG}/Point.java": (
                "package app;\npublic record Point(int x, int y) { }\n"
            ),
            f"{_PKG}/Shape.java": (
                "package app;\npublic class Shape {\n"
                "    public Shape(int x, int y) { }\n}\n"
            ),
            f"{_PKG}/Use.java": (
                "package app;\n"
                "class Use {\n"
                "    void good() { new Point(1, 2); }\n"
                "    void wrong() { new Point(1); }\n"
                "    void shape() { new Shape(1); }\n"
                "}\n"
            ),
        },
    )
    use = f"{_PKG}/Use.java::Use"
    point = f"{_PKG}/Point.java::Point"
    assert _callees(graph, f"{use}.good") == {point, f"{point}.Point"}
    # A wrong count fits no constructor: the type keeps the edge, as
    # it does for a class declaring the same constructor.
    assert _callees(graph, f"{use}.wrong") == {point}
    assert _callees(graph, f"{use}.shape") == {f"{_PKG}/Shape.java::Shape"}
    assert not [e for e in graph.external if e.caller.startswith(use)]


def test_an_extra_constructor_is_picked_by_its_count(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        {
            f"{_PKG}/Host.java": (
                "package app;\n"
                "public record Host(String name, int port) {\n"
                "    public Host(String name) { this(name, 80); }\n"
                "}\n"
            ),
            f"{_PKG}/Use.java": (
                "package app;\n"
                "class Use {\n"
                '    void one() { new Host("h"); }\n'
                '    void two() { new Host("h", 1); }\n'
                "}\n"
            ),
        },
    )
    use = f"{_PKG}/Use.java::Use"
    host = f"{_PKG}/Host.java::Host"
    assert _callees(graph, f"{use}.one") == {host, f"{host}.Host#2"}
    assert _callees(graph, f"{use}.two") == {host, f"{host}.Host"}


def test_two_constructors_of_one_count_are_a_tie(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        {
            f"{_PKG}/Tie.java": (
                "package app;\n"
                "public record Tie(Object a, String b) {\n"
                "    public Tie(Object a, Integer b) {\n"
                "        this(a, String.valueOf(b));\n"
                "    }\n"
                "}\n"
            ),
            f"{_PKG}/Use.java": (
                "package app;\n"
                "class Use {\n"
                "    void tie(Object o, Object v) { new Tie(o, v); }\n"
                "}\n"
            ),
        },
    )
    caller = f"{_PKG}/Use.java::Use.tie"
    tie = f"{_PKG}/Tie.java::Tie"
    assert _callees(graph, caller) == {tie}
    rows = [(c, n, sorted(ids)) for c, n, ids in graph.ambiguous]
    assert rows == [(caller, "Tie", [f"{tie}.Tie", f"{tie}.Tie#2"])]


_RECORD = f"{_PKG}/Point.java"


def _write(root: Path, rel: str, text: str) -> None:
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text(text)


def test_the_constructor_round_trips_through_map_json_and_the_cache(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        _RECORD,
        "package app;\nrecord Point(int x, int y) { }\n",
    )
    want = {"name": "Point", "kind": "method", "visibility": "package"}
    for _ in range(2):
        # The second run reads the file back from the extraction cache.
        assert cli.main(["map", str(tmp_path), "--quiet"]) == 0
        doc = json.loads((tmp_path / ".dekko/map.json").read_text())
        rows = {s["qualname"]: s for s in doc["symbols"]}
        row = rows["Point.Point"]
        assert {k: row.get(k) for k in want} == want
        assert [p["name"] for p in row["params"]] == ["x", "y"]
        index = load_map(tmp_path)
        assert index is not None
        loaded = index.symbols_by_id[f"{_RECORD}::Point.Point"]
        assert [p.type for p in loaded.params] == ["int", "int"]
        assert loaded.visibility == "package"


def _graph_json(root: Path) -> dict:
    doc = json.loads((root / ".dekko/map.json").read_text())
    keys = ("edges", "ambiguous", "external", "referenced", "symbols")
    return {k: doc.get(k) for k in keys}


def test_a_new_component_reaches_an_incremental_map(tmp_path: Path) -> None:
    _write(tmp_path, _RECORD, "package app;\nrecord Point(int x, int y) { }\n")
    _write(
        tmp_path,
        f"{_PKG}/Use.java",
        "package app;\nclass Use {\n"
        "    void two() { new Point(1, 2); }\n"
        "    void three() { new Point(1, 2, 3); }\n"
        "}\n",
    )
    assert cli.main(["map", str(tmp_path), "--quiet"]) == 0
    _write(
        tmp_path,
        _RECORD,
        "package app;\nrecord Point(int x, int y, int z) { }\n",
    )

    assert cli.main(["map", str(tmp_path), "--quiet"]) == 0
    incremental = _graph_json(tmp_path)
    assert cli.main(["map", str(tmp_path), "--quiet", "--full"]) == 0
    assert incremental == _graph_json(tmp_path)

    ctor = f"{_RECORD}::Point.Point"
    rows = {s["id"]: s for s in incremental["symbols"]}
    assert [p["name"] for p in rows[ctor]["params"]] == ["x", "y", "z"]
    index = load_map(tmp_path)
    assert index is not None
    callers = set(index.calls_in.get(ctor, ()))
    assert callers == {f"{_PKG}/Use.java::Use.three"}
