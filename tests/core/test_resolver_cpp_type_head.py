"""A one-scope C++ ``Type::name`` call reaches the type's own member.

C++ qualnames carry their namespaces (``tf.F.Make``), so a call
written ``F::Make()`` can't be matched by the bare qualname
``F.Make``. When the written scope is a single in-repo type, the
candidates whose qualname ends with ``F.Make`` are the ones it can
mean: one of them is the target, two or more stay a tie. A chain that
only starts with a type head is left to the rest of the ladder.
"""

from pathlib import Path

from dekko.core.model import CallGraph
from dekko.core.resolver import resolve
from dekko.repo_ops import map_repository


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


def _pairs(graph: CallGraph, caller: str) -> set[str]:
    return {e.callee for e in graph.edges if e.caller == caller}


def _ambiguous_for(graph: CallGraph, caller: str) -> dict[str, list[str]]:
    return {name: ids for c, name, ids in graph.ambiguous if c == caller}


_USE = "u/user.cc::tf.Use"

# Another type's ``Make`` and a free ``Make``, so the name alone ties.
_OTHERS = (
    "namespace tf {\n"
    "class G {\n"
    " public:\n"
    "  int Make() { return 1; }\n"
    "};\n"
    "int Make() { return 2; }\n"
    "}\n"
)


def _user(body: str) -> str:
    return f"namespace tf {{\nvoid Use() {{\n{body}\n}}\n}}\n"


def test_type_head_reaches_its_namespaced_member(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        {
            "lib/f.h": (
                "namespace tf {\n"
                "class D {};\n"
                "class F {\n"
                " public:\n"
                "  static D* Make();\n"
                "};\n"
                "}\n"
            ),
            "lib/f.cc": (
                '#include "lib/f.h"\n'
                "namespace tf {\n"
                "D* F::Make() { return nullptr; }\n"
                "}\n"
            ),
            "lib/g.cc": _OTHERS,
            "u/user.cc": _user("  F::Make();"),
        },
    )
    assert _pairs(graph, _USE) == {"lib/f.cc::tf.F.Make"}
    assert _ambiguous_for(graph, _USE) == {}


def test_two_types_of_that_name_stay_a_tie(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        {
            "lib/a.cc": (
                "namespace a {\n"
                "class F {\n"
                " public:\n"
                "  static int Make() { return 1; }\n"
                "};\n"
                "}\n"
            ),
            "lib/b.cc": (
                "namespace b {\n"
                "class F {\n"
                " public:\n"
                "  static int Make() { return 2; }\n"
                "};\n"
                "}\n"
            ),
            "lib/g.cc": _OTHERS,
            "u/user.cc": _user("  F::Make();"),
        },
    )
    assert _pairs(graph, _USE) == set()
    tied = _ambiguous_for(graph, _USE)["Make"]
    assert {"lib/a.cc::a.F.Make", "lib/b.cc::b.F.Make"} <= set(tied)


def test_chain_after_a_type_head_is_left_to_the_ladder(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            "lib/f.cc": (
                "namespace tf {\n"
                "class R {\n"
                " public:\n"
                "  void Run() {}\n"
                "};\n"
                "class F {\n"
                " public:\n"
                "  static R* Make() { return nullptr; }\n"
                "};\n"
                "}\n"
            ),
            "lib/h.cc": (
                "namespace tf {\nclass H {\n public:\n  void Run() {}\n};\n}\n"
            ),
            "lib/g.cc": _OTHERS,
            "u/user.cc": _user("  F::Make()->Run();"),
        },
    )
    # ``Run`` isn't written through a scope; the walk types the head's
    # return as it always has.
    assert _pairs(graph, _USE) == {
        "lib/f.cc::tf.F.Make",
        "lib/f.cc::tf.R.Run",
    }


def test_global_namespace_type_matches_by_qualname(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        {
            "lib/f.cc": (
                "class F {\n public:\n  static int Make() { return 1; }\n};\n"
            ),
            "lib/g.cc": _OTHERS,
            "u/user.cc": _user("  F::Make();"),
        },
    )
    assert _pairs(graph, _USE) == {"lib/f.cc::F.Make"}
