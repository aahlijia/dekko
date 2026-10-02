"""Tier-2 rows: the path language, the registry and the engine.

The engine cases run on the Python grammar, a core dependency, through
a row written for the test. That exercises the engine on every install,
not only where the optional grammar pack is present.
"""

import dataclasses
import os
import subprocess
import sys
from pathlib import Path

import pytest
from tree_sitter import Node, Parser

from dekko.core import extractor_generic, languages, tier2
from dekko.core.extractor_generic import extract_file_generic
from dekko.core.grammars import get_grammar
from dekko.core.model import FileMap
from dekko.core.tier2 import FUNCTION, Tier2Spec, walk

PY_ROW = Tier2Spec(
    functions={"function_definition": "name"},
    types={"class_definition": ("class", "name")},
    calls={"call": "function"},
)


def _root(source: str) -> Node:
    return Parser(get_grammar("python")).parse(source.encode()).root_node


def _first(root: Node, node_type: str) -> Node:
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == node_type:
            return node
        stack.extend(reversed(node.named_children))

    raise AssertionError(f"no {node_type} node")


def _extract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    row: Tier2Spec = PY_ROW,
) -> FileMap:
    monkeypatch.setattr(extractor_generic, "TIER2_SPECS", {"python": row})
    (tmp_path / "m.py").write_text(source)
    fm = extract_file_generic(tmp_path, "m.py", "python")
    assert fm.error is None
    return fm


def _symbols(fm: FileMap) -> list[tuple[str, str]]:
    return [(s.kind, s.qualname) for s in fm.symbols]


def _calls(fm: FileMap) -> list[tuple[str, str | None]]:
    return [(c.name, c.caller_id) for c in fm.calls]


# --- paths ------------------------------------------------------------


def _text(node: Node | None) -> str | None:
    return None if node is None else (node.text or b"").decode()


def test_path_steps_by_field_type_and_position() -> None:
    fn = _first(
        _root("def area(r, k):\n    return r\n"), "function_definition"
    )
    assert _text(walk(fn, "name")) == "area"
    assert _text(walk(fn, ":identifier")) == "area"
    assert _text(walk(fn, "parameters>#0")) == "r"
    assert _text(walk(fn, "parameters>#-1")) == "k"
    assert walk(fn, "parameters>#2") is None
    assert walk(fn, "no_such_field") is None


def test_path_steps_to_parent_and_previous_sibling() -> None:
    root = _root("def a():\n    pass\n\ndef b():\n    pass\n")
    second = root.named_children[1]
    assert _text(walk(second, "<>name")) == "a"
    assert walk(second, "name>^").id == second.id
    assert walk(root.named_children[0], "<") is None


def test_path_type_assertion_rejects_a_step_that_lands_elsewhere() -> None:
    fn = _first(_root("def area(r):\n    return r\n"), "function_definition")
    assert _text(walk(fn, "name=identifier")) == "area"
    assert _text(walk(fn, "name=string|identifier")) == "area"
    assert walk(fn, "name=string") is None


def test_path_alternatives_are_tried_in_order() -> None:
    fn = _first(_root("def area(r):\n    return r\n"), "function_definition")
    assert _text(walk(fn, "return_type,name")) == "area"
    assert _text(walk(fn, "name,parameters")) == "area"
    assert walk(fn, "return_type,no_such_field") is None


def test_path_condition_takes_the_path_only_when_the_probe_reads_a_word() -> (
    None
):
    fn = _first(_root("def area(r):\n    return r\n"), "function_definition")
    assert _text(walk(fn, "name?parameters>#0~r|x")) == "area"
    assert walk(fn, "name?parameters>#0~y") is None
    # A failed condition falls through to the next alternative.
    assert _text(walk(fn, "name?parameters>#0~y,parameters>#0")) == "r"


# --- the registry -----------------------------------------------------


def test_every_tier2_grammar_has_a_row_and_every_row_a_grammar() -> None:
    assert set(languages.TIER2_GRAMMARS.values()) == set(tier2.TIER2_SPECS)


@pytest.mark.parametrize(
    ("name", "language"),
    [("App.vue", "vue"), ("App.svelte", "svelte"), ("main.mojo", "mojo")],
)
def test_languages_no_row_can_read_are_disclosed_not_indexed(
    name: str, language: str
) -> None:
    # A grammar that hands back a component's script as raw text, or
    # fails on most real files, yields an empty file that looks
    # supported. A disclosed gap is the accurate answer.
    assert languages.tier2_grammar_for_path(name) is None
    assert not languages.is_supported(name)
    assert languages.known_unsupported_language(name) == language


