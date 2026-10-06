"""Java argument counts: a call lands only where its count fits.

Java has no default arguments, so a method takes exactly its parameter
count (or at least its fixed ones, with varargs). A pick the count
can't call is retried, one class's overload set is picked by count, an
ambiguous row lists only what the call could mean, and a call no
candidate can answer is external.
"""

from pathlib import Path

from dekko.core.model import CallGraph
from dekko.core.resolver import resolve
from dekko.repo_ops import map_repository

_MAIN = "src/main/java/app"


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


def _edges(graph: CallGraph) -> set[tuple[str, str]]:
    return {(e.caller, e.callee) for e in graph.edges}


def _external(graph: CallGraph) -> set[tuple[str, str]]:
    return {(e.caller, e.callee) for e in graph.external}


def _rows(graph: CallGraph, name: str) -> list[list[str]]:
    return [sorted(cands) for _, n, cands in graph.ambiguous if n == name]


def _class(name: str, body: str) -> str:
    return f"package app;\npublic class {name} {{\n{body}}}\n"


_LOG = _class(
    "Log",
    "    public void debug(Object m) { }\n"
    "    public void debug(Object m, Throwable t) { }\n",
)


def test_a_vetoed_same_file_pick_reaches_the_overload_that_fits(
    tmp_path: Path,
) -> None:
    """The same-file rung takes the test's own ``debug()``; two
    arguments can't call it, so the ladder runs again and the other
    file's overload set is picked by count."""
    graph = _graph(
        tmp_path,
        {
            f"{_MAIN}/Log.java": _LOG,
            f"{_MAIN}/LogTests.java": _class(
                "LogTests",
                "    private Log log;\n"
                "    void debug() { }\n"
                "    void go(Object m, Throwable t) {\n"
                "        this.log.debug(m, t);\n"
                "    }\n",
            ),
        },
    )
    edges = _edges(graph)
    caller = f"{_MAIN}/LogTests.java::LogTests.go"
    assert (caller, f"{_MAIN}/Log.java::Log.debug#2") in edges
    assert (caller, f"{_MAIN}/Log.java::Log.debug") not in edges
    assert (caller, f"{_MAIN}/LogTests.java::LogTests.debug") not in edges


def test_a_same_file_method_the_count_cant_call_is_external(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            f"{_MAIN}/Box.java": _class(
                "Box",
                "    void m(int a, int b) { }\n"
                "    void go(Object x) { x.m(1); }\n",
            ),
        },
    )
    caller = f"{_MAIN}/Box.java::Box.go"
    assert (caller, f"{_MAIN}/Box.java::Box.m") not in _edges(graph)
    assert (caller, "x.m") in _external(graph)


def test_varargs_take_any_count_from_the_fixed_minimum(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            f"{_MAIN}/Box.java": _class(
                "Box",
                "    void m(String... a) { }\n"
                "    void n(int a, String... b) { }\n"
                "    void zero(Object x) { x.m(); x.n(); }\n"
                '    void one(Object x) { x.m("a"); x.n(1); }\n'
                '    void three(Object x) { x.m("a", "b", "c"); }\n',
            ),
        },
    )
    edges = _edges(graph)
    box = f"{_MAIN}/Box.java::Box"
    for caller in ("zero", "one", "three"):
        assert (f"{box}.{caller}", f"{box}.m") in edges
    assert (f"{box}.one", f"{box}.n") in edges
    assert (f"{box}.zero", f"{box}.n") not in edges


_TOOL = _class(
    "Tool",
    "    public void m(int a) { }\n"
    "    public void m(int a, int b) { }\n"
    "    public void s(int a) { }\n"
    "    public void s(String a) { }\n",
)


def _user(body: str) -> str:
    return _class("User", f"    void go(Object t) {{ {body} }}\n")


def test_one_class_overloads_are_picked_by_count(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        {f"{_MAIN}/Tool.java": _TOOL, f"{_MAIN}/User.java": _user("t.m(1);")},
    )
    caller = f"{_MAIN}/User.java::User.go"
    assert (caller, f"{_MAIN}/Tool.java::Tool.m") in _edges(graph)
    assert not _rows(graph, "m")


