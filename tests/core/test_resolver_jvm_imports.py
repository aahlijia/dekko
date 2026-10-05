"""Java/Kotlin imports resolve by qualified path, not by simple name.

A JVM import names one file: ``org.acme.json.JsonContent`` is
``org/acme/json/JsonContent.java`` under a source root. An import of
an external class that happens to share a repo class's simple name is
external, and an import of one of several same-named repo classes
names exactly the one it spells.
"""

from pathlib import Path

import pytest

from dekko.core.model import CallGraph
from dekko.core.resolver import _strip_java_root, resolve
from dekko.repo_ops import map_repository

_MAIN = "core/src/main/java/org/acme"
_TEST = "app/src/test/java/org/acme/app"


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


def _externals(graph: CallGraph, caller: str) -> set[str]:
    return {x.callee for x in graph.external if x.caller == caller}


def _ambiguous_names(graph: CallGraph, caller: str) -> set[str]:
    return {a[1] for a in graph.ambiguous if a[0] == caller}


_JSON_CONTENT = """package org.acme.json;
public class JsonContent {
\tpublic JsonContent(String body) { }
\tpublic static JsonContent of(String body) { return null; }
}
"""

_MANIFEST = """package org.acme.docker;
public class Manifest {
\tpublic static Manifest read(String s) { return null; }
}
"""

_ASSERTIONS = """package org.acme.util;
public class Assertions {
\tpublic static void assertThat(Object o) { }
}
"""


def _test_class(name: str, imports: str, body: str) -> str:
    return (
        f"package org.acme.app;\n{imports}\n"
        f"public class {name} {{\n\tvoid run() {{\n{body}\t}}\n}}\n"
    )


_RUN = f"{_TEST}/Tests.java::Tests.run"


def test_external_import_of_a_same_named_class_is_external(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            f"{_MAIN}/json/JsonContent.java": _JSON_CONTENT,
            f"{_TEST}/Tests.java": _test_class(
                "Tests",
                "import org.springframework.test.json.JsonContent;",
                '\t\tObject j = new JsonContent("{}");\n',
            ),
        },
    )
    assert _callees(graph, _RUN) == set()
    assert _ambiguous_names(graph, _RUN) == set()
    assert _externals(graph, _RUN) == {"new JsonContent"}


def test_receiver_call_on_an_external_import_is_external(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            f"{_MAIN}/docker/Manifest.java": _MANIFEST,
            f"{_TEST}/Tests.java": _test_class(
                "Tests",
                "import java.util.jar.Manifest;",
                '\t\tObject m = Manifest.read("x");\n',
            ),
        },
    )
    assert _callees(graph, _RUN) == set()
    assert _externals(graph, _RUN) == {"Manifest.read"}


def test_static_import_of_an_external_member_is_external(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            f"{_MAIN}/util/Assertions.java": _ASSERTIONS,
            f"{_TEST}/Tests.java": _test_class(
                "Tests",
                "import static org.assertj.core.api.Assertions.assertThat;",
                "\t\tassertThat(1);\n",
            ),
        },
    )
    assert _callees(graph, _RUN) == set()
    assert _externals(graph, _RUN) == {"assertThat"}


def test_in_repo_import_picks_the_one_same_named_class_it_names(
    tmp_path: Path,
) -> None:
    other = _JSON_CONTENT.replace("org.acme.json", "org.acme.other")
    graph = _graph(
        tmp_path,
        {
            f"{_MAIN}/json/JsonContent.java": _JSON_CONTENT,
            f"{_MAIN}/other/JsonContent.java": other,
            f"{_TEST}/Tests.java": _test_class(
                "Tests",
                "import org.acme.other.JsonContent;",
                '\t\tObject j = new JsonContent("{}");\n',
            ),
        },
    )
    other_file = f"{_MAIN}/other/JsonContent.java"
    assert _callees(graph, _RUN) == {
        f"{other_file}::JsonContent",
        f"{other_file}::JsonContent.JsonContent",
    }
    assert _ambiguous_names(graph, _RUN) == set()


def test_in_repo_imports_still_resolve(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        {
            f"{_MAIN}/json/JsonContent.java": _JSON_CONTENT,
            f"{_MAIN}/docker/Manifest.java": _MANIFEST,
            f"{_MAIN}/util/Assertions.java": _ASSERTIONS,
            f"{_TEST}/Tests.java": _test_class(
                "Tests",
                "import org.acme.json.JsonContent;\n"
                "import org.acme.docker.Manifest;\n"
                "import static org.acme.util.Assertions.assertThat;",
                '\t\tObject j = JsonContent.of("{}");\n'
                '\t\tObject m = Manifest.read("x");\n'
                "\t\tassertThat(j);\n",
            ),
        },
    )
    assert _callees(graph, _RUN) == {
        f"{_MAIN}/json/JsonContent.java::JsonContent.of",
        f"{_MAIN}/docker/Manifest.java::Manifest.read",
        f"{_MAIN}/util/Assertions.java::Assertions.assertThat",
    }
    assert _externals(graph, _RUN) == set()


def test_nested_class_import_reaches_the_declaring_file(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            f"{_MAIN}/Outer.java": """package org.acme;
public class Outer {
\tpublic static class Inner {
\t\tpublic Inner() { }
\t}
}
""",
            f"{_TEST}/Tests.java": _test_class(
                "Tests",
                "import org.acme.Outer.Inner;",
                "\t\tObject i = new Inner();\n",
            ),
        },
    )
    assert _callees(graph, _RUN) == {
        f"{_MAIN}/Outer.java::Outer.Inner",
        f"{_MAIN}/Outer.java::Outer.Inner.Inner",
    }


