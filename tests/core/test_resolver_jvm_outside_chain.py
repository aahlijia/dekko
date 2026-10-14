"""A Java call chain that starts at a receiver declared outside the repo.

``ds.getConnection().getMetaData().getURL()`` on a ``DataSource ds``
asks JDBC for a connection, then for its metadata: every hop returns
an outside type, so the last call can't be the repo's one ``getURL``.
That holds when every call before the last takes no arguments and the
head's type names no in-repo type argument: a generic method can hand
back a type its arguments name (``template.findOne(q, Entry.class)``),
and ``Optional<Repo>.get()`` hands back the repo type.
"""

import textwrap
from pathlib import Path

from dekko.core.model import CallGraph
from dekko.core.resolver import resolve
from dekko.repo_ops import map_repository

_JAVA = "src/main/java/com/acme"

_URL = f"{_JAVA}/env/OriginTrackedResource.java"
_URL_ID = f"{_URL}::OriginTrackedResource.getURL"
_REPO = f"{_JAVA}/data/Repo.java"
_ENTRY = f"{_JAVA}/data/Entry.java"
_ENTRY_ID = f"{_ENTRY}::Entry.getDn"

_SOURCES = {
    _URL: """
        package com.acme.env;

        public class OriginTrackedResource {
            public String getURL() { return ""; }
            public String getY() { return ""; }
        }
    """,
    _REPO: """
        package com.acme.data;

        public class Repo {
            public void run() {}
        }
    """,
    _ENTRY: """
        package com.acme.data;

        public class Entry {
            public String getDn() { return ""; }
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


def test_a_chain_off_an_outside_local_or_parameter_is_external(
    tmp_path: Path,
) -> None:
    rel = f"{_JAVA}/jdbc/Probe.java"
    g = _graph(
        tmp_path,
        {
            rel: """
                package com.acme.jdbc;

                import javax.sql.DataSource;

                class Probe {
                    String local() throws Exception {
                        DataSource ds = make();
                        return ds.getConnection().getMetaData().getURL();
                    }

                    String param(DataSource ds) throws Exception {
                        return ds.getConnection().getMetaData().getURL();
                    }

                    DataSource make() { return null; }
                }
            """,
        },
    )
    for method in ("local", "param"):
        caller = f"{rel}::Probe.{method}"
        assert _URL_ID not in _callees(g, caller)
        assert "ds.getConnection().getMetaData().getURL" in _external(
            g, caller
        )


def test_a_chain_off_an_outside_this_field_is_external(
    tmp_path: Path,
) -> None:
    rel = f"{_JAVA}/flyway/Settings.java"
    g = _graph(
        tmp_path,
        {
            rel: """
                package com.acme.flyway;

                import org.flywaydb.core.Flyway;

                class Settings {
                    private final Flyway flyway;

                    Settings(Flyway flyway) { this.flyway = flyway; }

                    String run() {
                        return this.flyway.getConfiguration().getY();
                    }
                }
            """,
        },
    )
    caller = f"{rel}::Settings.run"
    assert f"{_URL}::OriginTrackedResource.getY" not in _callees(g, caller)
    assert "this.flyway.getConfiguration().getY" in _external(g, caller)


def test_an_in_repo_type_argument_keeps_the_chain_open(
    tmp_path: Path,
) -> None:
    rel = f"{_JAVA}/data/Opt.java"
    g = _graph(
        tmp_path,
        {
            rel: """
                package com.acme.data;

                import java.util.Optional;

                class Opt {
                    void run(Optional<Repo> opt) {
                        opt.get().run();
                    }
                }
            """,
        },
    )
    caller = f"{rel}::Opt.run"
    assert "opt.get().run" not in _external(g, caller)


def test_a_hop_with_arguments_keeps_the_chain_open(tmp_path: Path) -> None:
    rel = f"{_JAVA}/ldap/Lookup.java"
    g = _graph(
        tmp_path,
        {
            rel: """
                package com.acme.ldap;

                import com.acme.data.Entry;
                import org.springframework.ldap.core.LdapTemplate;

                class Lookup {
                    String run(LdapTemplate template, Object q) {
                        return template.findOne(q, Entry.class).getDn();
                    }
                }
            """,
        },
    )
    caller = f"{rel}::Lookup.run"
    assert _ENTRY_ID in _callees(g, caller)


def test_a_kotlin_chain_is_left_alone(tmp_path: Path) -> None:
    rel = "src/main/kotlin/com/acme/jdbc/Probe.kt"
    g = _graph(
        tmp_path,
        {
            rel: """
                package com.acme.jdbc

                import javax.sql.DataSource

                class Probe {
                    fun run(ds: DataSource): String =
                        ds.getConnection().getMetaData().getURL()
                }
            """,
        },
    )
    caller = f"{rel}::Probe.run"
    assert _URL_ID in _callees(g, caller)