def test_one_class_overloads_none_fits_is_external(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        {
            f"{_MAIN}/Tool.java": _TOOL,
            f"{_MAIN}/User.java": _user("t.m(1, 2, 3);"),
        },
    )
    caller = f"{_MAIN}/User.java::User.go"
    assert not [p for p in _edges(graph) if p[0] == caller]
    assert (caller, "t.m") in _external(graph)
    assert not _rows(graph, "m")


def test_one_class_overloads_of_one_count_stay_ambiguous(
    tmp_path: Path,
) -> None:
    """Argument types would tell ``s(int)`` from ``s(String)``; dekko
    reads counts only."""
    graph = _graph(
        tmp_path,
        {f"{_MAIN}/Tool.java": _TOOL, f"{_MAIN}/User.java": _user("t.s(1);")},
    )
    tool = f"{_MAIN}/Tool.java::Tool"
    assert _rows(graph, "s") == [[f"{tool}.s", f"{tool}.s#2"]]


_BAG = _class(
    "Bag",
    "    public void add(Object a) { }\n"
    "    public void add(Object a, Object b) { }\n"
    "    void self(Object x) { this.add(x); }\n",
)


def test_a_jdk_method_name_on_a_receiver_is_no_overload_pick(
    tmp_path: Path,
) -> None:
    """One class owning every in-repo ``add`` is no reason
    ``list.add(x)`` means it; inside the class, ``this.add(x)`` does."""
    graph = _graph(
        tmp_path,
        {
            f"{_MAIN}/Bag.java": _BAG,
            f"{_MAIN}/User.java": _class(
                "User",
                "    void go(java.util.List<Object> list, Object x) {\n"
                "        list.add(x);\n"
                "    }\n",
            ),
        },
    )
    bag = f"{_MAIN}/Bag.java::Bag"
    edges = _edges(graph)
    assert (f"{bag}.self", f"{bag}.add") in edges
    assert not [p for p in edges if p[0] == f"{_MAIN}/User.java::User.go"]
    assert [f"{bag}.add", f"{bag}.add#2"] in _rows(graph, "add")


def test_an_ambiguous_row_lists_what_the_call_could_mean(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            f"{_MAIN}/A.java": _class("A", "    public void look(int a) {}\n"),
            f"{_MAIN}/B.java": _class("B", "    public void look(int a) {}\n"),
            f"{_MAIN}/C.java": _class("C", "    public void look() {}\n"),
            f"{_MAIN}/D.java": _class(
                "D", "    private void look(int a) {}\n"
            ),
            f"{_MAIN}/User.java": _user("t.look(1);"),
        },
    )
    assert _rows(graph, "look") == [
        [f"{_MAIN}/A.java::A.look", f"{_MAIN}/B.java::B.look"]
    ]


def test_a_call_no_candidate_can_answer_is_external(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        {
            f"{_MAIN}/A.java": _class(
                "A", "    public void pair(int a, int b) { }\n"
            ),
            f"{_MAIN}/B.java": _class(
                "B", "    public void pair(int a, int b) { }\n"
            ),
            f"{_MAIN}/User.java": _user("t.pair(1);"),
        },
    )
    assert not _rows(graph, "pair")
    assert (f"{_MAIN}/User.java::User.go", "t.pair") in _external(graph)


def test_a_method_called_on_a_construction_is_counted(tmp_path: Path) -> None:
    """``new File(p).toURI()`` is a method call with a count; only the
    construction itself is left to the constructor overloads."""
    graph = _graph(
        tmp_path,
        {
            f"{_MAIN}/Runner.java": _class(
                "Runner",
                "    void run(String a) { }\n"
                "    void go() { new Thread().run(); }\n",
            ),
        },
    )
    runner = f"{_MAIN}/Runner.java::Runner"
    assert (f"{runner}.go", f"{runner}.run") not in _edges(graph)


def test_a_kotlin_call_is_not_judged_by_count(tmp_path: Path) -> None:
    """Kotlin has default and named arguments, which ``Param`` doesn't
    record."""
    tools = "src/main/kotlin/app/Tools.kt"
    graph = _graph(
        tmp_path,
        {
            tools: (
                "package app\n"
                "class Tools {\n"
                "    fun m(a: Int, b: Int = 2) {}\n"
                "    fun go(x: Tools) { x.m(1) }\n"
                "}\n"
            ),
        },
    )
    assert (f"{tools}::Tools.go", f"{tools}::Tools.m") in _edges(graph)
