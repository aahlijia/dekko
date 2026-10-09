"""A chained receiver typed by walking its segments through fields.

``this.mcpHub.callTool()`` reaches ``McpHub.callTool`` through the
declared type of ``mcpHub``; ``self.center.panes()`` never lands on the
enclosing type's own ``panes``; a field whose type is outside the repo
sends the call external.
"""

import textwrap
from pathlib import Path

from dekko.core.model import CallGraph
from dekko.core.resolver import resolve
from dekko.repo_ops import map_repository


def _graph(root: Path, sources: dict[str, str]) -> CallGraph:
    for rel, text in sources.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(textwrap.dedent(text))
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


def _external(graph: CallGraph, caller: str) -> set[str]:
    return {e.callee for e in graph.external if e.caller == caller}


def test_this_field_method_resolves_through_the_declared_type(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            "hub.ts": "export class McpHub { callTool() {} }\n",
            "b.ts": (
                "import { McpHub } from './hub';\n"
                "export class Builder {\n"
                "  constructor(private readonly mcpHub: McpHub) {}\n"
                "  run() { return this.mcpHub.callTool(); }\n"
                "}\n"
            ),
            "other.ts": "export class Other { callTool() {} }\n",
        },
    )
    assert _callees(g, "b.ts::Builder.run") == {"hub.ts::McpHub.callTool"}


def test_chained_receiver_never_lands_on_the_containers_own_method(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            "a.py": """
                class Group:
                    def panes(self):
                        return 1


                class Panel:
                    def __init__(self):
                        self.center = Group()

                    def panes(self):
                        return self.center.panes()
            """,
        },
    )
    assert _callees(g, "a.py::Panel.panes") == {"a.py::Group.panes"}


def test_foreign_field_type_goes_external(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "src/lib.rs": """
                pub struct B;
                impl B {
                    pub fn len(&self) -> usize { 0 }
                }
                pub struct S { items: Vec<u8> }
                impl S {
                    pub fn f(&self) -> usize { self.items.len() }
                }
            """,
        },
    )
    assert _callees(g, "src/lib.rs::S.f") == set()
    assert "self.items.len" in _external(g, "src/lib.rs::S.f")


def test_untyped_field_falls_through_without_the_container_rung(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            "a.py": """
                def helper():
                    return None


                class Panel:
                    def __init__(self):
                        self.x = helper()

                    def name(self):
                        return "p"

                    def show(self):
                        return self.x.name()
            """,
        },
    )
    assert "a.py::Panel.name" not in _callees(g, "a.py::Panel.show")


def test_typed_param_then_field(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "hub.ts": "export class McpHub { getServers() {} }\n",
            "ctl.ts": (
                "import { McpHub } from './hub';\n"
                "export class Controller { mcpHub: McpHub; }\n"
            ),
            "other.ts": "export class Other { getServers() {} }\n",
            "use.ts": (
                "import { Controller } from './ctl';\n"
                "export function sub(controller: Controller) {\n"
                "  controller.mcpHub.getServers();\n"
                "}\n"
            ),
        },
    )
    assert _callees(g, "use.ts::sub") == {"hub.ts::McpHub.getServers"}


def test_inherited_field_is_found_through_the_supertype(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            "p/Logger.java": (
                "package p;\nclass Logger { void info(String s) {} }\n"
            ),
            "p/Other.java": (
                "package p;\nclass Other { void info(String s) {} }\n"
            ),
            "p/Base.java": (
                "package p;\nclass Base { protected Logger logger; }\n"
            ),
            "p/Svc.java": (
                "package p;\n"
                "class Svc extends Base {\n"
                '  void f() { this.logger.info(""); }\n'
                "}\n"
            ),
        },
    )
    assert _callees(g, "p/Svc.java::Svc.f") == {"p/Logger.java::Logger.info"}


def test_two_same_named_types_that_disagree_leave_the_call_unknown(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            "a/x.py": "class Cfg:\n    h: 'Foo'\n\n\nclass Foo:\n"
            "    def run(self):\n        return 1\n",
            "a/y.py": "class Cfg:\n    h: 'Bar'\n\n\nclass Bar:\n"
            "    def run(self):\n        return 2\n",
            "a/z.py": "def f(c: Cfg):\n    return c.h.run()\n",
        },
    )
    assert _callees(g, "a/z.py::f") == set()


