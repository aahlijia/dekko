"""Resolver tests for heritage clauses (resolve_heritage()).

Exercises the same candidate ladder ``resolve()`` runs for calls,
applied to ``RawHeritage`` — same-file resolution, cross-file
resolution via import hints, the unique-repo-wide-name fallback,
honest ambiguous/external buckets, and the ``TYPE_KINDS`` candidate
pre-filter that keeps a base-class name from ever resolving to a
same-named function. Most of this file (synthetic ``FileMap``/
``Symbol`` fixtures tagged ``language="python"``) is language-agnostic
by design — the ladder itself never branches on language. The Phase 2
section near the end instead runs real Rust/C++ source through
``extract_file`` + ``resolve()`` end to end, confirming the ``impl``
relation and C++'s access-specifier-stripped names resolve correctly
through the unmodified ladder, plus one test proving Rust's ``impl``
"subtype resolved at extraction time" behavior propagates through
resolution unchanged.
"""

from pathlib import Path

from dekko.core.model import FileMap, Import, RawHeritage, Symbol
from dekko.core.resolver import resolve


def _cls(path: str, name: str, line: int = 1) -> Symbol:
    return Symbol(
        id=f"{path}::{name}",
        name=name,
        qualname=name,
        kind="class",
        path=path,
        language="python",
        start_line=line,
        end_line=line + 1,
    )


def _fn(path: str, name: str, line: int = 1) -> Symbol:
    return Symbol(
        id=f"{path}::{name}",
        name=name,
        qualname=name,
        kind="function",
        path=path,
        language="python",
        start_line=line,
        end_line=line + 1,
    )


def _heritage(
    subtype_id: str,
    path: str,
    name: str,
    text: str | None = None,
    receiver: str | None = None,
    relation: str = "extends",
    line: int = 1,
) -> RawHeritage:
    return RawHeritage(
        subtype_id=subtype_id,
        path=path,
        text=text or name,
        name=name,
        receiver=receiver,
        relation=relation,
        line=line,
    )


def test_same_file_resolution() -> None:
    base = _cls("a.py", "Base")
    foo = _cls("a.py", "Foo", line=3)
    files = [
        FileMap(
            "a.py",
            "python",
            symbols=[base, foo],
            heritage=[_heritage("a.py::Foo", "a.py", "Base")],
        )
    ]
    graph = resolve(files)
    assert graph.heritage_out["a.py::Foo"] == ["a.py::Base"]
    assert graph.heritage_in["a.py::Base"] == ["a.py::Foo"]
    assert graph.heritage[0].relation == "extends"
    assert graph.heritage_ambiguous == []
    assert graph.heritage_external == []


def test_cross_file_resolution_via_import_hint() -> None:
    base = _cls("base.py", "Base")
    foo = _cls("foo.py", "Foo")
    files = [
        FileMap("base.py", "python", symbols=[base]),
        FileMap(
            "foo.py",
            "python",
            symbols=[foo],
            imports=[Import(path="foo.py", name="Base", source="base.Base")],
            heritage=[_heritage("foo.py::Foo", "foo.py", "Base")],
        ),
    ]
    graph = resolve(files)
    assert graph.heritage_out["foo.py::Foo"] == ["base.py::Base"]


def test_unique_repo_wide_name_fallback() -> None:
    # No same-file candidate, no import hint — the name is unique
    # repo-wide, so the ladder's fast path resolves it.
    base = _cls("distant.py", "UniquelyNamedBase")
    foo = _cls("foo.py", "Foo")
    files = [
        FileMap("distant.py", "python", symbols=[base]),
        FileMap(
            "foo.py",
            "python",
            symbols=[foo],
            heritage=[_heritage("foo.py::Foo", "foo.py", "UniquelyNamedBase")],
        ),
    ]
    graph = resolve(files)
    assert graph.heritage_out["foo.py::Foo"] == [
        "distant.py::UniquelyNamedBase"
    ]


def test_same_named_types_in_two_files_are_ambiguous() -> None:
    base1 = _cls("a.py", "Base")
    base2 = _cls("b.py", "Base")
    foo = _cls("c.py", "Foo")
    files = [
        FileMap("a.py", "python", symbols=[base1]),
        FileMap("b.py", "python", symbols=[base2]),
        FileMap(
            "c.py",
            "python",
            symbols=[foo],
            heritage=[_heritage("c.py::Foo", "c.py", "Base")],
        ),
    ]
    graph = resolve(files)
    assert graph.heritage_out == {}
    assert graph.heritage_ambiguous == [
        ("c.py::Foo", "Base", ["a.py::Base", "b.py::Base"])
    ]


