"""Extraction tests for JS/TS import statements — I1 fix.

Side-effect imports (``import "./foo.css";``) and namespace imports
(``import * as ns from "mod";``) were previously silently dropped by
``_imports_js``/``JAVASCRIPT.import_query`` (no capture at all, since
neither had matched either of the two pre-existing query patterns).
See ``.features/fixes/post-indexing-tooling-bugfix-design.md`` I1.
"""

from pathlib import Path

from dekko.core import languages
from dekko.core.extractor import extract_file


def _imports(tmp_path: Path, filename: str, source: str) -> set:
    spec = languages.spec_for_path(filename)
    assert spec is not None
    (tmp_path / filename).write_text(source)
    fm = extract_file(tmp_path, filename, spec)
    assert fm.error is None
    return {(i.name, i.source) for i in fm.imports}


def test_js_side_effect_import_relative(tmp_path: Path) -> None:
    imports = _imports(tmp_path, "a.js", 'import "./foo.css";\n')
    assert imports == {("", "./foo.css")}


def test_js_side_effect_import_bare_specifier(tmp_path: Path) -> None:
    # The exact report repro.
    imports = _imports(tmp_path, "a.js", 'import "opentui-spinner/react";\n')
    assert imports == {("", "opentui-spinner/react")}


def test_ts_namespace_import(tmp_path: Path) -> None:
    imports = _imports(
        tmp_path, "a.ts", 'import * as ns from "namespace-mod";\n'
    )
    assert imports == {("ns", "namespace-mod/ns")}


def test_js_side_effect_import_does_not_disturb_named_default(
    tmp_path: Path,
) -> None:
    imports = _imports(
        tmp_path,
        "a.js",
        'import "./polyfill";\nimport React from "react";\n',
    )
    assert imports == {("", "./polyfill"), ("React", "react/React")}


def test_js_two_side_effect_imports_do_not_crash(tmp_path: Path) -> None:
    # Verified non-issue from the design doc: two side-effect imports
    # in one file both collapse to the "" local-name key in
    # resolver._imports_by_file (inert for its call-graph purpose),
    # but must both still appear in the raw imports list.
    imports = _imports(
        tmp_path, "a.js", 'import "./one.css";\nimport "./two.css";\n'
    )
    assert imports == {("", "./one.css"), ("", "./two.css")}


def test_js_named_and_default_imports_unaffected(tmp_path: Path) -> None:
    # Regression guard — the query addition must not disturb the
    # pre-existing named/default import patterns.
    imports = _imports(
        tmp_path,
        "a.js",
        'import React, { useState, useEffect as fx } from "react";\n',
    )
    assert imports == {
        ("React", "react/React"),
        ("useState", "react/useState"),
        ("fx", "react/useEffect"),
    }
