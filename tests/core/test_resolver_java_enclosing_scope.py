"""Java resolves a simple name in its enclosing classes first.

``new Node(..)`` inside a class that implements an interface declaring
a nested ``Node`` means that ``Node``, whatever other ``Node`` types
the repo has; a nested type's own name inside its own body means
itself. A call inside an anonymous class body sits in a class whose
supertype dekko doesn't record, so the rule leaves it alone.
"""

import textwrap
from pathlib import Path

from dekko.core.extractor import extract_file
from dekko.core.languages import spec_for_path
from dekko.core.model import CallGraph
from dekko.core.resolver import resolve
from dekko.repo_ops import map_repository

NODES = {
    "Bom.java": "class Bom { static class Node { Node(String h, int p) {} } }",
    "Layers.java": "class Layers { record Node(String h, int p) {} }",
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


def _referenced(graph: CallGraph, caller: str) -> set[str]:
    return {e.callee for e in graph.referenced if e.caller == caller}


def _anonymous_flags(root: Path, source: str) -> dict[str, bool]:
    rel = "Use.java"
    (root / rel).write_text(textwrap.dedent(source))
    fm = extract_file(root, rel, spec_for_path(rel))
    flags = {c.name: c.anonymous for c in fm.calls}
    flags.update({f"ref:{r.name}": r.anonymous for r in fm.refs})
    return flags


def test_an_inherited_member_type_is_constructed(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            **NODES,
            "Details.java": """
                interface Details {
                    record Node(String host, int port) {}
                }
            """,
            "Props.java": """
                class Props implements Details {
                    Object asNode() { return new Node("h", 1); }
                }
            """,
        },
    )
    assert _callees(g, "Props.java::Props.asNode") == {
        "Details.java::Details.Node",
        "Details.java::Details.Node.Node",
    }
    assert _ambiguous(g, "Props.java::Props.asNode") == {}


def test_a_types_own_name_in_its_field_initializer(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "q/GitHub.java": (
                "package q;\nclass GitHub { GitHub(String o) {} }\n"
            ),
            "Bom.java": (
                "class Bom { static class GitHub { GitHub(String o) {} } }"
            ),
            "Build.java": """
                class Build {
                    record GitHub(String owner) {
                        static final GitHub OPEN = new GitHub("x");
                    }
                }
            """,
        },
    )
    caller = "Build.java::Build.GitHub"
    assert _callees(g, caller) == {"Build.java::Build.GitHub.GitHub"}
    assert _ambiguous(g, caller) == {}


def test_a_sibling_member_type(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "Other.java": "class Other { static class Session {} }",
            "Top.java": "class Session {}",
            "Jms.java": """
                class Jms {
                    static class Listener {
                        Object make() { return new Session(); }
                    }
                    static class Session {}
                }
                class Pool {
                    static class Session {}
                }
            """,
        },
    )
    assert _callees(g, "Jms.java::Jms.Listener.make") == {
        "Jms.java::Jms.Session"
    }


def test_the_innermost_class_wins(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "Tests.java": """
                class First {
                    static class Nested {}
                    void a() { new Nested(); }
                }
                class Second {
                    static class Nested {}
                    void b() { new Nested(); }
                }
            """,
        },
    )
    assert _callees(g, "Tests.java::First.a") == {"Tests.java::First.Nested"}
    assert _callees(g, "Tests.java::Second.b") == {"Tests.java::Second.Nested"}


def test_a_constructor_reference_to_the_own_nested_type(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            **NODES,
            "Holder.java": """
                import java.util.function.Function;
                class Holder {
                    static class Node { Node(String s) {} }
                    Function<String, Node> f() { return Node::new; }
                }
                class Side {
                    static class Node { Node(String s) {} }
                }
            """,
        },
    )
    assert _referenced(g, "Holder.java::Holder.f") == {
        "Holder.java::Holder.Node",
        "Holder.java::Holder.Node.Node",
    }


def test_an_inherited_member_type_beats_a_single_type_import(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            "other/Node.java": (
                "package other;\npublic class Node { Node(String h) {} }\n"
            ),
            "Details.java": """
                interface Details {
                    record Node(String host) {}
                }
            """,
            "Props.java": """
                import other.Node;
                class Props implements Details {
                    Object asNode() { return new Node("h"); }
                }
            """,
        },
    )
    assert "Details.java::Details.Node" in _callees(
        g, "Props.java::Props.asNode"
    )
    assert "other/Node.java::Node" not in _callees(
        g, "Props.java::Props.asNode"
    )


def test_a_written_nested_path_keeps_its_path(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "Other.java": "class Other { static class Options {} }",
            "Use.java": """
                class Use {
                    static class Options {}
                    Object make() { return new Other.Options(); }
                }
            """,
        },
    )
    assert _callees(g, "Use.java::Use.make") == {"Other.java::Other.Options"}


