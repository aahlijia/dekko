"""Extraction tests for heritage clauses.

Phase 1 (Python/JS/TS/Java): single/multiple inheritance, the
metaclass/Generic[T] filtering edge cases the design doc calls out,
cross-language relation labeling (extends vs. implements), and the
"no heritage" empty case — one fixture per language, plus the
correlation-by-byte-span mechanism ``_collect_heritage`` uses to
attach clauses to their owning symbol.

Phase 2 (Rust/C++): Rust's ``impl Trait for Type`` (a separate
top-level construct with no ``@classdef`` to correlate against, unlike
every Phase 1 language — resolved by same-file name lookup instead,
see ``extractor._heritage_rust_impl``), inherent-impl exclusion,
supertrait bounds (including lifetime-bound filtering), and the
same-file-ambiguous-name/type-not-in-file skip cases the same-file
lookup can hit; C++'s access-specifier stripping and
qualified/templated base names.
"""

from pathlib import Path

from dekko.core import languages
from dekko.core.extractor import extract_file
from dekko.core.model import RawHeritage


def _heritage_by_subtype(
    items: list[RawHeritage],
) -> dict[str, list[RawHeritage]]:
    out: dict[str, list[RawHeritage]] = {}
    for h in items:
        out.setdefault(h.subtype_id, []).append(h)
    return out


def test_python_single_and_multiple_inheritance(tmp_path: Path) -> None:
    spec = languages.spec_for_path("a.py")
    assert spec is not None
    (tmp_path / "a.py").write_text(
        "class Base:\n"
        "    pass\n"
        "\n"
        "\n"
        "class Mixin:\n"
        "    pass\n"
        "\n"
        "\n"
        "class Single(Base):\n"
        "    pass\n"
        "\n"
        "\n"
        "class Multi(Base, Mixin):\n"
        "    pass\n"
        "\n"
        "\n"
        "class Bare:\n"
        "    pass\n"
    )
    fm = extract_file(tmp_path, "a.py", spec)
    assert fm.error is None
    by_subtype = _heritage_by_subtype(fm.heritage)

    single = by_subtype["a.py::Single"]
    assert len(single) == 1
    assert single[0].name == "Base"
    assert single[0].relation == "extends"

    multi = by_subtype["a.py::Multi"]
    assert {h.name for h in multi} == {"Base", "Mixin"}
    assert all(h.relation == "extends" for h in multi)

    assert "a.py::Bare" not in by_subtype


def test_python_metaclass_kwarg_filtered_generic_kept(tmp_path: Path) -> None:
    # Design doc's documented edge cases: `metaclass=Meta` is a keyword
    # argument, not a base, and must never appear in fm.heritage; a
    # `Generic[T]`/`Protocol[T]`-shaped entry looks like a real base
    # syntactically and is kept (no attempt to distinguish a
    # structural-typing marker from a real base).
    spec = languages.spec_for_path("a.py")
    assert spec is not None
    (tmp_path / "a.py").write_text(
        "from typing import Generic, TypeVar\n"
        "\n"
        "T = TypeVar('T')\n"
        "\n"
        "\n"
        "class Meta(type):\n"
        "    pass\n"
        "\n"
        "\n"
        "class Foo(Generic[T], metaclass=Meta):\n"
        "    pass\n"
    )
    fm = extract_file(tmp_path, "a.py", spec)
    by_subtype = _heritage_by_subtype(fm.heritage)
    names = {h.name for h in by_subtype["a.py::Foo"]}
    assert names == {"Generic"}


def test_python_attribute_qualified_base(tmp_path: Path) -> None:
    # `class Foo(module.Base):` — an `attribute` node, not a bare
    # `identifier`; the base's own name is the last segment, the
    # module the leading (receiver) segment.
    spec = languages.spec_for_path("a.py")
    assert spec is not None
    (tmp_path / "a.py").write_text(
        "import pydantic\n\n\nclass MyModel(pydantic.BaseModel):\n    pass\n"
    )
    fm = extract_file(tmp_path, "a.py", spec)
    [h] = fm.heritage
    assert h.name == "BaseModel"
    assert h.receiver == "pydantic"
    assert h.text == "pydantic.BaseModel"
    assert h.relation == "extends"


