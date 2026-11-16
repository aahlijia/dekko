"""A JVM call on a parameter declared with an in-repo type is walked.

``server.setPort(9000)`` on a parameter ``ConfigurableReactiveWebServer
Factory server`` can only call a ``setPort`` that type declares or
inherits. The typed-parameter rung read only the type's own name, so it
missed a method declared on an in-repo superinterface and never said
external for one the type's outside supertypes hold; the ladder then
tied every ``setPort`` in the repo. The parameter's declared type now
decides, as a local's or a field's does.
"""

import textwrap
from pathlib import Path

from dekko.core.model import CallGraph
from dekko.core.resolver import resolve
from dekko.repo_ops import map_repository

_JAVA = "src/main/java/com/acme"

_DECOYS = {
    f"{_JAVA}/a/Probe.java": """
        package com.acme.a;

        public class Probe {
            public void setPort(int port) {}
            public Object getBean(String name) { return null; }
            public String getStatus() { return ""; }
        }
    """,
    f"{_JAVA}/b/Reply.java": """
        package com.acme.b;

        public class Reply {
            public void setPort(int port) {}
            public Object getBean(String name) { return null; }
            public String getStatus() { return ""; }
        }
    """,
}


def _graph(root: Path, sources: dict[str, str]) -> CallGraph:
    for rel, text in {**_DECOYS, **sources}.items():
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


def test_a_parameter_takes_a_method_its_in_repo_superinterface_declares(
    tmp_path: Path,
) -> None:
    base = f"{_JAVA}/server/ConfigurableWebServerFactory.java"
    sub = f"{_JAVA}/server/ConfigurableReactiveWebServerFactory.java"
    rel = f"{_JAVA}/web/Customizer.java"
    g = _graph(
        tmp_path,
        {
            base: """
                package com.acme.server;

                public interface ConfigurableWebServerFactory {
                    void setPort(int port);
                }
            """,
            sub: """
                package com.acme.server;

                public interface ConfigurableReactiveWebServerFactory
                        extends ConfigurableWebServerFactory {
                    void setHandler(Object handler);
                }
            """,
            rel: """
                package com.acme.web;

                import com.acme.server.ConfigurableReactiveWebServerFactory;

                class Customizer {
                    void customize(
                            ConfigurableReactiveWebServerFactory server) {
                        server.setPort(9000);
                    }
                }
            """,
        },
    )
    caller = f"{rel}::Customizer.customize"
    assert _callees(g, caller) == {
        f"{base}::ConfigurableWebServerFactory.setPort"
    }
    assert "setPort" not in _ambiguous(g, caller)


def test_a_method_only_an_outside_superinterface_holds_is_external(
    tmp_path: Path,
) -> None:
    ctx = f"{_JAVA}/test/AssertableContext.java"
    rel = f"{_JAVA}/test/ContextTests.java"
    g = _graph(
        tmp_path,
        {
            ctx: """
                package com.acme.test;

                import org.springframework.context.ApplicationContext;

                interface AssertableContext extends ApplicationContext {
                    void close();
                }
            """,
            rel: """
                package com.acme.test;

                class ContextTests {
                    Object check(AssertableContext context) {
                        return context.getBean("dataSource");
                    }
                }
            """,
        },
    )
    caller = f"{rel}::ContextTests.check"
    assert "getBean" not in _ambiguous(g, caller)
    assert not any(c.endswith(".getBean") for c in _callees(g, caller))
    assert "context.getBean" in _external(g, caller)


def test_a_kotlin_parameter_takes_a_method_its_in_repo_base_declares(
    tmp_path: Path,
) -> None:
    base = f"{_JAVA}/health/Status.java"
    health = f"{_JAVA}/health/Health.java"
    rel = "src/main/kotlin/com/acme/web/Endpoint.kt"
    g = _graph(
        tmp_path,
        {
            base: """
                package com.acme.health;

                public class Status {
                    public String getStatus() { return ""; }
                }
            """,
            health: """
                package com.acme.health;

                public class Health extends Status {
                    public String getDetails() { return ""; }
                }
            """,
            rel: """
                package com.acme.web

                import com.acme.health.Health

                class Endpoint {
                    fun run(health: Health): String {
                        return health.getStatus()
                    }
                }
            """,
        },
    )
    caller = f"{rel}::Endpoint.run"
    assert _callees(g, caller) == {f"{base}::Status.getStatus"}


def test_a_parameter_whose_type_walks_unknown_keeps_the_typed_rung(
    tmp_path: Path,
) -> None:
    probe = f"{_JAVA}/a/Probe.java"
    rel = f"{_JAVA}/a/Runner.java"
    g = _graph(
        tmp_path,
        {
            rel: """
                package com.acme.a;

                class Runner<T> {
                    void run(T value, Probe probe) {
                        probe.setPort(1);
                    }
                }
            """,
        },
    )
    caller = f"{rel}::Runner.run"
    assert _callees(g, caller) == {f"{probe}::Probe.setPort"}
