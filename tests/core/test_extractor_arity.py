"""Round 25 structural layer 2, Phases 0-1
(`.features/plans/round25/06-structural-layer2-arity-resolution.md`):
``Param.has_default``/``Param.variadic`` per language parser, and
``RawCall.arg_count`` threaded through each Tier-1 language's
``call_query``. Phase 2's arity-gating logic itself is covered in
``tests/core/test_resolver.py``.
"""

from pathlib import Path

from tree_sitter import Node, Parser

from dekko.core import languages
from dekko.core.extractor import (
    _params_c,
    _params_generic,
    _params_go,
    _params_js,
    _params_python,
    _params_rust,
    _params_ts,
    extract_file,
)
from dekko.core.grammars import get_grammar

FIXTURES = Path(__file__).parent.parent / "fixtures"


def _find(node: Node, node_type: str) -> Node | None:
    if node.type == node_type:
        return node
    for child in node.children:
        found = _find(child, node_type)
        if found is not None:
            return found
    return None


def _params_node(grammar: str, code: str, wrapper_type: str) -> Node:
    parser = Parser(get_grammar(grammar))
    tree = parser.parse(code.encode())
    node = _find(tree.root_node, wrapper_type)
    assert node is not None, f"no {wrapper_type!r} node in: {code!r}"
    return node


# ---------------------------------------------------------------------
# Phase 0: Param.has_default / Param.variadic per language.


def test_python_default_and_variadic_params_flagged() -> None:
    node = _params_node(
        "python", "def f(a, b=1, *args, **kw): pass\n", "parameters"
    )
    params = _params_python(node)
    assert [(p.name, p.has_default, p.variadic) for p in params] == [
        ("a", False, False),
        ("b", True, False),
        ("*args", False, True),
        ("**kw", False, True),
    ]


def test_python_typed_default_param_flagged() -> None:
    node = _params_node(
        "python", "def f(a: int, b: int = 1): pass\n", "parameters"
    )
    params = _params_python(node)
    assert [(p.name, p.type, p.has_default) for p in params] == [
        ("a", "int", False),
        ("b", "int", True),
    ]


def test_python_keyword_only_separator_not_flagged_as_a_real_param() -> None:
    node = _params_node("python", "def f(a, *, b, c=2): pass\n", "parameters")
    params = _params_python(node)
    assert [(p.name, p.has_default, p.variadic) for p in params] == [
        ("a", False, False),
        ("*", False, False),
        ("b", False, False),
        ("c", True, False),
    ]


def test_rust_variadic_parameter_flagged() -> None:
    # Only reachable via an extern-block foreign function signature
    # (``function_signature_item``) in the pinned tree-sitter-rust
    # grammar -- never a plain ``fn`` -- so this parses the
    # ``parameters`` node directly rather than through a definition
    # query pattern that only matches ``function_item``.
    node = _params_node(
        "rust", 'extern "C" {\n    fn f(a: i32, ...);\n}\n', "parameters"
    )
    params = _params_rust(node)
    assert [(p.name, p.variadic) for p in params] == [
        ("a", False),
        ("...", True),
    ]


def test_c_variadic_parameter_flagged() -> None:
    node = _params_node("c", "void f(int a, ...);\n", "parameter_list")
    params = _params_c(node)
    assert [(p.name, p.variadic) for p in params] == [
        ("a", False),
        ("...", True),
    ]


def test_cpp_default_parameter_flagged() -> None:
    node = _params_node("cpp", "void f(int a, int b = 5);\n", "parameter_list")
    params = _params_c(node)
    assert [(p.name, p.has_default) for p in params] == [
        ("a", False),
        ("b", True),
    ]


def test_js_default_and_rest_params_flagged() -> None:
    node = _params_node(
        "javascript",
        "function f(a, b = 1, ...rest) {}\n",
        "formal_parameters",
    )
    params = _params_js(node)
    assert [(p.name, p.has_default, p.variadic) for p in params] == [
        ("a", False, False),
        ("b", True, False),
        ("...rest", False, True),
    ]


def test_ts_optional_defaulted_and_rest_params_flagged() -> None:
    node = _params_node(
        "typescript",
        "function f(a: number, b: number = 1, c?: number, "
        "...rest: number[]) {}\n",
        "formal_parameters",
    )
    params = _params_ts(node)
    assert [(p.name, p.has_default, p.variadic) for p in params] == [
        ("a", False, False),
        ("b", True, False),
        ("c?", True, False),
        ("...rest", False, True),
    ]


