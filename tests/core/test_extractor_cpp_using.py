"""Namespace-scope C++ ``using``-declarations (``FileMap.cpp_using``).

``namespace tensorflow { using tsl::StatusFromTF_Status; }`` makes
``tensorflow::StatusFromTF_Status`` the ``tsl`` function. The
extractor records each such declaration with the namespace it sits in;
the resolver reads them.
"""

from pathlib import Path

import pytest

from dekko.core import languages
from dekko.core.extractor import extract_file
from dekko.repo_ops import map_repository


def _using(tmp_path: Path, source: str, rel: str = "user.cc") -> list[str]:
    spec = languages.spec_for_path(rel)
    assert spec is not None
    (tmp_path / rel).write_text(source)
    fm = extract_file(tmp_path, rel, spec)
    assert fm.error is None
    return fm.cpp_using


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "namespace tensorflow { using tsl::StatusFromTF_Status; }",
            ["tensorflow=tsl::StatusFromTF_Status"],
        ),
        ("namespace a { namespace b { using c::X; } }", ["a.b=c::X"]),
        ("namespace a::b { using c::X; }", ["a.b=c::X"]),
        ("using ::foo::Bar;", ["=::foo::Bar"]),
        ("namespace a { using ::foo::Bar; }", ["a=::foo::Bar"]),
        ("namespace n { using ns::Tmpl<int>::f; }", ["n=ns::Tmpl::f"]),
        ("namespace a { namespace { using q::Y; } }", ["a=q::Y"]),
        ('extern "C++" { namespace a { using q::Y; } }', ["a=q::Y"]),
        ("#ifdef X\nnamespace a { using q::Y; }\n#endif\n", ["a=q::Y"]),
    ],
)
def test_namespace_scope_using_declarations_are_recorded(
    tmp_path: Path, source: str, expected: list[str]
) -> None:
    assert _using(tmp_path, source) == expected


@pytest.mark.parametrize(
    "source",
    [
        "namespace a { using namespace mlir; }",
        "namespace a { using T = int; }",
        "namespace a { class K : public B { using B::f; }; }",
        "void f() { using std::swap; }",
    ],
)
def test_other_using_forms_are_not_recorded(
    tmp_path: Path, source: str
) -> None:
    assert _using(tmp_path, source) == []


def test_source_order_is_kept(tmp_path: Path) -> None:
    source = (
        "using ::a::One;\n"
        "namespace n { using b::Two; }\n"
        "namespace m { using c::Three; }\n"
    )
    assert _using(tmp_path, source) == [
        "=::a::One",
        "n=b::Two",
        "m=c::Three",
    ]


def test_a_cpp_header_records_them_too(tmp_path: Path) -> None:
    # ``.h`` is claimed by C and C++; the mapping pipeline reads the
    # content to pick C++ here.
    (tmp_path / "helper.h").write_text(
        "namespace tensorflow {\nclass A {};\nusing tsl::F;\n}\n"
    )
    files, _ = map_repository(
        tmp_path, subpath=None, excludes=(), max_file_size=1_000_000
    )
    assert [fm.cpp_using for fm in files] == [["tensorflow=tsl::F"]]


def test_other_languages_record_nothing(tmp_path: Path) -> None:
    assert _using(tmp_path, "use a::b::C;\n", rel="lib.rs") == []
