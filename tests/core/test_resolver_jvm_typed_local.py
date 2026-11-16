"""A Java call on a local declared with an in-repo type is walked.

``health.getStatus()`` on a local ``Health health`` can only call
``Health``'s ``getStatus`` (or one ``Health`` inherits), but nothing
read a local's declared type, so the ladder saw every ``getStatus`` in
the repo and left an ambiguous row. The type a local's declaration
writes now decides, as a field's does: its own or inherited member is
the target, and a method none of its in-repo types declare is external
when the rest of its supertypes are outside the repo.

A local declared with an outside type is external, unless it is built
by an in-repo class right there (``Resource res = new
OriginTrackedResource()``): then that class decides.
"""

import textwrap
from pathlib import Path

from dekko.core.model import CallGraph
from dekko.core.resolver import resolve
from dekko.repo_ops import map_repository

_JAVA = "src/main/java/com/acme"

_STATUS_DECOYS = {
    f"{_JAVA}/a/Probe.java": """
        package com.acme.a;

        public class Probe {
            public String getStatus() { return ""; }
            public void refresh() {}
            public void close() {}
        }
    """,
    f"{_JAVA}/b/Reply.java": """
        package com.acme.b;

        public class Reply {
            public String getStatus() { return ""; }
            public void refresh() {}
            public void close() {}
        }
    """,
    f"{_JAVA}/c/Job.java": """
        package com.acme.c;

        public class Job {
            public String getStatus() { return ""; }
            public void refresh() {}
            public void close() {}
        }
    """,
}


def _graph(root: Path, sources: dict[str, str]) -> CallGraph:
    for rel, text in {**_STATUS_DECOYS, **sources}.items():
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


def test_a_local_typed_in_the_repo_takes_its_type_s_method(
    tmp_path: Path,
) -> None:
    health = f"{_JAVA}/health/Health.java"
    rel = f"{_JAVA}/web/Endpoint.java"
    g = _graph(
        tmp_path,
        {
            health: """
                package com.acme.health;

                public class Health {
                    public String getStatus() { return ""; }
                }
            """,
            rel: """
                package com.acme.web;

                import com.acme.health.Health;

                class Endpoint {
                    String run() {
                        Health health = lookup();
                        return health.getStatus();
                    }

                    Health lookup() { return null; }
                }
            """,
        },
    )
    caller = f"{rel}::Endpoint.run"
    assert f"{health}::Health.getStatus" in _callees(g, caller)
    assert "getStatus" not in _ambiguous(g, caller)


def test_a_local_takes_a_method_its_in_repo_base_declares(
    tmp_path: Path,
) -> None:
    base = f"{_JAVA}/core/Base.java"
    rel = f"{_JAVA}/core/User.java"
    g = _graph(
        tmp_path,
        {
            base: """
                package com.acme.core;

                class Base {
                    public void refresh() {}
                }
            """,
            f"{_JAVA}/core/Sub.java": """
                package com.acme.core;

                class Sub extends Base {
                }
            """,
            rel: """
                package com.acme.core;

                class User {
                    void run() {
                        Sub s = make();
                        s.refresh();
                    }

                    Sub make() { return null; }
                }
            """,
        },
    )
    caller = f"{rel}::User.run"
    assert _callees(g, caller) == {
        f"{base}::Base.refresh",
        f"{rel}::User.make",
    }
    assert _ambiguous(g, caller) == {}


def test_a_method_only_an_outside_base_can_have_is_external(
    tmp_path: Path,
) -> None:
    rel = f"{_JAVA}/env/User.java"
    g = _graph(
        tmp_path,
        {
            f"{_JAVA}/env/Resolver.java": """
                package com.acme.env;

                import org.springframework.core.env.StandardEnvironment;

                class Resolver extends StandardEnvironment {
                    void own() {}
                }
            """,
            rel: """
                package com.acme.env;

                class User {
                    void run() {
                        Resolver r = make();
                        r.close();
                    }

                    Resolver make() { return null; }
                }
            """,
        },
    )
    caller = f"{rel}::User.run"
    assert _callees(g, caller) == {f"{rel}::User.make"}
    assert _ambiguous(g, caller) == {}
    assert _external(g, caller) == {"r.close"}


def test_an_outside_typed_local_built_by_an_in_repo_class_takes_it(
    tmp_path: Path,
) -> None:
    impl = f"{_JAVA}/io/OriginTrackedResource.java"
    rel = f"{_JAVA}/web/Urls.java"
    g = _graph(
        tmp_path,
        {
            impl: """
                package com.acme.io;

                import org.springframework.core.io.Resource;

                public class OriginTrackedResource implements Resource {
                    public OriginTrackedResource(String s) {}
                    public void refresh() {}
                }
            """,
            rel: """
                package com.acme.web;

                import org.springframework.core.io.Resource;
                import com.acme.io.OriginTrackedResource;

                class Urls {
                    void built() {
                        Resource res = new OriginTrackedResource("x");
                        res.refresh();
                    }

                    void given() {
                        Resource res = Resource.EMPTY;
                        res.refresh();
                    }
                }
            """,
        },
    )
    built = f"{rel}::Urls.built"
    assert f"{impl}::OriginTrackedResource.refresh" in _callees(g, built)
    given = f"{rel}::Urls.given"
    assert _callees(g, given) == set()
    assert _external(g, given) == {"res.refresh"}


def test_a_local_declared_before_a_lambda_types_its_use_inside(
    tmp_path: Path,
) -> None:
    writer = f"{_JAVA}/json/JsonWriter.java"
    rel = f"{_JAVA}/json/User.java"
    g = _graph(
        tmp_path,
        {
            writer: """
                package com.acme.json;

                class JsonWriter {
                    void close() {}
                }
            """,
            rel: """
                package com.acme.json;

                import java.util.List;

                class User {
                    void run(List<String> all) {
                        JsonWriter writer = open();
                        all.forEach((s) -> {
                            writer.close();
                        });
                    }

                    JsonWriter open() { return null; }
                }
            """,
        },
    )
    caller = f"{rel}::User.run"
    assert f"{writer}::JsonWriter.close" in _callees(g, caller)
    assert "close" not in _ambiguous(g, caller)


def test_the_extractor_records_only_a_new_initializer(tmp_path: Path) -> None:
    rel = f"{_JAVA}/web/Init.java"
    (tmp_path / rel).parent.mkdir(parents=True)
    (tmp_path / rel).write_text(
        textwrap.dedent(
            """
            package com.acme.web;

            class Init {
                void run(Thing given) {
                    Thing built = new Box<String>("x");
                    Thing other = Thing.make();
                    Thing pool = new Box<String>("x").pool();
                    built.go();
                    other.go();
                    pool.go();
                    given.go();
                }
            }
            """
        )
    )
    files, _ = map_repository(
        tmp_path,
        subpath=None,
        excludes=(),
        max_file_size=1_000_000,
    )
    calls = {
        c.receiver: c.receiver_init
        for fm in files
        for c in fm.calls
        if c.name == "go"
    }
    assert calls == {
        "built": "Box",
        "other": None,
        "pool": None,
        "given": None,
    }
