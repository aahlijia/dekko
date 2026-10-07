"""Java ``X::new`` is a reference to ``X`` and, when ``X`` has one
reachable constructor, to that constructor too.

A constructor reference is a method reference: the constructor runs
when the functional interface it is passed to is invoked, and that
interface picks the overload. Before, nothing was recorded, so a class
built only through ``X::new`` had no inbound edge at all.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from dekko.analysis import sanity
from dekko.core.extractor import extract_file
from dekko.core.languages import spec_for_path
from dekko.core.model import CallGraph, Symbol
from dekko.core.resolver import resolve
from dekko.integrations import cli
from dekko.render.mapfile import MapIndex
from dekko.repo_ops import map_repository

from conftest import RepoFactory

_MAIN = "src/main/java/app"
_TEST = "src/test/java/app"


def _graph(root: Path, sources: dict[str, str]) -> CallGraph:
    for rel, text in sources.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    files, _ = map_repository(
        root,
        subpath=None,
        excludes=(),
        max_file_size=1_000_000,
    )
    return resolve(files)


def _referenced(graph: CallGraph, caller: str) -> set[str]:
    return {e.callee for e in graph.referenced if e.caller == caller}


def _ref_names(root: Path, body: str) -> list[str]:
    rel = f"{_MAIN}/Use.java"
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text(f"package app;\nclass Use {{\n{body}}}\n")
    fm = extract_file(root, rel, spec_for_path(rel))
    return [r.name for r in fm.refs]


def test_the_written_head_names_the_type(tmp_path: Path) -> None:
    names = _ref_names(
        tmp_path,
        "    void a() { Supplier<X> s = X::new; }\n"
        "    void b() { Supplier<X<String>> s = X<String>::new; }\n"
        "    void c() { Supplier<X<String>> s = X<>::new; }\n"
        "    void d() { Supplier<Outer.X> s = Outer.X::new; }\n"
        "    void e() { Supplier<Outer.X<T>> s = Outer.X<T>::new; }\n",
    )
    assert names == ["X", "X", "X", "X", "X"]


def test_arrays_and_package_paths_name_no_constructor(tmp_path: Path) -> None:
    names = _ref_names(
        tmp_path,
        "    void a() { IntFunction<int[]> f = int[]::new; }\n"
        "    void b() { IntFunction<X[]> f = X[]::new; }\n"
        "    void c() { Supplier<X> s = a.b.X::new; }\n"
        "    void d() { Supplier<List<X>> s = java.util.List<X>::new; }\n",
    )
    assert names == []


_ONE = f"{_MAIN}/One.java"
_TWO = f"{_MAIN}/Two.java"
_PRIV = f"{_MAIN}/Priv.java"
_REC = f"{_MAIN}/Rec.java"
_USE = f"{_MAIN}/Use.java"
_SOURCES = {
    _ONE: "package app;\nclass One { One(String s) { } }\n",
    _TWO: (
        "package app;\nclass Two {\n"
        "    Two(String s) { }\n"
        "    Two(int i) { }\n}\n"
    ),
    _PRIV: (
        "package app;\nclass Priv {\n"
        "    Priv(String s) { }\n"
        "    private Priv(int i) { }\n}\n"
    ),
    _REC: "package app;\nrecord Rec(String s) { }\n",
    _USE: (
        "package app;\n"
        "import java.util.function.Function;\n"
        "class Use {\n"
        "    void one() { Function<String, One> f = One::new; }\n"
        "    void two() { Function<String, Two> f = Two::new; }\n"
        "    void priv() { Function<String, Priv> f = Priv::new; }\n"
        "    void rec() { Function<String, Rec> f = Rec::new; }\n"
        "    void lib() { Function<String, StringBuilder> f ="
        " StringBuilder::new; }\n"
        "}\n"
    ),
}


def test_a_sole_constructor_is_referenced_with_its_type(
    tmp_path: Path,
) -> None:
    graph = _graph(tmp_path, _SOURCES)
    use = f"{_USE}::Use"
    assert _referenced(graph, f"{use}.one") == {
        f"{_ONE}::One",
        f"{_ONE}::One.One",
    }
    # Two constructors: the functional interface picks, so the class.
    assert _referenced(graph, f"{use}.two") == {f"{_TWO}::Two"}
    # A private constructor can't be the one this file runs.
    assert _referenced(graph, f"{use}.priv") == {
        f"{_PRIV}::Priv",
        f"{_PRIV}::Priv.Priv",
    }
    # A record's ``::new`` runs its canonical constructor.
    assert _referenced(graph, f"{use}.rec") == {
        f"{_REC}::Rec",
        f"{_REC}::Rec.Rec",
    }
    assert _referenced(graph, f"{use}.lib") == set()
    # A reference is never a call.
    assert not [e for e in graph.edges if e.caller.startswith(use)]


def test_query_callers_and_unused_see_the_reference(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(_SOURCES)
    target = f"{_TWO}:Two"
    assert cli.main(["query", "callers", target, "--root", str(root)]) == 0
    out = capsys.readouterr().out
    assert "referenced (not called):" in out
    assert "Use.java" in out

    cli.main(["unused", "--kinds", "all", "--json", "--root", str(root)])
    doc = json.loads(capsys.readouterr().out)
    flagged = {row["id"] for row in doc["results"]}
    referenced = {
        f"{_ONE}::One",
        f"{_ONE}::One.One",
        f"{_TWO}::Two",
        f"{_REC}::Rec",
        f"{_REC}::Rec.Rec",
    }
    assert not flagged & referenced


def _sanity_rows(
    root: Path, target: str, capsys: pytest.CaptureFixture, *extra: str
) -> dict[tuple[str, int], str]:
    args = ["sanity", target, "--root", str(root), "--json", *extra]
    assert cli.main(args) == 0
    doc = json.loads(capsys.readouterr().out)
    return {(r["file"], r["line"]): r["cause"] for r in doc["grep_only"]}


def test_sanity_credits_a_reference_from_another_source_set(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    test_file = f"{_TEST}/OneTest.java"
    root = make_mapped_repo(
        {
            _ONE: _SOURCES[_ONE],
            test_file: (
                "package app;\n"
                "import java.util.function.Function;\n"
                "class OneTest {\n"
                "    void t() { Function<String, One> f = One::new; }\n"
                "}\n"
            ),
        }
    )
    rows = _sanity_rows(root, f"{_ONE}:One", capsys, "--include-tests")
    assert rows[(test_file, 4)] == sanity.CAUSE_VALUE_REFERENCE


def _sym(path: str, language: str) -> Symbol:
    return Symbol(
        id=f"{path}::Thing",
        name="Thing",
        qualname="Thing",
        kind="class",
        path=path,
        language=language,
    )


def test_sanity_sees_jvm_packages_and_go_directories() -> None:
    index = cast(MapIndex, SimpleNamespace(imports_by_path={}))
    main_java = _sym("m/src/main/java/org/x/Thing.java", "java")
    assert sanity._can_see(index, "m/src/test/java/org/x/T.java", main_java)
    assert sanity._can_see(index, "m/src/test/kotlin/org/x/T.kt", main_java)
    assert not sanity._can_see(
        index, "m/src/test/java/org/y/T.java", main_java
    )
    # No source root to read a package from: the directory decides.
    loose = _sym("lib/org/x/Thing.java", "java")
    assert sanity._can_see(index, "lib/org/x/Other.java", loose)
    assert not sanity._can_see(index, "tools/org/x/Other.java", loose)
    # Go's package is its directory.
    go = _sym("src/main/java/org/x/thing.go", "go")
    assert not sanity._can_see(index, "src/test/java/org/x/t.go", go)
    assert sanity._can_see(index, "src/main/java/org/x/t.go", go)


def test_sanity_labels_a_constructors_own_class_reference(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(_SOURCES)
    rows = _sanity_rows(root, f"{_TWO}:Two.Two:3", capsys)
    assert rows[(_USE, 5)] == sanity.CAUSE_CONSTRUCTOR_REFERENCE