def test_depth_zero_calls_are_unchanged(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "w.ts": (
                "export class Widget {\n"
                "  static make() { return new Widget(); }\n"
                "  run() {}\n"
                "  go() { this.run(); }\n"
                "}\n"
            ),
            "o.ts": "export class O { run() {} go() {} static make() {} }\n",
            "u.ts": (
                "import { Widget } from './w';\n"
                "export function use(w: Widget) { w.run(); Widget.make(); }\n"
            ),
        },
    )
    assert _callees(g, "w.ts::Widget.go") == {"w.ts::Widget.run"}
    assert _callees(g, "u.ts::use") >= {
        "w.ts::Widget.run",
        "w.ts::Widget.make",
    }


def test_self_typed_field_walks_twice_and_terminates(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "n.ts": (
                "export class Node {\n"
                "  next: Node;\n"
                "  value() {}\n"
                "  walk() { this.next.next.value(); }\n"
                "}\n"
            ),
            "o.ts": "export class Other { value() {} }\n",
        },
    )
    assert _callees(g, "n.ts::Node.walk") == {"n.ts::Node.value"}


def test_in_repo_module_alias_receiver_is_not_sent_external(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            "utils.ts": (
                "export class Helper { run() {} }\n"
                "export const helper = new Helper();\n"
            ),
            "b.ts": (
                "import * as utils from './utils';\n"
                "export function f() { utils.helper.run(); }\n"
            ),
        },
    )
    assert "utils.helper.run" not in _external(g, "b.ts::f")


def test_go_receiver_then_field(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "s.go": (
                "package p\n"
                "type Store struct{}\n"
                "func (st *Store) Get() {}\n"
                "type Cache struct{}\n"
                "func (c *Cache) Get() {}\n"
                "type Server struct { store *Store }\n"
                "func (s *Server) Run() { s.store.Get() }\n"
            ),
        },
    )
    assert _callees(g, "s.go::Server.Run") == {"s.go::Store.Get"}


def test_rust_test_module_type_reads_its_own_fields(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "src/lib.rs": """
                pub struct Top;
                impl Top { pub fn run(&self) {} }
                pub struct Test;
                impl Test { pub fn run(&self) {} }
                pub struct S { t: Top }
                impl S { pub fn f(&self) { self.t.run() } }
                mod tests {
                    use super::*;
                    struct S { t: Test }
                    impl S { fn f(&self) { self.t.run() } }
                }
            """,
        },
    )
    assert _callees(g, "src/lib.rs::S.f") == {"src/lib.rs::Top.run"}
    assert _callees(g, "src/lib.rs::tests.S.f") == {"src/lib.rs::Test.run"}


def test_call_segment_hops_through_its_declared_return_type(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            "src/lib.rs": """
                pub struct MB;
                impl MB { pub fn anchor_at(&self) {} }
                pub struct BS;
                impl BS { pub fn anchor_at(&self) {} }
                pub struct E;
                impl E {
                    pub fn buffer_snapshot(&self) -> MB { MB }
                    pub fn f(&self) { self.buffer_snapshot().anchor_at() }
                }
            """,
        },
    )
    assert _callees(g, "src/lib.rs::E.f") >= {"src/lib.rs::MB.anchor_at"}
    assert "src/lib.rs::BS.anchor_at" not in _callees(g, "src/lib.rs::E.f")


def test_transparent_rust_calls_keep_the_type(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "src/lib.rs": """
                pub struct Mode;
                impl Mode { pub fn request(&self) {} }
                pub struct Other;
                impl Other { pub fn request(&self) {} }
                pub struct P { mode: Entity<Mode> }
                impl P {
                    pub fn f(&self, cx: &App) { self.mode.read(cx).request() }
                }
            """,
        },
    )
    assert _callees(g, "src/lib.rs::P.f") == {"src/lib.rs::Mode.request"}


