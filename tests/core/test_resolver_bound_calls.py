"""A bare call to a parameter is a call to whatever the caller passed in.

``makeC(requestCapability)`` returning ``() => requestCapability(..)``
calls its argument, not the repo's own ``requestCapability``. Where a
function's name is a value (Python, JS, TS), a parameter of that name
shadows it, so the call goes external. A called *local* keeps its edge:
it usually holds the very function it is named after
(``const { run } = helpers``). Java keeps method names apart from
values, so a parameter there shadows nothing.
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


def _external(graph: CallGraph, caller: str) -> set[str]:
    return {e.callee for e in graph.external if e.caller == caller}


_TS_LIB = {
    "lib.ts": "export function rc(a: string, b: string) {}\n",
    "other.ts": "export function other() {}\n",
}


def test_bare_call_to_a_parameter_is_never_a_repo_function(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            **_TS_LIB,
            "use.ts": (
                "import { other } from './other'\n"
                "export function makeA(rc: any) {\n"
                "  return () => { rc('a', 'b') }\n"
                "}\n"
                "export function makeC(rc: any) {\n"
                "  return () => rc('a', 'b')\n"
                "}\n"
                "export function makeF(rc: any) {\n"
                "  return () => ({\n"
                "    execute(i: string) { return rc(i, 'b') },\n"
                "  })\n"
                "}\n"
            ),
        },
    )
    assert all(e.callee != "lib.ts::rc" for e in g.edges)
    assert "rc" in _external(g, "use.ts::makeC")


def test_unbound_bare_call_is_unchanged(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            **_TS_LIB,
            "use.ts": (
                "import { rc } from './lib'\n"
                "export function make() { return () => rc('a', 'b') }\n"
            ),
        },
    )
    assert "lib.ts::rc" in {e.callee for e in g.edges}


def test_python_parameter_shadowing_a_same_file_function(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            "a.py": """
                def run():
                    return 1


                def apply(run):
                    return run()
            """,
        },
    )
    assert _callees(g, "a.py::apply") == set()
    assert "run" in _external(g, "a.py::apply")


def test_bound_local_bare_call_keeps_its_edge(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "helpers.ts": "export function run() {}\n",
            "use.ts": (
                "import * as helpers from './helpers'\n"
                "export function go() {\n"
                "  const { run } = helpers\n"
                "  run()\n"
                "}\n"
            ),
        },
    )
    assert _callees(g, "use.ts::go") == {"helpers.ts::run"}


def test_java_bare_call_named_like_a_parameter_keeps_its_edge(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            "Svc.java": (
                "class Svc {\n"
                "  void run() {}\n"
                "  void go(Runnable run) { run(); }\n"
                "}\n"
            ),
        },
    )
    assert _callees(g, "Svc.java::Svc.go") == {"Svc.java::Svc.run"}


def test_called_factory_fixture_keeps_its_edge(tmp_path: Path) -> None:
    # pytest injects a fixture by parameter name, so a test calling its
    # factory-fixture parameter calls that fixture's product; the edge
    # is what lets `affected` walk from the test through the fixture.
    g = _graph(
        tmp_path,
        {
            "tests/test_a.py": """
                import pytest


                @pytest.fixture
                def build_order():
                    return lambda n: n


                def test_b(build_order):
                    build_order(3)
            """,
        },
    )
    assert _callees(g, "tests/test_a.py::test_b") == {
        "tests/test_a.py::build_order"
    }


def test_called_conftest_fixture_keeps_its_edge(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "tests/conftest.py": """
                import pytest


                @pytest.fixture
                def make_repo():
                    return lambda: 1
            """,
            "tests/sub/test_a.py": """
                def test_b(make_repo):
                    make_repo()
            """,
        },
    )
    assert _callees(g, "tests/sub/test_a.py::test_b") == {
        "tests/conftest.py::make_repo"
    }
