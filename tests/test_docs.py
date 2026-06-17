"""Doc-line extraction: ``Symbol.doc`` and ``FileMap.doc``."""

import importlib.util
from pathlib import Path

import pytest

from dekko import languages
from dekko.extractor import extract_file
from dekko.extractor_generic import extract_file_generic
from dekko.model import FileMap

_HAS_PACK = importlib.util.find_spec("tree_sitter_language_pack") is not None


def _extract(tmp_path: Path, name: str, source: str) -> FileMap:
    (tmp_path / name).write_text(source)
    spec = languages.spec_for_path(name)
    assert spec is not None
    fm = extract_file(tmp_path, name, spec)
    assert fm.error is None
    return fm


def _docs(fm: FileMap) -> dict[str, str | None]:
    return {sym.qualname: sym.doc for sym in fm.symbols}


def test_python_docstrings(tmp_path: Path) -> None:
    fm = _extract(
        tmp_path,
        "mod.py",
        '"""Module summary line.\n'
        "\n"
        'More text.\n"""\n'
        "\n"
        "\n"
        "def documented():\n"
        '    """Do the thing."""\n'
        "\n"
        "\n"
        "def bare():\n"
        "    pass\n"
        "\n"
        "\n"
        "class Thing:\n"
        '    """A thing."""\n'
        "\n"
        "    def method(self):\n"
        '        """Method doc.\n'
        "\n"
        '        Longer body.\n        """\n',
    )
    assert fm.doc == "Module summary line."
    docs = _docs(fm)
    assert docs["documented"] == "Do the thing."
    assert docs["bare"] is None
    assert docs["Thing"] == "A thing."
    assert docs["Thing.method"] == "Method doc."


def test_rust_doc_comments(tmp_path: Path) -> None:
    fm = _extract(
        tmp_path,
        "lib.rs",
        "//! Crate-level docs.\n"
        "\n"
        "/// Adds numbers.\n"
        "///\n"
        "/// More detail.\n"
        "#[inline]\n"
        "pub fn add(a: i32, b: i32) -> i32 { a + b }\n"
        "\n"
        "fn bare() {}\n"
        "\n"
        "/// A point.\n"
        "pub struct Point { x: i32 }\n",
    )
    assert fm.doc == "Crate-level docs."
    docs = _docs(fm)
    assert docs["add"] == "Adds numbers."
    assert docs["bare"] is None
    assert docs["Point"] == "A point."


def test_go_doc_comments(tmp_path: Path) -> None:
    fm = _extract(
        tmp_path,
        "demo.go",
        "// Package demo does things.\n"
        "package demo\n"
        "\n"
        "// Greet says hello.\n"
        "func Greet(name string) string {\n"
        "\treturn name\n"
        "}\n"
        "\n"
        "func bare() {}\n",
    )
    assert fm.doc == "Package demo does things."
    docs = _docs(fm)
    assert docs["Greet"] == "Greet says hello."
    assert docs["bare"] is None


def test_go_blank_line_gap_breaks_doc(tmp_path: Path) -> None:
    fm = _extract(
        tmp_path,
        "gap.go",
        "package demo\n\n// Stale comment.\n\nfunc gapped() {}\n",
    )
    assert _docs(fm)["gapped"] is None


def test_js_doc_comments(tmp_path: Path) -> None:
    fm = _extract(
        tmp_path,
        "app.js",
        "/** App entry. */\n"
        "\n"
        "/** Runs the app. */\n"
        "export function run() {}\n"
        "\n"
        "// Helper note.\n"
        "const helper = () => {};\n"
        "\n"
        "function bare() {}\n",
    )
    assert fm.doc == "App entry."
    docs = _docs(fm)
    assert docs["run"] == "Runs the app."
    assert docs["helper"] == "Helper note."
    assert docs["bare"] is None


def test_ts_doc_comments(tmp_path: Path) -> None:
    fm = _extract(
        tmp_path,
        "svc.ts",
        "/** Service types. */\n"
        "\n"
        "/** A service. */\n"
        "export interface Svc {\n"
        "  name: string;\n"
        "}\n"
        "\n"
        "/** Make a service. */\n"
        'export const make = (): Svc => ({ name: "x" });\n',
    )
    assert fm.doc == "Service types."
    docs = _docs(fm)
    assert docs["Svc"] == "A service."
    assert docs["make"] == "Make a service."


def test_java_doc_comments(tmp_path: Path) -> None:
    fm = _extract(
        tmp_path,
        "App.java",
        "/** App javadoc. */\n"
        "public class App {\n"
        "    /** Runs once. */\n"
        "    public void run() {}\n"
        "\n"
        "    void bare() {}\n"
        "}\n",
    )
    assert fm.doc == "App javadoc."
    docs = _docs(fm)
    assert docs["App"] == "App javadoc."
    assert docs["App.run"] == "Runs once."
    assert docs["App.bare"] is None


def test_c_doc_comments(tmp_path: Path) -> None:
    fm = _extract(
        tmp_path,
        "util.c",
        "/* util helpers */\n"
        "\n"
        "/* Adds two ints. */\n"
        "int add(int a, int b) { return a + b; }\n"
        "\n"
        "int bare(void) { return 0; }\n",
    )
    assert fm.doc == "util helpers"
    docs = _docs(fm)
    assert docs["add"] == "Adds two ints."
    assert docs["bare"] is None


def test_doc_line_truncated(tmp_path: Path) -> None:
    long = "x" * 150
    fm = _extract(
        tmp_path,
        "long.py",
        f'def f():\n    """{long}"""\n',
    )
    doc = _docs(fm)["f"]
    assert doc is not None
    assert len(doc) <= 100
    assert doc.endswith("…")


@pytest.mark.skipif(
    not _HAS_PACK, reason="Tier-2 grammar pack not installed (dekko[all])"
)
def test_generic_ruby_doc_comments(tmp_path: Path) -> None:
    (tmp_path / "store.rb").write_text(
        "# Store module.\n\n# Fetches a value.\ndef fetch(key)\nend\n"
    )
    fm = extract_file_generic(tmp_path, "store.rb", "ruby")
    assert fm.error is None
    assert fm.doc == "Store module."
    assert _docs(fm)["fetch"] == "Fetches a value."
