"""Fields recorded on type symbols, per language."""

from pathlib import Path

from dekko.core.extractor import extract_file
from dekko.core.languages import spec_for_path
from dekko.core.model import TYPE_KINDS, Field, Param

FIXTURES = Path(__file__).parent.parent / "fixtures"


def _fields(rel: str) -> dict[str, dict[str, Field]]:
    spec = spec_for_path(rel)
    assert spec is not None
    fm = extract_file(FIXTURES, rel, spec)
    return {
        s.qualname: {f.name: f for f in s.fields}
        for s in fm.symbols
        if s.kind in TYPE_KINDS
    }


def test_ts_declared_inferred_literal_and_parameter_property_fields() -> None:
    f = _fields("ts/fields.ts")["Hub"]
    assert f["config"] == Field("config", "Config", 3)
    assert f["name"] == Field("name", "string", 4, True)
    assert f["count"] == Field("count", "number", 5, True)
    assert f["items"].type == "Thing[]"
    assert f["w"] == Field("w", "WD", 7, True)
    assert f["handler"].type is None
    assert f["handler"].inferred is False
    assert f["mcpHub"] == Field("mcpHub", "McpHub", 9)
    assert f["port"].type == "number"
    assert "plain" not in f
    assert f["extra"] == Field("extra", "Extra", 10, True)
    assert f["late"] == Field("late", None, 11)
    assert "local" not in f


def test_ts_interface_members_are_fields() -> None:
    f = _fields("ts/fields.ts")["Ctx"]
    assert f["host"].type == "SessionHost"
    assert f["opt"].type == "Opts"
    assert "run" not in f
    # A member of an inline object type belongs to that type, not Ctx.
    assert f["cb"].type == "{ inner: Foo }"
    assert "inner" not in f


def test_declared_type_wins_over_a_later_assignment() -> None:
    f = _fields("ts/fields.ts")["Dup"]
    assert list(f) == ["x"]
    assert f["x"] == Field("x", "Foo", 17)


def test_non_type_symbols_carry_no_fields() -> None:
    rel = "ts/fields.ts"
    spec = spec_for_path(rel)
    assert spec is not None
    fm = extract_file(FIXTURES, rel, spec)
    assert all(s.fields == [] for s in fm.symbols if s.kind not in TYPE_KINDS)


def test_js_field_definitions_and_this_assignment() -> None:
    f = _fields("js/fields.js")["Box"]
    assert f == {
        "x": Field("x", "Foo", 2, True),
        "y": Field("y", "number", 3, True),
        "z": Field("z", "Bar", 5, True),
    }


def test_python_class_level_and_self_assigned_fields() -> None:
    f = _fields("python/fields.py")["Svc"]
    assert f["x"] == Field("x", "Foo", 2)
    assert f["k"] == Field("k", "int", 3, True)
    assert f["a"] == Field("a", "Bar", 6, True)
    assert f["b"] == Field("b", None, 7)
    assert f["c"] == Field("c", "Baz", 8)
    assert f["d"] == Field("d", "list", 9, True)
    assert "e" not in f
    assert f["late"] == Field("late", "Thing", 13, True)


def test_rust_struct_tuple_and_test_module_fields() -> None:
    rel = "rust/fields.rs"
    spec = spec_for_path(rel)
    assert spec is not None
    fm = extract_file(FIXTURES, rel, spec)
    by_line = {s.start_line: s for s in fm.symbols if s.kind in TYPE_KINDS}
    top = {f.name: f.type for f in by_line[1].fields}
    assert top == {"a": "Entity<Foo>", "b": "Vec<u8>"}
    assert [(f.name, f.type) for f in by_line[6].fields] == [
        ("0", "Foo"),
        ("1", "u32"),
    ]
    assert by_line[8].fields == []
    assert [(f.name, f.type) for f in by_line[13].fields] == [("t", "Test")]


def test_go_fields_and_receiver_param() -> None:
    f = _fields("go/fields.go")["S"]
    assert {n: x.type for n, x in f.items()} == {
        "a": "Foo",
        "b": "Bar",
        "c": "int",
        "d": "int",
        "in": "struct{ z int }",
        # An embedded field is named after its type, as Go names it.
        "Embedded": "Embedded",
    }
    rel = "go/fields.go"
    spec = spec_for_path(rel)
    assert spec is not None
    run = next(
        s for s in extract_file(FIXTURES, rel, spec).symbols if s.name == "Run"
    )
    assert run.params[0] == Param("s", "S", receiver=True)
    assert run.params[1].name == "x"


def test_java_fields_records_and_enum_constants() -> None:
    fields = _fields("java/Fields.java")
    f = fields["Fields"]
    assert f["x"] == Field("x", "Foo", 2)
    assert f["y"].type == "Bar"
    assert f["z"].type == "Bar"
    assert "local" not in f
    assert {n: x.type for n, x in fields["Fields.R"].items()} == {
        "a": "Foo",
        "b": "int",
    }
    assert {n: x.type for n, x in fields["Fields.E"].items()} == {
        "A": "Fields.E",
        "B": "Fields.E",
    }


def test_kotlin_properties_class_parameters_and_enum_entries() -> None:
    fields = _fields("kotlin/Fields.kt")
    f = fields["A"]
    assert f["x"] == Field("x", "Foo", 1)
    assert "y" not in f
    assert f["r"] == Field("r", "Repo", 2)
    assert f["s"] == Field("s", "Svc", 3, True)
    assert "l" not in f
    assert {n: x.type for n, x in fields["C"].items()} == {
        "X": "C",
        "Y": "C",
    }


def test_cpp_members_skip_method_declarations() -> None:
    f = _fields("cpp/fields.cc")["C"]
    assert {n: x.type for n, x in f.items()} == {
        "a_": "Foo*",
        "b": "std::unique_ptr<Bar>",
        "c": "int",
        "d": "int",
    }


def test_c_struct_members() -> None:
    f = _fields("c/fields.c")["P"]
    assert {n: x.type for n, x in f.items()} == {
        "x": "int",
        "q": "struct Q*",
    }


def test_go_receiver_stays_out_of_signatures_and_arity() -> None:
    from dekko.analysis.outline import _outline_sig
    from dekko.core.resolver import _param_arity
    from dekko.textutil import signature

    rel = "go/fields.go"
    spec = spec_for_path(rel)
    assert spec is not None
    run = next(
        s for s in extract_file(FIXTURES, rel, spec).symbols if s.name == "Run"
    )
    assert signature(run) == "S.Run(x: int)"
    assert _outline_sig(run) == "Run(x: int)"
    assert _param_arity(run.params) == (1, 1)