def test_ocaml_interface_files_are_not_source_files() -> None:
    # An interface restates what its ``.ml`` defines. It is neither
    # indexed nor reported as a gap.
    assert not languages.is_supported("lib/cohttp.mli")
    assert languages.known_unsupported_language("lib/cohttp.mli") is None
    assert languages.tier2_grammar_for_path("lib/cohttp.ml") == "ocaml"


def test_named_node_types_and_fields_cover_keys_and_paths() -> None:
    row = Tier2Spec(
        functions={"fn_def": ":header>name=identifier|symbol"},
        types={"type_def": ("class", "*:entry>label")},
        bound={"assign": (FUNCTION, "left", "#0", ("lambda",))},
        guard={"fn_def": "^=module"},
        calls={"call": "", "apply": "<=word"},
        body_after={"fn_def": "block"},
        curried=frozenset({"apply"}),
        opaque=frozenset({"quote"}),
        skip_paths={"fn_def": ("params",)},
        form=tier2.FormSpec(
            nodes=("list",),
            functions={"define": ("#1=sym?#2=list>#0=sym~lambda",)},
            types={"struct": ("struct", "=fixed")},
            params=("braces",),
        ),
    )
    assert tier2.named_node_types(row) == {
        "fn_def", "header", "identifier", "symbol", "type_def", "entry",
        "assign", "lambda", "module", "call", "apply", "word", "block",
        "quote", "list", "sym", "braces",
    }  # fmt: skip
    assert tier2.named_fields(row) == {"name", "label", "left", "params"}


# --- the fingerprint --------------------------------------------------


def test_canonical_orders_sets_and_dicts() -> None:
    row = Tier2Spec(
        functions={"b": "name", "a": "name"},
        opaque=frozenset({"z", "a", "m"}),
    )
    flat = dict(tier2.canonical(row))
    assert flat["functions"] == (("a", "name"), ("b", "name"))
    assert flat["opaque"] == ("a", "m", "z")


def test_spec_fingerprint_changes_when_a_row_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A row is extraction logic. Editing one has to discard cached
    # extractions the same way editing a Tier-1 query does.
    baseline = languages.spec_fingerprint()
    ruby = languages.TIER2_SPECS["ruby"]
    edited = dataclasses.replace(ruby, calls={**ruby.calls, "yield": ""})
    monkeypatch.setitem(languages.TIER2_SPECS, "ruby", edited)
    assert languages.spec_fingerprint() != baseline


def test_spec_fingerprint_changes_with_the_tier2_engine_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = languages.spec_fingerprint()
    monkeypatch.setattr(languages, "_TIER2_ENGINE_VERSION", 999)
    assert languages.spec_fingerprint() != baseline


