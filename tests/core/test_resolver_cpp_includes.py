"""A C/C++ ``#include`` names a file by path and binds no name.

The whole-file include rung accepts a candidate only when an include
names its file: a path include by a component-boundary suffix (with
the header pairing its same-stem source file), a bare include by the
including file's own directory or a stem no other directory has. A
header stem is never a local binding, and C++ standard-library member
names go external from a C/C++ call site only.
"""

from pathlib import Path

import pytest

from dekko.core.model import CallGraph
from dekko.core.resolver import is_guarded_method_name, resolve
from dekko.repo_ops import map_repository

_FROB = "int Frob(int x) { return x; }\n"


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


def _user(include: str, body: str = "  Frob(1);") -> str:
    return f"#include {include}\nvoid Use() {{\n{body}\n}}\n"


def test_a_path_include_matches_its_own_header_pair(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        {
            "a/b/status.cc": _FROB,
            "c/d/status.cc": _FROB,
            "app/user.cc": _user('"a/b/status.h"'),
        },
    )
    assert _pairs(graph, "app/user.cc::Use") == {"a/b/status.cc::Frob"}


def test_a_path_include_does_not_match_a_same_stem_file_elsewhere(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            "a/b/status.cc": _FROB,
            "x/y/other.cc": _FROB,
            "app/user.cc": _user('"c/d/status.h"'),
        },
    )
    assert _pairs(graph, "app/user.cc::Use") == set()
    assert _ambiguous_for(graph, "app/user.cc::Use") == {
        "Frob": ["a/b/status.cc::Frob", "x/y/other.cc::Frob"],
    }


def test_a_relative_path_include_drops_its_leading_dots(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            "lib/core/util.cc": _FROB,
            "other/core/util.cc": _FROB,
            "lib/app/user.cc": _user('"../core/util.h"'),
        },
    )
    # ``core/util`` is a suffix of both: the include alone can't choose.
    assert _pairs(graph, "lib/app/user.cc::Use") == set()
    graph = _graph(
        tmp_path / "second",
        {
            "lib/core/util.cc": _FROB,
            "other/misc.cc": _FROB,
            "lib/app/user.cc": _user('"../core/util.h"'),
        },
    )
    assert _pairs(graph, "lib/app/user.cc::Use") == {"lib/core/util.cc::Frob"}


def test_a_bare_include_matches_its_sibling_not_another_directory(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            "lib/util.cc": _FROB,
            "other/util.cc": _FROB,
            "lib/main.cc": _user('"util.h"'),
            "app/main.cc": _user('"util.h"'),
        },
    )
    assert _pairs(graph, "lib/main.cc::Use") == {"lib/util.cc::Frob"}
    # ``util`` lives in two directories, neither of them app/'s.
    assert _pairs(graph, "app/main.cc::Use") == set()
    assert set(_ambiguous_for(graph, "app/main.cc::Use")) == {"Frob"}


def test_a_bare_include_of_a_one_directory_stem_matches_anywhere(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            "x/helpers.cc": _FROB,
            "x/helpers.h": "int Frob(int x);\n",
            "y/misc.cc": _FROB,
            "app/main.cc": _user('"helpers.h"'),
        },
    )
    assert "y/misc.cc::Frob" not in _pairs(graph, "app/main.cc::Use")
    assert _pairs(graph, "app/main.cc::Use") <= {
        "x/helpers.cc::Frob",
        "x/helpers.h::Frob",
    }
    assert _pairs(graph, "app/main.cc::Use")


def test_a_receiver_named_like_a_header_is_not_an_import(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            "core/registry.h": (
                "class Registry {\n public:\n"
                "  int Lookup(int k) { return k; }\n};\n"
            ),
            "app/user.cc": _user(
                '"core/registry.h"\n#include <map>',
                "  Registry map;\n  map.Lookup(1);",
            ),
        },
    )
    caller = "app/user.cc::Use"
    assert "map.Lookup" not in _externals(graph, caller)
    assert _pairs(graph, caller) == {"core/registry.h::Registry.Lookup"}


def test_an_std_member_name_goes_external_from_a_cpp_call(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            "util/status.h": (
                "class Status {\n public:\n"
                "  int status() const { return 0; }\n};\n"
            ),
            "app/user.cc": _user(
                "<string>", "  auto s = Load();\n  s.status();"
            ),
        },
    )
    caller = "app/user.cc::Use"
    assert "util/status.h::Status.status" not in _pairs(graph, caller)
    assert "s.status" in _externals(graph, caller)


@pytest.mark.parametrize(
    ("path", "guarded"),
    [
        ("a/user.cc", True),
        ("a/user.h", True),
        ("a/user.c", True),
        ("a/user.py", False),
        ("a/user.ts", False),
        ("", False),
    ],
)
def test_cpp_std_names_are_guarded_only_for_c_family_paths(
    path: str, guarded: bool
) -> None:
    assert is_guarded_method_name("size", path) is guarded
    assert is_guarded_method_name("message", path) is guarded
    # A shared-list name is guarded wherever the call is.
    assert is_guarded_method_name("unwrap", path)
