"""Rust ``Type::name(..)`` calls: std ``use`` bindings and ambiguous rows.

A ``use`` rooted at ``std``/``core``/``alloc`` never points into the
repo, whatever file stems its later segments match, so a call through
it is external even when the repo defines a same-named type. And when
a type-path call ends ambiguous, the row discloses only the members
the path narrowed to, not every same-named symbol in the language.
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


_PATH_RS = (
    "pub struct Path;\n"
    "impl Path {\n"
    "    pub fn new(x: &str) -> Path {\n"
    "        Path\n"
    "    }\n"
    "}\n"
)
_USE_IT = "src/user.rs::use_it"


def test_std_path_import_never_reaches_a_repo_path_type(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            "src/path.rs": _PATH_RS,
            "src/user.rs": (
                "use std::path::Path;\n"
                "pub fn use_it() {\n"
                '    Path::new("x");\n'
                "}\n"
            ),
        },
    )
    assert _pairs(graph, _USE_IT) == set()
    assert _ambiguous_for(graph, _USE_IT) == {}
    assert _externals(graph, _USE_IT) == {"Path::new"}


def test_std_collections_import_never_reaches_a_repo_alias(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            "src/collections.rs": (
                "pub type HashMap<K, V> = std::collections::HashMap<K, V>;\n"
            ),
            "src/other.rs": (
                "pub struct Other;\n"
                "impl Other {\n"
                "    pub fn new() -> Other {\n"
                "        Other\n"
                "    }\n"
                "}\n"
            ),
            "src/user.rs": (
                "use std::collections::HashMap;\n"
                "pub fn use_it() {\n"
                "    HashMap::new();\n"
                "}\n"
            ),
        },
    )
    assert _pairs(graph, _USE_IT) == set()
    assert _ambiguous_for(graph, _USE_IT) == {}
    assert _externals(graph, _USE_IT) == {"HashMap::new"}


def test_std_process_command_chain_is_external(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        {
            "src/process.rs": (
                "pub struct Command;\n"
                "impl Command {\n"
                "    pub fn new(x: &str) -> Command {\n"
                "        Command\n"
                "    }\n"
                "    pub fn arg(&self, y: &str) -> &Command {\n"
                "        self\n"
                "    }\n"
                "}\n"
            ),
            "src/user.rs": (
                "use std::process::Command;\n"
                "pub fn use_it() {\n"
                '    Command::new("x").arg("y");\n'
                "}\n"
            ),
        },
    )
    assert _pairs(graph, _USE_IT) == set()
    assert _ambiguous_for(graph, _USE_IT) == {}
    assert "Command::new" in _externals(graph, _USE_IT)


def test_crate_path_import_still_reaches_the_repo_type(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            "src/path.rs": _PATH_RS,
            "src/user.rs": (
                "use crate::path::Path;\n"
                "pub fn use_it() {\n"
                '    Path::new("x");\n'
                "}\n"
            ),
        },
    )
    assert "src/path.rs::Path.new" in _pairs(graph, _USE_IT)


def test_python_import_of_a_repo_module_is_still_in_repo(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            "path.py": "def new(x):\n    return x\n",
            "user.py": "import path\n\ndef use_it():\n    path.new(1)\n",
        },
    )
    assert _pairs(graph, "user.py::use_it") == {"path.py::new"}


def _struct_with_new(name: str) -> str:
    return (
        f"pub struct {name};\n"
        f"impl {name} {{\n"
        f"    pub fn new(a: i32) -> {name} {{\n"
        f"        {name}\n"
        f"    }}\n"
        f"}}\n"
    )


_EDITORS = {
    "crates/editor/src/editor.rs": _struct_with_new("Editor"),
    "crates/legacy/src/lib.rs": _struct_with_new("Editor"),
    "crates/misc/src/lib.rs": _struct_with_new("Other"),
}
_BUILD = "crates/app/src/view.rs::build"
_EDITOR_NEWS = [
    "crates/editor/src/editor.rs::Editor.new",
    "crates/legacy/src/lib.rs::Editor.new",
]
_ALL_NEWS = [*_EDITOR_NEWS, "crates/misc/src/lib.rs::Other.new"]


def test_ambiguous_type_path_row_lists_only_that_types_members(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            **_EDITORS,
            "crates/app/src/view.rs": (
                "pub fn build() {\n    Editor::new(1);\n}\n"
            ),
        },
    )
    assert _pairs(graph, _BUILD) == set()
    assert _ambiguous_for(graph, _BUILD) == {"new": _EDITOR_NEWS}


def test_dot_call_row_merged_into_the_type_path_row_is_the_union(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            **_EDITORS,
            "crates/app/src/view.rs": (
                "pub fn build() {\n"
                "    Editor::new(1);\n"
                "    let x = make();\n"
                "    x.new(1);\n"
                "}\n"
            ),
        },
    )
    assert _pairs(graph, _BUILD) == set()
    assert _ambiguous_for(graph, _BUILD) == {"new": _ALL_NEWS}


def test_bare_ambiguous_call_keeps_its_full_list(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        {
            "crates/a/src/lib.rs": "pub fn new(a: i32) -> i32 {\n    a\n}\n",
            "crates/b/src/lib.rs": "pub fn new(a: i32) -> i32 {\n    a\n}\n",
            "crates/app/src/view.rs": "pub fn build() {\n    new(1);\n}\n",
        },
    )
    assert _pairs(graph, _BUILD) == set()
    assert _ambiguous_for(graph, _BUILD) == {
        "new": ["crates/a/src/lib.rs::new", "crates/b/src/lib.rs::new"]
    }
