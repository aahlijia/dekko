"""A fully written C++ path resolves only to a symbol on that path.

``a::b::Name(..)`` and ``::ns::Name(..)`` name their target's scopes,
and C++ qualnames spell them, so the target's qualname has to end
with the written path (or, from the root, be all of it). A path that
names no candidate is external, never a guess, and a ``std``-rooted
path never reaches the repo.
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


def _externals(graph: CallGraph, caller: str) -> set[str]:
    return {e.callee for e in graph.external if e.caller == caller}


_USE = "u/user.cc::Use"


def _user(body: str) -> str:
    return f"void Use() {{\n{body}\n}}\n"


# Two functions named F, one in a::b and one in c::b, plus one more
# F in x: only the written path tells them apart.
_F_DEFS = {
    "lib/ab.cc": "namespace a { namespace b { void F() {} } }\n",
    "lib/cb.cc": "namespace c { namespace b { void F() {} } }\n",
    "lib/x.cc": "namespace x { void F() {} }\n",
}


def test_qualified_call_resolves_to_the_symbol_on_its_path(
    tmp_path: Path,
) -> None:
    graph = _graph(tmp_path, {**_F_DEFS, "u/user.cc": _user("  a::b::F();")})
    assert _pairs(graph, _USE) == {"lib/ab.cc::a.b.F"}
    assert _ambiguous_for(graph, _USE) == {}


def test_path_relative_to_an_enclosing_namespace_resolves(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            "lib/abc.cc": (
                "namespace a { namespace b { namespace c { void F() {} } } }\n"
            ),
            **{k: v for k, v in _F_DEFS.items() if k != "lib/ab.cc"},
            "u/user.cc": "namespace a {\n" + _user("  b::c::F();") + "}\n",
        },
    )
    assert _pairs(graph, "u/user.cc::a.Use") == {"lib/abc.cc::a.b.c.F"}


def test_path_on_no_candidate_is_external(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        {
            "lib/x.cc": "namespace x { void F() {} }\n",
            "u/user.cc": _user("  tsl::errors::F();"),
        },
    )
    assert _pairs(graph, _USE) == set()
    assert _ambiguous_for(graph, _USE) == {}
    assert _externals(graph, _USE) == {"tsl::errors::F"}


def test_std_rooted_path_never_reaches_a_repo_specialization(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            "lib/types.h": (
                "namespace std {\n"
                "template <> class numeric_limits<Half> {\n"
                " public:\n"
                "  static Half max() { return Half(); }\n"
                "};\n"
                "}\n"
            ),
            "u/user.cc": _user("  std::numeric_limits<int>::max();"),
        },
    )
    assert _pairs(graph, _USE) == set()
    assert _externals(graph, _USE) == {"std::numeric_limits<int>::max"}


def test_path_from_the_root_must_be_the_whole_qualname(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            "lib/a.cc": "namespace a { void F() {} }\n",
            "lib/xa.cc": "namespace x { namespace a { void F() {} } }\n",
            "u/user.cc": _user("  ::a::F();"),
        },
    )
    assert _pairs(graph, _USE) == {"lib/a.cc::a.F"}


def test_rooted_foreign_namespace_does_not_reach_a_repo_wrapper(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            "tf/status.h": (
                "namespace tensorflow {\n"
                "inline int OkStatus() { return ::absl::OkStatus(); }\n"
                "}\n"
            ),
            "u/user.cc": _user("  ::absl::OkStatus();"),
        },
    )
    assert _pairs(graph, _USE) == set()
    assert _pairs(graph, "tf/status.h::tensorflow.OkStatus") == set()
    assert _externals(graph, _USE) == {"::absl::OkStatus"}


def test_overloads_on_the_path_are_the_only_ambiguous_candidates(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            "lib/ab1.cc": "namespace a { namespace b { void F(int x) {} } }\n",
            "lib/ab2.cc": "namespace a { namespace b { void F(int y) {} } }\n",
            "lib/cb.cc": "namespace c { namespace b { void F(int z) {} } }\n",
            "u/user.cc": _user("  a::b::F(1);"),
        },
    )
    assert _pairs(graph, _USE) == set()
    assert _ambiguous_for(graph, _USE) == {
        "F": ["lib/ab1.cc::a.b.F", "lib/ab2.cc::a.b.F"],
    }


def test_qualified_construction_credits_the_constructor_by_arity(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            "lib/graph.h": (
                "namespace a { namespace b {\n"
                "class Graph {\n"
                " public:\n"
                "  Graph(int x) {}\n"
                "  Graph(int x, int y) {}\n"
                "};\n"
                "} }\n"
            ),
            "lib/other.h": (
                "namespace c { namespace b {\n"
                "class Graph {\n"
                " public:\n"
                "  Graph(int x) {}\n"
                "};\n"
                "} }\n"
            ),
            "u/user.cc": _user("  new a::b::Graph(1);"),
        },
    )
    assert _pairs(graph, _USE) == {
        "lib/graph.h::a.b.Graph",
        "lib/graph.h::a.b.Graph.Graph",
    }


def test_unique_path_match_is_not_second_guessed_by_name(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            "lib/ab.cc": "namespace a { namespace b { int get() {} } }\n",
            "lib/cb.cc": "namespace c { namespace b { int get() {} } }\n",
            "u/user.cc": _user("  a::b::get();"),
        },
    )
    assert _pairs(graph, _USE) == {"lib/ab.cc::a.b.get"}


def test_one_scope_call_still_goes_through_the_ladder(tmp_path: Path) -> None:
    # A one-scope ``x::F()`` isn't a fully written path; it resolves the
    # way it always has.
    graph = _graph(
        tmp_path,
        {
            "lib/x.cc": "namespace x { void F() {} }\n",
            "u/user.cc": _user("  x::F();"),
        },
    )
    assert _pairs(graph, _USE) == {"lib/x.cc::x.F"}


def test_qualified_base_class_resolves_to_the_type_on_its_path(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            "lib/ab.h": "namespace a { namespace b { class Base {}; } }\n",
            "lib/cb.h": "namespace c { namespace b { class Base {}; } }\n",
            "u/user.cc": "class A : public a::b::Base {};\n",
        },
    )
    assert graph.heritage_out["u/user.cc::A"] == ["lib/ab.h::a.b.Base"]
    assert graph.heritage_ambiguous == []


def test_member_call_on_a_chain_through_a_path_uses_the_ladder(
    tmp_path: Path,
) -> None:
    # `ns::Registry::Global()->LookUpOp(..)` is a member call on what
    # `Global()` returns, not a path to `LookUpOp`; a `std::`-rooted
    # chain is no exception.
    graph = _graph(
        tmp_path,
        {
            "lib/reg.h": (
                "namespace tf {\n"
                "class Registry {\n"
                " public:\n"
                "  static Registry* Global() { return nullptr; }\n"
                "  int LookUpOp(int x) { return x; }\n"
                "  int FinishUp() { return 0; }\n"
                "};\n"
                "}\n"
            ),
            "u/user.cc": _user(
                "  tf::Registry::Global()->LookUpOp(1);\n"
                "  std::move(r).FinishUp();"
            ),
        },
    )
    pairs = _pairs(graph, _USE)
    assert "lib/reg.h::tf.Registry.LookUpOp" in pairs
    assert "lib/reg.h::tf.Registry.FinishUp" in pairs
