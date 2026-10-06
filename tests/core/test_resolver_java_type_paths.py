"""JVM written type paths: a call reaches only the type it names.

``new a.b.C()`` and ``a.b.C.m()`` name a package and a top-level type,
``new Outer.Inner()`` a nesting. A candidate elsewhere is not the
target, however alike its name.
"""

from pathlib import Path

from dekko.core.model import CallGraph
from dekko.core.resolver import resolve
from dekko.repo_ops import map_repository

_SRC = "src/main/java"


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


def _java(package: str, body: str) -> str:
    return f"package {package};\n{body}"


_TOOL = _java(
    "org.mine",
    "public class Tool {\n"
    "    public Tool(int a) { }\n"
    "    public Tool(int a, int b) { }\n"
    "    public static void m(int a) { }\n"
    "}\n",
)
_USER = f"{_SRC}/app/User.java"


def _user(body: str) -> str:
    return _java("app", f"public class User {{\n{body}}}\n")


def test_a_package_path_construction_reaches_only_that_package(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            f"{_SRC}/org/mine/Tool.java": _TOOL,
            _USER: _user(
                "    Object mine() { return new org.mine.Tool(1); }\n"
                "    Object theirs() { return new org.other.Tool(1); }\n"
            ),
        },
    )
    edges = _edges(graph)
    cls = f"{_SRC}/org/mine/Tool.java::Tool"
    assert (f"{_USER}::User.mine", cls) in edges
    assert (f"{_USER}::User.mine", f"{cls}.Tool") in edges
    assert not [p for p in edges if p[0] == f"{_USER}::User.theirs"]
    assert (f"{_USER}::User.theirs", "new org.other.Tool") in _external(graph)


def test_same_named_classes_are_each_constructed_by_package(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            f"{_SRC}/org/a/C.java": _java("org.a", "public class C { }\n"),
            f"{_SRC}/org/b/C.java": _java("org.b", "public class C { }\n"),
            _USER: _user(
                "    Object one() { return new org.a.C(); }\n"
                "    Object two() { return new org.b.C(); }\n"
            ),
        },
    )
    edges = _edges(graph)
    assert (f"{_USER}::User.one", f"{_SRC}/org/a/C.java::C") in edges
    assert (f"{_USER}::User.two", f"{_SRC}/org/b/C.java::C") in edges
    assert not graph.ambiguous


def test_a_static_call_through_a_package_path(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        {
            f"{_SRC}/org/mine/Tool.java": _TOOL,
            _USER: _user(
                "    void mine() { org.mine.Tool.m(1); }\n"
                "    void theirs() { org.other.Tool.m(1); }\n"
                "    void jdk() { java.util.Collections.m(1); }\n"
            ),
        },
    )
    edges = _edges(graph)
    target = f"{_SRC}/org/mine/Tool.java::Tool.m"
    assert (f"{_USER}::User.mine", target) in edges
    assert (f"{_USER}::User.theirs", target) not in edges
    assert (f"{_USER}::User.jdk", target) not in edges
    external = _external(graph)
    assert (f"{_USER}::User.theirs", "org.other.Tool.m") in external
    assert (f"{_USER}::User.jdk", "java.util.Collections.m") in external


_NESTING = {
    f"{_SRC}/app/Outer.java": _java(
        "app", "public class Outer {\n    public static class Inner { }\n}\n"
    ),
    f"{_SRC}/app/Other.java": _java(
        "app", "public class Other {\n    public static class Inner { }\n}\n"
    ),
}


def test_an_outer_type_path_picks_its_nested_type(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        {
            **_NESTING,
            _USER: _user(
                "    Object plain() { return new Outer.Inner(); }\n"
                "    Object generic() { return new Outer<String>.Inner(); }\n"
                "    Object noted() { return new @Ann Outer.Inner(); }\n"
                "    Object ext() { return new Ext.Inner(); }\n"
            ),
        },
    )
    edges = _edges(graph)
    inner = f"{_SRC}/app/Outer.java::Outer.Inner"
    for caller in ("plain", "generic", "noted"):
        assert (f"{_USER}::User.{caller}", inner) in edges
    assert not [p for p in edges if p[0] == f"{_USER}::User.ext"]
    assert (f"{_USER}::User.ext", "new Ext.Inner") in _external(graph)
    assert not graph.ambiguous


def test_a_lowercase_head_that_is_no_package_is_left_alone(
    tmp_path: Path,
) -> None:
    """``foo`` is one lowercase segment and no known package root, so
    ``foo.Bar.m()`` is not read as a package path."""
    graph = _graph(
        tmp_path,
        {
            f"{_SRC}/app/Bar.java": _java(
                "app", "public class Bar {\n    public void m() { }\n}\n"
            ),
            _USER: _user("    void go(Object foo) { foo.Bar.m(); }\n"),
        },
    )
    assert (
        f"{_USER}::User.go",
        f"{_SRC}/app/Bar.java::Bar.m",
    ) in _edges(graph)


def test_a_kotlin_package_path_construction(tmp_path: Path) -> None:
    kt = "src/main/kotlin"
    graph = _graph(
        tmp_path,
        {
            f"{kt}/org/a/C.kt": "package org.a\nclass C\n",
            f"{kt}/org/b/C.kt": "package org.b\nclass C\n",
            f"{kt}/app/Use.kt": "package app\nfun use() = org.b.C()\n",
        },
    )
    assert (f"{kt}/app/Use.kt::use", f"{kt}/org/b/C.kt::C") in _edges(graph)
    assert not graph.ambiguous
