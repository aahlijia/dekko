"""How the receiver walk reads a JVM type name.

A Java file names a type through its single-type import, else its own
package, so ``Status status`` in a file importing
``com.acme.health.Status`` is that ``Status``, not a nearer nested
``GrpcProperties.Status``. A type the file declares still shadows an
import of the same simple name, and one fully qualified name present in
several modules reads as the nearest copy. ``Outer.Inner<T>`` is read
as the nested path, not as ``Outer``.
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


_HEALTH = "health/src/main/java/com/acme/health/Status.java"
_GRPC = "grpc/src/main/java/com/acme/grpc"


def test_an_imported_type_wins_over_a_nearer_nested_one(
    tmp_path: Path,
) -> None:
    mapper = f"{_GRPC}/Mapper.java"
    g = _graph(
        tmp_path,
        {
            _HEALTH: """
                package com.acme.health;

                public class Status {
                    public String getCode() { return ""; }
                }
            """,
            f"{_GRPC}/GrpcProperties.java": """
                package com.acme.grpc;

                public class GrpcProperties {
                    public static class Status {
                        public boolean isEnabled() { return true; }
                    }
                }
            """,
            mapper: """
                package com.acme.grpc;

                import com.acme.health.Status;

                class Mapper {
                    private final Status status = null;

                    String code() {
                        return this.status.getCode();
                    }
                }
            """,
        },
    )
    assert _callees(g, f"{mapper}::Mapper.code") == {
        f"{_HEALTH}::Status.getCode"
    }


def test_a_type_declared_in_the_file_shadows_an_import(
    tmp_path: Path,
) -> None:
    props = "core/src/main/java/com/acme/web/ServerProperties.java"
    g = _graph(
        tmp_path,
        {
            "core/src/main/java/com/acme/servlet/Session.java": """
                package com.acme.servlet;

                public class Session {
                    public int getTimeout() { return 0; }
                }
            """,
            props: """
                package com.acme.web;

                import com.acme.servlet.Session;

                public class ServerProperties {
                    private final Session session = null;

                    int timeout() {
                        return this.session.getTimeout();
                    }

                    public static class Session {
                        public int getTimeout() { return 1; }
                    }
                }
            """,
        },
    )
    assert _callees(g, f"{props}::ServerProperties.timeout") == {
        f"{props}::ServerProperties.Session.getTimeout"
    }


def test_one_imported_name_in_two_modules_reads_the_nearer_copy(
    tmp_path: Path,
) -> None:
    app = "smoke-b/src/main/java/com/acme/app/App.java"
    city = """
        package com.acme.data;

        public class City {
            public String getName() { return ""; }
        }
    """
    near = "smoke-b/src/main/java/com/acme/data/City.java"
    g = _graph(
        tmp_path,
        {
            "smoke-a/src/main/java/com/acme/data/City.java": city,
            near: city,
            app: """
                package com.acme.app;

                import com.acme.data.City;

                class App {
                    private final City city = null;

                    String name() {
                        return this.city.getName();
                    }
                }
            """,
        },
    )
    assert _callees(g, f"{app}::App.name") == {f"{near}::City.getName"}


def test_without_an_import_the_own_package_wins_over_nearer_types(
    tmp_path: Path,
) -> None:
    user = "b/src/main/java/com/acme/a/User.java"
    own = "a/src/main/java/com/acme/a/Widget.java"
    g = _graph(
        tmp_path,
        {
            own: """
                package com.acme.a;

                public class Widget {
                    public void spin() {}
                }
            """,
            "b/src/main/java/com/acme/b/Widget.java": """
                package com.acme.b;

                public class Widget {
                    public void spin() {}
                }
            """,
            user: """
                package com.acme.a;

                class User {
                    private final Widget widget = null;

                    void use() {
                        this.widget.spin();
                    }
                }
            """,
        },
    )
    assert _callees(g, f"{user}::User.use") == {f"{own}::Widget.spin"}


def test_a_nested_type_with_type_arguments_reads_the_nested_path(
    tmp_path: Path,
) -> None:
    writer = "core/src/main/java/com/acme/json/JsonWriter.java"
    user = "core/src/main/java/com/acme/json/Logs.java"
    g = _graph(
        tmp_path,
        {
            writer: """
                package com.acme.json;

                public class JsonWriter {
                    public void write(String s) {}

                    public static class Members<T> {
                        public Members<T> add(String name) { return this; }
                    }
                }
            """,
            "core/src/main/java/com/acme/other/Bag.java": """
                package com.acme.other;

                public class Bag {
                    public void add(String name) {}
                }
            """,
            user: """
                package com.acme.json;

                class Logs {
                    private final JsonWriter.Members<String> members = null;

                    void fields() {
                        this.members.add("level");
                    }
                }
            """,
        },
    )
    assert _callees(g, f"{user}::Logs.fields") == {
        f"{writer}::JsonWriter.Members.add"
    }
