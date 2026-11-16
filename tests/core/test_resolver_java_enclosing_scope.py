"""Java resolves a simple name in its enclosing classes first.

``new Node(..)`` inside a class that implements an interface declaring
a nested ``Node`` means that ``Node``, whatever other ``Node`` types
the repo has; a nested type's own name inside its own body means
itself. A bare ``m(..)`` means the methods called ``m`` of the nearest
enclosing class that has any, declared or inherited, by argument
count. A call inside an anonymous class body sits in a class whose
supertype dekko doesn't record, so neither rule judges it.
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


def test_each_class_calls_its_own_method(tmp_path: Path) -> None:
    # ``test`` is also a JS test global; a Java bare call that names an
    # enclosing class's method is that method, not noise.
    g = _graph(
        tmp_path,
        {
            "Source.java": """
                class Source {
                    private boolean test(Object value) { return true; }
                    void apply(Object v) { test(v); }
                    static class Always {
                        boolean test(Object value) { return true; }
                        void run(Object v) { test(v); }
                    }
                }
            """,
        },
    )
    assert _callees(g, "Source.java::Source.apply") == {
        "Source.java::Source.test"
    }
    assert _callees(g, "Source.java::Source.Always.run") == {
        "Source.java::Source.Always.test"
    }


def test_the_outer_class_method_beats_a_nested_namesake(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            "Metrics.java": """
                class Metrics {
                    Object getAvailableTags(Object meters) { return null; }
                    Object metric(Object meters) {
                        return getAvailableTags(meters);
                    }
                    static class Response {
                        Object getAvailableTags(Object tags) { return null; }
                    }
                }
            """,
        },
    )
    caller = "Metrics.java::Metrics.metric"
    assert _callees(g, caller) == {"Metrics.java::Metrics.getAvailableTags"}
    assert _ambiguous(g, caller) == {}


def test_an_own_overload_is_picked_by_count(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "Other.java": """
                class Other {
                    Object convert(Object a, String b) { return null; }
                }
            """,
            "ConvTests.java": """
                class ConvTests {
                    void check() { convert(null, "10"); }
                    Object convert(Object s, String v) { return null; }
                    Object convert(Object s, String v, Object u) {
                        return null;
                    }
                }
            """,
        },
    )
    caller = "ConvTests.java::ConvTests.check"
    assert _callees(g, caller) == {"ConvTests.java::ConvTests.convert"}
    assert _ambiguous(g, caller) == {}


def test_a_method_inherited_from_an_in_repo_superclass(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            "Base.java": """
                abstract class Base {
                    protected Object createEvent() { return null; }
                }
            """,
            "Other.java": (
                "class Other { Object createEvent() { return null; } }"
            ),
            "Json.java": """
                class Json extends Base {
                    void check() { createEvent(); }
                }
            """,
        },
    )
    assert _callees(g, "Json.java::Json.check") == {
        "Base.java::Base.createEvent"
    }


def test_an_interface_default_method_is_inherited(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "Details.java": """
                interface Details {
                    default Object getContainer() { return null; }
                }
            """,
            "Other.java": (
                "class Other { Object getContainer() { return null; } }"
            ),
            "Props.java": """
                class Props implements Details {
                    Object url() { return getContainer(); }
                }
            """,
        },
    )
    assert _callees(g, "Props.java::Props.url") == {
        "Details.java::Details.getContainer"
    }


def test_a_nested_class_calls_its_outer_classs_method(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            "Other.java": (
                "class Other { Object createDataSource(Object p) "
                "{ return null; } }"
            ),
            "Config.java": """
                class Config {
                    static Object createDataSource(Object p) { return null; }
                    static class Hikari {
                        Object dataSource(Object p) {
                            return createDataSource(p);
                        }
                    }
                    static class Dbcp {
                        Object createDataSource(Object p) { return null; }
                    }
                }
            """,
        },
    )
    assert _callees(g, "Config.java::Config.Hikari.dataSource") == {
        "Config.java::Config.createDataSource"
    }


def test_the_innermost_class_with_the_method_wins(tmp_path: Path) -> None:
    # ``Inner`` inherits ``compile`` from ``Base``: that shadows the
    # outer class's own ``compile``.
    g = _graph(
        tmp_path,
        {
            "Base.java": """
                class Base {
                    protected void compile(Object source) {}
                }
            """,
            "Gen.java": """
                class Gen {
                    void compile(Object source) {}
                    class Inner extends Base {
                        void check() { compile(null); }
                    }
                }
            """,
        },
    )
    assert _callees(g, "Gen.java::Gen.Inner.check") == {
        "Base.java::Base.compile"
    }


def test_a_private_superclass_method_in_another_file_is_not_inherited(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            "Base.java": "class Base { private void reset() {} }",
            "Other.java": "class Other { void reset() {} }",
            "Sub.java": """
                class Sub extends Base {
                    void check() { reset(); }
                }
            """,
        },
    )
    assert "Base.java::Base.reset" not in _callees(g, "Sub.java::Sub.check")


def test_a_private_superclass_method_in_the_same_file_is_reachable(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            "Other.java": "class Other { void reset() {} }",
            "Sub.java": """
                class Base { private void reset() {} }
                class Sub extends Base {
                    void check() { reset(); }
                }
                class Pool { void reset() {} }
            """,
        },
    )
    assert _callees(g, "Sub.java::Sub.check") == {"Sub.java::Base.reset"}


def test_two_count_fits_make_a_row_of_just_those_two(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "Other.java": "class Other { void bind(Object a) {} }",
            "Binder.java": """
                class Binder {
                    void bind(String a) {}
                    void bind(Integer a) {}
                    void bind(String a, String b) {}
                    void check(Object x) { bind(x); }
                }
            """,
        },
    )
    caller = "Binder.java::Binder.check"
    assert _callees(g, caller) == set()
    assert _ambiguous(g, caller) == {
        "bind": {"Binder.java::Binder.bind", "Binder.java::Binder.bind#2"}
    }


def test_an_outside_supertype_stops_before_an_outer_method(
    tmp_path: Path,
) -> None:
    # ``AbstractList`` may declare ``refresh`` itself: the ladder
    # decides, as it did before the rule.
    g = _graph(
        tmp_path,
        {
            "Outer.java": """
                import java.util.AbstractList;
                class Outer {
                    void refresh() {}
                    abstract static class Task extends AbstractList<String> {
                        void check() { refresh(); }
                    }
                }
                class Side {
                    void refresh() {}
                }
            """,
        },
    )
    caller = "Outer.java::Outer.Task.check"
    assert _callees(g, caller) == set()
    assert _ambiguous(g, caller) == {
        "refresh": {"Outer.java::Outer.refresh", "Outer.java::Side.refresh"}
    }


def test_an_own_fit_on_an_outside_supertype_is_taken(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "Side.java": "class Side { void refresh() {} }",
            "Ctx.java": """
                import java.util.AbstractList;
                abstract class Ctx extends AbstractList<String> {
                    void refresh() {}
                    void start() { refresh(); }
                    static class Two { void refresh() {} }
                }
            """,
        },
    )
    assert _callees(g, "Ctx.java::Ctx.start") == {"Ctx.java::Ctx.refresh"}


def test_a_self_fit_on_an_outside_supertype_is_left_to_the_ladder(
    tmp_path: Path,
) -> None:
    # ``write(buf)`` inside ``write(int)`` means ``OutputStream``'s
    # ``write(byte[])``, which dekko can't see.
    g = _graph(
        tmp_path,
        {
            "Side.java": "class Side { void write(byte[] b) {} }",
            "Out.java": """
                import java.io.OutputStream;
                class Out extends OutputStream {
                    private final byte[] buf = new byte[1];
                    public void write(int b) { write(this.buf); }
                }
            """,
        },
    )
    caller = "Out.java::Out.write"
    assert _callees(g, caller) == set()
    assert _ambiguous(g, caller) == {
        "write": {"Out.java::Out.write", "Side.java::Side.write"}
    }


def test_plain_recursion_on_a_closed_class_is_itself(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "Side.java": "class Side { int depth(int n) { return n; } }",
            "Tree.java": """
                class Tree {
                    int depth(int n) { return n == 0 ? 0 : depth(n - 1); }
                }
            """,
        },
    )
    caller = "Tree.java::Tree.depth"
    assert _callees(g, caller) == set()
    assert _ambiguous(g, caller) == {}


def test_implicit_members_are_left_to_the_ladder(tmp_path: Path) -> None:
    # ``equals`` is every class's (``Object``), ``values`` every enum's,
    # and a zero-argument call in a record may be a component accessor:
    # none of them is a symbol dekko has, so the search stops there.
    g = _graph(
        tmp_path,
        {
            "Outer.java": """
                class Outer {
                    public boolean equals(Object o) { return true; }
                    static Object values() { return null; }
                    int major() { return 1; }
                    static class Plain {
                        void check(Object o) { equals(o); }
                    }
                    enum Kind {
                        A;
                        void check() { values(); }
                    }
                    record Version(int major) {
                        int check() { return major(); }
                    }
                }
                class Side {
                    public boolean equals(Object o) { return true; }
                    static Object values() { return null; }
                    int major() { return 1; }
                }
            """,
        },
    )
    assert _callees(g, "Outer.java::Outer.Plain.check") == set()
    assert _callees(g, "Outer.java::Outer.Kind.check") == set()
    assert _callees(g, "Outer.java::Outer.Version.check") == set()


def test_a_bare_call_in_an_anonymous_class_is_left_alone(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            "Use.java": """
                class Use {
                    void refresh() {}
                    Runnable make() {
                        return new Runnable() {
                            public void run() { refresh(); }
                        };
                    }
                }
                class Side {
                    void refresh() {}
                }
            """,
        },
    )
    rows = [
        set(ids)
        for c, name, ids in g.ambiguous
        if c.startswith("Use.java::") and name == "refresh"
    ]
    assert rows == [{"Use.java::Use.refresh", "Use.java::Side.refresh"}]


def test_a_kotlin_bare_call_is_unchanged(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "Side.kt": "class Side { fun refresh() {} }\n",
            "Outer.kt": (
                "class Outer {\n"
                "    fun refresh() {}\n"
                "    class Task {\n"
                "        fun check() { refresh() }\n"
                "    }\n"
                "    class Two { fun refresh() {} }\n"
                "}\n"
            ),
        },
    )
    assert _callees(g, "Outer.kt::Outer.Task.check") == set()


def _external(graph: CallGraph, caller: str) -> set[str]:
    return {e.callee for e in graph.external if e.caller == caller}


# A JVM source root, so a static import reads as an in-repo path.
SRC = "src/main/java/"
CONTRIBUTION = {
    "Contribution.java": """
        class Contribution {
            Object getProject() { return null; }
        }
    """,
}


def test_a_pick_no_enclosing_class_has_is_external(tmp_path: Path) -> None:
    # ``getProject`` is ``DefaultTask``'s; the repo's one ``getProject``
    # belongs to a class a bare call here can't name.
    g = _graph(
        tmp_path,
        {
            **CONTRIBUTION,
            "Playbook.java": """
                import org.gradle.api.DefaultTask;
                abstract class Playbook extends DefaultTask {
                    void write() { getProject(); }
                }
            """,
        },
    )
    caller = "Playbook.java::Playbook.write"
    assert _callees(g, caller) == set()
    assert _external(g, caller) == {"getProject"}


def test_a_records_own_accessor_is_external(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "Library.java": "class Library { int minor() { return 1; } }",
            "ComposeVersion.java": """
                record ComposeVersion(int major, int minor) {
                    boolean isLessThan(int m) { return minor() < m; }
                }
            """,
        },
    )
    caller = "ComposeVersion.java::ComposeVersion.isLessThan"
    assert _callees(g, caller) == set()
    assert _external(g, caller) == {"minor"}


def test_a_statically_imported_pick_is_kept(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            f"{SRC}p/Util.java": (
                "package p;\npublic class Util "
                "{ public static Object getProject() { return null; } }\n"
            ),
            f"{SRC}r/Playbook.java": """
                package r;
                import static p.Util.getProject;
                import org.gradle.api.DefaultTask;
                abstract class Playbook extends DefaultTask {
                    void write() { getProject(); }
                }
            """,
        },
    )
    assert _callees(g, f"{SRC}r/Playbook.java::Playbook.write") == {
        f"{SRC}p/Util.java::Util.getProject"
    }


def test_a_pick_through_an_on_demand_static_import_is_kept(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            f"{SRC}p/Util.java": (
                "package p;\npublic class Util "
                "{ public static Object getProject() { return null; } }\n"
            ),
            f"{SRC}r/Playbook.java": """
                package r;
                import static p.Util.*;
                import org.gradle.api.DefaultTask;
                abstract class Playbook extends DefaultTask {
                    void write() { getProject(); }
                }
            """,
        },
    )
    assert _callees(g, f"{SRC}r/Playbook.java::Playbook.write") == {
        f"{SRC}p/Util.java::Util.getProject"
    }


def test_an_outer_method_past_an_outside_supertype_is_kept(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            "Outer.java": """
                import java.util.AbstractList;
                class Outer {
                    Object getProject() { return null; }
                    abstract static class Task extends AbstractList<String> {
                        void check() { getProject(); }
                    }
                }
            """,
        },
    )
    assert _callees(g, "Outer.java::Outer.Task.check") == {
        "Outer.java::Outer.getProject"
    }


def test_a_pick_in_an_anonymous_class_is_not_judged(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            **CONTRIBUTION,
            "Use.java": """
                class Use {
                    Runnable make() {
                        return new Runnable() {
                            public void run() { getProject(); }
                        };
                    }
                }
            """,
        },
    )
    targets = {e.callee for e in g.edges if e.caller.startswith("Use.java")}
    assert targets == {"Contribution.java::Contribution.getProject"}


def test_an_unread_in_repo_supertype_name_abstains(tmp_path: Path) -> None:
    # ``KMid`` is the repo's Kotlin class, which the scope walk doesn't
    # read, and ``JBase.reset`` is inherited through it: the ladder's
    # pick stays.
    g = _graph(
        tmp_path,
        {
            f"{SRC}r/JBase.java": (
                "package r;\npublic class JBase "
                "{ protected void reset() {} }\n"
            ),
            f"{SRC}r/KMid.kt": "package r\nopen class KMid : JBase()\n",
            f"{SRC}r/Sub.java": """
                package r;
                class Sub extends KMid {
                    void check() { reset(); }
                }
            """,
        },
    )
    assert _callees(g, f"{SRC}r/Sub.java::Sub.check") == {
        f"{SRC}r/JBase.java::JBase.reset"
    }
