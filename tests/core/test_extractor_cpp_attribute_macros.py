"""C/C++ attribute macros masked before parsing.

tree-sitter doesn't know ``ABSL_DEPRECATED("...")``. With a long enough
message it gives up on the whole enclosing class: the class symbol is
lost, its inline methods become free functions, and every later class
in the file drops a namespace level. A macro between ``class`` and its
name (``class TF_LOCKABLE Sqlite``) breaks the parse the same way, and
a thread-safety annotation after a member's parameter list
(``void Run() TF_LOCKS_EXCLUDED(mu_) {``) is read as the member's name.
The extractor blanks each one out, byte for byte, before the parse.
"""

from pathlib import Path

from tree_sitter import Parser

from dekko.core import languages
from dekko.core.extractor import (
    _mask_attribute_macros,
    extract_file,
    looks_like_cpp_header,
)
from dekko.core.grammars import get_grammar
from dekko.core.resolver import resolve
from dekko.repo_ops import map_repository

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


def _node_types(source: bytes) -> set[str]:
    tree = Parser(get_grammar("cpp")).parse(_mask_attribute_macros(source))
    seen: set[str] = set()
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        seen.add(node.type)
        stack.extend(node.children)
    return seen


def test_an_empty_deprecate_and_inline_leaves_an_alias() -> None:
    source = (
        b"namespace tensorflow {\n"
        b"using Status ABSL_DEPRECATE_AND_INLINE() = ::absl::Status;\n"
        b"}\n"
    )
    masked = _mask_attribute_macros(source)
    assert b"DEPRECATE_AND_INLINE" not in masked
    assert len(masked) == len(source)
    assert "alias_declaration" in _node_types(source)


def test_a_macro_between_class_and_its_name_keeps_the_class(
    tmp_path: Path,
) -> None:
    source = (
        "namespace t {\n"
        "class TF_LOCKABLE Sqlite : public Base {\n"
        " public:\n"
        "  int Prepare() { return 0; }\n"
        "};\n"
        "struct TF_PACKED Hdr {\n"
        "  int size() const { return 1; }\n"
        "};\n"
        'class TF_CAPABILITY("mutex") mutex {\n'
        "  void lock() {}\n"
        "};\n"
        "}\n"
    )
    symbols = _symbols(tmp_path, source, "s.h")
    assert symbols["t.Sqlite"] == 2
    assert symbols["t.Sqlite.Prepare"] == 4
    assert "t.Hdr.size" in symbols
    assert "t.mutex.lock" in symbols
    assert not any("TF_" in name for name in symbols)


def test_a_class_macro_mask_keeps_every_byte_offset() -> None:
    source = b'class TF_CAPABILITY("mutex") mutex final : B {\n};\n'
    masked = _mask_attribute_macros(source)
    assert len(masked) == len(source)
    assert masked.startswith(b"class ")
    assert masked.endswith(b"mutex final : B {\n};\n")
    assert b"CAPABILITY" not in masked


def test_plain_class_heads_are_left_as_written() -> None:
    source = (
        b"class RNG {\n};\n"
        b"class Foo final : Bar {\n};\n"
        b"struct STAT_T x = {0};\n"
    )
    assert _mask_attribute_macros(source) == source


def test_a_trailing_lock_annotation_leaves_the_member_its_name(
    tmp_path: Path,
) -> None:
    source = (
        "class Cache {\n"
        " public:\n"
        "  void Lookup(int k) TF_LOCKS_EXCLUDED(mu_) { Find(k); }\n"
        "  int Size() const override TF_SHARED_LOCKS_REQUIRED(mu_) {\n"
        "    return 0;\n"
        "  }\n"
        "  void Run() TF_NO_THREAD_SAFETY_ANALYSIS { Lookup(1); }\n"
        "  void Evict() ABSL_EXCLUSIVE_LOCKS_REQUIRED(mu_)\n"
        "      ABSL_LOCKS_EXCLUDED(other_) {}\n"
        "};\n"
    )
    symbols = _symbols(tmp_path, source, "cache.h")
    assert symbols["Cache.Lookup"] == 3
    assert symbols["Cache.Size"] == 4
    assert symbols["Cache.Run"] == 7
    assert symbols["Cache.Evict"] == 8
    assert not [q for q in symbols if "LOCK" in q or "THREAD" in q]