def test_fluent_builder_returning_its_own_type(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "p/Base.java": (
                "package p;\n"
                "abstract class Base<SELF extends Base<SELF>> {\n"
                "  SELF withX() { return null; }\n"
                "  void run() {}\n"
                "}\n"
            ),
            "p/Other.java": "package p;\nclass Other { void run() {} }\n",
            "p/Use.java": (
                "package p;\n"
                "class Use {\n"
                "  Base<?> runner;\n"
                "  void f() { this.runner.withX().run(); }\n"
                "}\n"
            ),
            "b.ts": (
                "export class B { withX(): this { return this; } run() {} }\n"
                "export class O { run() {} }\n"
                "export class U { b: B; f() { this.b.withX().run(); } }\n"
            ),
            "src/lib.rs": """
                pub struct B;
                impl B {
                    pub fn with_x(self) -> Self { self }
                    pub fn run(&self) {}
                }
                pub struct O;
                impl O { pub fn run(&self) {} }
                pub struct U { b: B }
                impl U { pub fn f(&self) { self.b.with_x().run() } }
            """,
        },
    )
    assert _callees(g, "p/Use.java::Use.f") >= {"p/Base.java::Base.run"}
    assert "p/Other.java::Other.run" not in _callees(g, "p/Use.java::Use.f")
    assert _callees(g, "b.ts::U.f") >= {"b.ts::B.run"}
    assert "b.ts::O.run" not in _callees(g, "b.ts::U.f")
    assert _callees(g, "src/lib.rs::U.f") >= {"src/lib.rs::B.run"}
    assert "src/lib.rs::O.run" not in _callees(g, "src/lib.rs::U.f")


def test_nested_type_segment_and_enum_constant(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "p/ConfigData.java": (
                "package p;\n"
                "class ConfigData {\n"
                "  static class Options {\n"
                "    static Options of() { return null; }\n"
                "  }\n"
                "}\n"
            ),
            "p/PeriodStyle.java": (
                "package p;\n"
                "enum PeriodStyle {\n"
                "  SIMPLE;\n"
                "  Object parse(String s) { return null; }\n"
                "}\n"
            ),
            "p/X.java": (
                "package p;\n"
                "class X {\n"
                "  static void of() {}\n"
                "  Object parse(String s) { return null; }\n"
                "}\n"
            ),
            "p/Use.java": (
                "package p;\n"
                "class Use {\n"
                "  void f() {\n"
                "    ConfigData.Options.of();\n"
                '    PeriodStyle.SIMPLE.parse("");\n'
                "  }\n"
                "}\n"
            ),
        },
    )
    assert _callees(g, "p/Use.java::Use.f") == {
        "p/ConfigData.java::ConfigData.Options.of",
        "p/PeriodStyle.java::PeriodStyle.parse",
    }


def test_qualified_this_and_dot_class(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "p/Repo.java": "package p;\nclass Repo { void save() {} }\n",
            "p/Other.java": (
                "package p;\n"
                "class Other {\n"
                "  void save() {}\n"
                "  String getName() { return null; }\n"
                "}\n"
            ),
            "p/Outer.java": (
                "package p;\n"
                "class Outer {\n"
                "  Repo repo;\n"
                "  class Inner {\n"
                "    void f() { Outer.this.repo.save(); }\n"
                "    void g() { Outer.class.getName(); }\n"
                "  }\n"
                "}\n"
            ),
        },
    )
    assert _callees(g, "p/Outer.java::Outer.Inner.f") == {
        "p/Repo.java::Repo.save"
    }
    assert _callees(g, "p/Outer.java::Outer.Inner.g") == set()
    assert "Outer.class.getName" in _external(g, "p/Outer.java::Outer.Inner.g")


def test_bare_call_at_hop_zero_uses_the_return_type(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "p/Repo.java": "package p;\nclass Repo { void findAll() {} }\n",
            "p/Other.java": "package p;\nclass Other { void findAll() {} }\n",
            "p/Svc.java": (
                "package p;\n"
                "class Svc {\n"
                "  Repo getRepo() { return null; }\n"
                "  void f() { getRepo().findAll(); }\n"
                "}\n"
            ),
        },
    )
    assert _callees(g, "p/Svc.java::Svc.f") >= {"p/Repo.java::Repo.findAll"}
    assert "p/Other.java::Other.findAll" not in _callees(
        g, "p/Svc.java::Svc.f"
    )