def test_nested_class_import_picks_among_same_named_classes(
    tmp_path: Path,
) -> None:
    # Two nested classes named ``Inner``; the import names the one in
    # ``Outer``, so the construction resolves to it, not ambiguously.
    nested = """package org.acme;
public class {outer} {{
\tpublic static class Inner {{
\t\tpublic Inner() {{ }}
\t}}
}}
"""
    graph = _graph(
        tmp_path,
        {
            f"{_MAIN}/Outer.java": nested.format(outer="Outer"),
            f"{_MAIN}/Other.java": nested.format(outer="Other"),
            f"{_TEST}/Tests.java": _test_class(
                "Tests",
                "import org.acme.Outer.Inner;",
                "\t\tObject i = new Inner();\n",
            ),
        },
    )
    assert _callees(graph, _RUN) == {
        f"{_MAIN}/Outer.java::Outer.Inner",
        f"{_MAIN}/Outer.java::Outer.Inner.Inner",
    }
    assert "Inner" not in _ambiguous_names(graph, _RUN)


def test_static_member_import_reaches_the_declaring_file(
    tmp_path: Path,
) -> None:
    helper = """package org.acme;
public class {outer} {{
\tpublic static int helper() {{ return 1; }}
}}
"""
    graph = _graph(
        tmp_path,
        {
            f"{_MAIN}/Outer.java": helper.format(outer="Outer"),
            f"{_MAIN}/Other.java": helper.format(outer="Other"),
            f"{_TEST}/Tests.java": _test_class(
                "Tests",
                "import static org.acme.Outer.helper;",
                "\t\thelper();\n",
            ),
        },
    )
    assert _callees(graph, _RUN) == {f"{_MAIN}/Outer.java::Outer.helper"}


def test_kotlin_top_level_function_import_still_resolves(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            "core/src/main/kotlin/org/acme/Ext.kt": (
                "package org.acme\nfun runApp(args: Array<String>) { }\n"
            ),
            "app/src/main/kotlin/org/acme/app/Main.kt": (
                "package org.acme.app\nimport org.acme.runApp\n"
                "fun main(args: Array<String>) { runApp(args) }\n"
            ),
        },
    )
    main = "app/src/main/kotlin/org/acme/app/Main.kt::main"
    assert _callees(graph, main) == {
        "core/src/main/kotlin/org/acme/Ext.kt::runApp"
    }


def test_any_gradle_source_set_is_a_source_root(tmp_path: Path) -> None:
    invoker = "cli/src/intTest/java/org/acme/cli/Invoker.java"
    version = "core/src/main/javaTemplates/org/acme/Version.java"
    caller = "cli/src/intTest/java/org/acme/cli/CliIT.java"
    graph = _graph(
        tmp_path,
        {
            invoker: """package org.acme.cli;
public class Invoker {
\tpublic Invoker(String cmd) { }
}
""",
            version: """package org.acme;
public class Version {
\tpublic static String get() { return "1"; }
}
""",
            caller: """package org.acme.cli;
import org.acme.cli.Invoker;
import org.acme.Version;
public class CliIT {
\tvoid runs() { new Invoker(Version.get()); }
}
""",
        },
    )
    runs = f"{caller}::CliIT.runs"
    assert _callees(graph, runs) == {
        f"{invoker}::Invoker",
        f"{invoker}::Invoker.Invoker",
        f"{version}::Version.get",
    }
    assert set(graph.modules.deps_out.get(caller, [])) == {invoker, version}


def test_rootless_file_matches_by_path_suffix_only(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        {
            "java/org/x/Y.java": """package org.x;
public class Y {
\tpublic Y() { }
}
""",
            "java/org/w/Own.java": """package org.w;
import org.x.Y;
public class Own {
\tvoid run() { new Y(); }
}
""",
            "java/org/w/Foreign.java": """package org.w;
import com.z.Y;
public class Foreign {
\tvoid run() { new Y(); }
}
""",
        },
    )
    own = "java/org/w/Own.java::Own.run"
    foreign = "java/org/w/Foreign.java::Foreign.run"
    assert _callees(graph, own) == {
        "java/org/x/Y.java::Y",
        "java/org/x/Y.java::Y.Y",
    }
    assert _callees(graph, foreign) == set()
    assert _externals(graph, foreign) == {"new Y"}


def test_external_supertype_beside_a_same_named_repo_type(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            f"{_MAIN}/metrics/Repository.java": (
                "package org.acme.metrics;\npublic class Repository { }\n"
            ),
            f"{_MAIN}/data/UserRepository.java": """package org.acme.data;
import org.springframework.data.repository.Repository;
public class UserRepository extends Repository { }
""",
        },
    )
    assert graph.heritage == []
    assert graph.heritage_ambiguous == []
    assert [x.callee for x in graph.heritage_external] == ["Repository"]


def test_python_imports_keep_the_stem_test(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/mod.py": "def name():\n    return 1\n",
            "pkg/use.py": (
                "from pkg.mod import name\n\n\ndef go():\n    name()\n"
            ),
        },
    )
    assert _callees(graph, "pkg/use.py::go") == {"pkg/mod.py::name"}


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("a/src/intTest/java/x/Y.java", "x/Y.java"),
        ("src/main/javaTemplates/x/Y.java", "x/Y.java"),
        ("core/src/main/kotlin/x/Y.kt", "x/Y.kt"),
        ("src/test/resources/x/Y.java", "test/resources/x/Y.java"),
        ("java/x/Y.java", None),
    ],
)
def test_strip_java_root(path: str, expected: str | None) -> None:
    assert _strip_java_root(path.split("/")) == expected