def test_cross_language_name_collision_no_longer_ambiguous() -> None:
    """Round 21 (tensorflow.md §4/§7, Track D): before the language-
    aware candidate pre-filter, a bare type name colliding with a
    same-named type *anywhere* in the repo -- even a completely
    unrelated one in a different language -- landed the whole clause
    in ``heritage_ambiguous``, dropping a locally, unambiguously
    determinable real target purely because ``_pick_candidate`` never
    compared candidate/call-site language. Contrast with
    ``test_same_named_types_in_two_files_are_ambiguous`` above, a
    genuine same-language collision, which must stay ambiguous
    unchanged (not this fix's target -- see round 21's
    IMPLEMENTATION-PLAN.md Track D, "substantially improve, but not
    fully fix, Issue 7")."""
    base_py = Symbol(
        id="unrelated/base.py::Base",
        name="Base",
        qualname="Base",
        kind="class",
        path="unrelated/base.py",
        language="python",
    )
    base_cpp = Symbol(
        id="kernels/base.cc::Base",
        name="Base",
        qualname="Base",
        kind="class",
        path="kernels/base.cc",
        language="cpp",
    )
    foo = Symbol(
        id="kernels/foo.cc::Foo",
        name="Foo",
        qualname="Foo",
        kind="class",
        path="kernels/foo.cc",
        language="cpp",
    )
    files = [
        FileMap("unrelated/base.py", "python", symbols=[base_py]),
        FileMap("kernels/base.cc", "cpp", symbols=[base_cpp]),
        FileMap(
            "kernels/foo.cc",
            "cpp",
            symbols=[foo],
            heritage=[
                _heritage("kernels/foo.cc::Foo", "kernels/foo.cc", "Base")
            ],
        ),
    ]
    graph = resolve(files)
    assert graph.heritage_out["kernels/foo.cc::Foo"] == [
        "kernels/base.cc::Base"
    ]
    assert graph.heritage_ambiguous == []


def test_cross_family_heritage_miss_lands_in_ambiguous() -> None:
    """Heritage-path counterpart to ``test_resolve_call_records_cross_
    family_miss_as_ambiguous`` (`.features/fixes/resolver-vendored-
    exclusion-false-match.md`): a C++ base-class name with only a
    same-bare-name, unrelated-language Python candidate (no C/C++
    family candidate at all -- simulating the real C++ base living
    outside the map, e.g. under an excluded vendored directory) must
    land in ``heritage_ambiguous``, not silently resolve to the Python
    class. ``resolve_heritage`` shares ``_pick_candidate`` with
    ``resolve()``, so it must get the identical fail-safe guarantee
    the call-resolution test above pins down."""
    base_py = Symbol(
        id="unrelated/base.py::Base",
        name="Base",
        qualname="Base",
        kind="class",
        path="unrelated/base.py",
        language="python",
    )
    foo = Symbol(
        id="kernels/foo.cc::Foo",
        name="Foo",
        qualname="Foo",
        kind="class",
        path="kernels/foo.cc",
        language="cpp",
    )
    files = [
        FileMap("unrelated/base.py", "python", symbols=[base_py]),
        FileMap(
            "kernels/foo.cc",
            "cpp",
            symbols=[foo],
            heritage=[
                _heritage("kernels/foo.cc::Foo", "kernels/foo.cc", "Base")
            ],
        ),
    ]
    graph = resolve(files)
    assert graph.heritage_out == {}
    assert graph.heritage_ambiguous == [
        ("kernels/foo.cc::Foo", "Base", ["unrelated/base.py::Base"])
    ]


def test_bare_name_with_no_in_repo_candidate_is_external() -> None:
    foo = _cls("a.py", "MyModel")
    files = [
        FileMap(
            "a.py",
            "python",
            symbols=[foo],
            heritage=[_heritage("a.py::MyModel", "a.py", "BaseModel")],
        )
    ]
    graph = resolve(files)
    assert graph.heritage_out == {}
    assert graph.heritage_ambiguous == []
    assert len(graph.heritage_external) == 1
    ext = graph.heritage_external[0]
    assert ext.caller == "a.py::MyModel"
    assert ext.callee == "BaseModel"


