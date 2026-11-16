"""A JVM call on a parameter or local declared with an outside type.

``config.getMaxRequestSize()`` on a local ``MultipartConfigElement
config`` is the servlet API's method, not the repo's one
``getMaxRequestSize``: a receiver that is one plain name bound to a
parameter or a local is read from the type its declaration writes, and
an outside type sends the call external, as it already did for a field
of that type. That holds for an outside interface an in-repo class
implements too. A declaration with no usable type (``var``, an untyped
lambda parameter) and an in-repo type leave the call to the ladder.
"""

import textwrap
from pathlib import Path

from dekko.core.model import CallGraph
from dekko.core.resolver import resolve
from dekko.repo_ops import map_repository

_JAVA = "src/main/java/com/acme"
_KT = "src/main/kotlin/com/acme"

_PROPS = f"{_JAVA}/props/MultipartProperties.java"
_PROPS_ID = f"{_PROPS}::MultipartProperties.getMaxRequestSize"

_SOURCES = {
    _PROPS: """
        package com.acme.props;

        public class MultipartProperties {
            public long getMaxRequestSize() { return 0; }
            public int length() { return 0; }
            public StringBuilder append(String s) { return null; }
            public String getName() { return ""; }
            public String get(String k) { return k; }
        }
    """,
}


def _graph(root: Path, sources: dict[str, str]) -> CallGraph:
    for rel, text in {**_SOURCES, **sources}.items():
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


def test_outside_typed_local_and_parameter_go_external(
    tmp_path: Path,
) -> None:
    rel = f"{_JAVA}/web/Factory.java"
    g = _graph(
        tmp_path,
        {
            rel: """
                package com.acme.web;

                import jakarta.servlet.MultipartConfigElement;

                class Factory {
                    long local() {
                        MultipartConfigElement config = make();
                        return config.getMaxRequestSize();
                    }

                    long param(MultipartConfigElement config) {
                        return config.getMaxRequestSize();
                    }

                    MultipartConfigElement make() { return null; }
                }
            """,
        },
    )
    for method in ("local", "param"):
        caller = f"{rel}::Factory.{method}"
        assert _PROPS_ID not in _callees(g, caller)
        assert _external(g, caller) == {"config.getMaxRequestSize"}


def test_java_lang_and_container_receivers_go_external(
    tmp_path: Path,
) -> None:
    rel = f"{_JAVA}/web/Text.java"
    g = _graph(
        tmp_path,
        {
            rel: """
                package com.acme.web;

                import java.util.Map;

                class Text {
                    void run(String s, Thread t, Map<String, String> m) {
                        StringBuilder sb = new StringBuilder();
                        sb.append(s);
                        s.length();
                        t.getName();
                        m.get("k");
                    }
                }
            """,
        },
    )
    caller = f"{rel}::Text.run"
    assert _callees(g, caller) == set()
    assert {"sb.append", "s.length", "t.getName", "m.get"} <= _external(
        g, caller
    )


def test_two_in_repo_candidates_leave_no_ambiguous_row(
    tmp_path: Path,
) -> None:
    rel = f"{_JAVA}/web/Retry.java"
    g = _graph(
        tmp_path,
        {
            f"{_JAVA}/amqp/Backoff.java": """
                package com.acme.amqp;

                public class Backoff {
                    public long getInitialInterval() { return 0; }
                }
            """,
            f"{_JAVA}/kafka/Backoff.java": """
                package com.acme.kafka;

                public class Backoff {
                    public long getInitialInterval() { return 0; }
                }
            """,
            rel: """
                package com.acme.web;

                import org.springframework.util.backoff.ExponentialBackOff;

                class Retry {
                    long run(ExponentialBackOff backOff) {
                        return backOff.getInitialInterval();
                    }
                }
            """,
        },
    )
    caller = f"{rel}::Retry.run"
    assert _ambiguous(g, caller) == {}
    assert _external(g, caller) == {"backOff.getInitialInterval"}


def test_an_outside_interface_with_an_in_repo_implementor_is_external(
    tmp_path: Path,
) -> None:
    rel = f"{_JAVA}/web/Urls.java"
    g = _graph(
        tmp_path,
        {
            f"{_JAVA}/env/OriginTrackedResource.java": """
                package com.acme.env;

                import org.springframework.core.io.Resource;

                public class OriginTrackedResource implements Resource {
                    public String getURL() { return ""; }
                }
            """,
            rel: """
                package com.acme.web;

                import org.springframework.core.io.Resource;

                class Urls {
                    String run(Resource res) { return res.getURL(); }
                }
            """,
        },
    )
    caller = f"{rel}::Urls.run"
    assert _callees(g, caller) == set()
    assert _external(g, caller) == {"res.getURL"}


def test_untyped_and_in_repo_typed_receivers_keep_the_ladder(
    tmp_path: Path,
) -> None:
    rel = f"{_JAVA}/web/Keep.java"
    g = _graph(
        tmp_path,
        {
            rel: """
                package com.acme.web;

                import java.util.List;
                import com.acme.props.MultipartProperties;

                class Keep {
                    long inferred() {
                        var config = MultipartProperties.class.cast(null);
                        return config.getMaxRequestSize();
                    }

                    void lambda(List<MultipartProperties> all) {
                        all.forEach(p -> p.getMaxRequestSize());
                    }

                    long typed(MultipartProperties props) {
                        return props.getMaxRequestSize();
                    }
                }
            """,
        },
    )
    for method in ("inferred", "lambda", "typed"):
        assert _callees(g, f"{rel}::Keep.{method}") == {_PROPS_ID}


def test_an_outside_typed_field_stays_external(tmp_path: Path) -> None:
    rel = f"{_JAVA}/web/Holder.java"
    g = _graph(
        tmp_path,
        {
            rel: """
                package com.acme.web;

                import jakarta.servlet.MultipartConfigElement;

                class Holder {
                    private MultipartConfigElement config;

                    long run() { return config.getMaxRequestSize(); }
                }
            """,
        },
    )
    caller = f"{rel}::Holder.run"
    assert _callees(g, caller) == set()
    assert _external(g, caller) == {"config.getMaxRequestSize"}


def test_kotlin_outside_typed_parameter_goes_external(
    tmp_path: Path,
) -> None:
    rel = f"{_KT}/web/Factory.kt"
    g = _graph(
        tmp_path,
        {
            rel: """
                package com.acme.web

                import jakarta.servlet.MultipartConfigElement
                import com.acme.props.MultipartProperties

                class Factory {
                    fun param(config: MultipartConfigElement): Long {
                        return config.getMaxRequestSize()
                    }

                    fun local(): Long {
                        val props = MultipartProperties()
                        return props.getMaxRequestSize()
                    }
                }
            """,
        },
    )
    assert _callees(g, f"{rel}::Factory.param") == set()
    assert _external(g, f"{rel}::Factory.param") == {
        "config.getMaxRequestSize"
    }
    assert _PROPS_ID in _callees(g, f"{rel}::Factory.local")
