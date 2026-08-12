"""Extraction tests for the Tier-1 Python and Rust queries."""

from pathlib import Path

from dekko import languages
from dekko.extractor import _parse_rust_use, extract_file
from dekko.model import Symbol

FIXTURES = Path(__file__).parent / "fixtures"


def _by_qualname(symbols: list[Symbol]) -> dict[str, Symbol]:
    return {sym.qualname: sym for sym in symbols}


def test_python_symbols() -> None:
    spec = languages.spec_for_path("util.py")
    assert spec is not None
    fm = extract_file(FIXTURES / "python", "util.py", spec)
    assert fm.error is None
    syms = _by_qualname(fm.symbols)
    assert set(syms) == {"helper", "Config", "Config.load", "Config.validate"}

    helper = syms["helper"]
    assert helper.kind == "function"
    assert [(p.name, p.type) for p in helper.params] == [
        ("x", "int"),
        ("y", "int"),
    ]
    assert helper.returns == "int"

    load = syms["Config.load"]
    assert load.kind == "method"
    assert [(p.name, p.type) for p in load.params] == [
        ("self", None),
        ("path", "str"),
    ]
    assert load.returns == '"Config"'
    assert syms["Config"].kind == "class"


def test_python_splat_params_and_imports() -> None:
    spec = languages.spec_for_path("main.py")
    assert spec is not None
    fm = extract_file(FIXTURES / "python", "main.py", spec)
    run = _by_qualname(fm.symbols)["run"]
    assert [p.name for p in run.params] == ["args", "*extra", "**kw"]
    assert run.params[0].type == "list[str]"

    imports = {(i.name, i.source) for i in fm.imports}
    assert ("util", "util") in imports
    assert ("helper", "util.helper") in imports


def test_python_relative_import_sources(tmp_path: Path) -> None:
    spec = languages.spec_for_path("rel.py")
    assert spec is not None
    (tmp_path / "rel.py").write_text(
        "from . import sibling\n"
        "from .. import parent\n"
        "from .pkg import thing\n"
        "from ..pkg import other\n"
    )
    fm = extract_file(tmp_path, "rel.py", spec)
    imports = {(i.name, i.source) for i in fm.imports}
    # Relative dots must not be doubled.
    assert ("sibling", ".sibling") in imports
    assert ("parent", "..parent") in imports
    assert ("thing", ".pkg.thing") in imports
    assert ("other", "..pkg.other") in imports


def test_python_calls_attributed_to_enclosing_function() -> None:
    spec = languages.spec_for_path("main.py")
    assert spec is not None
    fm = extract_file(FIXTURES / "python", "main.py", spec)
    in_run = {c.name for c in fm.calls if c.caller_id}
    assert {"Config", "validate", "helper"} <= in_run
    top_level = {c.name for c in fm.calls if c.caller_id is None}
    assert "run" in top_level


def test_rust_symbols() -> None:
    spec = languages.spec_for_path("lib.rs")
    assert spec is not None
    fm = extract_file(FIXTURES / "rust", "lib.rs", spec)
    assert fm.error is None
    syms = _by_qualname(fm.symbols)
    assert set(syms) == {"Point", "Point.new", "Point.dist", "norm"}

    new = syms["Point.new"]
    assert new.kind == "method"
    assert [(p.name, p.type) for p in new.params] == [
        ("x", "f64"),
        ("y", "f64"),
    ]
    assert new.returns == "Self"

    dist = syms["Point.dist"]
    assert dist.params[0].name == "&self"
    assert dist.params[1].type == "&Point"
    assert syms["norm"].kind == "function"


def test_rust_calls_and_receivers() -> None:
    spec = languages.spec_for_path("main.rs")
    assert spec is not None
    fm = extract_file(FIXTURES / "rust", "main.rs", spec)
    calls = {(c.name, c.receiver) for c in fm.calls if c.caller_id}
    assert ("new", "Point") in calls
    assert ("norm", None) in calls
    assert any(name == "dist" for name, _ in calls)


def test_rust_nested_fn_not_a_method(tmp_path: Path) -> None:
    # Bug #2(a): a fn nested inside another fn's body is a closure-
    # local helper, not a member of whatever impl block contains the
    # outer fn — it must not climb past the outer fn to inherit
    # Point's qualname/kind.
    spec = languages.spec_for_path("point.rs")
    assert spec is not None
    (tmp_path / "point.rs").write_text(
        "struct Point { x: f64, y: f64 }\n"
        "\n"
        "impl Point {\n"
        "    fn dist(&self) -> f64 {\n"
        "        fn helper(v: f64) -> f64 {\n"
        "            v.abs()\n"
        "        }\n"
        "        helper(self.x)\n"
        "    }\n"
        "}\n"
    )
    fm = extract_file(tmp_path, "point.rs", spec)
    assert fm.error is None
    syms = _by_qualname(fm.symbols)
    assert "Point.helper" not in syms
    assert syms["helper"].kind == "function"
    assert syms["Point.dist"].kind == "method"