def test_a_guarded_field_makes_no_method(tmp_path: Path) -> None:
    source = (
        "class Counter {\n"
        " public:\n"
        "  int Get() const { return n_; }\n"
        " private:\n"
        "  size_t n_ TF_GUARDED_BY(mu_) = 0;\n"
        "  int* p_ TF_PT_GUARDED_BY(mu_);\n"
        "  int Next() { return 1; }\n"
        "};\n"
    )
    symbols = _symbols(tmp_path, source, "counter.h")
    assert set(symbols) == {"Counter", "Counter.Get", "Counter.Next"}
    assert symbols["Counter.Next"] == 7


def test_a_lock_annotation_mask_keeps_every_byte_offset() -> None:
    source = (
        b"  void Lookup(int k) const TF_LOCKS_EXCLUDED(mu_) {}\n"
        b"  size_t n_ ABSL_GUARDED_BY(mu_);\n"
    )
    masked = _mask_attribute_macros(source)
    assert len(masked) == len(source)
    assert masked.count(b"\n") == source.count(b"\n")
    assert b"void Lookup(int k) const " in masked
    assert b"LOCKS" not in masked
    assert b"GUARDED" not in masked


def test_a_directive_carrying_a_lock_annotation_is_left_as_written(
    tmp_path: Path,
) -> None:
    define = (
        b"#define DECLARE_GET(T) \\\n"
        b"  T Get() TF_LOCKS_EXCLUDED(mu_); \\\n"
        b"  int n_ TF_GUARDED_BY(mu_)\n"
    )
    assert _mask_attribute_macros(define) == define
    source = define.decode() + (
        "class Db {\n public:\n  int PrepareOrDie() { return 0; }\n};\n"
    )
    assert "Db.PrepareOrDie" in _symbols(tmp_path, source, "db.h")


def test_other_trailing_macros_are_left_as_written() -> None:
    source = (
        b"  TF_RETURN_IF_ERROR(f(x));\n"
        b"  void Run() TF_ATTRIBUTE_NOINLINE {}\n"
        b"  int Make() TF_MUST_USE_RESULT;\n"
    )
    assert _mask_attribute_macros(source) == source


def test_a_renamed_member_does_not_take_an_outside_types_call(
    tmp_path: Path,
) -> None:
    sources = {
        "model.h": (
            "namespace tflite {\n"
            "class GraphFloat32 {\n"
            " public:\n"
            "  struct NodeDef {\n"
            "    int Id() { return 0; }\n"
            "  };\n"
            "};\n"
            "}\n"
        ),
        "node.h": (
            "namespace data {\n"
            "class Node {\n"
            " public:\n"
            "  void add_input(int x) TF_LOCKS_EXCLUDED(mu_) {}\n"
            "};\n"
            "}\n"
        ),
        "rewrite.cc": (
            "namespace grappler {\n"
            "void Rewrite(NodeDef* d) { d->add_input(1); }\n"
            "}\n"
        ),
    }
    for rel, text in sources.items():
        (tmp_path / rel).write_text(text)
    files, _ = map_repository(
        tmp_path,
        subpath=None,
        excludes=(),
        max_file_size=1_000_000,
    )
    graph = resolve(files, root=tmp_path)
    ids = {s.id for fm in files for s in fm.symbols}
    assert "node.h::data.Node.add_input" in ids
    assert not [
        e for e in graph.edges if e.callee == "node.h::data.Node.add_input"
    ]
