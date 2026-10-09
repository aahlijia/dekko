"""Rust type paths walked through crates, re-exports and globs.

``use geo::Point; Point::new()`` names the one ``Point`` that
``geo``'s root re-exports, not every ``Point.new`` in the workspace.
The walk follows ``pub use``, renames and globs from the crate's lib
root (``Cargo.toml``), and a member of a type named ``Point`` in a
crate that declares a ``Point`` of its own is that other type's.
"""

from pathlib import Path

from dekko.core.model import CallGraph, Import
from dekko.core.resolver import (
    _build_index,
    _ImportResolveContext,
    _imports_by_file,
    _resolve_import_rust,
    _rust_crate_roots_index_all,
    _rust_foreign_owner_crates,
    _RustPaths,
    load_cargo_lib_roots,
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
    return resolve(files, root=root)


def _callees(graph: CallGraph, caller: str) -> set[str]:
    return {e.callee for e in graph.edges if e.caller == caller}


def _ambiguous(graph: CallGraph, caller: str) -> dict[str, set[str]]:
    return {name: set(ids) for c, name, ids in graph.ambiguous if c == caller}


def _externals(graph: CallGraph, caller: str) -> set[str]:
    return {e.callee for e in graph.external if e.caller == caller}


def _point(extra: str = "") -> str:
    return (
        f"{extra}pub struct Point;\n"
        "impl Point {\n"
        "    pub fn new() -> Point {\n"
        "        Point\n"
        "    }\n"
        "}\n"
    )


def _crate(name: str, root: str) -> dict[str, str]:
    """A crate laid out the way zed's are: ``src/<name>.rs`` as the
    lib root."""
    return {
        f"crates/{name}/Cargo.toml": (
            f'[package]\nname = "{name}"\n\n[lib]\npath = "src/{name}.rs"\n'
        ),
        f"crates/{name}/src/{name}.rs": root,
    }


def _workspace(geo_root: str, app: str) -> dict[str, str]:
    """Three crates each with a ``Point`` and ``Point.new``; ``geo``
    keeps its own in ``point.rs`` behind ``geo_root``."""
    return {
        **_crate("geo", geo_root),
        "crates/geo/src/point.rs": _point(),
        **_crate("gfx", _point()),
        **_crate("term", _point()),
        **_crate("app", app),
    }


_RUN = "crates/app/src/app.rs::run"
_GEO_NEW = "crates/geo/src/point.rs::Point.new"
_USE_POINT = "use geo::Point;\npub fn run() {\n    Point::new();\n}\n"


def test_a_re_exported_type_settles_on_its_crate(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        _workspace("pub mod point;\npub use point::Point;\n", _USE_POINT),
    )
    assert _callees(graph, _RUN) == {_GEO_NEW}


def test_through_a_glob_at_the_root(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path, _workspace("pub mod point;\npub use point::*;\n", _USE_POINT)
    )
    assert _callees(graph, _RUN) == {_GEO_NEW}


def test_through_a_renaming_re_export(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        _workspace(
            "pub mod point;\npub use point::Point as P;\n",
            "use geo::P;\npub fn run() {\n    P::new();\n}\n",
        ),
    )
    assert _callees(graph, _RUN) == {_GEO_NEW}


def test_a_written_path_with_no_use(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        _workspace(
            "pub mod point;\npub use point::Point;\n",
            "pub fn run() {\n    geo::Point::new();\n}\n",
        ),
    )
    assert _callees(graph, _RUN) == {_GEO_NEW}


def test_a_lib_path_outside_src_beats_a_same_named_fixture(
    tmp_path: Path,
) -> None:
    sources = {
        "crates/shared/Cargo.toml": (
            '[package]\nname = "shared"\n\n[lib]\npath = "shared.rs"\n'
        ),
        "crates/shared/shared.rs": _point(),
        "tooling/lints/test_fixture/shared/Cargo.toml": (
            '[package]\nname = "shared"\n'
        ),
        "tooling/lints/test_fixture/shared/src/lib.rs": _point(),
        **_crate(
            "app", "use shared::Point;\npub fn run() {\n    Point::new();\n}\n"
        ),
    }
    graph = _graph(tmp_path, sources)
    assert _callees(graph, _RUN) == {"crates/shared/shared.rs::Point.new"}


def test_a_trait_default_method_keeps_its_edge(tmp_path: Path) -> None:
    sources = {
        **_crate(
            "settings",
            "pub trait Settings {\n"
            "    fn get_global() -> u32 {\n"
            "        0\n"
            "    }\n"
            "}\n",
        ),
        **_crate(
            "project",
            "pub struct ProjectSettings;\n"
            "impl settings::Settings for ProjectSettings {}\n",
        ),
        **_crate(
            "other",
            "pub struct Store;\n"
            "impl Store {\n"
            "    pub fn get_global() -> u32 {\n"
            "        1\n"
            "    }\n"
            "}\n",
        ),
        **_crate(
            "app",
            "use project::ProjectSettings;\n"
            "pub fn run() {\n"
            "    ProjectSettings::get_global();\n"
            "}\n",
        ),
    }
    graph = _graph(tmp_path, sources)
    assert _callees(graph, _RUN) == {
        "crates/settings/src/settings.rs::Settings.get_global"
    }


def test_a_derived_member_is_not_another_crates_type(tmp_path: Path) -> None:
    sources = {
        "crates/sandbox/Cargo.toml": '[package]\nname = "sandbox"\n',
        "crates/sandbox/src/lib.rs": (
            "#[derive(Default)]\n"
            "pub struct Perms;\n"
            "pub fn run() {\n"
            "    Perms::default();\n"
            "}\n"
        ),
        "crates/settings/Cargo.toml": '[package]\nname = "settings"\n',
        "crates/settings/src/lib.rs": (
            "pub struct Perms;\n"
            "impl Perms {\n"
            "    pub fn default() -> Perms {\n"
            "        Perms\n"
            "    }\n"
            "}\n"
        ),
    }
    graph = _graph(tmp_path, sources)
    caller = "crates/sandbox/src/lib.rs::run"
    assert _callees(graph, caller) == set()
    assert _externals(graph, caller) == {"Perms::default"}


def test_an_impl_in_a_crate_without_the_type_stays_a_candidate(
    tmp_path: Path,
) -> None:
    sources = _workspace("pub mod point;\npub use point::Point;\n", _USE_POINT)
    sources |= _crate(
        "shape",
        "pub trait Make {\n"
        "    fn new() -> Self;\n"
        "}\n"
        "impl Make for geo::Point {\n"
        "    fn new() -> Self {\n"
        "        geo::Point\n"
        "    }\n"
        "}\n",
    )
    for rel, text in sources.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    files, _ = map_repository(
        tmp_path, subpath=None, excludes=(), max_file_size=1_000_000
    )
    index = _build_index(files)
    table = _imports_by_file(files, rust_paths=_RustPaths(files, {}))
    app = "crates/app/src/app.rs"
    call = next(c for fm in files if fm.path == app for c in fm.calls)
    assert _rust_foreign_owner_crates(
        call, index, table[app], "Point"
    ) == frozenset({"crates/gfx", "crates/term"})


def test_a_trait_path_call_passes_self_explicitly(tmp_path: Path) -> None:
    trait = (
        "pub trait ToOffset {\n"
        "    fn to_offset(&self, s: &u32) -> usize {\n"
        "        0\n"
        "    }\n"
        "}\n"
    )
    sources = {
        "crates/text/Cargo.toml": '[package]\nname = "text"\n',
        "crates/text/src/lib.rs": trait,
        "crates/buffer/Cargo.toml": '[package]\nname = "buffer"\n',
        "crates/buffer/src/lib.rs": trait,
        **_crate(
            "app",
            "pub fn run(a: u32, b: u32) {\n"
            "    text::ToOffset::to_offset(&a, &b);\n"
            "}\n",
        ),
    }
    graph = _graph(tmp_path, sources)
    assert _callees(graph, _RUN) == {
        "crates/text/src/lib.rs::ToOffset.to_offset"
    }


def test_a_glob_cycle_ends(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        _workspace(
            "pub mod a;\npub mod b;\npub use a::*;\n",
            "use geo::Missing;\npub fn run() {\n    Missing::new();\n}\n",
        )
        | {
            "crates/geo/src/a.rs": "pub use super::b::*;\n",
            "crates/geo/src/b.rs": "pub use super::a::*;\n",
        },
    )
    assert _callees(graph, _RUN) == set()


def test_cargo_lib_roots(tmp_path: Path) -> None:
    files = {
        "Cargo.toml": '[workspace]\nmembers = ["crates/*"]\n',
        "crates/a/Cargo.toml": '[package]\nname = "a-crate"\n',
        "crates/b/Cargo.toml": (
            '[package]\nname = "b"\n\n[lib]\nname = "bee"\npath = "b.rs"\n'
        ),
    }
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    assert load_cargo_lib_roots(tmp_path) == {
        "a_crate": frozenset({"crates/a/src/lib.rs"}),
        "bee": frozenset({"crates/b/b.rs"}),
    }


def _ctx(paths: set[str]) -> _ImportResolveContext:
    frozen = frozenset(paths)
    return _ImportResolveContext(
        paths=frozen, crate_roots=_rust_crate_roots_index_all(frozen)
    )


def test_a_glob_names_its_module_file() -> None:
    ctx = _ctx({"c/src/lib.rs", "c/src/a.rs", "c/src/a/b.rs"})
    imp = Import(path="c/src/lib.rs", name="*", source="a::*")
    assert _resolve_import_rust(imp, "c/src/lib.rs", ctx) == "c/src/a.rs"


def test_a_super_glob_names_the_parent_module() -> None:
    ctx = _ctx({"c/src/lib.rs", "c/src/a.rs", "c/src/a/b.rs"})
    imp = Import(path="c/src/a/b.rs", name="*", source="super::*")
    assert _resolve_import_rust(imp, "c/src/a/b.rs", ctx) == "c/src/a.rs"


def test_a_glob_of_the_file_itself_is_no_edge() -> None:
    ctx = _ctx({"c/src/lib.rs", "c/src/a.rs"})
    imp = Import(path="c/src/a.rs", name="*", source="self::*")
    assert _resolve_import_rust(imp, "c/src/a.rs", ctx) is None


def test_self_in_a_crate_root_named_after_its_crate() -> None:
    ctx = _ctx({"crates/a/src/a.rs", "crates/a/src/dialog.rs"})
    imp = Import(path="crates/a/src/a.rs", name="f", source="self::dialog::f")
    assert (
        _resolve_import_rust(imp, "crates/a/src/a.rs", ctx)
        == "crates/a/src/dialog.rs"
    )


def test_a_module_reached_by_a_glob_shadows_a_crate_of_its_name(
    tmp_path: Path,
) -> None:
    entity = "pub struct Entity;\nimpl Entity {\n    pub fn update() {}\n}\n"
    sources = {
        **_crate("extension", entity),
        **_crate("collab", "pub mod tables;\npub mod queries;\n"),
        "crates/collab/src/tables.rs": "pub mod extension;\n",
        "crates/collab/src/tables/extension.rs": entity,
        "crates/collab/src/queries.rs": (
            "pub use crate::tables::*;\npub mod rooms;\n"
        ),
        "crates/collab/src/queries/rooms.rs": (
            "use super::*;\n"
            "pub fn run() {\n"
            "    extension::Entity::update();\n"
            "}\n"
        ),
    }
    graph = _graph(tmp_path, sources)
    assert _callees(graph, "crates/collab/src/queries/rooms.rs::run") == {
        "crates/collab/src/tables/extension.rs::Entity.update"
    }


def _session_ids() -> dict[str, str]:
    """A crate with a ``SessionId.new`` and a ``from_value`` method:
    what an outside path's last segment would land on."""
    return _crate(
        "ids",
        "pub struct SessionId;\n"
        "impl SessionId {\n"
        "    pub fn new() -> SessionId {\n"
        "        SessionId\n"
        "    }\n"
        "    pub fn from_value(v: u32) -> SessionId {\n"
        "        SessionId\n"
        "    }\n"
        "}\n"
        "pub fn channel() {}\n",
    )


def test_an_extern_crate_head_with_no_use_is_external(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        _session_ids()
        | _crate(
            "app", "pub fn run(x: u32) {\n    serde_json::from_value(x);\n}\n"
        ),
    )
    assert _callees(graph, _RUN) == set()
    assert _externals(graph, _RUN) == {"serde_json::from_value"}


def test_a_multi_segment_extern_path_is_external(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        _session_ids()
        | _crate(
            "app",
            "pub fn run() {\n    futures::channel::oneshot::channel();\n}\n",
        ),
    )
    assert _callees(graph, _RUN) == set()
    assert _ambiguous(graph, _RUN) == {}


_SIDEBAR_RUN = "crates/app/src/sidebar.rs::run"
_SIDEBAR = "use super::*;\npub fn run() {\n    acp::SessionId::new();\n}\n"


def test_an_outside_alias_reached_by_a_glob_is_external(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        _session_ids()
        | _crate("app", "mod sidebar;\nuse ext::v1 as acp;\n")
        | {"crates/app/src/sidebar.rs": _SIDEBAR},
    )
    assert _callees(graph, _SIDEBAR_RUN) == set()
    assert _externals(graph, _SIDEBAR_RUN) == {"acp::SessionId::new"}


def test_an_in_repo_alias_reached_by_a_glob_resolves(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        _session_ids()
        | _crate("app", "mod ids;\nmod sidebar;\nuse crate::ids as acp;\n")
        | {
            "crates/app/src/ids.rs": (
                "pub struct SessionId;\n"
                "impl SessionId {\n"
                "    pub fn new() -> SessionId {\n"
                "        SessionId\n"
                "    }\n"
                "}\n"
            ),
            "crates/app/src/sidebar.rs": _SIDEBAR,
        },
    )
    assert _callees(graph, _SIDEBAR_RUN) == {
        "crates/app/src/ids.rs::SessionId.new"
    }


def test_a_primitive_head_is_external(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        _crate(
            "app",
            "pub struct Meters;\n"
            "impl From<u8> for Meters {\n"
            "    fn from(v: u8) -> Meters {\n"
            "        Meters\n"
            "    }\n"
            "}\n"
            "pub fn run(x: u8) {\n"
            "    f32::from(x);\n"
            "}\n",
        ),
    )
    assert _callees(graph, _RUN) == set()
    assert _externals(graph, _RUN) == {"f32::from"}


def test_an_inline_module_head_resolves(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        _session_ids()
        | _crate(
            "app",
            "mod persistence {\n"
            "    pub struct Db;\n"
            "    impl Db {\n"
            "        pub fn new() -> Db {\n"
            "            Db\n"
            "        }\n"
            "    }\n"
            "}\n"
            "pub fn run() {\n"
            "    persistence::Db::new();\n"
            "}\n",
        ),
    )
    assert _callees(graph, _RUN) == {
        "crates/app/src/app.rs::persistence.Db.new"
    }


def test_an_inline_module_reached_by_a_glob_resolves(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        _session_ids()
        | _crate(
            "app",
            "mod sidebar;\n"
            "mod test_mocks {\n"
            "    pub struct SessionId;\n"
            "    impl SessionId {\n"
            "        pub fn new() -> SessionId {\n"
            "            SessionId\n"
            "        }\n"
            "    }\n"
            "}\n",
        )
        | {
            "crates/app/src/sidebar.rs": (
                "use super::*;\n"
                "pub fn run() {\n"
                "    test_mocks::SessionId::new();\n"
                "}\n"
            )
        },
    )
    assert _externals(graph, _SIDEBAR_RUN) == set()


def test_a_sibling_crate_head_is_unchanged(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        _session_ids()
        | _crate("release_channel", "pub fn init() {}\n")
        | _crate("app", "pub fn run() {\n    release_channel::init();\n}\n"),
    )
    assert _callees(graph, _RUN) == {
        "crates/release_channel/src/release_channel.rs::init"
    }


def test_a_primitive_head_is_external_with_no_manifest(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            "src/meters.rs": (
                "pub struct Meters;\n"
                "impl From<u8> for Meters {\n"
                "    fn from(v: u8) -> Meters {\n"
                "        Meters\n"
                "    }\n"
                "}\n"
            ),
            "src/app.rs": "pub fn run(x: u8) {\n    u32::from(x);\n}\n",
        },
    )
    assert _callees(graph, "src/app.rs::run") == set()
    assert _externals(graph, "src/app.rs::run") == {"u32::from"}


