"""A JVM call the receiver walk typed keeps its common method name.

``this.gradleBuild.build("x")`` on a ``GradleBuild`` field names
``GradleBuild``'s own ``build``: the walk read the receiver's type and
narrowed the call to that type's members. The guard that sends a
common name such as ``build`` or ``parse`` external exists for picks
made on the name alone, so it no longer overrules a typed narrowing.
An untyped receiver still meets the guard.
"""

import textwrap
from pathlib import Path

from dekko.core.model import CallGraph
from dekko.core.resolver import resolve
from dekko.repo_ops import map_repository

_JAVA = "src/main/java/com/acme"

_BUILDER = f"{_JAVA}/build/GradleBuild.java"


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


def _external(graph: CallGraph, caller: str) -> set[str]:
    return {e.callee for e in graph.external if e.caller == caller}


def _ambiguous(graph: CallGraph, caller: str) -> dict[str, set[str]]:
    return {name: set(ids) for c, name, ids in graph.ambiguous if c == caller}


def _builder(body: str) -> dict[str, str]:
    return {
        _BUILDER: f"""
            package com.acme.build;

            public class GradleBuild {{
            {textwrap.indent(textwrap.dedent(body), "    ")}
            }}
        """,
    }


def _user(body: str) -> dict[str, str]:
    rel = f"{_JAVA}/build/Runner.java"
    return {
        rel: f"""
            package com.acme.build;

            class Runner {{
                private final GradleBuild gradleBuild = new GradleBuild();

            {textwrap.indent(textwrap.dedent(body), "    ")}
            }}
        """,
    }


_RUNNER = f"{_JAVA}/build/Runner.java::Runner"


def test_typed_field_overloads_keep_a_guarded_name(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            **_builder(
                """
                public String build() { return ""; }
                public String build(String task) { return task; }
                """
            ),
            **_user(
                """
                String run() {
                    return this.gradleBuild.build("bootJar");
                }
                """
            ),
        },
    )
    assert _callees(g, f"{_RUNNER}.run") == {
        f"{_BUILDER}::GradleBuild.build#2"
    }
    assert "build" not in _ambiguous(g, f"{_RUNNER}.run")


def test_typed_field_tie_names_only_the_type_members(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            **_builder(
                """
                public String build(String task) { return task; }
                public String build(Integer count) { return ""; }
                """
            ),
            f"{_JAVA}/other/Other.java": """
                package com.acme.other;

                public class Other {
                    public String build(String task) { return task; }
                }
            """,
            **_user(
                """
                String run(Object task) {
                    return this.gradleBuild.build(task);
                }
                """
            ),
        },
    )
    assert _callees(g, f"{_RUNNER}.run") == set()
    assert _ambiguous(g, f"{_RUNNER}.run")["build"] == {
        f"{_BUILDER}::GradleBuild.build",
        f"{_BUILDER}::GradleBuild.build#2",
    }


def test_untyped_receiver_still_meets_the_guard(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            **_builder(
                """
                public String build() { return ""; }
                public String build(String task) { return task; }
                """
            ),
            **_user(
                """
                String run() {
                    var other = make();
                    return other.build("bootJar");
                }

                Object make() { return null; }
                """
            ),
        },
    )
    assert _callees(g, f"{_RUNNER}.run") == {f"{_RUNNER}.make"}
    assert "build" not in _ambiguous(g, f"{_RUNNER}.run")
    assert any("build" in name for name in _external(g, f"{_RUNNER}.run"))
