"""Every Tier-2 row, against its grammar and a fixture in its language.

These need the optional grammar pack (``dekko[all]``) and skip without
it. One CI leg installs the pack so they run there.
"""

import importlib.util
import json
from pathlib import Path

import pytest
from tree_sitter import Language

from dekko.core import languages, tier2
from dekko.core.extractor_generic import extract_file_generic
from dekko.core.grammars import get_grammar
from dekko.core.model import FileMap

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("tree_sitter_language_pack") is None,
    reason="Tier-2 grammar pack not installed (dekko[all])",
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "tier2"
EXPECTED = json.loads((FIXTURES / "expected.json").read_text())
GRAMMARS = sorted(tier2.TIER2_SPECS)


def _extract(tmp_path: Path, name: str, source: str) -> FileMap:
    grammar = languages.tier2_grammar_for_path(name)
    assert grammar is not None
    (tmp_path / name).write_text(source)
    fm = extract_file_generic(tmp_path, name, grammar)
    assert fm.error is None
    return fm


def _names(fm: FileMap) -> list[tuple[str, str]]:
    return [(s.kind, s.qualname) for s in fm.symbols]


def _calls(fm: FileMap) -> list[tuple[str, str | None]]:
    """``(callee name, caller qualname)`` for every call."""
    by_id = {s.id: s.qualname for s in fm.symbols}
    return [(c.name, by_id.get(c.caller_id)) for c in fm.calls]


# --- one fixture per row ----------------------------------------------


def test_every_row_has_a_fixture_and_every_fixture_a_row() -> None:
    assert sorted(EXPECTED) == GRAMMARS
    on_disk = {p.name for p in FIXTURES.iterdir()} - {"expected.json"}
    assert on_disk == {EXPECTED[g]["file"] for g in GRAMMARS}
    for grammar in GRAMMARS:
        name = EXPECTED[grammar]["file"]
        assert languages.tier2_grammar_for_path(name) == grammar


@pytest.mark.parametrize("grammar", GRAMMARS)
def test_row_reads_its_fixture(grammar: str) -> None:
    # Each fixture holds a function, a type, a method, a call inside a
    # body and a call at module level, as far as the language has them.
    expected = EXPECTED[grammar]
    fm = extract_file_generic(FIXTURES, expected["file"], grammar)
    assert fm.error is None
    by_id = {s.id: s.qualname for s in fm.symbols}
    symbols = [
        [s.kind, s.qualname, s.start_line, s.end_line] for s in fm.symbols
    ]
    calls = [[c.name, by_id.get(c.caller_id), c.line] for c in fm.calls]
    assert symbols == expected["symbols"]
    assert calls == expected["calls"]


def _node_kinds(language: Language) -> set[str]:
    return {
        language.node_kind_for_id(i) for i in range(language.node_kind_count)
    }


def _field_names(language: Language) -> set[str | None]:
    # Field ids start at 1.
    return {
        language.field_name_for_id(i)
        for i in range(1, language.field_count + 1)
    }


@pytest.mark.parametrize("grammar", GRAMMARS)
def test_row_names_only_node_types_and_fields_its_grammar_has(
    grammar: str,
) -> None:
    # A grammar update that renames a node type turns a rule into dead
    # weight without an error anywhere. This is where it goes red.
    row = tier2.TIER2_SPECS[grammar]
    language = get_grammar(grammar)
    assert tier2.named_node_types(row) - _node_kinds(language) == set()
    assert tier2.named_fields(row) - _field_names(language) == set()


# --- symbols that must not exist --------------------------------------


def test_php_parameter_and_namespace_are_not_symbols(tmp_path: Path) -> None:
    source = (
        "<?php\nnamespace App;\n\n"
        "class App {\n"
        "    public function __construct(Factory $factory, $container) {}\n"
        "}\n"
    )
    fm = _extract(tmp_path, "App.php", source)
    assert _names(fm) == [("class", "App"), ("method", "App.__construct")]


def test_haxe_argument_is_not_a_function(tmp_path: Path) -> None:
    source = (
        "class Basic {\n\tpublic function update(elapsed:Float):Void {}\n}\n"
    )
    fm = _extract(tmp_path, "Basic.hx", source)
    assert _names(fm) == [("class", "Basic"), ("method", "Basic.update")]


def test_gleam_parameter_is_not_a_function(tmp_path: Path) -> None:
    source = "pub fn add(left: Int, right: Int) -> Int {\n  left + right\n}\n"
    fm = _extract(tmp_path, "math.gleam", source)
    assert _names(fm) == [("function", "add")]


def test_scala_class_parameter_is_not_a_class(tmp_path: Path) -> None:
    source = "class Circle(radius: Double) {\n  def area: Double = radius\n}\n"
    fm = _extract(tmp_path, "Circle.scala", source)
    assert _names(fm) == [("class", "Circle"), ("method", "Circle.area")]


def test_ada_variable_is_not_a_class(tmp_path: Path) -> None:
    source = (
        "package body Shapes is\n"
        "   Count : Integer := 0;\n"
        "   procedure Bump is\n"
        "   begin\n"
        "      Count := Count + 1;\n"
        "   end Bump;\n"
        "end Shapes;\n"
    )
    fm = _extract(tmp_path, "shapes.adb", source)
    assert _names(fm) == [("class", "Shapes"), ("method", "Shapes.Bump")]