def test_javascript_single_extends_only(tmp_path: Path) -> None:
    spec = languages.spec_for_path("a.js")
    assert spec is not None
    (tmp_path / "a.js").write_text(
        "class Base {}\nclass Foo extends Base {}\nclass Plain {}\n"
    )
    fm = extract_file(tmp_path, "a.js", spec)
    by_subtype = _heritage_by_subtype(fm.heritage)
    [h] = by_subtype["a.js::Foo"]
    assert h.name == "Base"
    assert h.relation == "extends"
    assert "a.js::Plain" not in by_subtype
    assert "a.js::Base" not in by_subtype


def test_typescript_extends_and_multiple_implements(tmp_path: Path) -> None:
    spec = languages.spec_for_path("a.ts")
    assert spec is not None
    (tmp_path / "a.ts").write_text(
        "class Base {}\n"
        "interface IFoo {}\n"
        "interface IBar {}\n"
        "class Foo extends Base implements IFoo, IBar {}\n"
    )
    fm = extract_file(tmp_path, "a.ts", spec)
    by_subtype = _heritage_by_subtype(fm.heritage)
    entries = {(h.name, h.relation) for h in by_subtype["a.ts::Foo"]}
    assert entries == {
        ("Base", "extends"),
        ("IFoo", "implements"),
        ("IBar", "implements"),
    }


def test_typescript_interface_extends_multiple_interfaces(
    tmp_path: Path,
) -> None:
    spec = languages.spec_for_path("a.ts")
    assert spec is not None
    (tmp_path / "a.ts").write_text(
        "interface IFoo {}\n"
        "interface IBar {}\n"
        "interface IBaz extends IFoo, IBar {}\n"
    )
    fm = extract_file(tmp_path, "a.ts", spec)
    by_subtype = _heritage_by_subtype(fm.heritage)
    entries = {(h.name, h.relation) for h in by_subtype["a.ts::IBaz"]}
    assert entries == {("IFoo", "extends"), ("IBar", "extends")}


def test_typescript_class_with_no_heritage_has_no_entry(
    tmp_path: Path,
) -> None:
    spec = languages.spec_for_path("a.ts")
    assert spec is not None
    (tmp_path / "a.ts").write_text("class Plain {}\n")
    fm = extract_file(tmp_path, "a.ts", spec)
    assert fm.heritage == []


def test_java_class_extends_and_implements(tmp_path: Path) -> None:
    spec = languages.spec_for_path("A.java")
    assert spec is not None
    (tmp_path / "A.java").write_text(
        "class Base {}\n"
        "interface IFoo {}\n"
        "interface IBar {}\n"
        "class Foo extends Base implements IFoo, IBar {}\n"
    )
    fm = extract_file(tmp_path, "A.java", spec)
    by_subtype = _heritage_by_subtype(fm.heritage)
    entries = {(h.name, h.relation) for h in by_subtype["A.java::Foo"]}
    assert entries == {
        ("Base", "extends"),
        ("IFoo", "implements"),
        ("IBar", "implements"),
    }


def test_java_interface_extends_multiple_interfaces(tmp_path: Path) -> None:
    spec = languages.spec_for_path("A.java")
    assert spec is not None
    (tmp_path / "A.java").write_text(
        "interface IFoo {}\n"
        "interface IBar {}\n"
        "interface IBaz extends IFoo, IBar {}\n"
    )
    fm = extract_file(tmp_path, "A.java", spec)
    by_subtype = _heritage_by_subtype(fm.heritage)
    entries = {(h.name, h.relation) for h in by_subtype["A.java::IBaz"]}
    assert entries == {("IFoo", "extends"), ("IBar", "extends")}


def test_java_generic_and_scoped_interface_names(tmp_path: Path) -> None:
    # `implements Comparable<Foo>` (generic_type) and `implements
    # java.util.List` (scoped_type_identifier) both need the
    # base/receiver split to strip the generic argument and take only
    # the last dotted segment as the name, matching a callee's own
    # dotted-path splitting.
    spec = languages.spec_for_path("A.java")
    assert spec is not None
    (tmp_path / "A.java").write_text(
        "class Foo implements Comparable<Foo>, java.util.List {}\n"
    )
    fm = extract_file(tmp_path, "A.java", spec)
    [h1, h2] = fm.heritage
    names = {h.name for h in (h1, h2)}
    assert names == {"Comparable", "List"}
    scoped = next(h for h in (h1, h2) if h.name == "List")
    assert scoped.receiver == "java"


def test_java_class_with_no_heritage_has_no_entry(tmp_path: Path) -> None:
    spec = languages.spec_for_path("A.java")
    assert spec is not None
    (tmp_path / "A.java").write_text("class Plain {}\n")
    fm = extract_file(tmp_path, "A.java", spec)
    assert fm.heritage == []


