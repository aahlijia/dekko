"""Rust ``Type::name(..)`` calls: std ``use`` bindings and ambiguous rows.

A ``use`` rooted at ``std``/``core``/``alloc`` never points into the
repo, whatever file stems its later segments match, so a call through
it is external even when the repo defines a same-named type. And when
a type-path call ends ambiguous, the row discloses only the members
the path narrowed to, not every same-named symbol in the language.
"""

from pathlib import Path

from dekko.core.model import CallGraph, RawCall
from dekko.core.resolver import (
    _rust_is_associated_type_path,
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

    api = frozenset({"crates/extension_api"})
    onboarding = frozenset({"crates/onboarding"})
    assert load_cargo_crates(tmp_path) == {
        "zed_extension_api": api,
        "extension_api": api,
        "language_onboarding": onboarding,
        "onboarding": onboarding,
        "onboard": onboarding,
        "test_util": frozenset(),
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


def test_cargo_fingerprint_sees_crates_trading_directories(
    tmp_path: Path,
) -> None:
    """``some_crate::name(..)`` is narrowed to the crate's directory,
    so the digest reads directories, not only names."""

    def write(a: str, b: str) -> str:
        (tmp_path / "crates/a").mkdir(parents=True, exist_ok=True)
        (tmp_path / "crates/b").mkdir(parents=True, exist_ok=True)
        (tmp_path / "crates/a/Cargo.toml").write_text(
            f'[package]\nname = "{a}"\n'
        )
        (tmp_path / "crates/b/Cargo.toml").write_text(
            f'[package]\nname = "{b}"\n'
        )
        return cargo_fingerprint(tmp_path)

    assert write("one", "two") != write("two", "one")


# A path whose type is an alias, a renaming ``use`` or a turbofish is
# read as the type it names; a lowercase path through a workspace
# crate keeps that crate's candidates.


def _point_with_new(name: str = "Point") -> str:
    return (
        f"pub struct {name};\n"
        f"impl {name} {{\n"
        f"    pub fn new(x: i32, y: i32) -> {name} {{\n"
        f"        {name}\n"
        f"    }}\n"
        f"}}\n"
    )


_POINTS = {
    "src/point.rs": _point_with_new(),
    "src/decoy.rs": _point_with_new("Decoy"),
}
_POINT_NEW = "src/point.rs::Point.new"
_USER = "src/user.rs::use_it"


def _user(body: str, head: str = "") -> dict[str, str]:
    return {"src/user.rs": f"{head}pub fn use_it() {{\n    {body}\n}}\n"}


def test_alias_to_an_outside_type_is_external(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        {
            "src/collections.rs": (
                "pub type HashMap<K, V> = FxHashMap<K, V>;\n"
            ),
            **_user(
                "HashMap::default();",
                "pub struct Thing;\n"
                "impl Thing {\n"
                "    pub fn default() -> Thing {\n"
                "        Thing\n"
                "    }\n"
                "}\n",
            ),
        },
    )
    assert _pairs(graph, _USER) == set()
    assert _ambiguous_for(graph, _USER) == {}
    assert _externals(graph, _USER) == {"HashMap::default"}


def test_alias_to_a_repo_type_reaches_its_members(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        {
            **_POINTS,
            "src/alias.rs": "pub type P = Point<f32>;\n",
            **_user("P::new(1, 2);"),
        },
    )
    assert _pairs(graph, _USER) == {_POINT_NEW}


def test_alias_chain_is_followed(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        {
            **_POINTS,
            "src/alias.rs": (
                "pub type A = B;\npub type B = &'static crate::Point;\n"
            ),
            **_user("A::new(1, 2);"),
        },
    )
    assert _pairs(graph, _USER) == {_POINT_NEW}


def test_alias_cycle_is_left_alone(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        {
            **_POINTS,
            "src/alias.rs": "pub type A = B;\npub type B = A;\n",
            **_user("A::new(1, 2);"),
        },
    )
    assert _pairs(graph, _USER) == set()
    assert _ambiguous_for(graph, _USER) == {
        "new": ["src/decoy.rs::Decoy.new", _POINT_NEW]
    }


def test_renaming_use_reaches_the_original_type(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        {
            **_POINTS,
            **_user("Spot::new(1, 2);", "use crate::point::Point as Spot;\n"),
        },
    )
    assert _pairs(graph, _USER) == {_POINT_NEW}


def test_rename_made_in_another_crate_reaches_the_original(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            "crates/geo/src/point.rs": _point_with_new(),
            "crates/geo/src/decoy.rs": _point_with_new("Decoy"),
            "crates/shapes/src/shapes.rs": "pub use geo::Point as Spot;\n",
            "crates/app/src/user.rs": (
                "use shapes::Spot;\n"
                "pub fn use_it() {\n"
                "    Spot::new(1, 2);\n"
                "}\n"
            ),
        },
    )
    assert _pairs(graph, "crates/app/src/user.rs::use_it") == {
        "crates/geo/src/point.rs::Point.new"
    }


def test_turbofish_on_an_outside_type_is_external(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        _user(
            "Vec::<u8>::new();",
            "pub struct Thing;\n"
            "impl Thing {\n"
            "    pub fn new() -> Thing {\n"
            "        Thing\n"
            "    }\n"
            "}\n",
        ),
    )
    assert _pairs(graph, _USER) == set()
    assert _externals(graph, _USER) == {"Vec::<u8>::new"}


def test_turbofish_on_a_repo_type_reaches_its_members(
    tmp_path: Path,
) -> None:
    graph = _graph(tmp_path, {**_POINTS, **_user("Point::<f32>::new(1, 2);")})
    assert _pairs(graph, _USER) == {_POINT_NEW}


def test_associated_type_path_is_still_one(tmp_path: Path) -> None:
    for receiver, expected in (
        ("T::Output", True),
        ("Vec::<T>", False),
        ("gpui::Point::<f32>", False),
    ):
        call = RawCall(
            caller_id=None,
            path="src/a.rs",
            text=f"{receiver}::new",
            name="new",
            receiver=receiver,
        )
        assert _rust_is_associated_type_path(call) is expected


def _init(params: str) -> str:
    return f"pub fn init({params}) {{}}\n"


_CRATES = {
    "Cargo.toml": '[workspace]\nmembers = ["crates/*"]\n',
    "crates/other_crate/Cargo.toml": '[package]\nname = "other-crate"\n',
    "crates/other_crate/src/lib.rs": _init("a: i32, b: i32"),
    "crates/third/Cargo.toml": '[package]\nname = "third"\n',
    "crates/third/src/lib.rs": _init("a: i32, b: i32"),
    "crates/app/Cargo.toml": '[package]\nname = "app"\n',
}
_MAIN = "crates/app/src/main.rs::main"


def test_crate_path_reaches_that_crates_function(tmp_path: Path) -> None:
    graph = _rooted(
        tmp_path,
        {
            **_CRATES,
            "crates/app/src/main.rs": (
                "pub fn init(a: i32, b: i32) {}\n"
                "pub fn main() {\n"
                "    other_crate::init(1, 2);\n"
                "}\n"
            ),
        },
    )
    assert _pairs(graph, _MAIN) == {"crates/other_crate/src/lib.rs::init"}


def test_crate_path_through_a_use_of_its_module(tmp_path: Path) -> None:
    graph = _rooted(
        tmp_path,
        {
            **_CRATES,
            "crates/other_crate/src/sub.rs": "pub fn run() {}\n",
            "crates/third/src/sub.rs": "pub fn run() {}\n",
            "crates/app/src/main.rs": (
                "use other_crate::sub;\n"
                "pub fn run() {}\n"
                "pub fn main() {\n"
                "    sub::run();\n"
                "}\n"
            ),
        },
    )
    assert _pairs(graph, _MAIN) == {"crates/other_crate/src/sub.rs::run"}


def test_crate_that_only_reexports_the_function_is_no_evidence(
    tmp_path: Path,
) -> None:
    graph = _rooted(
        tmp_path,
        {
            **_CRATES,
            "crates/other_crate/src/lib.rs": "pub use third::helper;\n",
            "crates/third/src/lib.rs": "pub fn helper() {}\n",
            "crates/fourth/Cargo.toml": '[package]\nname = "fourth"\n',
            "crates/fourth/src/lib.rs": "pub fn helper() {}\n",
            "crates/app/src/main.rs": (
                "pub fn main() {\n    other_crate::helper();\n}\n"
            ),
        },
    )
    assert _pairs(graph, _MAIN) == set()
    assert _ambiguous_for(graph, _MAIN) == {
        "helper": [
            "crates/fourth/src/lib.rs::helper",
            "crates/third/src/lib.rs::helper",
        ]
    }


def test_impl_through_a_crate_path_names_that_crates_trait(
    tmp_path: Path,
) -> None:
    graph = _rooted(
        tmp_path,
        {
            **_CRATES,
            "crates/other_crate/src/lib.rs": "pub trait Item {}\n",
            "crates/third/src/lib.rs": "pub trait Item {}\n",
            "crates/app/src/main.rs": (
                "pub struct Note;\nimpl other_crate::Item for Note {}\n"
            ),
        },
    )
    assert {(h.subtype, h.supertype) for h in graph.heritage} == {
        (
            "crates/app/src/main.rs::Note",
            "crates/other_crate/src/lib.rs::Item",
        )
    }


# An ambiguous row lists only what the call could mean.

_NEWS = {
    "src/a.rs": (
        "pub struct A;\nimpl A {\n    pub fn new(&mut self, f: i32) {}\n}\n"
    ),
    "src/b.rs": (
        "pub struct B;\nimpl B {\n    pub fn new(&self, f: i32) {}\n}\n"
    ),
    "src/c.rs": (
        "pub struct C;\nimpl C {\n"
        "    pub fn new(a: i32, b: i32) -> C {\n        C\n    }\n}\n"
    ),
    "src/free.rs": "pub fn new(f: i32) {}\n",
}


def test_dot_call_row_lists_methods_it_can_call(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path, {**_NEWS, **_user("let cx = make();\n    cx.new(1);")}
    )
    assert _pairs(graph, _USER) == set()
    assert _ambiguous_for(graph, _USER) == {
        "new": ["src/a.rs::A.new", "src/b.rs::B.new"]
    }


def test_dot_call_row_with_one_fit_keeps_its_list(tmp_path: Path) -> None:
    news = {k: v for k, v in _NEWS.items() if k != "src/b.rs"}
    graph = _graph(
        tmp_path, {**news, **_user("let cx = make();\n    cx.new(1);")}
    )
    assert _ambiguous_for(graph, _USER) == {
        "new": ["src/a.rs::A.new", "src/c.rs::C.new", "src/free.rs::new"]
    }


def test_type_with_only_members_row_lists_them(tmp_path: Path) -> None:
    def from_impl(source: str) -> str:
        return (
            f"pub struct {source};\n"
            f"impl From<{source}> for String {{\n"
            f"    fn from(x: {source}) -> String {{\n"
            "        String::new()\n"
            "    }\n"
            "}\n"
        )

    graph = _graph(
        tmp_path,
        {
            "src/a.rs": from_impl("Alpha"),
            "src/b.rs": from_impl("Beta"),
            "src/c.rs": (
                "pub struct Other;\nimpl Other {\n"
                "    pub fn from(x: i32) -> Other {\n        Other\n    }\n}\n"
            ),
            **_user("String::from(1);"),
        },
    )
    assert _ambiguous_for(graph, _USER) == {
        "from": ["src/a.rs::String.from", "src/b.rs::String.from"]
    }


def test_cfg_argument_leaves_the_count_unknown(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        {
            **_POINTS,
            **_user("Point::new(1, #[cfg(test)] 2, 3);"),
        },
    )
    # Three written, two in a build without `test`: the count can't
    # rule `Point.new(x, y)` out.
    assert _pairs(graph, _USER) == {_POINT_NEW}


def test_cfg_argument_call_has_no_arg_count(tmp_path: Path) -> None:
    (tmp_path / "a.rs").write_text(
        "fn f() {\n    g(1, #[cfg(test)] 2);\n    g(1, 2);\n}\n"
    )
    files, _ = map_repository(
        tmp_path, subpath=None, excludes=(), max_file_size=1_000_000
    )
    assert [c.arg_count for c in files[0].calls] == [None, 2]


def test_type_path_whose_own_member_fails_arity_has_no_second_guess(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            **_POINTS,
            "src/wide.rs": (
                "pub trait Wide {\n"
                "    fn new(a: i32, b: i32, c: i32) -> Self\n"
                "    where\n        Self: Sized,\n    {\n"
                "        todo!()\n    }\n}\n"
            ),
            **_user("Point::new(1, 2, 3);"),
        },
    )
    # Rust takes the inherent `Point::new` over any trait's, so a count
    # it can't fit means the count is off, not that a trait's three-
    # argument `new` is meant.
    assert _pairs(graph, _USER) == set()
    assert _externals(graph, _USER) == {"Point::new"}


def test_type_path_does_not_take_a_same_file_member_of_another_type(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            "src/a.rs": (
                "pub struct Alpha;\nimpl From<Alpha> for String {\n"
                "    fn from(x: Alpha) -> String {\n"
                "        String::new()\n    }\n}\n"
            ),
            **_user(
                'String::from("x");',
                "pub struct ThreadError;\n"
                "impl From<i32> for ThreadError {\n"
                "    fn from(x: i32) -> Self {\n        ThreadError\n    }\n"
                "}\n",
            ),
        },
    )
    assert "src/user.rs::ThreadError.from" not in _pairs(graph, _USER)


def test_alias_in_the_same_file_keeps_its_targets_member(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            "src/other.rs": _point_with_new("Other"),
            **_user(
                "Coverage::new(1, 2);",
                "pub type Coverage = Point;\n" + _point_with_new(),
            ),
        },
    )
    assert _pairs(graph, _USER) == {"src/user.rs::Point.new"}
