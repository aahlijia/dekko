"""``dekko query supertypes``/``subtypes``: heritage graph queries.

``RUST_HERITAGE``/``CPP_HERITAGE`` and their CLI tests near the end of
this file cover Phase 2 (Rust ``impl Trait for Type``, C++
``base_class_clause``) end to end through the same ``query
supertypes``/``subtypes`` surface Phase 1's Python/Java fixtures
already exercise — no query.py/CLI code changed for Phase 2, so these
confirm the read surface really is language-agnostic as designed, not
just assumed.
"""

import json

import pytest

from dekko.analysis import query
from dekko.integrations import cli
from dekko.render import mapfile
from dekko.core.model import (
    CallGraph,
    ExternalCall,
    FileMap,
    HeritageEdge,
    Symbol,
)

from conftest import RepoFactory

PY_HERITAGE = {
    "base.py": "class Animal:\n    pass\n",
    "dog.py": ("from base import Animal\n\n\nclass Dog(Animal):\n    pass\n"),
    "helper.py": "def helper():\n    pass\n",
}

JAVA_HERITAGE = {
    "Shapes.java": (
        "class Base {}\n"
        "interface IFoo {}\n"
        "interface IBar {}\n"
        "class Foo extends Base implements IFoo, IBar {}\n"
    ),
}

RUST_HERITAGE = {
    "shapes.rs": (
        "pub trait Shape {}\n"
        "\n"
        "pub struct Circle;\n"
        "\n"
        "impl Shape for Circle {}\n"
    ),
}

CPP_HERITAGE = {
    "shapes.cpp": (
        "class Base1 {};\n"
        "class Base2 {};\n"
        "class Derived : public Base1, private Base2 {\n"
        "public:\n"
        "};\n"
    ),
}


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