def test_an_outside_supertype_leaves_the_tie(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            **NODES,
            "Task.java": """
                import java.util.AbstractList;
                class Task extends AbstractList<String> {
                    Object make() { return new Node("h", 1); }
                }
            """,
        },
    )
    assert _callees(g, "Task.java::Task.make") == set()
    assert _ambiguous(g, "Task.java::Task.make") == {
        "Node": {
            "Bom.java::Bom.Node",
            "Bom.java::Bom.Node.Node",
            "Layers.java::Layers.Node",
            "Layers.java::Layers.Node.Node",
        }
    }


def test_an_outside_supertype_stops_before_an_outer_member_type(
    tmp_path: Path,
) -> None:
    # ``AbstractList`` could declare a ``Node`` that shadows the outer
    # class's; dekko can't see it, so the ladder decides.
    g = _graph(
        tmp_path,
        {
            "Outer.java": """
                import java.util.AbstractList;
                class Outer {
                    static class Node {}
                    abstract static class Task extends AbstractList<String> {
                        Object make() { return new Node(); }
                    }
                }
                class Side {
                    static class Node {}
                }
            """,
        },
    )
    caller = "Outer.java::Outer.Task.make"
    assert _callees(g, caller) == set()
    assert _ambiguous(g, caller) == {
        "Node": {"Outer.java::Outer.Node", "Outer.java::Side.Node"}
    }


def test_a_construction_in_an_anonymous_class_is_left_alone(
    tmp_path: Path,
) -> None:
    # Outside the anonymous body ``Use.Node`` would win; inside it, the
    # anonymous class's own supertype could declare a ``Node``.
    g = _graph(
        tmp_path,
        {
            "Use.java": """
                class Use {
                    static class Node {}
                    Runnable make() {
                        return new Runnable() {
                            public void run() { new Node(); }
                        };
                    }
                }
                class Side {
                    static class Node {}
                }
            """,
        },
    )
    rows = [
        set(ids)
        for c, name, ids in g.ambiguous
        if c.startswith("Use.java::") and name == "Node"
    ]
    assert rows == [{"Use.java::Use.Node", "Use.java::Side.Node"}]


def test_kotlin_is_unchanged(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "Bom.kt": "class Bom { class Node(val h: String) }\n",
            "Details.kt": "interface Details { class Node(val h: String) }\n",
            "Props.kt": (
                "class Props : Details {\n"
                '    fun asNode(): Any = Node("h")\n'
                "}\n"
            ),
        },
    )
    assert _callees(g, "Props.kt::Props.asNode") == set()
    assert _ambiguous(g, "Props.kt::Props.asNode") == {
        "Node": {
            "Bom.kt::Bom.Node",
            "Bom.kt::Bom.Node.Node",
            "Details.kt::Details.Node",
            "Details.kt::Details.Node.Node",
        }
    }


def test_calls_inside_an_anonymous_class_body_are_flagged(
    tmp_path: Path,
) -> None:
    flags = _anonymous_flags(
        tmp_path,
        """
        import java.util.function.Supplier;
        class Use {
            void outer() {
                Runnable r = new Runnable() {
                    public void run() { inBody(); }
                };
                Supplier<Object> s = Thing::new;
                plain();
            }
        }
        """,
    )
    assert flags["inBody"] is True
    assert flags["Runnable"] is False
    assert flags["plain"] is False
    assert flags["ref:Thing"] is False


def test_an_enum_constant_body_is_anonymous(tmp_path: Path) -> None:
    flags = _anonymous_flags(
        tmp_path,
        """
        enum Mode {
            FAST { void go() { inConstant(); } };
            void go() { inEnum(); }
        }
        """,
    )
    assert flags["inConstant"] is True
    assert flags["inEnum"] is False


def test_a_local_or_lambda_body_is_not_anonymous(tmp_path: Path) -> None:
    flags = _anonymous_flags(
        tmp_path,
        """
        class Use {
            void outer() {
                class Local { void f() { inLocal(); } }
                Runnable r = () -> inLambda();
            }
        }
        """,
    )
    assert flags["inLocal"] is False
    assert flags["inLambda"] is False


def test_a_class_declared_inside_an_anonymous_body_is_anonymous(
    tmp_path: Path,
) -> None:
    # Its scope chain runs through the anonymous class, whose
    # supertype dekko doesn't record.
    flags = _anonymous_flags(
        tmp_path,
        """
        class Use {
            Runnable a = new Runnable() {
                public void run() {
                    class Deeper { void g() { inDeeper(); } }
                }
            };
        }
        """,
    )
    assert flags["inDeeper"] is True