def test_qualified_receiver_pointing_outside_repo_is_external() -> None:
    # `class MyModel(pydantic.BaseModel):` where the repo happens to
    # also define its own class named `BaseModel` elsewhere — the
    # receiver ("pydantic") is a locally-imported name whose source
    # matches no file in the repo, so this must resolve external
    # rather than accidentally landing on the repo's own same-named
    # class (mirrors `_receiver_is_external`'s call-graph precedent).
    unrelated_base = _cls("unrelated.py", "BaseModel")
    foo = _cls("a.py", "MyModel")
    files = [
        FileMap("unrelated.py", "python", symbols=[unrelated_base]),
        FileMap(
            "a.py",
            "python",
            symbols=[foo],
            imports=[Import(path="a.py", name="pydantic", source="pydantic")],
            heritage=[
                _heritage(
                    "a.py::MyModel",
                    "a.py",
                    "BaseModel",
                    text="pydantic.BaseModel",
                    receiver="pydantic",
                )
            ],
        ),
    ]
    graph = resolve(files)
    assert graph.heritage_out == {}
    assert len(graph.heritage_external) == 1
    assert graph.heritage_external[0].callee == "pydantic.BaseModel"


def test_candidates_filtered_to_type_kinds() -> None:
    # A same-named *function* must never be picked as a resolved
    # supertype — heritage candidates are pre-filtered to TYPE_KINDS
    # before the ladder runs at all.
    fn = _fn("a.py", "Base")
    foo = _cls("b.py", "Foo")
    files = [
        FileMap("a.py", "python", symbols=[fn]),
        FileMap(
            "b.py",
            "python",
            symbols=[foo],
            heritage=[_heritage("b.py::Foo", "b.py", "Base")],
        ),
    ]
    graph = resolve(files)
    assert graph.heritage_out == {}
    # No real TYPE_KINDS candidate exists for "Base" once the function
    # is filtered out, so this is an honest external, not a dropped
    # resolution.
    assert len(graph.heritage_external) == 1


def test_relation_kept_per_edge() -> None:
    base = _cls("a.py", "Base")
    iface = _cls("a.py", "IFoo", line=3)
    foo = _cls("a.py", "Foo", line=5)
    files = [
        FileMap(
            "a.py",
            "python",
            symbols=[base, iface, foo],
            heritage=[
                _heritage("a.py::Foo", "a.py", "Base", relation="extends"),
                _heritage("a.py::Foo", "a.py", "IFoo", relation="implements"),
            ],
        )
    ]
    graph = resolve(files)
    by_supertype = {e.supertype: e.relation for e in graph.heritage}
    assert by_supertype == {
        "a.py::Base": "extends",
        "a.py::IFoo": "implements",
    }


def test_heritage_lines_deduplicated_and_sorted() -> None:
    base = _cls("a.py", "Base")
    foo = _cls("a.py", "Foo", line=3)
    files = [
        FileMap(
            "a.py",
            "python",
            symbols=[base, foo],
            heritage=[
                _heritage("a.py::Foo", "a.py", "Base", line=5),
                _heritage("a.py::Foo", "a.py", "Base", line=3),
            ],
        )
    ]
    graph = resolve(files)
    assert graph.heritage[0].lines == [3, 5]


def test_type_fixture_repo_resolves(tmp_path: Path) -> None:
    (tmp_path / "base.py").write_text("class Animal:\n    pass\n")
    (tmp_path / "dog.py").write_text(
        "from base import Animal\n\n\nclass Dog(Animal):\n    pass\n"
    )
    from dekko.core import languages
    from dekko.core.extractor import extract_file

    spec = languages.spec_for_path("x.py")
    assert spec is not None
    files = [
        extract_file(tmp_path, "base.py", spec),
        extract_file(tmp_path, "dog.py", spec),
    ]
    graph = resolve(files)
    assert graph.heritage_out["dog.py::Dog"] == ["base.py::Animal"]