def test_supertypes_one_hop(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY_HERITAGE)
    code = cli.main(["query", "supertypes", "Dog", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "class Animal" in out
    assert "[extends]" in out


def test_subtypes_one_hop(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY_HERITAGE)
    code = cli.main(["query", "subtypes", "Animal", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "class Dog" in out
    assert "[extends]" in out


def test_relation_filter(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(JAVA_HERITAGE)
    code = cli.main(
        [
            "query",
            "supertypes",
            "Foo",
            "--relation",
            "implements",
            "--root",
            str(root),
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "IFoo" in out
    assert "IBar" in out
    assert "Base" not in out


def test_no_heritage_is_empty_not_error(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY_HERITAGE)
    code = cli.main(["query", "supertypes", "Animal", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "no supertypes" in out


def test_target_not_a_type_reports_specific_message(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY_HERITAGE)
    code = cli.main(["query", "supertypes", "helper", "--root", str(root)])
    assert code == query.EXIT_NOT_FOUND
    err = capsys.readouterr().err
    assert "not a type" in err
    assert "supertypes/subtypes" in err


def test_target_not_found(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY_HERITAGE)
    code = cli.main(
        ["query", "supertypes", "Nonexistent", "--root", str(root)]
    )
    assert code == query.EXIT_NOT_FOUND


def test_no_tests_excludes_test_file_subtypes(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    files = dict(
        PY_HERITAGE,
        **{
            "tests/test_dog.py": (
                "from base import Animal\n"
                "\n"
                "\n"
                "class FakeAnimal(Animal):\n"
                "    pass\n"
            )
        },
    )
    root = make_mapped_repo(files)
    code = cli.main(
        ["query", "subtypes", "Animal", "--root", str(root), "--no-tests"]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "Dog" in out
    assert "FakeAnimal" not in out


def _diamond_index() -> mapfile.MapIndex:
    """A hand-built diamond-inheritance heritage graph.

    D extends B and C; both B and C extend A. A transitive walk from D
    must find A exactly once, at depth 2 (its shallowest path), never
    twice.
    """
    files = [
        FileMap(
            path="m.py",
            language="python",
            symbols=[
                _cls("m.py", "A"),
                _cls("m.py", "B", 3),
                _cls("m.py", "C", 5),
                _cls("m.py", "D", 7),
            ],
        ),
    ]
    graph = CallGraph(
        heritage=[
            HeritageEdge(
                subtype="m.py::D", supertype="m.py::B", relation="extends"
            ),
            HeritageEdge(
                subtype="m.py::D", supertype="m.py::C", relation="extends"
            ),
            HeritageEdge(
                subtype="m.py::B", supertype="m.py::A", relation="extends"
            ),
            HeritageEdge(
                subtype="m.py::C", supertype="m.py::A", relation="extends"
            ),
        ]
    )
    return mapfile.index_from_maps(files, graph, "demo")


def test_transitive_diamond_inheritance_dedup(
    capsys: pytest.CaptureFixture,
) -> None:
    index = _diamond_index()
    code = query.run(
        index,
        "supertypes",
        "D",
        as_json=True,
        limit=50,
        transitive=True,
    )
    assert code == query.EXIT_OK
    doc = json.loads(capsys.readouterr().out)
    ids = [r["id"] for r in doc["results"]]
    # A appears exactly once, even though it's reachable via both B
    # and C.
    assert ids.count("m.py::A") == 1
    depths = {r["id"]: r["depth"] for r in doc["results"]}
    assert depths["m.py::B"] == 1
    assert depths["m.py::C"] == 1
    assert depths["m.py::A"] == 2


def test_one_hop_has_no_depth_beyond_1(capsys: pytest.CaptureFixture) -> None:
    index = _diamond_index()
    code = query.run(index, "supertypes", "D", as_json=True, limit=50)
    assert code == query.EXIT_OK
    doc = json.loads(capsys.readouterr().out)
    assert doc["transitive"] is False
    ids = {r["id"] for r in doc["results"]}
    assert ids == {"m.py::B", "m.py::C"}
    assert all(r["depth"] == 1 for r in doc["results"])


def _ambiguous_external_index() -> mapfile.MapIndex:
    files = [
        FileMap(path="a.py", language="python", symbols=[_cls("a.py", "Foo")]),
        FileMap(
            path="b.py", language="python", symbols=[_cls("b.py", "Base")]
        ),
        FileMap(
            path="c.py", language="python", symbols=[_cls("c.py", "Base")]
        ),
    ]
    graph = CallGraph(
        heritage_ambiguous=[
            ("a.py::Foo", "Base", ["b.py::Base", "c.py::Base"])
        ],
        heritage_external=[
            ExternalCall(
                caller="a.py::Foo", callee="pydantic.BaseModel", lines=[2]
            )
        ],
    )
    return mapfile.index_from_maps(files, graph, "demo")


def test_ambiguous_supertype_disclosed_as_note(
    capsys: pytest.CaptureFixture,
) -> None:
    index = _ambiguous_external_index()
    code = query.run(index, "supertypes", "Foo", as_json=False, limit=50)
    assert code == query.EXIT_OK
    err = capsys.readouterr().err
    assert "resolved ambiguously" in err


def test_external_supertype_shown_as_labeled_row(
    capsys: pytest.CaptureFixture,
) -> None:
    index = _ambiguous_external_index()
    code = query.run(index, "supertypes", "Foo", as_json=False, limit=50)
    assert code == query.EXIT_OK
    out = capsys.readouterr().out
    assert "(external) pydantic.BaseModel" in out


def test_heritage_synthetic_tiebreak_disclosed_as_note(
    capsys: pytest.CaptureFixture,
) -> None:
    # Round 24 (``.features/plans/round24/
    # 03-heritage-crate-decoy-tiebreak.md``): a nonzero repo-wide
    # tiebreak count must surface as an advisory note on both
    # ``supertypes`` and ``subtypes`` text output, not just get
    # silently dropped.
    index = _ambiguous_external_index()
    index.heritage_synthetic_tiebreak_count = 2
    code = query.run(index, "supertypes", "Foo", as_json=False, limit=50)
    assert code == query.EXIT_OK
    err = capsys.readouterr().err
    assert "2 heritage edge(s)" in err
    assert "non-test-fixture/vendor crate root" in err


def test_heritage_synthetic_tiebreak_absent_when_0(
    capsys: pytest.CaptureFixture,
) -> None:
    index = _ambiguous_external_index()
    assert index.heritage_synthetic_tiebreak_count == 0
    code = query.run(index, "supertypes", "Foo", as_json=False, limit=50)
    assert code == query.EXIT_OK
    err = capsys.readouterr().err
    assert "heritage edge(s)" not in err


def test_heritage_synthetic_tiebreak_in_json_output(
    capsys: pytest.CaptureFixture,
) -> None:
    index = _ambiguous_external_index()
    index.heritage_synthetic_tiebreak_count = 5
    code = query.run(index, "supertypes", "Foo", as_json=True, limit=50)
    assert code == query.EXIT_OK
    doc = json.loads(capsys.readouterr().out)
    assert doc["heritage_synthetic_tiebreak_count"] == 5


def test_heritage_synthetic_tiebreak_omitted_from_json_when_0(
    capsys: pytest.CaptureFixture,
) -> None:
    index = _ambiguous_external_index()
    code = query.run(index, "supertypes", "Foo", as_json=True, limit=50)
    assert code == query.EXIT_OK
    doc = json.loads(capsys.readouterr().out)
    assert "heritage_synthetic_tiebreak_count" not in doc


# round-18 claude-code finding: a TS object-type alias (``type X =
# {...}``) used with ``implements`` isn't extracted as a heritage-
# eligible symbol, so the resolver's terminal fallback previously
# labeled it ``(external)`` -- identical to a genuine out-of-repo
# framework base -- even though it's first-party code sitting one
# `query symbol` lookup away, imported from a relative path in the
# same repo.
#
# Round 26 gave ``type_alias_declaration`` a real ``Symbol`` (kind
# "type_alias", in ``TYPE_KINDS``), so the resolver's heritage
# candidate filtering (``resolver.py``, ``c.kind in TYPE_KINDS``) now
# resolves ``ShellCommand`` as a genuine cross-file supertype edge
# instead of falling through to the round-18/19 ``(unresolved)``
# presentation fallback below -- a strictly better outcome than the
# workaround these tests originally pinned.
TS_TYPE_ALIAS_HERITAGE = {
    "types.ts": "export type ShellCommand = {\n  run(): void;\n};\n",
    "impl.ts": (
        "import { ShellCommand } from './types';\n"
        "\n"
        "export class ShellCommandImpl implements ShellCommand {\n"
        "  run(): void {}\n"
        "}\n"
    ),
}


def test_type_alias_implements_target_resolves_as_real_supertype(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(TS_TYPE_ALIAS_HERITAGE)
    code = cli.main(
        ["query", "supertypes", "ShellCommandImpl", "--root", str(root)]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "types.ts" in out
    assert "type_alias ShellCommand" in out
    assert "(unresolved)" not in out
    assert "(external)" not in out


def test_type_alias_implements_target_json_resolves_no_external_flag(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(TS_TYPE_ALIAS_HERITAGE)
    code = cli.main(
        [
            "query",
            "supertypes",
            "ShellCommandImpl",
            "--root",
            str(root),
            "--json",
        ]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert not any(r.get("external") for r in doc["results"])
    hit = next(
        r for r in doc["results"] if r["id"] == "types.ts::ShellCommand"
    )
    assert hit["kind"] == "type_alias"


# round-19 claude-code finding: the round-18 fix above only catches
# the cross-file case (a same-named relative import exists to check
# against) -- a *same-file* type alias needs no import statement at
# all, so that loop never even had a candidate for ``ShellCommand``
# and the resolver's terminal fallback still mislabeled it
# ``(external)``. Same repro shape as claude-code's own
# ``src/utils/ShellCommand.ts``, just collapsed into one file.
#
# Round 26 (see cross-file variant above) makes this resolve as a real
# same-file supertype edge too, superseding the round-19
# ``(unresolved)`` presentation fallback.
TS_SAME_FILE_TYPE_ALIAS_HERITAGE = {
    "shell_command.ts": (
        "export type ShellCommand = {\n"
        "  run(): void;\n"
        "};\n"
        "\n"
        "export class ShellCommandImpl implements ShellCommand {\n"
        "  run(): void {}\n"
        "}\n"
    ),
}


def test_same_file_type_alias_implements_target_resolves_as_supertype(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(TS_SAME_FILE_TYPE_ALIAS_HERITAGE)
    code = cli.main(
        ["query", "supertypes", "ShellCommandImpl", "--root", str(root)]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "type_alias ShellCommand" in out
    assert "(unresolved)" not in out
    assert "(external)" not in out


def test_same_file_type_alias_implements_target_json_resolves(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(TS_SAME_FILE_TYPE_ALIAS_HERITAGE)
    code = cli.main(
        [
            "query",
            "supertypes",
            "ShellCommandImpl",
            "--root",
            str(root),
            "--json",
        ]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert not any(r.get("external") for r in doc["results"])
    hit = next(
        r
        for r in doc["results"]
        if r["id"] == "shell_command.ts::ShellCommand"
    )
    assert hit["kind"] == "type_alias"


def test_genuine_external_base_still_labeled_external(
    capsys: pytest.CaptureFixture,
) -> None:
    # Regression guard: a real out-of-repo base (no matching relative
    # import anywhere) must keep the plain "external" label.
    index = _ambiguous_external_index()
    code = query.run(index, "supertypes", "Foo", as_json=True, limit=50)
    assert code == query.EXIT_OK
    doc = json.loads(capsys.readouterr().out)
    ext = next(r for r in doc["results"] if r.get("external"))
    assert ext["unresolved_local"] is False


def test_json_result_shape_has_relation_and_depth(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY_HERITAGE)
    code = cli.main(
        ["query", "supertypes", "Dog", "--root", str(root), "--json"]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["action"] == "supertypes"
    assert doc["transitive"] is False
    result = doc["results"][0]
    assert result["relation"] == "extends"
    assert result["depth"] == 1


# ---------------------------------------------------------------------
# Phase 2: Rust / C++


def test_rust_supertypes_shows_impl_relation(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(RUST_HERITAGE)
    code = cli.main(["query", "supertypes", "Circle", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "trait Shape" in out
    assert "[impl]" in out


def test_rust_subtypes_finds_implementor(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(RUST_HERITAGE)
    code = cli.main(["query", "subtypes", "Shape", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "struct Circle" in out
    assert "[impl]" in out


def test_rust_relation_filter_impl(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(RUST_HERITAGE)
    code = cli.main(
        [
            "query",
            "supertypes",
            "Circle",
            "--relation",
            "impl",
            "--root",
            str(root),
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "Shape" in out


def test_cpp_supertypes_multiple_inheritance(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(CPP_HERITAGE)
    code = cli.main(["query", "supertypes", "Derived", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "Base1" in out
    assert "Base2" in out
    assert "[extends]" in out


def test_cpp_subtypes_finds_derived(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(CPP_HERITAGE)
    code = cli.main(["query", "subtypes", "Base1", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "Derived" in out
