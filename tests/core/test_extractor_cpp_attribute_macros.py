"""C/C++ deprecation macros masked before parsing.

tree-sitter doesn't know ``ABSL_DEPRECATED("...")``. With a long enough
message it gives up on the whole enclosing class: the class symbol is
lost, its inline methods become free functions, and every later class
in the file drops a namespace level. The extractor blanks the macro
out, byte for byte, before the parse.
"""

from pathlib import Path

from dekko.core import languages
from dekko.core.extractor import (
    _mask_attribute_macros,
    extract_file,
    looks_like_cpp_header,
)

_LONG = "Use the overload that passes in the options parameter."


def _symbols(tmp_path: Path, source: str, rel: str) -> dict[str, int]:
    (tmp_path / rel).write_text(source)
    fm = extract_file(tmp_path, rel, languages.CPP)
    assert fm.error is None
    return {s.qualname: s.start_line for s in fm.symbols}


def test_a_class_with_a_long_deprecation_message_keeps_its_members(
    tmp_path: Path,
) -> None:
    source = (
        "class Dep : public Base {\n"
        " public:\n"
        "  Dep();\n"
        f'  ABSL_DEPRECATED("{_LONG}")\n'
        "  int64_t Cardinality() const;\n"
        "  int other() const { return 1; }\n"
        "};\n"
    )
    assert looks_like_cpp_header(source.encode()) is True
    symbols = _symbols(tmp_path, source, "d.h")
    assert "Dep" in symbols
    assert "Dep.other" in symbols


def test_the_namespace_survives_past_a_deprecated_member(
    tmp_path: Path,
) -> None:
    source = (
        "namespace a {\n"
        "namespace b {\n"
        "class X : public Y {\n"
        " public:\n"
        f'  ABSL_DEPRECATED("{_LONG}")\n'
        "  int f() const { return 0; }\n"
        "};\n"
        "class Z {};\n"
        "}\n"
        "}\n"
    )
    symbols = _symbols(tmp_path, source, "x.h")
    assert "a.b.X.f" in symbols
    assert "a.b.Z" in symbols
    assert "a.b.f" not in symbols


def test_a_multi_line_message_is_masked_and_lines_stay_put(
    tmp_path: Path,
) -> None:
    source = (
        "namespace n {\n"
        "class X : public Y {\n"
        " public:\n"
        "  ABSL_DEPRECATED(\n"
        '      "Use the overload that passes in "\n'
        '      "the options parameter instead.")\n'
        "  int f() const { return 0; }\n"
        "  int g() const { return 1; }\n"
        "};\n"
        "}\n"
    )
    symbols = _symbols(tmp_path, source, "x.h")
    assert symbols["n.X.f"] == 7
    assert symbols["n.X.g"] == 8


def test_the_mask_keeps_every_byte_offset() -> None:
    source = b'class X {\n  ABSL_DEPRECATED("a\\"b"\n    "c") int f();\n};\n'
    masked = _mask_attribute_macros(source)
    assert len(masked) == len(source)
    assert masked.count(b"\n") == source.count(b"\n")
    assert b"DEPRECATED" not in masked
    assert masked.endswith(b"int f();\n};\n")


def test_a_macro_definition_is_left_as_written() -> None:
    source = (
        b"#define ABSL_DEPRECATED(message) "
        b"__attribute__((deprecated(message)))\n"
    )
    assert _mask_attribute_macros(source) == source


def test_a_non_string_argument_is_left_as_written() -> None:
    source = b"ABSL_DEPRECATED(kMessage) int f();\n"
    assert _mask_attribute_macros(source) == source


def test_other_deprecation_macros_are_masked_too(tmp_path: Path) -> None:
    source = (
        "namespace t {\n"
        "class R : public B {\n"
        " public:\n"
        f'  TFLITE_DEPRECATED("{_LONG}")\n'
        "  int f() const { return 0; }\n"
        "};\n"
        "}\n"
    )
    symbols = _symbols(tmp_path, source, "r.h")
    assert "t.R.f" in symbols
