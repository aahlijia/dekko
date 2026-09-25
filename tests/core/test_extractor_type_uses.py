"""Extraction tests for ``FileMap.type_uses`` (TS/TSX).

``Symbol.params``/``Symbol.returns`` only ever held the annotations of
the five function shapes ``_TS_DEFINITIONS`` names. A type used only
as a parameter of a returned or callback arrow function, a
function-typed interface member, a method/overload signature or a
class-field arrow was invisible to ``dekko query type`` and looked
dead to ``dekko unused``. ``extractor._collect_type_uses`` records
those annotations as ``model.TypeUse`` rows, one per typed parameter
and per return type, attributed to the innermost enclosing
definition.
"""

from pathlib import Path

from dekko.core import languages
from dekko.core.extractor import extract_file
from dekko.core.model import TypeUse


def _type_uses(tmp_path: Path, filename: str, source: str) -> list[TypeUse]:
    spec = languages.spec_for_path(filename)
    assert spec is not None
    (tmp_path / filename).write_text(source)
    fm = extract_file(tmp_path, filename, spec)
    assert fm.error is None
    return fm.type_uses


def _sites(uses: list[TypeUse]) -> set[tuple[str, str, str | None, str]]:
    return {(u.site, u.usage, u.param_name, u.type) for u in uses}


TS_SHAPES = (
    "interface AccountContext { id?: string }\n"
    "type SessionConfig = string;\n"
    "function make(t: string) {\n"
    "  return (account?: AccountContext) => { };\n"
    "}\n"
    "export interface Opts {\n"
    "  build: (config: SessionConfig, input: { cwd: string }) => StartInput\n"
    "  run(x: Foo): Bar\n"
    "  new (a: CtorArg): Built\n"
    "  (c: CallArg): CallRet\n"
    "}\n"
    "type Fn = (a: AliasArg) => AliasRet;\n"
    "function f(a: Overload): OverloadRet;\n"
    "function f(a: any): any { return a }\n"
    "class K {\n"
    "  handler = (e: Ev): Res => e;\n"
    "}\n"
    "export const obj = { cb: (a: PairArg) => a };\n"
    "items.map((it: Item) => it);\n"
)


def test_returned_arrow_params_owned_by_enclosing_function(
    tmp_path: Path,
) -> None:
    uses = _type_uses(tmp_path, "a.ts", TS_SHAPES)
    hit = [u for u in uses if u.type == "AccountContext"]
    assert len(hit) == 1
    use = hit[0]
    assert use.owner_id == "a.ts::make"
    assert use.site == "arrow_function"
    assert use.usage == "param"
    assert use.param_name == "account?"
    assert use.line == 4
    assert use.path == "a.ts"


def test_function_typed_interface_member_owned_by_interface(
    tmp_path: Path,
) -> None:
    uses = _type_uses(tmp_path, "a.ts", TS_SHAPES)
    by_type = {u.type: u for u in uses if u.owner_id == "a.ts::Opts"}
    assert by_type["SessionConfig"].site == "function_type"
    assert by_type["SessionConfig"].param_name == "config"
    assert by_type["StartInput"].usage == "return"
    assert by_type["StartInput"].param_name is None
    # An object-literal parameter type is recorded verbatim; the read
    # side's token match finds ``string`` inside it.
    assert by_type["{ cwd: string }"].param_name == "input"


def test_every_signature_shape_is_a_site(tmp_path: Path) -> None:
    uses = _type_uses(tmp_path, "a.ts", TS_SHAPES)
    sites = _sites(uses)
    assert ("method_signature", "param", "x", "Foo") in sites
    assert ("method_signature", "return", None, "Bar") in sites
    assert ("construct_signature", "param", "a", "CtorArg") in sites
    assert ("construct_signature", "return", None, "Built") in sites
    assert ("call_signature", "param", "c", "CallArg") in sites
    assert ("call_signature", "return", None, "CallRet") in sites
    assert ("function_signature", "param", "a", "Overload") in sites
    assert ("function_signature", "return", None, "OverloadRet") in sites


def test_type_alias_function_type_owned_by_alias(tmp_path: Path) -> None:
    uses = _type_uses(tmp_path, "a.ts", TS_SHAPES)
    alias = [u for u in uses if u.owner_id == "a.ts::Fn"]
    assert _sites(alias) == {
        ("function_type", "param", "a", "AliasArg"),
        ("function_type", "return", None, "AliasRet"),
    }


def test_class_field_arrow_owned_by_class(tmp_path: Path) -> None:
    uses = _type_uses(tmp_path, "a.ts", TS_SHAPES)
    field = [u for u in uses if u.type in ("Ev", "Res")]
    assert {u.owner_id for u in field} == {"a.ts::K"}
    assert {u.site for u in field} == {"arrow_function"}


def test_object_literal_arrow_owned_by_exported_variable(
    tmp_path: Path,
) -> None:
    uses = _type_uses(tmp_path, "a.ts", TS_SHAPES)
    pair = [u for u in uses if u.type == "PairArg"]
    assert len(pair) == 1
    assert pair[0].owner_id == "a.ts::obj"


def test_module_level_callback_has_no_owner(tmp_path: Path) -> None:
    uses = _type_uses(tmp_path, "a.ts", TS_SHAPES)
    top = [u for u in uses if u.type == "Item"]
    assert len(top) == 1
    assert top[0].owner_id is None
    assert top[0].site == "arrow_function"
    assert top[0].line == 19


def test_named_definitions_own_params_are_not_sites(tmp_path: Path) -> None:
    # A named function, a declarator-bound arrow, an object-literal
    # method and a class method all go through
    # ``_collect_definitions`` into ``Symbol.params``; none may be
    # recorded a second time here.
    source = (
        "function g(t: Own): OwnRet { return t }\n"
        "const bound = (q: Bound): BoundRet => q;\n"
        "const o = { m(b: Method): MethodRet { return b } };\n"
        "class C { n(z: ClassMethod): void {} }\n"
    )
    assert _type_uses(tmp_path, "b.ts", source) == []


def test_non_function_annotations_are_not_sites(tmp_path: Path) -> None:
    # A field, a variable annotation, a generic argument and an
    # untyped callback are not parameter lists with annotations.
    source = (
        "interface Shape { field: FieldType; other: Wrapper<Inner> }\n"
        "const v: VarType = 1;\n"
        "items.forEach((x) => x);\n"
    )
    assert _type_uses(tmp_path, "c.ts", source) == []


def test_javascript_has_no_type_uses(tmp_path: Path) -> None:
    source = "function f(a) { return (b) => b }\nitems.map((x) => x)\n"
    assert _type_uses(tmp_path, "d.js", source) == []


def test_tsx_grammar_records_callback_param_types(tmp_path: Path) -> None:
    source = (
        "const C = (p: Props) => (\n"
        "  <div onClick={(e: ClickEvent) => p.f(e)} />\n"
        ");\n"
    )
    uses = _type_uses(tmp_path, "e.tsx", source)
    assert _sites(uses) == {("arrow_function", "param", "e", "ClickEvent")}
    assert uses[0].owner_id == "e.tsx::C"


def test_multiline_parameter_lists_record_each_params_line(
    tmp_path: Path,
) -> None:
    source = (
        "export interface Opts {\n"
        "  build: (\n"
        "    first: First,\n"
        "    second: Second,\n"
        "  ) => Out\n"
        "}\n"
    )
    uses = _type_uses(tmp_path, "f.ts", source)
    lines = {u.type: u.line for u in uses}
    assert lines == {"First": 3, "Second": 4, "Out": 2}