def test_csharp_constructor_is_a_method_not_a_struct(tmp_path: Path) -> None:
    source = "class Circle {\n    public Circle(double r) {}\n}\n"
    fm = _extract(tmp_path, "Circle.cs", source)
    assert _names(fm) == [("class", "Circle"), ("method", "Circle.Circle")]


def test_sql_table_is_its_create_statement_only(tmp_path: Path) -> None:
    # Every statement that mentions a table is not another definition
    # of it, and the quotes are not part of its name.
    source = (
        'CREATE TABLE "users" (id INT);\n'
        "CREATE INDEX idx ON users (id);\n"
        "ALTER TABLE users ADD COLUMN name TEXT;\n"
        "INSERT INTO users (id) VALUES (1);\n"
    )
    fm = _extract(tmp_path, "schema.sql", source)
    assert _names(fm) == [("class", "users")]


def test_erlang_module_reference_is_not_a_class(tmp_path: Path) -> None:
    source = "-module(shapes).\n\narea(R) ->\n    math:pi() * R.\n"
    fm = _extract(tmp_path, "shapes.erl", source)
    assert _names(fm) == [("class", "shapes"), ("function", "area")]


# --- names ------------------------------------------------------------


def test_r_function_is_named_by_its_assignment(tmp_path: Path) -> None:
    source = "pad <- function(string, width) {\n  string\n}\n"
    fm = _extract(tmp_path, "pad.r", source)
    assert _names(fm) == [("function", "pad")]


def test_erlang_clauses_of_one_function_are_one_symbol(tmp_path: Path) -> None:
    source = (
        "area({circle, R}) ->\n    R;\n"
        "area({square, S}) ->\n    S;\n"
        "area(_) ->\n    0.\n"
    )
    fm = _extract(tmp_path, "shapes.erl", source)
    assert [(s.id, s.start_line, s.end_line) for s in fm.symbols] == [
        ("shapes.erl::area", 1, 6)
    ]


def test_solidity_function_is_qualified_by_its_contract(
    tmp_path: Path,
) -> None:
    source = (
        "contract Token {\n    function transfer(address to) external {}\n}\n"
    )
    fm = _extract(tmp_path, "Token.sol", source)
    assert _names(fm) == [("class", "Token"), ("method", "Token.transfer")]


def test_lua_function_bound_to_a_field_is_found(tmp_path: Path) -> None:
    source = "local M = {}\nM.run = function(a)\n  return a\nend\nreturn M\n"
    fm = _extract(tmp_path, "m.lua", source)
    assert _names(fm) == [("method", "M.run")]


def test_ocaml_value_is_not_a_function(tmp_path: Path) -> None:
    source = "let limit = 3\nlet double x = x * 2\nlet apply = fun f -> f 1\n"
    fm = _extract(tmp_path, "m.ml", source)
    assert _names(fm) == [("function", "double"), ("function", "apply")]


def test_fortran_symbol_ends_on_its_end_statement(tmp_path: Path) -> None:
    # The grammar ends the node on the newline after ``end function``,
    # one line too far.
    source = (
        "real function square(x)\n"
        "  real, intent(in) :: x\n"
        "  square = x * x\n"
        "end function square\n"
        "\n"
        "subroutine other()\n"
        "end subroutine other\n"
    )
    fm = _extract(tmp_path, "m.f90", source)
    assert [(s.name, s.start_line, s.end_line) for s in fm.symbols] == [
        ("square", 1, 4),
        ("other", 6, 7),
    ]


# --- calls ------------------------------------------------------------


def test_vim_call_in_a_function_body_has_that_function_as_caller(
    tmp_path: Path,
) -> None:
    # The function's header line is a node of its own. A symbol spanning
    # only that would own none of the body's calls.
    source = (
        "function! s:Area(r) abort\n  return shapes#Square(a:r)\nendfunction\n"
    )
    fm = _extract(tmp_path, "shapes.vim", source)
    assert _calls(fm) == [("shapes#Square", "s.Area")]


def test_dart_call_in_a_method_body_has_that_method_as_caller(
    tmp_path: Path,
) -> None:
    # The grammar puts the body next to the signature, not inside it.
    source = (
        "class Circle {\n"
        "  double area() {\n"
        "    return square(radius);\n"
        "  }\n"
        "}\n"
    )
    fm = _extract(tmp_path, "circle.dart", source)
    assert _calls(fm) == [("square", "Circle.area")]


def test_elixir_def_is_a_definition_not_a_call(tmp_path: Path) -> None:
    source = (
        "defmodule Shapes do\n"
        "  def area(r), do: square(r)\n"
        "  defp square(x), do: x * x\n"
        "end\n"
    )
    fm = _extract(tmp_path, "shapes.ex", source)
    assert _names(fm) == [
        ("class", "Shapes"),
        ("method", "Shapes.area"),
        ("method", "Shapes.square"),
    ]
    assert _calls(fm) == [("square", "Shapes.area")]


