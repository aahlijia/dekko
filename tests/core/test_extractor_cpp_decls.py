"""C++ function prototypes (``FileMap.cpp_decls``).

A default argument lives on the header prototype, and the ``.cc``
definition can't repeat it. The extractor records each prototype's
qualname, parameter count and trailing defaults; the resolver reads
them to give the definition the arity its header declares.
"""

from pathlib import Path

import pytest

from dekko.core import languages
from dekko.core.extractor import extract_file
from dekko.repo_ops import map_repository


def _decls(tmp_path: Path, source: str, rel: str = "api.cc") -> list[str]:
    spec = languages.spec_for_path(rel)
    assert spec is not None
    (tmp_path / rel).write_text(source)
    fm = extract_file(tmp_path, rel, spec)
    assert fm.error is None
    return fm.cpp_decls


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("namespace tf { int F(int a, int b = 1); }", ["tf.F/2=1"]),
        (
            "namespace a::b { class C { public: void M(int x = 0); }; }",
            ["a.b.C.M/1=1"],
        ),
        ("template <typename T> void G(T x, int y = 2);", ["G/2=1"]),
        ("int F(int a, int b);", ["F/2=0"]),
        ("int F(int a = 1, int b);", ["F/2=0"]),
        ("int F(int a, ...);", ["F/1=0"]),
        (
            "struct S { S(int a, int b = 2); ~S(); };",
            ["S.S/2=1", "S.~S/0=0"],
        ),
        ("template <typename T> class K { void P(int z = 3); };", ["K.P/1=1"]),
        ('extern "C" { int H(int q); }', ["H/1=0"]),
        ("#ifdef X\nint F(int a = 1);\n#endif\n", ["F/1=1"]),
        ("int Outer::Inner::F(int a = 1);", ["Outer.Inner.F/1=1"]),
        ("int *F(int a = 1), &G(int b);", ["F/1=1", "G/1=0"]),
    ],
)
def test_prototypes_are_recorded(
    tmp_path: Path, source: str, expected: list[str]
) -> None:
    assert _decls(tmp_path, source) == expected


@pytest.mark.parametrize(
    "source",
    [
        "int F(int a = 1) { return a; }",
        "void F() { Graph g(ops); }",
        "class C { friend void F(int a = 1); };",
        "int (*fp)(int);",
        "struct S { int (*fp)(int); };",
        "int x, *y;",
    ],
)
def test_definitions_locals_friends_and_pointers_are_not(
    tmp_path: Path, source: str
) -> None:
    assert _decls(tmp_path, source) == []


def test_a_cpp_header_records_them(tmp_path: Path) -> None:
    # ``.h`` is claimed by C and C++; the mapping pipeline reads the
    # content to pick C++ here.
    (tmp_path / "scope.h").write_text(
        "namespace tf {\nclass Scope {\n"
        "  int ToGraph(int g, int opts = 0);\n};\n}\n"
    )
    files, _ = map_repository(
        tmp_path, subpath=None, excludes=(), max_file_size=1_000_000
    )
    assert [fm.cpp_decls for fm in files] == [["tf.Scope.ToGraph/2=1"]]


def test_a_c_file_records_nothing(tmp_path: Path) -> None:
    # C has no default arguments, and a C definition's own params are
    # already its declared ones.
    assert _decls(tmp_path, "int F(int a);\n", rel="api.c") == []
