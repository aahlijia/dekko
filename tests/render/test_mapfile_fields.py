"""A type's fields and a Go receiver survive map.json and the caches.

A map written by a newer dekko, with keys this build lacks, still loads.
"""

import json
from dataclasses import asdict
from pathlib import Path

from dekko.core.model import Field, Param, Symbol
from dekko.core.resolver import name_delta
from dekko.integrations import cli
from dekko.render.mapfile import _symbol_from_dict, load_map
from dekko.render.render_json import _symbol_row


def _cls(fields: list[Field]) -> Symbol:
    return Symbol(
        id="a.py::C",
        name="C",
        qualname="C",
        kind="class",
        path="a.py",
        language="python",
        fields=fields,
    )


def test_symbol_fields_round_trip_through_json() -> None:
    sym = _cls([Field("x", "Foo", 3), Field("y", None, 4, True)])
    row = _symbol_row(sym)
    assert row["fields"] == [
        {"name": "x", "type": "Foo", "line": 3, "inferred": False},
        {"name": "y", "type": None, "line": 4, "inferred": True},
    ]
    assert _symbol_from_dict(row).fields == sym.fields


def test_symbol_without_fields_key_loads_with_empty_list() -> None:
    d = {
        "id": "a.py::C",
        "name": "C",
        "qualname": "C",
        "kind": "class",
        "path": "a.py",
        "language": "python",
    }
    assert _symbol_from_dict(d).fields == []
    assert _symbol_from_dict(d).params == []


def test_empty_fields_and_plain_params_stay_off_the_row() -> None:
    # Most symbols have no fields and no receiver; writing the defaults
    # on every row would grow map.json for nothing.
    sym = _cls([])
    sym.params = [Param("x", "int")]
    row = _symbol_row(sym)
    assert "fields" not in row
    assert "receiver" not in row["params"][0]
    assert _symbol_from_dict(row) == sym


def test_param_receiver_defaults_false_and_round_trips() -> None:
    p = Param("s", "*Server", receiver=True)
    assert Param(**asdict(p)) == p
    assert Param("x").receiver is False
    sym = _cls([])
    sym.params = [p]
    assert _symbol_from_dict(_symbol_row(sym)).params == [p]


def test_field_type_change_on_a_type_blocks_reuse() -> None:
    delta = name_delta(
        [_cls([Field("x", "Foo")])],
        [_cls([Field("x", "Bar")])],
    )
    assert "C" in delta.changed
    assert delta.blocks_reuse


def test_a_newer_maps_unknown_keys_are_ignored() -> None:
    # A map from a newer dekko may add a key to a param or a field;
    # this build loads what it knows instead of raising TypeError.
    sym = _cls([Field("x", "Foo", 3)])
    sym.params = [Param("a", "int")]
    row = _symbol_row(sym)
    row["params"][0]["later_param_key"] = 1
    row["fields"][0]["later_field_key"] = [2]

    assert _symbol_from_dict(row) == sym


def test_a_map_with_unknown_import_keys_still_loads(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def f() -> int:\n    return 1\n")
    (tmp_path / "b.py").write_text("from a import f\n\nf()\n")
    assert cli.main(["map", str(tmp_path), "--quiet"]) == 0
    path = tmp_path / ".dekko" / "map.json"
    doc = json.loads(path.read_text())
    for entry in doc["files"]:
        for imp in entry["imports"]:
            imp["later_import_key"] = True
    path.write_text(json.dumps(doc))

    index = load_map(tmp_path)

    assert index is not None
    assert [i.name for i in index.imports_by_path["b.py"]] == ["f"]