def test_julia_signature_is_not_a_call_to_the_function_it_defines(
    tmp_path: Path,
) -> None:
    source = "function helper(a::Int)\n    return other(a)\nend\n"
    fm = _extract(tmp_path, "m.jl", source)
    assert _calls(fm) == [("other", "helper")]


def test_nix_curried_call_is_one_call_named_by_its_function(
    tmp_path: Path,
) -> None:
    source = '{ lib }:\n{\n  pair = lib.nameValuePair "a" 1;\n}\n'
    fm = _extract(tmp_path, "m.nix", source)
    assert [(c.name, c.receiver) for c in fm.calls] == [
        ("nameValuePair", "lib")
    ]


def test_fortran_member_call_is_named_by_its_last_part(tmp_path: Path) -> None:
    source = (
        "subroutine run(json)\n"
        "  call json%get('a', value)\n"
        "end subroutine run\n"
    )
    fm = _extract(tmp_path, "m.f90", source)
    assert [c.name for c in fm.calls] == ["get"]


def test_perl_sigil_call_is_named_without_the_sigil(tmp_path: Path) -> None:
    source = "sub locchk { return 1; }\n&locchk(1);\n"
    fm = _extract(tmp_path, "m.pl", source)
    assert _calls(fm) == [("locchk", None)]


def test_scheme_lambda_bound_by_define_keeps_its_body_calls(
    tmp_path: Path,
) -> None:
    source = "(define run\n  (lambda (y)\n    (helper (other y))))\n"
    fm = _extract(tmp_path, "m.scm", source)
    assert _names(fm) == [("function", "run")]
    assert _calls(fm) == [("helper", "run"), ("other", "run")]


def test_scheme_arrow_in_a_name_is_part_of_the_name(tmp_path: Path) -> None:
    # A function defined as ``list->set`` is reached only by a call
    # still named ``list->set``.
    source = "(define (list->set xs) xs)\n(list->set (string->list s))\n"
    fm = _extract(tmp_path, "m.scm", source)
    assert _names(fm) == [("function", "list->set")]
    assert _calls(fm) == [("list->set", None), ("string->list", None)]


def test_lisp_binding_and_parameter_lists_are_not_calls(
    tmp_path: Path,
) -> None:
    source = (
        "(defun describe-shape (c &optional (stream t))\n"
        "  (let ((value (area c)))\n"
        '    (format stream "~a" value)))\n'
    )
    fm = _extract(tmp_path, "m.lisp", source)
    assert _calls(fm) == [
        ("area", "describe-shape"),
        ("format", "describe-shape"),
    ]


def test_lisp_type_form_fields_are_not_calls(tmp_path: Path) -> None:
    # A parent list and a slot list call nothing. A slot's default
    # value, nested below them, still does.
    source = (
        "(defclass circle (shape)\n"
        "  ((radius :initarg :radius)\n"
        "   (lock :initform (make-lock))))\n"
    )
    fm = _extract(tmp_path, "m.lisp", source)
    assert _names(fm) == [("class", "circle")]
    assert _calls(fm) == [("make-lock", "circle")]


def test_scheme_record_type_fields_are_not_calls(tmp_path: Path) -> None:
    source = (
        "(define-record-type point\n"
        "  (make-point x y)\n"
        "  point?\n"
        "  (x point-x))\n"
    )
    fm = _extract(tmp_path, "m.scm", source)
    assert _names(fm) == [("struct", "point")]
    assert fm.calls == []


def test_tcl_method_parameter_list_is_not_a_call(tmp_path: Path) -> None:
    source = (
        "oo::class create Stack {\n"
        "    method push {value args} {\n"
        "        my Store $value\n"
        "    }\n"
        "}\n"
    )
    fm = _extract(tmp_path, "stack.tcl", source)
    assert _names(fm) == [("class", "Stack"), ("method", "Stack.push")]
    assert "value" not in {c.name for c in fm.calls}


def test_tcl_absolute_namespace_call_is_kept(tmp_path: Path) -> None:
    source = (
        "namespace eval ::shapes {\n"
        "    proc area {r} { return $r }\n"
        "}\n"
        "::shapes::area 2\n"
    )
    fm = _extract(tmp_path, "m.tcl", source)
    assert _names(fm) == [("method", "shapes.area")]
    assert ("area", None) in _calls(fm)


def test_shell_function_name_with_a_separator_is_one_name(
    tmp_path: Path,
) -> None:
    # ``_omz::log`` is a plain function name in a shell. The call and
    # the definition have to agree on it.
    source = '_omz::log() {\n  echo "$@"\n}\n\n_omz::log info\n'
    fm = _extract(tmp_path, "m.sh", source)
    assert _names(fm) == [("function", "_omz::log")]
    assert ("_omz::log", None) in _calls(fm)


def test_r_dotted_name_is_one_name_and_a_package_prefix_is_not(
    tmp_path: Path,
) -> None:
    source = "check <- function(x) {\n  is.na(stringr::str_c(x))\n}\n"
    fm = _extract(tmp_path, "m.r", source)
    assert _calls(fm) == [("is.na", "check"), ("str_c", "check")]
