"""Rust calls never reach a function their shape or count rules out.

Rust has no overloads, default arguments or optional parameters, so a
dot-call needs a ``self`` method taking the written count, a bare call
can't reach a method, and a ``Type::method(obj, ..)`` path writes
``self`` too. A pick that breaks one of those is skipped and the
ladder runs again without it. A dot-call tied with a same-file free
function of its name, which it can't call, takes the repo's one method
of the name that takes ``self``.
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


_DELETE = (
    "pub async fn delete(id: u8) {}\n"
    "pub struct D;\n"
    "impl D {\n"
    "    pub fn delete(&self, ix: u8) {}\n"
    "}\n"
    "fn make() -> D { D }\n"
)


def test_a_dot_call_tie_with_a_same_file_function_takes_the_method(
    tmp_path: Path,
) -> None:
    # The free ``delete`` can't be what ``d.delete(1)`` means, and the
    # repo has one ``delete`` that takes ``self``.
    graph = _graph(
        tmp_path,
        {
            "src/lib.rs": _DELETE
            + "pub fn use_it() {\n    let d = make();\n    d.delete(1);\n}\n"
        },
    )
    assert _pairs(graph, _USE_IT) == {
        "src/lib.rs::make",
        "src/lib.rs::D.delete",
    }
    assert _ambiguous_for(graph, _USE_IT) == {}


def test_a_dot_call_tie_through_a_closure_parameter_takes_the_method(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            "src/lib.rs": _DELETE
            + "fn run(f: impl Fn(&D)) { f(&make()) }\n"
            + "pub fn use_it() {\n    run(|this| this.delete(1));\n}\n"
        },
    )
    assert "src/lib.rs::D.delete" in _pairs(graph, _USE_IT)
    assert "delete" not in _ambiguous_for(graph, _USE_IT)


def test_a_dot_call_tie_reaches_a_method_in_another_file(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            "src/db.rs": (
                "pub struct Db;\n"
                "impl Db {\n"
                "    pub fn decline_call(&self, x: u8) {}\n"
                "}\n"
                "pub fn open() -> Db { Db }\n"
            ),
            "src/lib.rs": (
                "mod db;\n"
                "pub fn decline_call(x: u8) {\n"
                "    let db = db::open();\n"
                "    db.decline_call(x);\n"
                "}\n"
            ),
        },
    )
    assert "src/db.rs::Db.decline_call" in _pairs(
        graph, "src/lib.rs::decline_call"
    )


def test_two_methods_of_the_name_keep_the_tie(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        {
            "src/git.rs": (
                "pub struct Branch;\n"
                "impl Branch {\n"
                "    pub fn name(&self) -> u8 { 0 }\n"
                "}\n"
            ),
            "src/lib.rs": (
                "mod git;\n"
                "pub struct Entry;\n"
                "impl Entry {\n"
                "    pub fn name(&self) -> u8 { 0 }\n"
                "}\n"
                'pub fn name() -> &\'static str { "x" }\n'
                "fn get() -> u8 { 0 }\n"
                "pub fn use_it() {\n"
                "    let branch = get();\n"
                "    branch.name();\n"
                "}\n"
            ),
        },
    )
    assert "src/lib.rs::Entry.name" not in _pairs(graph, _USE_IT)
    assert set(_ambiguous_for(graph, _USE_IT)["name"]) == {
        "src/lib.rs::Entry.name",
        "src/git.rs::Branch.name",
    }


def test_a_typed_parameter_receiver_is_not_narrowed(tmp_path: Path) -> None:
    # ``term`` is declared ``&Term``, an outside type: the typed-parameter
    # rung already found ``Term`` owns no ``lines`` in the repo.
    graph = _graph(
        tmp_path,
        {
            "src/lib.rs": (
                "use foreign::Term;\n"
                "pub struct Bounds;\n"
                "impl Bounds {\n"
                "    pub fn lines(&self) -> usize { 0 }\n"
                "}\n"
                "pub fn lines(term: &Term) -> usize {\n"
                "    term.lines()\n"
                "}\n"
            ),
        },
    )
    assert "src/lib.rs::Bounds.lines" not in _pairs(graph, "src/lib.rs::lines")


def test_no_same_file_blocker_leaves_the_tie(tmp_path: Path) -> None:
    # Without a same-file function to rule out, a lone in-repo method
    # proves nothing: ``m`` here is a std map.
    graph = _graph(
        tmp_path,
        {
            "src/tree.rs": (
                "pub struct TreeMap;\n"
                "impl TreeMap {\n"
                "    pub fn values(&self) -> u8 { 0 }\n"
                "}\n"
            ),
            "src/util.rs": "pub fn values() -> u8 { 0 }\n",
            "src/lib.rs": (
                "mod tree;\nmod util;\n"
                "fn get() -> u8 { 0 }\n"
                "pub fn use_it() {\n"
                "    let m = get();\n"
                "    m.values();\n"
                "}\n"
            ),
        },
    )
    before = _pairs(graph, _USE_IT)
    assert "src/tree.rs::TreeMap.values" not in before


def test_the_one_method_still_needs_the_count(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        {
            "src/lib.rs": _DELETE
            + "pub fn use_it() {\n    let d = make();\n    d.delete();\n}\n"
        },
    )
    assert _pairs(graph, _USE_IT) == {"src/lib.rs::make"}
    assert _externals(graph, _USE_IT) == {"d.delete"}


def test_a_path_call_is_not_narrowed(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        {
            "src/lib.rs": _DELETE
            + "pub fn use_it() {\n"
            + "    let d = make();\n    D::delete(&d, 1);\n}\n"
        },
    )
    assert "src/lib.rs::D.delete" in _pairs(graph, _USE_IT)
