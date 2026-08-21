"""Extraction tests for TS/TSX type-alias declarations — round-19
claude-code finding, bug #3.

``type X = {...}`` object aliases were never extracted as ``Symbol``s
at all, so an ``implements``/``extends`` clause naming a same-file
alias had no structural signal to distinguish it from a genuinely
external base type (``query._heritage_external_label`` mislabeled it
``(external)``). ``LanguageSpec.type_alias_query`` (TS/TSX only) closes
that gap by giving the extractor a lightweight, file-scoped registry
of alias names — see ``FileMap.type_aliases``'s docstring.
"""

from pathlib import Path

from dekko.core import languages
from dekko.core.extractor import extract_file


def _type_aliases(tmp_path: Path, filename: str, source: str) -> list[str]:
    spec = languages.spec_for_path(filename)
    assert spec is not None
    (tmp_path / filename).write_text(source)
    fm = extract_file(tmp_path, filename, spec)
    assert fm.error is None
    return fm.type_aliases


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
