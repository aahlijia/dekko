"""Extraction tests for JS/TS re-exports and default exports.

``export { X } from "./x"``, ``export * from "./x"`` and "this file's
default export is the symbol ``Foo``" bind no local name, so the
import query never saw them and a barrel file was a dead end.
"""

from pathlib import Path

import pytest

from dekko.core import languages
from dekko.core.extractor import extract_file


def _reexports(tmp_path: Path, filename: str, source: str) -> list:
    spec = languages.spec_for_path(filename)
    assert spec is not None
    (tmp_path / filename).write_text(source)
    fm = extract_file(tmp_path, filename, spec)
    assert fm.error is None
    return [(r.name, r.original, r.source) for r in fm.reexports]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('export { A } from "./a";\n', [("A", "A", "./a")]),
        ('export { B as C } from "./a";\n', [("C", "B", "./a")]),
        ('export type { T } from "./t";\n', [("T", "T", "./t")]),
        ('export { type U } from "./t";\n', [("U", "U", "./t")]),
        ('export * from "./s";\n', [("*", "*", "./s")]),
        ('export * as ns from "./n";\n', [("ns", "*", "./n")]),
        (
            'export { default as Text } from "./x";\n',
            [("Text", "default", "./x")],
        ),
        ('export { default } from "./y";\n', [("default", "default", "./y")]),
        ("const a = 1;\nexport { a as b };\n", [("b", "a", "")]),
        ("const a = 1;\nexport { a as default };\n", [("default", "a", "")]),
        (
            "export default function ThemedText() {}\n",
            [("default", "ThemedText", "")],
        ),
        ("export default class X {}\n", [("default", "X", "")]),
        (
            "const ThemedBox = 1;\nexport default ThemedBox;\n",
            [("default", "ThemedBox", "")],
        ),
        (
            "export default async function* gen() {}\n",
            [("default", "gen", "")],
        ),
    ],
)
def test_ts_reexport_forms(
    tmp_path: Path, source: str, expected: list
) -> None:
    assert _reexports(tmp_path, "index.ts", source) == expected


@pytest.mark.parametrize(
    "source",
    [
        # A plain local export: the file's own symbol or import
        # binding already says everything.
        "const L = 1;\nexport { L };\n",
        "export default () => 1;\n",
        "export default memo(Foo);\n",
        "export default function () {}\n",
        "export const x = 1;\n",
        "export function f() {}\n",
    ],
)
def test_forms_that_record_nothing(tmp_path: Path, source: str) -> None:
    assert _reexports(tmp_path, "index.ts", source) == []


def test_multi_line_clause_keeps_every_item_in_order(tmp_path: Path) -> None:
    source = (
        "export {\n"
        "  A,\n"
        "  B as C,\n"
        "  type D,\n"
        '} from "./lib";\n'
        'export * from "./other";\n'
    )
    assert _reexports(tmp_path, "index.ts", source) == [
        ("A", "A", "./lib"),
        ("C", "B", "./lib"),
        ("D", "D", "./lib"),
        ("*", "*", "./other"),
    ]


@pytest.mark.parametrize("filename", ["index.js", "index.mjs", "index.tsx"])
def test_js_family_files_extract_reexports(
    tmp_path: Path, filename: str
) -> None:
    source = (
        'export { default as Text } from "./x";\n'
        'export * from "./s";\n'
        "export default function Main() {}\n"
    )
    assert _reexports(tmp_path, filename, source) == [
        ("Text", "default", "./x"),
        ("*", "*", "./s"),
        ("default", "Main", ""),
    ]


def test_a_python_file_has_no_reexports(tmp_path: Path) -> None:
    assert _reexports(tmp_path, "a.py", "from b import c\n") == []