def test_rust_inline_test_module_marks_symbol_test(tmp_path: Path) -> None:
    # Master report #7 (round 11, zed): Rust's idiomatic
    # ``#[cfg(test)] mod tests { ... }`` co-locates unit tests inside
    # the same file as the production code they test, so the
    # file-level `is_test_path` pass in `cli.map_repository` never
    # sees a test-path file and never sets `Symbol.test`. The
    # extractor's own `_qualify` already climbs through the `mod_item`
    # container and knows the qualname prefix is `tests` — it must now
    # also seed `Symbol.test = True` from that same signal.
    spec = languages.spec_for_path("buffer_search.rs")
    assert spec is not None
    (tmp_path / "buffer_search.rs").write_text(
        "pub fn update_search_settings(x: i32) -> i32 {\n"
        "    x + 1\n"
        "}\n"
        "\n"
        "#[cfg(test)]\n"
        "mod tests {\n"
        "    use super::*;\n"
        "\n"
        "    fn helper() -> i32 {\n"
        "        update_search_settings(1)\n"
        "    }\n"
        "}\n"
    )
    fm = extract_file(tmp_path, "buffer_search.rs", spec)
    assert fm.error is None
    syms = _by_qualname(fm.symbols)
    assert set(syms) == {"update_search_settings", "tests.helper"}
    assert syms["update_search_settings"].test is False
    assert syms["tests.helper"].test is True


def test_rust_non_test_mod_named_something_else_stays_untested(
    tmp_path: Path,
) -> None:
    # A production `mod` that isn't named `tests`/`test` must not be
    # misclassified — only the specific bare-name heuristic fires.
    spec = languages.spec_for_path("lib2.rs")
    assert spec is not None
    (tmp_path / "lib2.rs").write_text(
        "mod helpers {\n    pub fn util() -> i32 {\n        1\n    }\n}\n"
    )
    fm = extract_file(tmp_path, "lib2.rs", spec)
    assert fm.error is None
    syms = _by_qualname(fm.symbols)
    assert syms["helpers.util"].test is False


def test_typescript_variable_export_indexed_as_symbol(
    tmp_path: Path,
) -> None:
    spec = languages.spec_for_path("data.ts")
    assert spec is not None
    (tmp_path / "data.ts").write_text(
        "export const jobs = [1, 2, 3];\n"
        'const CONFIG = { host: "local" };\n'
        "export const build = () => jobs.length;\n"
    )
    fm = extract_file(tmp_path, "data.ts", spec)
    syms = _by_qualname(fm.symbols)
    assert syms["jobs"].kind == "variable"
    assert syms["jobs"].exported is True
    assert syms["CONFIG"].kind == "variable"
    assert syms["CONFIG"].exported is False
    # Arrow-function values are already covered by the dedicated
    # function-definition pattern; the broad variable-export pattern
    # must not add a duplicate "variable" symbol for the same node.
    assert syms["build"].kind == "function"
    assert sum(1 for s in fm.symbols if s.name == "build") == 1


def test_javascript_variable_export_indexed_as_symbol(
    tmp_path: Path,
) -> None:
    spec = languages.spec_for_path("data.js")
    assert spec is not None
    (tmp_path / "data.js").write_text(
        "export const jobs = [1, 2, 3];\n"
        "export const run = () => jobs.length;\n"
    )
    fm = extract_file(tmp_path, "data.js", spec)
    syms = _by_qualname(fm.symbols)
    assert syms["jobs"].kind == "variable"
    assert syms["run"].kind == "function"
    assert sum(1 for s in fm.symbols if s.name == "run") == 1


def test_typescript_template_substitution_captured_as_ref(
    tmp_path: Path,
) -> None:
    """A ``${...}`` template-literal identifier is a bare reference.

    Regression test for the false-positive-unused gap where a
    module-level ``const`` used only inside a template-literal
    substitution (e.g. ANSI color constants like
    ``` `${RED}x${NC}` ```) was invisible to ``dekko unused`` because
    ``_JS_REFERENCE_BASE`` had no pattern for
    ``template_substitution`` nodes.
    """
    spec = languages.spec_for_path("data.ts")
    assert spec is not None
    (tmp_path / "data.ts").write_text(
        'const RED = "\\x1b[31m";\n'
        'const NC = "\\x1b[0m";\n'
        "export const shout = () => `${RED}x${NC}`;\n"
    )
    fm = extract_file(tmp_path, "data.ts", spec)
    ref_names = {ref.name for ref in fm.refs}
    assert "RED" in ref_names
    assert "NC" in ref_names