_MATCHER = (
    "pub struct Matcher;\n"
    "impl Matcher {\n"
    "    pub fn is_match(&self, s: &str) -> bool {\n"
    "        true\n"
    "    }\n"
    "}\n"
)


def test_a_local_named_like_an_outside_head_keeps_its_edge(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        _crate(
            "app",
            f"{_MATCHER}"
            "pub fn run(regex: Matcher) {\n"
            '    regex::escape("x");\n'
            '    regex.is_match("x");\n'
            "}\n",
        ),
    )
    assert _callees(graph, _RUN) == {"crates/app/src/app.rs::Matcher.is_match"}
    assert _externals(graph, _RUN) == {"regex::escape"}


def test_a_turbofish_head_is_no_path(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        _crate(
            "app",
            f"{_MATCHER}"
            "pub fn find<T>() -> Matcher {\n"
            "    Matcher\n"
            "}\n"
            "pub fn run() {\n"
            '    find::<u8>().is_match("x");\n'
            "    find::<u8>();\n"
            "}\n",
        ),
    )
    assert "crates/app/src/app.rs::find" in _callees(graph, _RUN)


def _others() -> dict[str, str]:
    """Two more crates with a ``Point.new``, and a free ``new``."""
    return (
        _crate("gfx", _point())
        | _crate("term", _point())
        | _crate("util", "pub fn new() {}\n")
    )


