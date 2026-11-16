"""A JVM type written as a nested path is read as that path.

``Health.Builder builder`` names the nested ``Builder``, not ``Health``;
``LoaderZipEntries.@Nullable WrittenEntries`` names ``WrittenEntries``
past its type-use annotation; ``class Options extends Api.Options``
inherits from ``Api.Options``, not from itself. Every fixture has an
unrelated same-named method elsewhere, so no result comes from the
name being unique in the repo.
"""

import textwrap
from pathlib import Path

from dekko.core.model import CallGraph
from dekko.core.resolver import resolve
from dekko.repo_ops import map_repository

OTHER = {"Other.java": "class Other { void go() {} void down() {} }\n"}
OUTER = {
    "Outer.java": """
        class Outer {
            static void go() {}
            static class Inner {
                void go() {}
                void down() {}
                void down(Exception ex) {}
            }
        }
    """,
}


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


def test_annotated_nested_field_reaches_the_nested_type(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            **OUTER,
            **OTHER,
            "Use.java": """
                class Use {
                    private Outer.@Nullable Inner f;
                    void viaThis() { this.f.go(); }
                    void bare() { f.go(); }
                }
            """,
        },
    )
    assert _callees(g, "Use.java::Use.viaThis") == {
        "Outer.java::Outer.Inner.go"
    }
    assert _callees(g, "Use.java::Use.bare") == {"Outer.java::Outer.Inner.go"}


def test_annotation_with_arguments_is_dropped(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            **OUTER,
            **OTHER,
            "Use.java": """
                class Use {
                    private Outer.@Size(max = 3) Inner f;
                    void run() { this.f.go(); }
                }
            """,
        },
    )
    assert _callees(g, "Use.java::Use.run") == {"Outer.java::Outer.Inner.go"}


def test_package_qualified_annotated_field(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            **OTHER,
            "p/q/Inner.java": "package p.q;\nclass Inner { void go() {} }\n",
            "Use.java": """
                class Use {
                    private p.q.@Nullable Inner f;
                    void run() { this.f.go(); }
                }
            """,
        },
    )
    assert _callees(g, "Use.java::Use.run") == {"p/q/Inner.java::Inner.go"}


def test_annotated_nested_return_type(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            **OUTER,
            **OTHER,
            "Use.java": """
                class Use {
                    Outer.@Nullable Inner make() { return null; }
                    void run() { make().go(); }
                }
            """,
        },
    )
    assert _callees(g, "Use.java::Use.run") == {
        "Outer.java::Outer.Inner.go",
        "Use.java::Use.make",
    }


def test_nested_path_parameter_beats_the_outer_types_method(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            **OUTER,
            **OTHER,
            "Use.java": """
                class Use {
                    void plain(Outer.Inner p) { p.go(); }
                    void annotated(Outer.@Nullable Inner p) { p.go(); }
                }
            """,
        },
    )
    assert _callees(g, "Use.java::Use.plain") == {"Outer.java::Outer.Inner.go"}
    assert _callees(g, "Use.java::Use.annotated") == {
        "Outer.java::Outer.Inner.go"
    }


def test_nested_path_parameter_no_repo_type_has_is_external(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            "Other.java": "class Other { void setLoadOnStartup(int n) {} }\n",
            "Use.java": """
                class Use {
                    void setLoadOnStartup(int n) {}
                    void run(Servlet.Dynamic r) { r.setLoadOnStartup(1); }
                }
            """,
        },
    )
    assert _callees(g, "Use.java::Use.run") == set()
    assert "run" not in _ambiguous(g, "Use.java::Use.run")
    assert _external(g, "Use.java::Use.run") == {"r.setLoadOnStartup"}


def test_nested_path_parameter_reaches_an_inherited_method(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            **OTHER,
            "Base.java": "class Base { void go() {} }\n",
            "Outer.java": """
                class Outer {
                    static void go() {}
                    static class Inner extends Base {}
                }
            """,
            "Use.java": "class Use { void run(Outer.Inner p) { p.go(); } }\n",
        },
    )
    assert _callees(g, "Use.java::Use.run") == {"Base.java::Base.go"}


def test_overloads_under_a_nested_path_parameter_pick_by_count(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            **OUTER,
            **OTHER,
            "Use.java": """
                class Use {
                    void none(Outer.Inner p) { p.down(); }
                    void one(Outer.Inner p, Exception ex) { p.down(ex); }
                }
            """,
        },
    )
    assert _callees(g, "Use.java::Use.none") == {
        "Outer.java::Outer.Inner.down"
    }
    assert _callees(g, "Use.java::Use.one") == {
        "Outer.java::Outer.Inner.down#2"
    }


def test_nested_path_supertype_is_read_by_its_path(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "Other.java": "class Other { int getNumThreads() { return 0; } }",
            "Api.java": """
                class Api {
                    static class Options {
                        int getNumThreads() { return 1; }
                    }
                }
            """,
            "Impl.java": """
                class Impl {
                    static class Options extends Api.Options {}
                }
            """,
            "Use.java": """
                class Use {
                    void run(Impl.Options o) { o.getNumThreads(); }
                }
            """,
        },
    )
    assert _callees(g, "Use.java::Use.run") == {
        "Api.java::Api.Options.getNumThreads"
    }


def test_field_typed_by_a_subtype_of_a_same_named_nested_type(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            "Other.java": "class Other { void clear() {} }\n",
            "Session.java": """
                class Session {
                    static class NativeReference { void clear() {} }
                }
            """,
            "Holder.java": """
                class Holder {
                    static class NativeReference
                            extends Session.NativeReference {}
                }
            """,
            "Use.java": """
                class Use {
                    private Holder.NativeReference ref;
                    void run() { this.ref.clear(); }
                }
            """,
        },
    )
    assert _callees(g, "Use.java::Use.run") == {
        "Session.java::Session.NativeReference.clear"
    }


def test_kotlin_nested_path_parameter(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "Other.kt": "class Other { fun withDetail() {} }\n",
            "Info.kt": """
                class Info {
                    fun withDetail() {}
                    class Builder { fun withDetail() {} }
                }
            """,
            "Use.kt": "fun f(b: Info.Builder) { b.withDetail() }\n",
        },
    )
    assert _callees(g, "Use.kt::f") == {"Info.kt::Info.Builder.withDetail"}


def test_annotation_inside_type_arguments_keeps_the_outer_type(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            "Foo.java": "class Foo { void get(String k) {} }\n",
            "Use.java": """
                import java.util.Map;
                class Use {
                    private Map<String, @Nullable Foo> m;
                    void run() { this.m.get("k"); }
                }
            """,
        },
    )
    assert _callees(g, "Use.java::Use.run") == set()
    assert _external(g, "Use.java::Use.run") == {"this.m.get"}


def test_typescript_namespace_parameter_is_unchanged(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "ns.ts": (
                "export namespace NS { export class Thing { go() {} } }\n"
                "export class Other { go() {} }\n"
            ),
            "use.ts": (
                "import { NS } from './ns';\n"
                "export function run(p: NS.Thing) { p.go(); }\n"
            ),
        },
    )
    assert _callees(g, "use.ts::run") == set()
    assert _ambiguous(g, "use.ts::run") == {
        "go": {"ns.ts::Thing.go", "ns.ts::Other.go"}
    }
