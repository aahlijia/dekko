"""A type's fields and a Go receiver survive map.json and the caches."""

from dataclasses import asdict

from dekko.core.model import Field, Param, Symbol
from dekko.core.resolver import name_delta
from dekko.render.mapfile import _symbol_from_dict
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
