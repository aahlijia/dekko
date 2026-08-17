"""Resolver tests for heritage clauses (resolve_heritage()).

Exercises the same candidate ladder ``resolve()`` runs for calls,
applied to ``RawHeritage`` — same-file resolution, cross-file
resolution via import hints, the unique-repo-wide-name fallback,
honest ambiguous/external buckets, and the ``TYPE_KINDS`` candidate
pre-filter that keeps a base-class name from ever resolving to a
same-named function.
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
