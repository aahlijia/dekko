"""Extraction tests for Phase 1 heritage clauses (Python/JS/TS/Java).

Covers single/multiple inheritance, the metaclass/Generic[T] filtering
edge cases the design doc calls out, cross-language relation labeling
(extends vs. implements), and the "no heritage" empty case — one
fixture per language, plus the correlation-by-byte-span mechanism
``_collect_heritage`` uses to attach clauses to their owning symbol.
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