def test_call_segment_without_return_type_is_unknown(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "a.py": """
                class B:
                    def h(self):
                        return 1


                class C:
                    def h(self):
                        return 2


                class A:
                    def g(self):
                        return B()

                    def f(self):
                        return self.g().h()
            """,
        },
    )
    assert "self.g().h" not in _external(g, "a.py::A.f")


def test_typed_receiver_with_several_members_is_ambiguous_among_them_only(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            "src/lib.rs": """
                use std::fmt;
                pub struct Foo;
                impl fmt::Display for Foo { fn fmt(&self) {} }
                impl fmt::Debug for Foo { fn fmt(&self) {} }
                pub struct Bar;
                impl Bar { pub fn fmt(&self) {} }
                pub struct H { f: Foo }
                impl H { pub fn g(&self) { self.f.fmt() } }
            """,
        },
    )
    caller = "src/lib.rs::H.g"
    assert "src/lib.rs::Bar.fmt" not in _callees(g, caller)
    row = _ambiguous(g, caller).get("fmt", set())
    assert "src/lib.rs::Bar.fmt" not in row
    picked = _callees(g, caller) | row
    assert picked and all(i.startswith("src/lib.rs::Foo.fmt") for i in picked)


def test_interface_typed_field_dispatches_to_its_implementors(
    tmp_path: Path,
) -> None:
    common = {
        "ad.ts": "export interface Adapter { upsert(): void }\n",
        "o.ts": "export class Other { upsert() {} }\n",
        "s.ts": (
            "import { Adapter } from './ad';\n"
            "export class S {\n"
            "  constructor(private adapter: Adapter) {}\n"
            "  f() { this.adapter.upsert(); }\n"
            "}\n"
        ),
    }
    a = (
        "import { Adapter } from './ad';\n"
        "export class A implements Adapter { upsert() {} }\n"
    )
    b = (
        "import { Adapter } from './ad';\n"
        "export class B implements Adapter { upsert() {} }\n"
    )
    two = _graph(tmp_path / "two", {**common, "a.ts": a, "b.ts": b})
    assert _callees(two, "s.ts::S.f") == set()
    assert _ambiguous(two, "s.ts::S.f") == {
        "upsert": {"a.ts::A.upsert", "b.ts::B.upsert"}
    }
    one = _graph(tmp_path / "one", {**common, "a.ts": a})
    assert _callees(one, "s.ts::S.f") == {"a.ts::A.upsert"}


def test_repo_type_without_the_member_is_external(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "p/UserRepository.java": (
                "package p;\n"
                "interface UserRepository\n"
                "    extends JpaRepository<User, Long> {}\n"
            ),
            "p/Other.java": "package p;\nclass Other { void findAll() {} }\n",
            "p/S.java": (
                "package p;\n"
                "class S {\n"
                "  UserRepository repo;\n"
                "  void f() { this.repo.findAll(); }\n"
                "}\n"
            ),
        },
    )
    assert _callees(g, "p/S.java::S.f") == set()
    assert _ambiguous(g, "p/S.java::S.f") == {}
    assert "this.repo.findAll" in _external(g, "p/S.java::S.f")


def test_aliased_type_import_in_a_parameter_and_a_field(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            "svc.ts": "export class Widget { run() {} }\n",
            "x.ts": "export class X { run() {} }\n",
            "u.ts": (
                "import { Widget as WD } from './svc';\n"
                "export function f(v: WD) { v.run(); }\n"
                "export class H {\n"
                "  private w: WD;\n"
                "  g() { this.w.run(); }\n"
                "}\n"
            ),
        },
    )
    assert _callees(g, "u.ts::f") == {"svc.ts::Widget.run"}
    assert _callees(g, "u.ts::H.g") == {"svc.ts::Widget.run"}