# ---------------------------------------------------------------------
def test_cpp_heritage_disambiguated_via_whole_file_include() -> None:
    """Round 22 tensorflow.md §5 (``resolve_heritage`` never built or
    threaded ``raw_imports`` at all, unlike ``_resolve_files_chunk``'s
    call-resolution path — see ``test_resolver.py::
    test_cpp_call_disambiguated_via_whole_file_include`` for the
    call-path counterpart this mirrors). Two files each declare an
    unrelated, same-bare-name ``OpKernel`` class; a third file
    ``#include``s only one of them and declares ``class Derived :
    public OpKernel {}`` — no receiver, no same-file, no per-name
    import hint (C++ ``#include`` binds no single symbol name), so
    only the whole-file-include fallback can disambiguate. Before
    threading ``raw_imports`` through, this always landed in
    ``heritage_ambiguous``."""
    real = Symbol(
        id="kernels/op_kernel.h::OpKernel",
        name="OpKernel",
        qualname="OpKernel",
        kind="class",
        path="kernels/op_kernel.h",
        language="cpp",
    )
    unrelated = Symbol(
        id="other/pkg/kernel_base.h::OpKernel",
        name="OpKernel",
        qualname="OpKernel",
        kind="class",
        path="other/pkg/kernel_base.h",
        language="cpp",
    )
    derived = Symbol(
        id="kernels/my_op.cc::MyOp",
        name="MyOp",
        qualname="MyOp",
        kind="class",
        path="kernels/my_op.cc",
        language="cpp",
    )
    files = [
        FileMap("kernels/op_kernel.h", "cpp", symbols=[real]),
        FileMap("other/pkg/kernel_base.h", "cpp", symbols=[unrelated]),
        FileMap(
            "kernels/my_op.cc",
            "cpp",
            symbols=[derived],
            imports=[
                Import(
                    path="kernels/my_op.cc",
                    name="op_kernel",
                    source="kernels/op_kernel.h",
                )
            ],
            heritage=[
                _heritage(
                    "kernels/my_op.cc::MyOp",
                    "kernels/my_op.cc",
                    "OpKernel",
                )
            ],
        ),
    ]
    graph = resolve(files)
    assert graph.heritage_out["kernels/my_op.cc::MyOp"] == [
        "kernels/op_kernel.h::OpKernel"
    ]
    assert graph.heritage_ambiguous == []


def test_cpp_heritage_stays_ambiguous_when_no_include_matches() -> None:
    """Regression guard mirroring ``test_resolver.py::
    test_cpp_call_stays_ambiguous_when_no_include_matches``: the
    whole-file-include fallback must not fire when nothing in the
    file's own ``#include`` list matches either candidate — the clause
    must still land in ``heritage_ambiguous``, not guess."""
    real = Symbol(
        id="kernels/op_kernel.h::OpKernel",
        name="OpKernel",
        qualname="OpKernel",
        kind="class",
        path="kernels/op_kernel.h",
        language="cpp",
    )
    unrelated = Symbol(
        id="other/pkg/kernel_base.h::OpKernel",
        name="OpKernel",
        qualname="OpKernel",
        kind="class",
        path="other/pkg/kernel_base.h",
        language="cpp",
    )
    derived = Symbol(
        id="kernels/my_op.cc::MyOp",
        name="MyOp",
        qualname="MyOp",
        kind="class",
        path="kernels/my_op.cc",
        language="cpp",
    )
    files = [
        FileMap("kernels/op_kernel.h", "cpp", symbols=[real]),
        FileMap("other/pkg/kernel_base.h", "cpp", symbols=[unrelated]),
        FileMap(
            "kernels/my_op.cc",
            "cpp",
            symbols=[derived],
            imports=[
                Import(
                    path="kernels/my_op.cc",
                    name="unrelated_header",
                    source="kernels/unrelated_header.h",
                )
            ],
            heritage=[
                _heritage(
                    "kernels/my_op.cc::MyOp",
                    "kernels/my_op.cc",
                    "OpKernel",
                )
            ],
        ),
    ]
    graph = resolve(files)
    assert graph.heritage_out == {}
    assert graph.heritage_ambiguous == [
        (
            "kernels/my_op.cc::MyOp",
            "OpKernel",
            [
                "kernels/op_kernel.h::OpKernel",
                "other/pkg/kernel_base.h::OpKernel",
            ],
        )
    ]


# Phase 2: Rust / C++ (real extraction, end to end through resolve())


def test_rust_impl_trait_resolves_same_file(tmp_path: Path) -> None:
    from dekko.core import languages
    from dekko.core.extractor import extract_file

    (tmp_path / "a.rs").write_text(
        "trait Super {}\nstruct Foo;\n\nimpl Super for Foo {}\n"
    )
    spec = languages.spec_for_path("a.rs")
    assert spec is not None
    graph = resolve([extract_file(tmp_path, "a.rs", spec)])
    assert graph.heritage_out["a.rs::Foo"] == ["a.rs::Super"]
    assert graph.heritage[0].relation == "impl"


