"""Rust calls never reach a function their shape or count rules out.

Rust has no overloads, default arguments or optional parameters, so a
dot-call needs a ``self`` method taking the written count, a bare call
can't reach a method, and a ``Type::method(obj, ..)`` path writes
``self`` too. A pick that breaks one of those is skipped and the
ladder runs again without it.
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


_USE_IT = "src/lib.rs::use_it"


def test_dot_call_never_takes_a_method_wanting_more_arguments(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            "src/lib.rs": (
                "pub struct Editor;\n"
                "impl Editor {\n"
                "    pub fn clone(&self, cx: u8) -> Editor {\n"
                "        Editor\n"
                "    }\n"
                "}\n"
                "pub fn use_it(x: String) {\n"
                "    x.clone();\n"
                "}\n"
            ),
        },
    )
    assert _pairs(graph, _USE_IT) == set()
    assert _ambiguous_for(graph, _USE_IT) == {}
    assert _externals(graph, _USE_IT) == {"x.clone"}


def test_dot_call_never_takes_an_associated_function(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        {
            "src/lib.rs": (
                "pub struct Summary;\n"
                "impl Summary {\n"
                "    pub fn min(a: u8, b: u8) -> u8 {\n"
                "        a\n"
                "    }\n"
                "}\n"
                "pub fn use_it(a: u8, b: u8) {\n"
                "    a.min(b);\n"
                "}\n"
            ),
        },
    )
    assert _pairs(graph, _USE_IT) == set()
    assert _externals(graph, _USE_IT) == {"a.min"}


def test_bare_call_never_takes_a_method(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        {
            "src/lib.rs": (
                "pub struct Terminal;\n"
                "impl Terminal {\n"
                "    pub fn drop(&mut self) {}\n"
                "    pub fn element(a: u8, b: u8) {}\n"
                "}\n"
                "pub fn use_it(x: u8) {\n"
                "    drop(x);\n"
                "    element(x, x);\n"
                "}\n"
            ),
        },
    )
    assert _pairs(graph, _USE_IT) == set()
    assert _externals(graph, _USE_IT) == {"drop", "element"}


def test_bare_call_never_takes_a_self_function_in_a_local_impl(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            "src/lib.rs": (
                "pub fn use_it(task: u8) {\n"
                "    struct Guard;\n"
                "    impl Drop for Guard {\n"
                "        fn drop(&mut self) {}\n"
                "    }\n"
                "    drop(task);\n"
                "}\n"
            ),
        },
    )
    assert _pairs(graph, _USE_IT) == set()
    assert _externals(graph, _USE_IT) == {"drop"}


def test_path_call_never_takes_a_function_wanting_more_arguments(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            "src/lib.rs": (
                "pub fn init(cx: u8) {}\n"
                "pub fn use_it() {\n"
                "    zlog::init();\n"
                "}\n"
            ),
        },
    )
    assert _pairs(graph, _USE_IT) == set()
    assert _externals(graph, _USE_IT) == {"zlog::init"}


def test_path_call_counts_the_self_argument(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        {
            "src/lib.rs": (
                "pub struct S;\n"
                "impl S {\n"
                "    pub fn m(&self, a: u8) {}\n"
                "}\n"
                "pub fn use_it(s: S) {\n"
                "    S::m(&s, 1);\n"
                "}\n"
            ),
        },
    )
    assert _pairs(graph, _USE_IT) == {"src/lib.rs::S.m"}


def test_wrapper_call_reaches_the_wrapped_method(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        {
            "src/inner.rs": (
                "pub struct Inner;\n"
                "impl Inner {\n"
                "    pub fn set(&mut self, a: u8) {}\n"
                "}\n"
            ),
            "src/outer.rs": (
                "use crate::inner::Inner;\n"
                "pub struct Outer {\n"
                "    inner: Inner,\n"
                "}\n"
                "impl Outer {\n"
                "    pub fn set(&mut self, a: u8, b: u8) {\n"
                "        self.inner.set(a);\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    assert _pairs(graph, "src/outer.rs::Outer.set") == {
        "src/inner.rs::Inner.set"
    }


def test_skipped_pick_leaves_the_call_ambiguous_among_the_rest(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            "src/a.rs": (
                "pub struct A;\n"
                "impl A {\n"
                "    pub fn set(&mut self, a: u8) {}\n"
                "}\n"
            ),
            "src/b.rs": (
                "pub struct B;\n"
                "impl B {\n"
                "    pub fn set(&mut self, a: u8) {}\n"
                "}\n"
            ),
            "src/outer.rs": (
                "pub struct Outer;\n"
                "impl Outer {\n"
                "    pub fn set(&mut self, a: u8, b: u8) {\n"
                "        self.inner.set(a);\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    caller = "src/outer.rs::Outer.set"
    assert _pairs(graph, caller) == set()
    assert {"src/a.rs::A.set", "src/b.rs::B.set"} <= set(
        _ambiguous_for(graph, caller)["set"]
    )


def test_uncounted_macro_arguments_keep_the_edge(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        {
            "src/lib.rs": (
                "pub struct Editor;\n"
                "impl Editor {\n"
                "    pub fn check(&self, f: u8, g: u8) -> bool {\n"
                "        true\n"
                "    }\n"
                "}\n"
                "pub fn use_it(e: Editor) {\n"
                "    assert!(e.check(|a, b| a));\n"
                "}\n"
            ),
        },
    )
    assert _pairs(graph, _USE_IT) == {"src/lib.rs::Editor.check"}


def test_other_languages_keep_a_mismatched_pick(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        {
            "app.py": (
                "class Editor:\n"
                "    def tidy(self, cx):\n"
                "        pass\n"
                "    def use_it(self):\n"
                "        self.tidy()\n"
            ),
            "app.ts": (
                "export class Panel {\n"
                "  tidy(a: number, b: number) {}\n"
                "  useIt() {\n"
                "    this.tidy();\n"
                "  }\n"
                "}\n"
            ),
        },
    )
    assert _pairs(graph, "app.py::Editor.use_it") == {"app.py::Editor.tidy"}
    assert _pairs(graph, "app.ts::Panel.useIt") == {"app.ts::Panel.tidy"}
