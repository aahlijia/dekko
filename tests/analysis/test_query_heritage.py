"""``dekko query supertypes``/``subtypes``: heritage graph queries."""

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