def test_rust_impl_trait_resolves_cross_file_via_import(
    tmp_path: Path,
) -> None:
    from dekko.core import languages
    from dekko.core.extractor import extract_file

    (tmp_path / "shapes.rs").write_text("pub trait Shape {}\n")
    (tmp_path / "circle.rs").write_text(
        "use crate::shapes::Shape;\n"
        "\n"
        "struct Circle;\n"
        "\n"
        "impl Shape for Circle {}\n"
    )
    spec = languages.spec_for_path("a.rs")
    assert spec is not None
    files = [
        extract_file(tmp_path, "shapes.rs", spec),
        extract_file(tmp_path, "circle.rs", spec),
    ]
    graph = resolve(files)
    assert graph.heritage_out["circle.rs::Circle"] == ["shapes.rs::Shape"]


def test_rust_impl_unknown_trait_is_external(tmp_path: Path) -> None:
    from dekko.core import languages
    from dekko.core.extractor import extract_file

    (tmp_path / "a.rs").write_text(
        "struct Foo;\n\nimpl std::fmt::Debug for Foo {}\n"
    )
    spec = languages.spec_for_path("a.rs")
    assert spec is not None
    graph = resolve([extract_file(tmp_path, "a.rs", spec)])
    assert graph.heritage_out == {}
    assert len(graph.heritage_external) == 1
    assert graph.heritage_external[0].callee == "std::fmt::Debug"


def test_rust_impl_trait_resolves_via_crate_root_reexport(
    tmp_path: Path,
) -> None:
    """Round 22 zed.md §3.2: ``Render`` is declared in ``gpui``'s
    ``element.rs``, only reachable elsewhere via the crate root's
    ``pub use element::*;`` re-export, and collides repo-wide with an
    unrelated, differently-named trait in a completely different
    crate. ``impl Render for Editor`` imports it as ``use
    gpui::Render;`` -- a hint ``_module_matches`` can never match
    against ``element.rs``'s own stem, so before threading
    ``_rust_crate_roots_index`` through ``_import_match``, this always
    fell through to ``heritage_ambiguous`` (dekko 0.43.8, confirmed
    reproducing live against the real zed checkout)."""
    from dekko.core import languages
    from dekko.core.extractor import extract_file

    (tmp_path / "crates" / "gpui" / "src").mkdir(parents=True)
    (tmp_path / "crates" / "gpui" / "src" / "gpui.rs").write_text(
        "pub use element::*;\n"
    )
    (tmp_path / "crates" / "gpui" / "src" / "element.rs").write_text(
        "pub trait Render {}\n"
    )
    (tmp_path / "crates" / "other" / "src").mkdir(parents=True)
    (tmp_path / "crates" / "other" / "src" / "lib.rs").write_text(
        "pub trait Render {}\n"
    )
    (tmp_path / "crates" / "editor" / "src").mkdir(parents=True)
    (tmp_path / "crates" / "editor" / "src" / "editor.rs").write_text(
        "use gpui::Render;\n\nstruct Editor;\n\nimpl Render for Editor {}\n"
    )
    spec = languages.spec_for_path("a.rs")
    assert spec is not None
    rel_paths = [
        "crates/gpui/src/gpui.rs",
        "crates/gpui/src/element.rs",
        "crates/other/src/lib.rs",
        "crates/editor/src/editor.rs",
    ]
    files = [extract_file(tmp_path, p, spec) for p in rel_paths]
    graph = resolve(files)
    assert graph.heritage_out["crates/editor/src/editor.rs::Editor"] == [
        "crates/gpui/src/element.rs::Render"
    ]
    assert graph.heritage_ambiguous == []


def test_cpp_multiple_inheritance_resolves(tmp_path: Path) -> None:
    from dekko.core import languages
    from dekko.core.extractor import extract_file

    (tmp_path / "shapes.cpp").write_text(
        "class Base1 {};\n"
        "class Base2 {};\n"
        "class Derived : public Base1, private Base2 {\n"
        "public:\n"
        "};\n"
    )
    spec = languages.spec_for_path("a.cpp")
    assert spec is not None
    graph = resolve([extract_file(tmp_path, "shapes.cpp", spec)])
    assert graph.heritage_out["shapes.cpp::Derived"] == [
        "shapes.cpp::Base1",
        "shapes.cpp::Base2",
    ]
    assert all(e.relation == "extends" for e in graph.heritage)
