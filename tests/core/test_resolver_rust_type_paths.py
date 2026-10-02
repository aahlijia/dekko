"""Rust ``Type::name(..)`` calls: std ``use`` bindings and ambiguous rows.

A ``use`` rooted at ``std``/``core``/``alloc`` never points into the
repo, whatever file stems its later segments match, so a call through
it is external even when the repo defines a same-named type. And when
a type-path call ends ambiguous, the row discloses only the members
the path narrowed to, not every same-named symbol in the language.
"""

from pathlib import Path

from dekko.core.model import CallGraph
from dekko.core.resolver import (
    cargo_fingerprint,
    load_cargo_crates,
    resolve,
)
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


# A ``use`` rooted at a crate the repo doesn't have is external, even
# when a repo file shares a segment's name (zed's ``windows.rs`` made
# ``use windows::core::HSTRING`` look in-repo). The crate list comes
# from the ``Cargo.toml`` files, so these resolve with a root.


def _rooted(root: Path, sources: dict[str, str]) -> CallGraph:
    for rel, text in sources.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    files, _ = map_repository(
        root,
        subpath=None,
        excludes=(),
        max_file_size=1_000_000,
    )
    return resolve(files, root=root)


_APP_TOML = '[package]\nname = "app"\n'
_RUN = "src/main.rs::run"
_SOURCE_RS = "pub fn play() {}\n"
_OUTSIDE_USER = (
    "mod source;\n"
    "use rodio::source as sound;\n"
    "pub fn run() {\n"
    "    sound::play();\n"
    "}\n"
)


def test_use_of_an_outside_crate_is_external(tmp_path: Path) -> None:
    graph = _rooted(
        tmp_path,
        {
            "Cargo.toml": _APP_TOML,
            "src/source.rs": _SOURCE_RS,
            "src/main.rs": _OUTSIDE_USER,
        },
    )
    assert _pairs(graph, _RUN) == set()
    assert _externals(graph, _RUN) == {"sound::play"}


def test_path_dependency_counts_as_a_repo_crate(tmp_path: Path) -> None:
    graph = _rooted(
        tmp_path,
        {
            "Cargo.toml": _APP_TOML
            + '\n[dependencies]\nrodio = { path = "../rodio" }\n',
            "src/source.rs": _SOURCE_RS,
            "src/main.rs": _OUTSIDE_USER,
        },
    )
    assert _pairs(graph, _RUN) == {"src/source.rs::play"}


def test_no_cargo_toml_keeps_the_stem_test(tmp_path: Path) -> None:
    graph = _rooted(
        tmp_path,
        {"src/source.rs": _SOURCE_RS, "src/main.rs": _OUTSIDE_USER},
    )
    assert _pairs(graph, _RUN) == {"src/source.rs::play"}


def test_cargo_crate_names(tmp_path: Path) -> None:
    files = {
        "Cargo.toml": (
            "[workspace]\n"
            'members = ["crates/*"]\n'
            "\n"
            "[workspace.dependencies]\n"
            'zed-extension-api = { path = "crates/extension_api" }\n'
            'serde = { version = "1" }\n'
            'gpui = { git = "https://example.com/gpui" }\n'
        ),
        "crates/extension_api/Cargo.toml": (
            '[package]\nname = "zed_extension_api"\n'
        ),
        "crates/onboarding/Cargo.toml": (
            "[package]\n"
            'name = "language-onboarding"\n'
            "\n"
            "[lib]\n"
            'name = "onboard"\n'
            'path = "src/python.rs"\n'
            "\n"
            "[dev-dependencies]\n"
            'test_util = { workspace = true, path = "../test_util" }\n'
        ),
    }
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)

    assert load_cargo_crates(tmp_path) == {
        "zed_extension_api",
        "extension_api",
        "language_onboarding",
        "onboarding",
        "onboard",
        "test_util",
    }
    assert cargo_fingerprint(tmp_path) != ""
    assert cargo_fingerprint(tmp_path / "crates/missing") == ""


def test_own_crate_module_use_is_in_repo(tmp_path: Path) -> None:
    # ``util`` is no crate and no crate's top-level module, but it is a
    # module of the importing file's own crate (2018 uniform paths).
    graph = _rooted(
        tmp_path,
        {
            "Cargo.toml": _APP_TOML,
            "src/net/util.rs": _SOURCE_RS,
            "src/main.rs": (
                "use util as u;\npub fn run() {\n    u::play();\n}\n"
            ),
        },
    )
    assert _pairs(graph, _RUN) == {"src/net/util.rs::play"}


def test_another_crates_top_module_is_in_repo(tmp_path: Path) -> None:
    # A fixture copied out of ``editor`` into ``agent`` still writes
    # ``use scroll::..``, a module of the crate it came from.
    graph = _rooted(
        tmp_path,
        {
            "Cargo.toml": ('[workspace]\nmembers = ["crates/*"]\n'),
            "crates/editor/Cargo.toml": '[package]\nname = "editor"\n',
            "crates/editor/src/scroll.rs": "pub fn autoscroll() {}\n",
            "crates/agent/Cargo.toml": '[package]\nname = "agent"\n',
            "crates/agent/src/before.rs": (
                "use scroll as s;\npub fn run() {\n    s::autoscroll();\n}\n"
            ),
        },
    )
    assert _pairs(graph, "crates/agent/src/before.rs::run") == {
        "crates/editor/src/scroll.rs::autoscroll"
    }


# A type imported from inside the repo that no repo symbol carries is
# a rename (``pub use text::Buffer as TextBuffer``) only when something
# in the repo renames to it. Otherwise a workspace crate re-exports an
# outside type (``collections`` is ``pub use std::collections::*``).

_OTHER_NEW = (
    "pub struct Other;\n"
    "impl Other {\n"
    "    pub fn new() -> Other {\n"
    "        Other\n"
    "    }\n"
    "}\n"
)
_USE_MAP = "crates/app/src/view.rs::build"


def test_reexported_outside_type_is_external(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        {
            "crates/collections/src/collections.rs": (
                "pub use std::collections::*;\n"
            ),
            "crates/other/src/lib.rs": _OTHER_NEW,
            "crates/app/src/view.rs": (
                "use collections::BTreeMap;\n"
                "pub fn build() {\n"
                "    BTreeMap::new();\n"
                "}\n"
            ),
        },
    )
    assert _pairs(graph, _USE_MAP) == set()
    assert _ambiguous_for(graph, _USE_MAP) == {}
    assert _externals(graph, _USE_MAP) == {"BTreeMap::new"}


def test_renamed_type_from_another_crate_still_resolves(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            "crates/text/src/text.rs": (
                "pub struct Buffer;\n"
                "impl Buffer {\n"
                "    pub fn new(a: i32) -> Buffer {\n"
                "        Buffer\n"
                "    }\n"
                "}\n"
            ),
            "crates/language/src/language.rs": (
                "pub use text::Buffer as TextBuffer;\n"
            ),
            "crates/other/src/lib.rs": _OTHER_NEW,
            "crates/app/src/view.rs": (
                "use language::TextBuffer;\n"
                "pub fn build() {\n"
                "    TextBuffer::new(1);\n"
                "}\n"
            ),
        },
    )
    assert "crates/other/src/lib.rs::Other.new" not in _pairs(graph, _USE_MAP)
    assert _externals(graph, _USE_MAP) == set()