def test_go_variadic_parameter_flagged() -> None:
    node = _params_node(
        "go", "package m\nfunc f(a int, b ...int) {}\n", "parameter_list"
    )
    params = _params_go(node)
    assert [(p.name, p.variadic) for p in params] == [
        ("a", False),
        ("b", True),
    ]


def test_java_varargs_parameter_flagged_via_generic_parser() -> None:
    # Java (param_style="generic") is the one Tier-1 language
    # dispatched through the otherwise Tier-2-only ``_params_generic``.
    node = _params_node(
        "java",
        "class C { void m(int a, String... args) {} }\n",
        "formal_parameters",
    )
    params = _params_generic(node)
    assert params[0].name == "a"
    assert params[0].variadic is False
    assert params[1].variadic is True


# ---------------------------------------------------------------------
# Phase 1: RawCall.arg_count per language.


def test_python_call_arg_count(tmp_path: Path) -> None:
    spec = languages.spec_for_path("m.py")
    assert spec is not None
    (tmp_path / "m.py").write_text("def run():\n    foo(1, 2, 3)\n    bar()\n")
    fm = extract_file(tmp_path, "m.py", spec)
    counts = {c.name: c.arg_count for c in fm.calls}
    assert counts["foo"] == 3
    assert counts["bar"] == 0


def test_rust_call_arg_count(tmp_path: Path) -> None:
    spec = languages.spec_for_path("m.rs")
    assert spec is not None
    (tmp_path / "m.rs").write_text("fn run() {\n    foo(1, 2);\n}\n")
    fm = extract_file(tmp_path, "m.rs", spec)
    counts = {c.name: c.arg_count for c in fm.calls}
    assert counts["foo"] == 2


def test_c_call_arg_count(tmp_path: Path) -> None:
    spec = languages.spec_for_path("m.c")
    assert spec is not None
    (tmp_path / "m.c").write_text("void run() {\n    foo(1, 2, 3);\n}\n")
    fm = extract_file(tmp_path, "m.c", spec)
    counts = {c.name: c.arg_count for c in fm.calls}
    assert counts["foo"] == 3


def test_cpp_call_arg_count(tmp_path: Path) -> None:
    spec = languages.spec_for_path("m.cpp")
    assert spec is not None
    (tmp_path / "m.cpp").write_text("void run() {\n    foo(1, 2);\n}\n")
    fm = extract_file(tmp_path, "m.cpp", spec)
    counts = {c.name: c.arg_count for c in fm.calls}
    assert counts["foo"] == 2


def test_js_call_and_new_expression_arg_count(tmp_path: Path) -> None:
    spec = languages.spec_for_path("m.js")
    assert spec is not None
    (tmp_path / "m.js").write_text(
        "function run() {\n  foo(1, 2);\n  new Bar(1);\n  new Baz;\n}\n"
    )
    fm = extract_file(tmp_path, "m.js", spec)
    counts = {c.name: c.arg_count for c in fm.calls}
    assert counts["foo"] == 2
    assert counts["Bar"] == 1
    # Paren-less `new Baz` has no arguments node at all -- `None`
    # ("no signal"), never coerced to 0.
    assert counts["Baz"] is None


def test_ts_call_and_new_expression_arg_count(tmp_path: Path) -> None:
    spec = languages.spec_for_path("m.ts")
    assert spec is not None
    (tmp_path / "m.ts").write_text(
        "function run(): void {\n  foo(1, 2, 3);\n  new Bar();\n}\n"
    )
    fm = extract_file(tmp_path, "m.ts", spec)
    counts = {c.name: c.arg_count for c in fm.calls}
    assert counts["foo"] == 3
    assert counts["Bar"] == 0


def test_go_call_arg_count(tmp_path: Path) -> None:
    spec = languages.spec_for_path("m.go")
    assert spec is not None
    (tmp_path / "m.go").write_text(
        "package main\n\nfunc run() {\n\tfoo(1, 2)\n}\n"
    )
    fm = extract_file(tmp_path, "m.go", spec)
    counts = {c.name: c.arg_count for c in fm.calls}
    assert counts["foo"] == 2


def test_java_call_and_constructor_arg_count(tmp_path: Path) -> None:
    spec = languages.spec_for_path("M.java")
    assert spec is not None
    (tmp_path / "M.java").write_text(
        "class M {\n"
        "  void run() {\n"
        "    foo(1, 2, 3);\n"
        "    bar();\n"
        "    new Baz(1);\n"
        "  }\n"
        "}\n"
    )
    fm = extract_file(tmp_path, "M.java", spec)
    counts = {c.name: c.arg_count for c in fm.calls}
    assert counts["foo"] == 3
    assert counts["bar"] == 0
    assert counts["Baz"] == 1
