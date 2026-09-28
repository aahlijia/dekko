"""C++ paths with more than one scope, or written from the root.

tree-sitter-cpp nests ``a::b::Name`` as scope ``a`` around the
qualifier ``b::Name``, and gives ``::ns::Name`` no scope at all. The
call's name is the last segment and its receiver every scope before
it, the shape a one-scope ``a::Name`` already has; the callee text
stays as written.
"""

from pathlib import Path

import pytest

from dekko.core import languages
from dekko.core.extractor import extract_file
from dekko.core.model import FileMap


def _extract(tmp_path: Path, source: str) -> FileMap:
    spec = languages.spec_for_path("user.cc")
    assert spec is not None
    (tmp_path / "user.cc").write_text(source)
    fm = extract_file(tmp_path, "user.cc", spec)
    assert fm.error is None
    return fm


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        ("a::b::Name(1);", ("a::b::Name", "Name", "a::b")),
        ("x::y::z::Deep(5);", ("x::y::z::Deep", "Deep", "x::y::z")),
        ("::t::G(2);", ("::t::G", "G", "t")),
        ("x::y::Make<int>(4);", ("x::y::Make<int>", "Make<int>", "x::y")),
        (
            "Foo<int>::Bar::baz(1);",
            ("Foo<int>::Bar::baz", "baz", "Foo<int>::Bar"),
        ),
        ("new a::b::Graph(3);", ("a::b::Graph", "Graph", "a::b")),
        ("new a::b::Box<int>(3);", ("a::b::Box", "Box", "a::b")),
        # The one-scope and root-only shapes were already right.
        ("One::Two(9);", ("One::Two", "Two", "One")),
        ("::Global(8);", ("::Global", "Global", None)),
    ],
)
def test_qualified_call_names_the_last_segment(
    tmp_path: Path,
    statement: str,
    expected: tuple[str, str, str | None],
) -> None:
    fm = _extract(tmp_path, f"void Use() {{\n  {statement}\n}}\n")
    assert [(c.text, c.name, c.receiver) for c in fm.calls] == [expected]


def test_make_unique_of_a_qualified_type_constructs_it(tmp_path: Path) -> None:
    fm = _extract(
        tmp_path,
        "void Use() {\n  auto p = std::make_unique<a::b::Graph>(4);\n}\n",
    )
    built = [c for c in fm.calls if c.name == "Graph"]
    assert [(c.text, c.receiver) for c in built] == [("a::b::Graph", "a::b")]


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        ("a::b::Base", ("Base", "a::b")),
        ("a::b::Base<T>", ("Base", "a::b")),
        ("a::Foo<X>::Base", ("Base", "a::Foo<X>")),
        ("::tensorflow::Base", ("Base", "tensorflow")),
        ("x::Base", ("Base", "x")),
    ],
)
def test_qualified_base_class_keeps_every_scope(
    tmp_path: Path, base: str, expected: tuple[str, str]
) -> None:
    fm = _extract(tmp_path, f"class A : public {base} {{}};\n")
    assert [(h.name, h.receiver) for h in fm.heritage] == [expected]