def test_spec_fingerprint_is_the_same_in_every_process() -> None:
    # Rows hold frozensets of strings, which iterate in an order that
    # changes with the hash seed. A fingerprint that followed it would
    # differ on every run and throw the extraction cache away.
    code = "from dekko.core import languages as l; print(l.spec_fingerprint())"
    seen = set()
    for seed in ("1", "2", "3"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        done = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        seen.add(done.stdout.strip())

    assert seen == {languages.spec_fingerprint()}


# --- the engine -------------------------------------------------------

SOURCE = """\
class Store:
    def put(self, key):
        return normalize(key)

def normalize(key):
    return key.strip()

normalize("a")
"""


def test_a_grammar_without_a_row_is_an_error_not_an_empty_file(
    tmp_path: Path,
) -> None:
    (tmp_path / "App.vue").write_text("<script>function f() {}</script>\n")
    fm = extract_file_generic(tmp_path, "App.vue", "vue")
    assert fm.symbols == []
    assert fm.error == "no Tier-2 row for grammar 'vue'"


def test_engine_qualifies_a_function_by_its_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fm = _extract(tmp_path, monkeypatch, SOURCE)
    assert _symbols(fm) == [
        ("class", "Store"),
        ("method", "Store.put"),
        ("function", "normalize"),
    ]
    put = fm.symbols[1]
    assert (put.id, put.start_line, put.end_line) == ("m.py::Store.put", 2, 3)
    assert [p.name for p in put.params] == ["self", "key"]


def test_engine_attributes_a_call_to_the_innermost_symbol_around_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fm = _extract(tmp_path, monkeypatch, SOURCE)
    assert _calls(fm) == [
        ("normalize", "m.py::Store.put"),
        ("strip", "m.py::normalize"),
        ("normalize", None),
    ]
    strip = fm.calls[1]
    assert (strip.text, strip.receiver, strip.line) == ("key.strip", "key", 6)


def test_engine_reads_nothing_the_row_does_not_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No rule is inferred from how a node type is spelled: a row with
    # only a call rule finds no definition.
    row = Tier2Spec(calls={"call": "function"})
    fm = _extract(tmp_path, monkeypatch, SOURCE, row)
    assert fm.symbols == []
    assert [c.name for c in fm.calls] == ["normalize", "strip", "normalize"]


def test_engine_drops_a_callee_that_is_an_expression(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No symbol is named ``handlers[0]`` or ``(a or b)``. Kept, they
    # are ``external`` rows nothing can ever match.
    source = "handlers[0](1)\n(a or b)(2)\nrun(3)\n"
    fm = _extract(tmp_path, monkeypatch, source)
    assert [c.name for c in fm.calls] == ["run"]


def test_engine_keeps_a_callee_whole_when_the_row_does_not_split(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = dataclasses.replace(PY_ROW, call_split=None)
    fm = _extract(tmp_path, monkeypatch, "pkg.run(1)\n", row)
    call = fm.calls[0]
    assert (call.name, call.receiver) == ("pkg.run", None)


def test_engine_guard_must_resolve_for_a_node_to_be_a_definition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = dataclasses.replace(
        PY_ROW, guard={"function_definition": "return_type"}
    )
    source = (
        "def typed() -> int:\n    return 1\n\ndef plain():\n    return 2\n"
    )
    fm = _extract(tmp_path, monkeypatch, source, row)
    assert _symbols(fm) == [("function", "typed")]


def test_engine_bound_rule_names_a_function_by_its_assignment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = Tier2Spec(
        bound={"assignment": (FUNCTION, "left", "right", ("lambda",))},
        calls={"call": "function"},
    )
    source = "double = lambda x: twice(x)\nlimit = 3\n"
    fm = _extract(tmp_path, monkeypatch, source, row)
    assert _symbols(fm) == [("function", "double")]
    assert _calls(fm) == [("twice", "m.py::double")]


def test_engine_scope_qualifies_without_becoming_a_symbol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = Tier2Spec(
        functions={"function_definition": "name"},
        scopes={"class_definition": "name"},
    )
    fm = _extract(tmp_path, monkeypatch, SOURCE, row)
    assert _symbols(fm) == [("method", "Store.put"), ("function", "normalize")]


def test_engine_merges_consecutive_clauses_of_one_function(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = dataclasses.replace(PY_ROW, merge_clauses=True)
    source = (
        "def area(a):\n    return one(a)\n"
        "def area(b):\n    return two(b)\n"
        "def other():\n    pass\n"
        "def area(c):\n    pass\n"
    )
    fm = _extract(tmp_path, monkeypatch, source, row)
    # The first two are one function spanning both. The third, after
    # another symbol, is a separate definition of the same name.
    assert [(s.id, s.start_line, s.end_line) for s in fm.symbols] == [
        ("m.py::area", 1, 4),
        ("m.py::other", 5, 6),
        ("m.py::area#2", 7, 8),
    ]
    assert _calls(fm) == [("one", "m.py::area"), ("two", "m.py::area")]


def test_engine_numbers_repeated_definitions_without_merging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = "def area(a):\n    pass\ndef area(b):\n    pass\n"
    fm = _extract(tmp_path, monkeypatch, source)
    assert [s.id for s in fm.symbols] == ["m.py::area", "m.py::area#2"]


def test_engine_fixed_name_and_skipped_parameters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = Tier2Spec(
        functions={"lambda": "=anonymous"},
        calls={"call": "function"},
        skip_paths={"lambda": ("parameters",)},
    )
    source = "f = lambda x=default(): body(x)\n"
    fm = _extract(tmp_path, monkeypatch, source, row)
    assert _symbols(fm) == [("function", "anonymous")]
    # ``default()`` sits in the parameter list and is not descended
    # into.
    assert [c.name for c in fm.calls] == ["body"]


def test_engine_strips_sigils_and_quotes_the_row_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = Tier2Spec(
        functions={"function_definition": "name"},
        calls={"call": "function"},
        sigils="_",
    )
    fm = _extract(tmp_path, monkeypatch, "__run(1)\n", row)
    assert [c.name for c in fm.calls] == ["run"]
