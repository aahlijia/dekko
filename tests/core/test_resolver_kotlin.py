"""Kotlin call and import resolution, alone and alongside Java.

Kotlin and Java are one JVM family: a Kotlin file imports and calls
Java classes, and Java calls Kotlin. Each language still prefers its
own candidates, so a Java call with a Java target never sees a Kotlin
one.
"""

from pathlib import Path

from dekko.core.model import CallGraph
from dekko.core.resolver import resolve
from dekko.repo_ops import map_repository

_KT = "src/main/kotlin"
_JAVA = "src/main/java"


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


def _imports(graph: CallGraph, importer: str) -> list[str]:
    return graph.modules.deps_out.get(importer, [])


_POINT = """\
package geo

class Point(val x: Int, val y: Int = 0) {
    constructor(s: String) : this(s.length)
}
"""


def _kotlin_user(body: str, imports: str = "import geo.Point") -> str:
    return f"package app\n\n{imports}\n\nfun build() {{\n    {body}\n}}\n"


_P = f"{_KT}/geo/Point.kt::Point"
_BUILD = f"{_KT}/app/Build.kt::build"


def test_construction_credits_the_primary_constructor(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        {
            f"{_KT}/geo/Point.kt": _POINT,
            f"{_KT}/app/Build.kt": _kotlin_user("Point(1, 2)"),
        },
    )
    assert _callees(graph, _BUILD) == {_P, f"{_P}.Point"}


def test_exact_count_beats_a_fit_through_defaults(tmp_path: Path) -> None:
    # One argument fits the primary constructor (``y`` has a default)
    # and the one-parameter secondary one. Kotlin, like the resolver,
    # prefers the candidate that needs no defaults.
    graph = _graph(
        tmp_path,
        {
            f"{_KT}/geo/Point.kt": _POINT,
            f"{_KT}/app/Build.kt": _kotlin_user('Point("xy")'),
        },
    )
    assert _callees(graph, _BUILD) == {_P, f"{_P}.Point#2"}


_JAVA_APP = """\
package boot;

public class SpringApplication {
    public static void run(Object source, String... args) {}
}
"""

_EXTENSIONS = """\
package boot

fun runApplication(vararg args: String) {
    SpringApplication.run(Any::class.java, *args)
}
"""


def test_kotlin_calls_java_through_its_import(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        {
            f"{_JAVA}/boot/SpringApplication.java": _JAVA_APP,
            f"{_KT}/boot/Extensions.kt": _EXTENSIONS,
            f"{_KT}/app/Build.kt": _kotlin_user(
                "SpringApplication.run(this)",
                imports="import boot.SpringApplication",
            ),
        },
    )
    run = f"{_JAVA}/boot/SpringApplication.java::SpringApplication.run"
    assert run in _callees(graph, _BUILD)
    assert run in _callees(graph, f"{_KT}/boot/Extensions.kt::runApplication")
    assert _imports(graph, f"{_KT}/app/Build.kt") == [
        f"{_JAVA}/boot/SpringApplication.java",
    ]


def test_top_level_function_import_resolves(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        {
            f"{_JAVA}/boot/SpringApplication.java": _JAVA_APP,
            f"{_KT}/boot/Extensions.kt": _EXTENSIONS,
            f"{_KT}/other/Run.kt": (
                "package other\n\nfun runApplication(n: Int) {}\n"
            ),
            f"{_KT}/app/Build.kt": _kotlin_user(
                "runApplication()",
                imports="import boot.runApplication",
            ),
        },
    )
    assert _callees(graph, _BUILD) == {
        f"{_KT}/boot/Extensions.kt::runApplication",
    }
    assert _imports(graph, f"{_KT}/app/Build.kt") == [
        f"{_KT}/boot/Extensions.kt",
    ]


def test_twin_types_resolve_to_the_importers_own_language(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            f"{_JAVA}/docs/Props.java": (
                "package docs;\n\npublic class Props {\n"
                "    public void load() {}\n}\n"
            ),
            f"{_KT}/docs/Props.kt": (
                "package docs\n\nclass Props {\n    fun load() {}\n}\n"
            ),
            f"{_KT}/app/Build.kt": _kotlin_user(
                "Props().load()", imports="import docs.Props"
            ),
            f"{_JAVA}/app/Use.java": (
                "package app;\n\nimport docs.Props;\n\n"
                "public class Use {\n"
                "    void go() { new Props().load(); }\n}\n"
            ),
        },
    )
    assert _imports(graph, f"{_KT}/app/Build.kt") == [f"{_KT}/docs/Props.kt"]
    assert _imports(graph, f"{_JAVA}/app/Use.java") == [
        f"{_JAVA}/docs/Props.java",
    ]
    assert f"{_KT}/docs/Props.kt::Props" in _callees(graph, _BUILD)
    java_callees = _callees(graph, f"{_JAVA}/app/Use.java::Use.go")
    assert f"{_JAVA}/docs/Props.java::Props" in java_callees
    assert not any(c.startswith(f"{_KT}/") for c in java_callees)


def test_nested_type_import_resolves_to_the_outer_file(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        {
            f"{_JAVA}/web/Outer.java": (
                "package web;\n\npublic class Outer {\n"
                "    public static class Inner {}\n}\n"
            ),
            f"{_KT}/app/Build.kt": _kotlin_user(
                "Inner()", imports="import web.Outer.Inner"
            ),
        },
    )
    assert _imports(graph, f"{_KT}/app/Build.kt") == [
        f"{_JAVA}/web/Outer.java",
    ]


def test_java_calls_kotlin_when_only_kotlin_defines_it(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        {
            f"{_KT}/geo/Point.kt": _POINT,
            f"{_JAVA}/app/Use.java": (
                "package app;\n\nimport geo.Point;\n\n"
                "public class Use {\n"
                "    void go() { new Point(1, 2); }\n}\n"
            ),
        },
    )
    assert _imports(graph, f"{_JAVA}/app/Use.java") == [f"{_KT}/geo/Point.kt"]
    assert _callees(graph, f"{_JAVA}/app/Use.java::Use.go") == {
        _P,
        f"{_P}.Point",
    }


def test_kotlin_supertype_in_java_resolves(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        {
            f"{_JAVA}/base/Base.java": (
                "package base;\n\npublic class Base {}\n"
            ),
            f"{_KT}/app/Child.kt": (
                "package app\n\nimport base.Base\n\nclass Child : Base()\n"
            ),
        },
    )
    assert graph.heritage_out[f"{_KT}/app/Child.kt::Child"] == [
        f"{_JAVA}/base/Base.java::Base",
    ]