def test_heritage_lines_are_1_based_and_at_clause_site(tmp_path: Path) -> None:
    spec = languages.spec_for_path("a.py")
    assert spec is not None
    (tmp_path / "a.py").write_text(
        "class Base:\n    pass\n\n\nclass Foo(\n    Base,\n):\n    pass\n"
    )
    fm = extract_file(tmp_path, "a.py", spec)
    [h] = fm.heritage
    assert h.line == 6  # the "Base," line, not Foo's own def line


# ---------------------------------------------------------------------
# Phase 2: Rust


def test_rust_impl_trait_for_type_is_impl_relation(tmp_path: Path) -> None:
    spec = languages.spec_for_path("a.rs")
    assert spec is not None
    (tmp_path / "a.rs").write_text(
        "trait Super {}\nstruct Foo;\n\nimpl Super for Foo {}\n"
    )
    fm = extract_file(tmp_path, "a.rs", spec)
    by_subtype = _heritage_by_subtype(fm.heritage)
    [h] = by_subtype["a.rs::Foo"]
    assert h.name == "Super"
    assert h.relation == "impl"
    assert h.text == "Super"


def test_rust_inherent_impl_produces_no_heritage(tmp_path: Path) -> None:
    # `impl Foo { ... }` has no `trait:` field — the query itself
    # never matches it, so it must never flood `heritage` with a
    # `Foo -> Foo`-shaped no-op edge (the design doc's own documented
    # false-signal risk: inherent impls vastly outnumber trait impls).
    spec = languages.spec_for_path("a.rs")
    assert spec is not None
    (tmp_path / "a.rs").write_text(
        "struct Foo;\n\nimpl Foo {\n    fn new() -> Self { Foo }\n}\n"
    )
    fm = extract_file(tmp_path, "a.rs", spec)
    assert fm.heritage == []


def test_rust_supertrait_bound(tmp_path: Path) -> None:
    spec = languages.spec_for_path("a.rs")
    assert spec is not None
    (tmp_path / "a.rs").write_text("trait Super {}\ntrait Sub: Super {}\n")
    fm = extract_file(tmp_path, "a.rs", spec)
    by_subtype = _heritage_by_subtype(fm.heritage)
    [h] = by_subtype["a.rs::Sub"]
    assert h.name == "Super"
    assert h.relation == "extends"


def test_rust_multiple_supertrait_bounds_lifetime_filtered(
    tmp_path: Path,
) -> None:
    # `trait Sub<'a>: 'a + Super + Clone2 {}` — the lifetime bound
    # (`'a`) is a `lifetime` node, not a type, and must never appear
    # as a heritage entry; the two real supertrait bounds must both
    # be kept.
    spec = languages.spec_for_path("a.rs")
    assert spec is not None
    (tmp_path / "a.rs").write_text(
        "trait Super {}\n"
        "trait Clone2 {}\n"
        "trait Sub<'a>: 'a + Super + Clone2 {}\n"
    )
    fm = extract_file(tmp_path, "a.rs", spec)
    by_subtype = _heritage_by_subtype(fm.heritage)
    names = {h.name for h in by_subtype["a.rs::Sub"]}
    assert names == {"Super", "Clone2"}
    assert all(h.relation == "extends" for h in by_subtype["a.rs::Sub"])


def test_rust_impl_for_type_defined_elsewhere_is_skipped(
    tmp_path: Path,
) -> None:
    # `impl Bar for External {}` where `External` is only imported,
    # not defined in this file — there's no same-file symbol to attach
    # a `RawHeritage` to (`subtype_id` is never `None`), so the impl
    # block is silently skipped rather than guessed at. Cross-file
    # `impl` blocks are a real, documented limitation, not a bug.
    spec = languages.spec_for_path("a.rs")
    assert spec is not None
    (tmp_path / "a.rs").write_text(
        "use crate::other::External;\n"
        "\n"
        "trait Bar {}\n"
        "\n"
        "impl Bar for External {}\n"
    )
    fm = extract_file(tmp_path, "a.rs", spec)
    assert fm.heritage == []