def test_javascript_template_substitution_captured_as_ref(
    tmp_path: Path,
) -> None:
    """Same template-literal capture, JS grammar (JSX-inclusive query)."""
    spec = languages.spec_for_path("data.js")
    assert spec is not None
    (tmp_path / "data.js").write_text(
        'const RED = "\\x1b[31m";\nexport const shout = () => `${RED}x`;\n'
    )
    fm = extract_file(tmp_path, "data.js", spec)
    ref_names = {ref.name for ref in fm.refs}
    assert "RED" in ref_names


def test_go_struct_type_positions_captured_as_refs(tmp_path: Path) -> None:
    """Track G / bug #1.1a: Go struct types used only as *types*.

    A struct referenced solely as a parameter type, an unnamed pointer
    return type, a ``var`` declaration's type, and a composite-literal
    type — never itself constructed via a call-shaped site — must
    still surface as a ``@ref`` so ``unused.py`` doesn't flag it as
    dead code.
    """
    spec = languages.spec_for_path("data.go")
    assert spec is not None
    (tmp_path / "data.go").write_text(
        "package main\n\n"
        "type prEvent struct {\n"
        "	Name string\n"
        "}\n\n"
        "type entry struct {\n"
        "	ID int\n"
        "}\n\n"
        "func process(e prEvent) *entry {\n"
        "	var x entry\n"
        "	return &x\n"
        "}\n\n"
        "func main() {\n"
        '	process(prEvent{Name: "a"})\n'
        "}\n"
    )
    fm = extract_file(tmp_path, "data.go", spec)
    ref_names = {ref.name for ref in fm.refs}
    assert "prEvent" in ref_names
    assert "entry" in ref_names


def test_go_struct_field_type_captured_as_ref(tmp_path: Path) -> None:
    """Follow-up to Track G / bug #1.1a: a struct field's own type.

    A struct used only as another struct's field type (``Meta
    RepoMeta``), and a struct embedded anonymously (no separate field
    name), were both outside the original ``_GO_REFERENCE_QUERY``'s
    coverage — the query had no ``field_declaration type:`` pattern.
    Both must now surface as a ``@ref``.
    """
    spec = languages.spec_for_path("data.go")
    assert spec is not None
    (tmp_path / "data.go").write_text(
        "package main\n\n"
        "type RepoMeta struct {\n"
        "	Name string\n"
        "}\n\n"
        "type Embedded struct {\n"
        "	Kind string\n"
        "}\n\n"
        "type Wrapper struct {\n"
        "	Meta RepoMeta\n"
        "	Embedded\n"
        "}\n"
    )
    fm = extract_file(tmp_path, "data.go", spec)
    ref_names = {ref.name for ref in fm.refs}
    assert "RepoMeta" in ref_names
    assert "Embedded" in ref_names


def test_cpp_include_derives_header_stem_as_import_name(
    tmp_path: Path,
) -> None:
    """C++ import-hint fix (1.5-remainder, part 1).

    The generic import fallback derived a C/C++ ``#include``'s "local
    name" by splitting the include path on ``[./:]`` and keeping the
    *last* segment — for ``#include "a/b/rewrite_utils.h"`` that's the
    literal string ``"h"`` (the file extension), never usable and
    colliding across nearly every include in a file. ``_imports_cpp``
    derives the header's own stem instead.
    """
    spec = languages.spec_for_path("caller.cc")
    assert spec is not None
    (tmp_path / "caller.cc").write_text(
        '#include "tensorflow/core/data/rewrite_utils.h"\n#include <vector>\n'
    )
    fm = extract_file(tmp_path, "caller.cc", spec)
    names = {imp.name for imp in fm.imports}
    sources = {imp.source for imp in fm.imports}
    assert "rewrite_utils" in names
    assert "h" not in names
    assert "tensorflow/core/data/rewrite_utils.h" in sources


def test_tsx_jsx_tag_name_captured_as_ref(tmp_path: Path) -> None:
    """Track G / bug #1.1b: a TSX component used only as ``<Foo />``.

    ``_JS_REFERENCE_QUERY`` already captured JSX *attribute* expression
    values (``onClick={handleClick}``); it must also capture the JSX
    element tag name itself, since a component rendered only via
    ``<Sidebar />`` (never called as a plain function) was previously
    invisible to the reference pipeline.
    """
    spec = languages.spec_for_path("data.tsx")
    assert spec is not None
    (tmp_path / "data.tsx").write_text(
        "function Sidebar() { return null; }\n"
        "function App() { return <div><Sidebar /></div>; }\n"
    )
    fm = extract_file(tmp_path, "data.tsx", spec)
    ref_names = {ref.name for ref in fm.refs}
    assert "Sidebar" in ref_names