def test_python_aliased_import(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "w.py": "class Widget:\n    def run(self):\n        return 1\n",
            "x.py": "class X:\n    def run(self):\n        return 2\n",
            "u.py": (
                "from w import Widget as WD\n\n\n"
                "def f(v: WD):\n    return v.run()\n"
            ),
        },
    )
    assert _callees(g, "u.py::f") == {"w.py::Widget.run"}


def test_go_embedded_struct_methods_are_members(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "s.go": (
                "package p\n"
                "type Base struct{}\n"
                "func (b *Base) Log() {}\n"
                "type Other struct{}\n"
                "func (o *Other) Log() {}\n"
                "type Svc struct {\n"
                "\tBase\n"
                "}\n"
                "type App struct { svc *Svc }\n"
                "func (a *App) Run() { a.svc.Log() }\n"
            ),
        },
    )
    assert _callees(g, "s.go::App.Run") == {"s.go::Base.Log"}


def test_rust_deref_type_without_the_member_is_not_external(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            "src/lib.rs": """
                use std::ops::Deref;
                pub struct Inner;
                impl Inner { pub fn inner_m(&self) {} }
                pub struct Other;
                impl Other { pub fn inner_m(&self) {} }
                pub struct W { inner: Inner }
                impl Deref for W {
                    type Target = Inner;
                    fn deref(&self) -> &Inner { &self.inner }
                }
                pub struct H { w: W }
                impl H { pub fn f(&self) { self.w.inner_m() } }
            """,
        },
    )
    assert "self.w.inner_m" not in _external(g, "src/lib.rs::H.f")


def test_imported_type_wins_over_a_nearer_namesake(tmp_path: Path) -> None:
    # A test stub of the same class sits closer to the caller than the
    # file the import names; the import says which one the field is.
    g = _graph(
        tmp_path,
        {
            "sdk/core/psm.ts": "export class Psm { get() {} }\n",
            "apps/vscode/test/stub.ts": "export class Psm { get() {} }\n",
            "apps/cli/agent.ts": (
                "import { Psm } from '../../sdk/core/psm';\n"
                "export class Agent {\n"
                "  private psm = new Psm();\n"
                "  run() { this.psm.get(); }\n"
                "}\n"
            ),
        },
    )
    assert _callees(g, "apps/cli/agent.ts::Agent.run") == {
        "sdk/core/psm.ts::Psm.get"
    }


def test_call_through_a_callback_field_is_left_to_the_ladder(
    tmp_path: Path,
) -> None:
    # ``options.post`` is a slot some caller fills, usually with a repo
    # function of that name; the walk can't say which, so it doesn't
    # claim the call leaves the repo.
    g = _graph(
        tmp_path,
        {
            "o.ts": "export interface Opts { post: () => void }\n",
            "ctl.ts": "export class Controller { post() {} }\n",
            "c.ts": (
                "import { Opts } from './o';\n"
                "export class C {\n"
                "  constructor(private options: Opts) {}\n"
                "  f() { this.options.post(); }\n"
                "}\n"
            ),
        },
    )
    assert "this.options.post" not in _external(g, "c.ts::C.f")
    assert _callees(g, "c.ts::C.f") == {"ctl.ts::Controller.post"}


def test_inline_object_type_parameter_types_its_member(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            "c.ts": "export class Client { getSchedule() {} }\n",
            "o.ts": "export class Other { getSchedule() {} }\n",
            "a.ts": (
                "import { Client } from './c';\n"
                "export function f(input: { bot: Bot; client: Client }) {\n"
                "  input.client.getSchedule();\n"
                "}\n"
            ),
        },
    )
    assert _callees(g, "a.ts::f") == {"c.ts::Client.getSchedule"}


def test_ts_interface_without_implementor_is_left_to_the_ladder(
    tmp_path: Path,
) -> None:
    # TS typing is structural: an object literal fills ``Io`` without
    # naming it, so "no class implements it" doesn't mean the method is
    # outside the repo.
    g = _graph(
        tmp_path,
        {
            "io.ts": (
                "export interface Io { writeErr(m: string): void }\n"
                "export function writeErr(m: string) {}\n"
            ),
            "a.ts": (
                "import { Io } from './io';\n"
                "export function f(input: { io: Io }) {\n"
                '  input.io.writeErr("x");\n'
                "}\n"
            ),
        },
    )
    assert "input.io.writeErr" not in _external(g, "a.ts::f")