def test_rust_impl_for_ambiguous_same_file_name_is_skipped(
    tmp_path: Path,
) -> None:
    # Two `mod` blocks in the same file each define their own `Foo` —
    # legal Rust, and the same-file name lookup can't tell which one
    # `impl Marker for Foo` means, so no heritage edge is emitted for
    # either rather than guessing.
    spec = languages.spec_for_path("a.rs")
    assert spec is not None
    (tmp_path / "a.rs").write_text(
        "mod a { pub struct Foo; }\n"
        "mod b { pub struct Foo; }\n"
        "trait Marker {}\n"
        "impl Marker for Foo {}\n"
    )
    fm = extract_file(tmp_path, "a.rs", spec)
    assert fm.heritage == []


def test_rust_heritage_line_at_impl_block_site(tmp_path: Path) -> None:
    spec = languages.spec_for_path("a.rs")
    assert spec is not None
    (tmp_path / "a.rs").write_text(
        "trait Super {}\nstruct Foo;\n\n\nimpl Super for Foo {}\n"
    )
    fm = extract_file(tmp_path, "a.rs", spec)
    [h] = fm.heritage
    assert h.line == 5


# ---------------------------------------------------------------------
# Phase 2: C++


def test_cpp_single_base_no_access_specifier(tmp_path: Path) -> None:
    # `struct S : Base1 {};` — a struct base defaults to public and
    # carries no `access_specifier` sibling at all.
    spec = languages.spec_for_path("a.cpp")
    assert spec is not None
    (tmp_path / "a.cpp").write_text("struct Base1 {};\nstruct S : Base1 {};\n")
    fm = extract_file(tmp_path, "a.cpp", spec)
    by_subtype = _heritage_by_subtype(fm.heritage)
    [h] = by_subtype["a.cpp::S"]
    assert h.name == "Base1"
    assert h.relation == "extends"


def test_cpp_multiple_bases_access_specifiers_stripped(
    tmp_path: Path,
) -> None:
    # `class Derived : public Base1, private Base2 {};` — the
    # `access_specifier` keyword must be stripped, not treated as part
    # of the type name (a missed strip would silently produce zero
    # matches: "public Base1" never equals any real symbol name).
    spec = languages.spec_for_path("a.cpp")
    assert spec is not None
    (tmp_path / "a.cpp").write_text(
        "class Base1 {};\n"
        "class Base2 {};\n"
        "class Derived : public Base1, private Base2 {\n"
        "public:\n"
        "    void run();\n"
        "};\n"
    )
    fm = extract_file(tmp_path, "a.cpp", spec)
    by_subtype = _heritage_by_subtype(fm.heritage)
    entries = {(h.name, h.relation) for h in by_subtype["a.cpp::Derived"]}
    assert entries == {("Base1", "extends"), ("Base2", "extends")}


def test_cpp_qualified_and_templated_base(tmp_path: Path) -> None:
    # `public std::vector<int>, public ns::Qualified` — a generic
    # (template) argument stripped and a namespace-qualified base's
    # last segment taken as the name, mirroring how a callee's own
    # dotted/scoped-path splitting already works.
    spec = languages.spec_for_path("a.cpp")
    assert spec is not None
    (tmp_path / "a.cpp").write_text(
        "namespace ns { class Qualified {}; }\n"
        "namespace std { template<typename T> class vector {}; }\n"
        "class Foo : public std::vector<int>, public ns::Qualified {\n"
        "public:\n"
        "};\n"
    )
    fm = extract_file(tmp_path, "a.cpp", spec)
    by_subtype = _heritage_by_subtype(fm.heritage)
    entries = by_subtype["a.cpp::Foo"]
    assert len(entries) == 2
    by_name = {h.name: h for h in entries}
    assert by_name["vector"].receiver == "std"
    assert by_name["vector"].text == "std::vector<int>"
    assert by_name["Qualified"].receiver == "ns"


def test_cpp_class_with_no_heritage_has_no_entry(tmp_path: Path) -> None:
    spec = languages.spec_for_path("a.cpp")
    assert spec is not None
    (tmp_path / "a.cpp").write_text("class Plain {};\n")
    fm = extract_file(tmp_path, "a.cpp", spec)
    assert fm.heritage == []


def test_cpp_forward_declaration_not_extracted(tmp_path: Path) -> None:
    # A forward declaration (`class Fwd;`, no body) is excluded from
    # heritage extraction exactly as it's already excluded from
    # definitions — `heritage_query` requires `body:
    # (field_declaration_list)`, mirroring `definition_query`.
    spec = languages.spec_for_path("a.cpp")
    assert spec is not None
    (tmp_path / "a.cpp").write_text("class Fwd;\nclass Plain {};\n")
    fm = extract_file(tmp_path, "a.cpp", spec)
    assert fm.heritage == []