def test_self_in_a_trait_impl_is_the_impl_type(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        _others()
        | _crate("app", "mod p;\nmod conv;\n")
        | {
            "crates/app/src/p.rs": (
                "pub struct P;\n"
                "impl P {\n"
                "    pub fn new(a: u8) -> P {\n"
                "        P\n"
                "    }\n"
                "}\n"
            ),
            "crates/app/src/conv.rs": (
                "use crate::p::P;\n"
                "pub struct A(pub u8);\n"
                "impl From<A> for P {\n"
                "    fn from(a: A) -> Self {\n"
                "        Self::new(a.0)\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    assert _callees(graph, "crates/app/src/conv.rs::P.from") == {
        "crates/app/src/p.rs::P.new"
    }


def test_self_of_an_outside_type_is_external(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        _others()
        | _crate(
            "app",
            "pub struct A(pub u8);\n"
            "impl From<A> for ext::P {\n"
            "    fn from(a: A) -> Self {\n"
            "        Self::new(a.0)\n"
            "    }\n"
            "}\n",
        ),
    )
    caller = "crates/app/src/app.rs::ext::P.from"
    assert _callees(graph, caller) == set()
    assert _ambiguous(graph, caller) == {}
    assert _externals(graph, caller) == {"ext::P::new"}


def test_self_variant_is_not_a_same_named_struct(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        _crate(
            "app",
            "mod other;\n"
            "pub enum E {\n"
            "    Variant(u8),\n"
            "}\n"
            "impl E {\n"
            "    pub fn make() -> E {\n"
            "        Self::Variant(1)\n"
            "    }\n"
            "}\n",
        )
        | {"crates/app/src/other.rs": "pub struct Variant(pub u8);\n"},
    )
    assert _callees(graph, "crates/app/src/app.rs::E.make") == set()


def test_self_in_a_trait_default_is_the_trait(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        _crate(
            "app",
            "pub trait Tr {\n"
            "    fn other() {}\n"
            "    fn go() {\n"
            "        Self::other();\n"
            "    }\n"
            "}\n"
            "pub struct X;\n"
            "impl X {\n"
            "    pub fn other() {}\n"
            "}\n",
        ),
    )
    assert _callees(graph, "crates/app/src/app.rs::Tr.go") == {
        "crates/app/src/app.rs::Tr.other"
    }


def test_a_qualified_self_type_reads_as_its_type(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        _others()
        | _crate(
            "app",
            "pub trait Tr {\n"
            "    fn new() -> Self;\n"
            "}\n"
            "pub struct T;\n"
            "impl T {\n"
            "    pub fn new() -> T {\n"
            "        T\n"
            "    }\n"
            "}\n"
            "pub fn run() {\n"
            "    <Vec<u8>>::new();\n"
            "    <T as Tr>::new();\n"
            "}\n",
        ),
    )
    assert _callees(graph, _RUN) == {"crates/app/src/app.rs::T.new"}
    assert _ambiguous(graph, _RUN) == {}
    assert _externals(graph, _RUN) == {"Vec<u8>::new"}


def test_a_written_path_to_a_same_file_macro_type(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        _crate(
            "app",
            "mod other;\n"
            "mod sys {\n"
            "    impl DisplayLink {\n"
            "        pub fn new() {}\n"
            "    }\n"
            "}\n"
            "pub fn run() {\n"
            "    sys::DisplayLink::new();\n"
            "}\n",
        )
        | {
            "crates/app/src/other.rs": (
                "impl DisplayLink {\n    pub fn new() {}\n}\n"
            )
        },
    )
    assert _callees(graph, _RUN) == {
        "crates/app/src/app.rs::sys.DisplayLink.new"
    }


def test_a_test_module_type_shadows_only_inside_it(tmp_path: Path) -> None:
    stub = "pub struct Stub;\nimpl Stub {\n    pub fn make() {}\n}\n"
    graph = _graph(
        tmp_path,
        _crate(
            "app",
            "mod support;\n"
            "use support::Stub;\n"
            "pub fn run() {\n"
            "    Stub::make();\n"
            "}\n"
            "#[cfg(test)]\n"
            "mod tests {\n"
            "    pub struct Stub;\n"
            "    impl Stub {\n"
            "        pub fn make() {}\n"
            "    }\n"
            "    fn t() {\n"
            "        Stub::make();\n"
            "    }\n"
            "}\n",
        )
        | {"crates/app/src/support.rs": stub},
    )
    assert _callees(graph, "crates/app/src/app.rs::tests.t") == {
        "crates/app/src/app.rs::tests.Stub.make"
    }
    assert _callees(graph, _RUN) == {"crates/app/src/support.rs::Stub.make"}


def test_a_bare_call_never_names_a_method(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        _others()
        | _crate(
            "app",
            "pub struct X;\n"
            "impl X {\n"
            "    pub fn new() -> X {\n"
            "        X\n"
            "    }\n"
            "    pub fn handler() {}\n"
            "}\n"
            "fn new() {}\n"
            "pub fn run() {\n"
            "    new();\n"
            "    handler();\n"
            "}\n",
        ),
    )
    assert _callees(graph, _RUN) == {"crates/app/src/app.rs::new"}
    assert _externals(graph, _RUN) == {"handler"}


def test_a_one_segment_path_to_a_same_file_impl_of_std_is_std(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        _crate(
            "app",
            "pub struct Name(u8);\n"
            "impl From<Name> for String {\n"
            "    fn from(n: Name) -> String {\n"
            "        String::new()\n"
            "    }\n"
            "}\n"
            "mod util {\n"
            "    pub fn from(a: u8) {}\n"
            "}\n"
            "pub fn run() {\n"
            '    String::from("x");\n'
            "    util::from(1);\n"
            "}\n",
        ),
    )
    assert _callees(graph, _RUN) == {"crates/app/src/app.rs::util.from"}