def test_rust_trait_impl_in_another_crate_is_a_member(tmp_path: Path) -> None:
    def crate(name: str, body: str) -> dict[str, str]:
        return {
            f"crates/{name}/Cargo.toml": (
                f'[package]\nname = "{name}"\n\n'
                f'[lib]\npath = "src/{name}.rs"\n'
            ),
            f"crates/{name}/src/{name}.rs": body,
        }

    g = _graph(
        tmp_path,
        {
            **crate("geo", "pub struct Point;\nimpl Point {}\n"),
            **crate(
                "ed",
                "use geo::Point;\n"
                "pub trait ToDisplay { fn to_display(&self); }\n"
                "impl ToDisplay for Point { fn to_display(&self) {} }\n"
                "pub struct Other;\n"
                "impl Other { pub fn to_display(&self) {} }\n"
                "pub struct V { p: Point }\n"
                "impl V { pub fn f(&self) { self.p.to_display() } }\n",
            ),
        },
    )
    assert _callees(g, "crates/ed/src/ed.rs::V.f") == {
        "crates/ed/src/ed.rs::Point.to_display"
    }


def test_fluent_self_call_chain_keeps_the_containers_method(
    tmp_path: Path,
) -> None:
    # ``self.f().g()`` with an untyped ``f``: the walk can't type it, and
    # a fluent API returning self is the common case, so the container's
    # own ``g`` stays a candidate.
    g = _graph(
        tmp_path,
        {
            "a.py": """
                class S:
                    def hidden(self):
                        return self

                    def done(self):
                        return 1

                    def run(self):
                        return self.hidden().done()
            """,
        },
    )
    assert _callees(g, "a.py::S.run") >= {"a.py::S.done"}


def test_rust_closure_this_is_not_self(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "src/lib.rs": """
                pub struct W { d: D }
                pub struct D;
                impl D { pub fn go(&self) {} }
                pub struct E { d: Other }
                pub struct Other;
                impl Other { pub fn go(&self) {} }
                impl E {
                    pub fn f(&self, w: &W) {
                        let run = |this: &W| this.d.go();
                        run(w);
                    }
                }
            """,
        },
    )
    assert "src/lib.rs::Other.go" not in _callees(g, "src/lib.rs::E.f")


def test_a_nullish_placeholder_does_not_hide_the_real_assignment(
    tmp_path: Path,
) -> None:
    # ``self.conn = None`` in ``__init__`` says nothing about the type;
    # the later ``Connection()`` does.
    g = _graph(
        tmp_path,
        {
            "db.py": """
                class Connection:
                    def execute(self):
                        return 1


                class Repo:
                    def __init__(self):
                        self.conn = None

                    def open(self):
                        self.conn = Connection()

                    def run(self):
                        return self.conn.execute()
            """,
            "c.ts": (
                "export class Client { send() {} }\n"
                "export class Svc {\n"
                "  private client = null;\n"
                "  open() { this.client = new Client(); }\n"
                "  run() { this.client.send(); }\n"
                "}\n"
            ),
        },
    )
    assert _callees(g, "db.py::Repo.run") == {"db.py::Connection.execute"}
    assert _callees(g, "c.ts::Svc.run") == {"c.ts::Client.send"}


def test_a_foreign_hop_before_a_call_leaves_the_receiver_unknown(
    tmp_path: Path,
) -> None:
    # ``tasks.get(id)`` returns a ``Task``; the walk can't read that off
    # ``Map``, so it mustn't claim the call leaves the repo.
    g = _graph(
        tmp_path,
        {
            "t.ts": (
                "export class Task { abort() {} }\n"
                "export class Runner {\n"
                "  private tasks: Map<string, Task>;\n"
                "  stop(id: string) { this.tasks.get(id)!.abort(); }\n"
                "}\n"
            ),
        },
    )
    assert "this.tasks.get().abort" not in _external(g, "t.ts::Runner.stop")
    assert _callees(g, "t.ts::Runner.stop") == {"t.ts::Task.abort"}
