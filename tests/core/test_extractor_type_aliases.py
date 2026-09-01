"""Extraction tests for TS/TSX type-alias declarations — round-19
claude-code finding, bug #3, and round-26's fuller follow-up.

``type X = {...}`` object aliases were never extracted as ``Symbol``s
at all, so an ``implements``/``extends`` clause naming a same-file
alias had no structural signal to distinguish it from a genuinely
external base type (``query._heritage_external_label`` mislabeled it
``(external)``). ``LanguageSpec.type_alias_query`` (TS/TSX only) closed
that gap by giving the extractor a lightweight, file-scoped registry
of alias names — see ``FileMap.type_aliases``'s docstring.

Round 26 closes the fuller gap: ``type_alias_declaration`` now also
matches a ``@classdef`` pattern in ``_TS_DEFINITIONS``, producing a
real ``Symbol`` (``kind == "type_alias"``, in ``model.TYPE_KINDS``) so
type aliases are resolvable by ``query_symbol``, heritage, and
unused-types the same way interfaces/classes/enums already are. The
``FileMap.type_aliases`` bare-name registry above is unchanged and
still exists in parallel for ``_heritage_external_label``'s narrower
presentation fallback.
"""

from pathlib import Path

from dekko.core import languages
from dekko.core.extractor import extract_file
from dekko.core.model import Symbol


def _type_aliases(tmp_path: Path, filename: str, source: str) -> list[str]:
    spec = languages.spec_for_path(filename)
    assert spec is not None
    (tmp_path / filename).write_text(source)
    fm = extract_file(tmp_path, filename, spec)
    assert fm.error is None
    return fm.type_aliases


def _type_alias_symbols(
    tmp_path: Path, filename: str, source: str
) -> list[Symbol]:
    spec = languages.spec_for_path(filename)
    assert spec is not None
    (tmp_path / filename).write_text(source)
    fm = extract_file(tmp_path, filename, spec)
    assert fm.error is None
    return [s for s in fm.symbols if s.kind == "type_alias"]


def test_ts_object_type_alias_captured(tmp_path: Path) -> None:
    aliases = _type_aliases(
        tmp_path,
        "shell_command.ts",
        "export type ShellCommand = {\n  run(): void;\n};\n",
    )
    assert aliases == ["ShellCommand"]


def test_ts_union_type_alias_captured(tmp_path: Path) -> None:
    # Not just object-type aliases -- any type_alias_declaration shape
    # (union, primitive, etc.) is a valid same-file heritage target in
    # principle, so the query shouldn't be object-literal-specific.
    aliases = _type_aliases(
        tmp_path, "status.ts", 'type Status = "ok" | "error";\n'
    )
    assert aliases == ["Status"]


def test_tsx_type_alias_captured(tmp_path: Path) -> None:
    aliases = _type_aliases(
        tmp_path,
        "widget.tsx",
        "export type Props = {\n  label: string;\n};\n",
    )
    assert aliases == ["Props"]


def test_js_has_no_type_alias_query_and_returns_empty(
    tmp_path: Path,
) -> None:
    # Plain JS has no `type` keyword/type_alias_declaration node type
    # at all -- JAVASCRIPT.type_alias_query stays None, so this must
    # return empty rather than erroring on a query the js grammar
    # can't compile.
    aliases = _type_aliases(tmp_path, "plain.js", "export const x = 1;\n")
    assert aliases == []


def test_ts_file_with_no_type_alias_returns_empty(tmp_path: Path) -> None:
    aliases = _type_aliases(tmp_path, "empty.ts", "export class Foo {}\n")
    assert aliases == []


# --- Round 26: type_alias_declaration -> real Symbol ------------------


def test_ts_object_type_alias_produces_type_alias_symbol(
    tmp_path: Path,
) -> None:
    syms = _type_alias_symbols(
        tmp_path,
        "shell_command.ts",
        "export type ShellCommand = {\n  run(): void;\n};\n",
    )
    assert len(syms) == 1
    assert syms[0].name == "ShellCommand"
    assert syms[0].kind == "type_alias"
    assert syms[0].start_line == 1


def test_ts_union_type_alias_produces_type_alias_symbol(
    tmp_path: Path,
) -> None:
    syms = _type_alias_symbols(
        tmp_path, "status.ts", 'type Status = "ok" | "error";\n'
    )
    assert len(syms) == 1
    assert syms[0].name == "Status"
    assert syms[0].kind == "type_alias"


def test_ts_generic_type_alias_produces_type_alias_symbol(
    tmp_path: Path,
) -> None:
    # `type Box<T> = ...` -- the `name:` field capture (type_identifier)
    # must not be perturbed by the trailing type_parameters node, the
    # same way `class Foo<T>`/`interface Foo<T>` already parse cleanly
    # via this identical grammar shape.
    syms = _type_alias_symbols(
        tmp_path, "box.ts", "export type Box<T> = { value: T };\n"
    )
    assert len(syms) == 1
    assert syms[0].name == "Box"
    assert syms[0].kind == "type_alias"


def test_ts_function_type_alias_produces_type_alias_symbol(
    tmp_path: Path,
) -> None:
    # `type Fn = (x: number) => void` -- the alias's `value:` is itself
    # a function_type, not an object/union; the whole
    # type_alias_declaration node is what's captured as @classdef, so
    # this must still be a clean type_alias Symbol match.
    syms = _type_alias_symbols(
        tmp_path,
        "handler.ts",
        "export type Handler = (req: string) => void;\n",
    )
    assert len(syms) == 1
    assert syms[0].name == "Handler"
    assert syms[0].kind == "type_alias"


def test_ts_mapped_type_alias_produces_type_alias_symbol(
    tmp_path: Path,
) -> None:
    # Mapped types live entirely inside `value:`, so the top-level node
    # is still a plain type_alias_declaration.
    syms = _type_alias_symbols(
        tmp_path,
        "readonly.ts",
        "export type ReadonlyBox<T> = {\n"
        "  readonly [K in keyof T]: T[K];\n"
        "};\n",
    )
    assert len(syms) == 1
    assert syms[0].name == "ReadonlyBox"
    assert syms[0].kind == "type_alias"


def test_tsx_type_alias_produces_type_alias_symbol(tmp_path: Path) -> None:
    syms = _type_alias_symbols(
        tmp_path,
        "widget.tsx",
        "export type Props = {\n  label: string;\n};\n",
    )
    assert len(syms) == 1
    assert syms[0].name == "Props"
    assert syms[0].kind == "type_alias"


def test_js_produces_no_type_alias_symbols(tmp_path: Path) -> None:
    # Plain JS has no `type` keyword/type_alias_declaration node type
    # at all, so there is nothing for the new @classdef pattern to
    # match either.
    syms = _type_alias_symbols(tmp_path, "plain.js", "export const x = 1;\n")
    assert syms == []