def test_ts_object_literal_shorthand_captured_as_ref(
    tmp_path: Path,
) -> None:
    """Object-literal shorthand (a genuine value read) is captured.

    ``{ helper }`` as an object-literal value is a real reference to
    whatever ``helper`` currently resolves to — must still surface as
    a ``@ref``, matching the pre-existing (unscoped) behavior.
    """
    spec = languages.spec_for_path("data.ts")
    assert spec is not None
    (tmp_path / "data.ts").write_text(
        "function helper() {}\nconst bundle = { helper };\n"
    )
    fm = extract_file(tmp_path, "data.ts", spec)
    ref_names = {ref.name for ref in fm.refs}
    assert "helper" in ref_names


def test_ts_destructured_const_shorthand_not_captured_as_ref(
    tmp_path: Path,
) -> None:
    """A destructured local binding is not a reference to anything.

    ``const { helper } = source`` *introduces* a new local name
    ``helper``; it is not a read of any existing ``helper`` binding
    (repo-wide or otherwise). tree-sitter-javascript already gives
    this shape a distinct node type
    (``shorthand_property_identifier_pattern``, not
    ``shorthand_property_identifier``), so the ``_JS_REFERENCE_BASE``
    shorthand-property pattern must not — and, verified live against
    the pinned grammar, does not — capture it.
    """
    spec = languages.spec_for_path("data.ts")
    assert spec is not None
    (tmp_path / "data.ts").write_text(
        "function helper() {}\n"
        "const source = { helper: 1 };\n"
        "const { helper } = source;\n"
    )
    fm = extract_file(tmp_path, "data.ts", spec)
    ref_names = {ref.name for ref in fm.refs}
    assert "helper" not in ref_names


def test_ts_destructured_parameter_shorthand_not_captured_as_ref(
    tmp_path: Path,
) -> None:
    """Round-12 §3.11/§4.5's exact reported shape: a destructured
    function parameter must not be attributed as a reference.

    ``function f({ description }) {}`` declares a new parameter named
    ``description``; it must not register as a "reference" to an
    unrelated repo-wide symbol of the same name (the cline
    ``description`` false-positive report's dominant shape).
    """
    spec = languages.spec_for_path("data.ts")
    assert spec is not None
    (tmp_path / "data.ts").write_text(
        "function description() {}\n"
        "function f({ description }: { description: string }) {\n"
        "  return description;\n"
        "}\n"
    )
    fm = extract_file(tmp_path, "data.ts", spec)
    # The parameter's own declaration site must never surface as a
    # @ref. (The bare `return description;` inside the function body
    # is a separate, still-open shadowing gap — Phase B of round-12
    # §3's design, real lexical scope tracking — since no query-level
    # pattern in `_JS_REFERENCE_BASE` matches a standalone identifier
    # in `return` position at all, so it isn't asserted either way
    # here.)
    param_line = 2
    assert not any(
        ref.name == "description" and ref.line == param_line for ref in fm.refs
    )


def test_ts_array_destructuring_not_captured_as_ref(
    tmp_path: Path,
) -> None:
    """Confirms ``array`` vs. ``array_pattern`` is already handled.

    The existing ``(array (identifier) @ref)`` pattern is already
    parent-scoped, so ``const [a, b] = pair`` (an ``array_pattern``,
    not an ``array``) was never captured — verified here rather than
    assumed, per round-12 §3's own verification-plan ask.
    """
    spec = languages.spec_for_path("data.ts")
    assert spec is not None
    (tmp_path / "data.ts").write_text(
        "const pair = [1, 2];\nconst [a, b] = pair;\n"
    )
    fm = extract_file(tmp_path, "data.ts", spec)
    ref_names = {ref.name for ref in fm.refs}
    assert "a" not in ref_names
    assert "b" not in ref_names


def test_parse_rust_use() -> None:
    assert _parse_rust_use("a::b::c") == [("c", "a::b::c")]
    assert _parse_rust_use("a::b as d") == [("d", "a::b")]
    assert sorted(_parse_rust_use("a::{b, c as d}")) == [
        ("b", "a::b"),
        ("d", "a::c"),
    ]
    assert _parse_rust_use("a::*") == []
    assert ("e", "x::e") in _parse_rust_use("x::{y::{z}, e}")
