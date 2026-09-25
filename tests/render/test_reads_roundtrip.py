"""The ``reads`` section and the literal-member symbol fields, written
and read back."""

import json
from pathlib import Path

from dekko.core.model import CallGraph, FileMap, ReadSite, Symbol
from dekko.render import mapfile
from dekko.render.render_json import render_json


def _sym(path: str, name: str, **kw: object) -> Symbol:
    return Symbol(
        id=f"{path}::{name}",
        name=name,
        qualname=name,
        kind="function",
        path=path,
        language="typescript",
        in_literal=bool(kw.get("in_literal", False)),
        literal_consumer=kw.get("literal_consumer"),  # type: ignore[arg-type]
    )


def _sample() -> tuple[list[FileMap], CallGraph]:
    files = [
        FileMap(
            path="cmd.ts",
            language="typescript",
            symbols=[
                _sym("cmd.ts", "isHidden", in_literal=True),
                _sym(
                    "cmd.ts",
                    "hideInstance",
                    in_literal=True,
                    literal_consumer="createReconciler",
                ),
                _sym("cmd.ts", "helper"),
            ],
        ),
        FileMap(
            path="use.ts",
            language="typescript",
            symbols=[_sym("use.ts", "pick")],
        ),
        FileMap(
            path="tests/use.test.ts",
            language="typescript",
            symbols=[_sym("tests/use.test.ts", "check")],
        ),
    ]
    graph = CallGraph(
        reads=[
            ReadSite(reader="use.ts::pick", name="isHidden", lines=[3, 9]),
            ReadSite(
                reader="tests/use.test.ts::check", name="isHidden", lines=[2]
            ),
        ]
    )
    return files, graph


def test_reads_are_interned_and_symbol_rows_omit_default_flags() -> None:
    files, graph = _sample()
    doc = json.loads(render_json(files, graph, "demo"))
    rows = doc["reads"]
    assert len(rows) == 2
    assert all(isinstance(r["reader"], int) for r in rows)
    assert doc["ids"][rows[0]["reader"]] == "use.ts::pick"
    assert doc["ids"][rows[0]["name"]] == "isHidden"
    assert rows[0]["lines"] == [3, 9]
    by_id = {s["id"]: s for s in doc["symbols"]}
    assert by_id["cmd.ts::isHidden"]["in_literal"] is True
    assert "literal_consumer" not in by_id["cmd.ts::isHidden"]
    assert by_id["cmd.ts::hideInstance"]["literal_consumer"] == (
        "createReconciler"
    )
    assert "in_literal" not in by_id["cmd.ts::helper"]
    assert "literal_consumer" not in by_id["cmd.ts::helper"]


def test_reads_and_literal_fields_round_trip_through_load_map(
    tmp_path: Path,
) -> None:
    files, graph = _sample()
    map_dir = tmp_path / ".dekko"
    map_dir.mkdir()
    (map_dir / "map.json").write_bytes(render_json(files, graph, "demo"))
    index = mapfile.load_map(tmp_path)
    assert index is not None
    sites = index.reads_by_name["isHidden"]
    assert [(s.reader, s.lines) for s in sites] == [
        ("use.ts::pick", [3, 9]),
        ("tests/use.test.ts::check", [2]),
    ]
    hide = index.symbols_by_id["cmd.ts::hideInstance"]
    assert (hide.in_literal, hide.literal_consumer) == (
        True,
        "createReconciler",
    )
    helper = index.symbols_by_id["cmd.ts::helper"]
    assert (helper.in_literal, helper.literal_consumer) == (False, None)


def test_index_from_maps_and_without_tests_filter_readers() -> None:
    files, graph = _sample()
    index = mapfile.index_from_maps(files, graph, "demo")
    assert len(index.reads_by_name["isHidden"]) == 2
    prod = index.without_tests()
    assert [s.reader for s in prod.reads_by_name["isHidden"]] == [
        "use.ts::pick"
    ]


def test_a_map_without_a_reads_section_loads_with_none(
    tmp_path: Path,
) -> None:
    files, graph = _sample()
    doc = json.loads(render_json(files, graph, "demo"))
    del doc["reads"]
    for row in doc["symbols"]:
        row.pop("in_literal", None)
        row.pop("literal_consumer", None)
    map_dir = tmp_path / ".dekko"
    map_dir.mkdir()
    (map_dir / "map.json").write_text(json.dumps(doc))
    index = mapfile.load_map(tmp_path)
    assert index is not None
    assert index.reads_by_name == {}
    assert index.symbols_by_id["cmd.ts::isHidden"].in_literal is False
