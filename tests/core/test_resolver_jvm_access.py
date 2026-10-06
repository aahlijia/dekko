"""JVM access rules: a pick the call site can't reach is no pick.

A private method is reachable from its own file only, a package-private
Java one from its own package only, and a member's reach is the
narrowest of its own and every enclosing type's. The name-only rungs
took them from anywhere, so AssertJ's ``.extracting(..)`` landed on a
private test helper of the same name.
"""

import json
from pathlib import Path

import pytest

from dekko.core.extractor import extract_file
from dekko.core.languages import spec_for_path
from dekko.core.model import CallGraph
from dekko.core.resolver import resolve
from dekko.integrations import cli
from dekko.render.mapfile import load_map
from dekko.repo_ops import map_repository

_MAIN = "src/main/java/app"
_OTHER = "src/main/java/other"


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
    return resolve(files)


def _edges(graph: CallGraph) -> set[tuple[str, str]]:
    return {(e.caller, e.callee) for e in graph.edges}


def _external(graph: CallGraph) -> set[tuple[str, str]]:
    return {(e.caller, e.callee) for e in graph.external}


def _visibility(root: Path, rel: str, text: str) -> dict[str, str | None]:
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text(text)
    fm = extract_file(root, rel, spec_for_path(rel))
    return {s.qualname: s.visibility for s in fm.symbols}


def test_java_visibility_is_the_narrowest_enclosing_access(
    tmp_path: Path,
) -> None:
    got = _visibility(
        tmp_path,
        f"{_MAIN}/Outer.java",
        "package app;\n"
        "public class Outer {\n"
        "    public void open() { }\n"
        "    protected void guarded() { }\n"
        "    void local() { }\n"
        "    private void own() { }\n"
        "    private Outer(int a) { }\n"
        "    private static class Inner {\n"
        "        public void ping() { }\n"
        "        static class Deeper { public void deep() { } }\n"
        "    }\n"
        "    protected static class Kin { public void kin() { } }\n"
        "    void anon() {\n"
        "        new Runnable() { public void run() { } };\n"
        "    }\n"
        "}\n"
        "class Hidden {\n"
        "    public void hide() { }\n"
        "}\n"
        "interface Api {\n"
        "    void serve();\n"
        "}\n",
    )
    assert got["Outer"] is None
    assert got["Outer.open"] is None
    assert got["Outer.guarded"] is None
    assert got["Outer.local"] == "package"
    assert got["Outer.own"] == "private"
    assert got["Outer.Outer"] == "private"
    assert got["Outer.Inner.ping"] == "private"
    assert got["Outer.Inner.Deeper.deep"] == "private"
    assert got["Outer.Kin.kin"] is None
    assert got["Hidden.hide"] == "package"
    # A package-private interface narrows its members, which are
    # otherwise public without a modifier.
    assert got["Api.serve"] == "package"
    anonymous = [q for q in got if q.endswith(".run")]
    assert all(got[q] is None for q in anonymous)


def test_kotlin_visibility_is_private_in_a_private_scope(
    tmp_path: Path,
) -> None:
    got = _visibility(
        tmp_path,
        "src/main/kotlin/app/Tools.kt",
        "package app\n"
        "private fun secret() {}\n"
        "fun open() {}\n"
        "internal fun inside() {}\n"
        "private class Box {\n"
        "    fun lid() {}\n"
        "}\n"
        "class Crate {\n"
        "    private fun nail() {}\n"
        "    fun board() {}\n"
        "}\n",
    )
    assert got["secret"] == "private"
    assert got["open"] is None
    assert got["inside"] is None
    assert got["Box.lid"] == "private"
    assert got["Crate.nail"] == "private"
    assert got["Crate.board"] is None


_HELPER = (
    "package app;\n"
    "public class Helper {\n"
    "    private Object extracting(String a) { return a; }\n"
    '    void self() { extracting("x"); }\n'
    "}\n"
)
_USER = (
    "package app;\n"
    "public class User {\n"
    '    void go(Object r) { assertThat(r).extracting("x"); }\n'
    "}\n"
)


def test_a_private_method_is_reached_from_its_own_file_only(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {f"{_MAIN}/Helper.java": _HELPER, f"{_MAIN}/User.java": _USER},
    )
    target = f"{_MAIN}/Helper.java::Helper.extracting"
    edges = _edges(graph)
    assert (f"{_MAIN}/Helper.java::Helper.self", target) in edges
    assert (f"{_MAIN}/User.java::User.go", target) not in edges
    assert any(
        caller == f"{_MAIN}/User.java::User.go" and text.endswith("extracting")
        for caller, text in _external(graph)
    )


_PKG_HELPER = (
    "package app;\npublic class Helper {\n    void tidy(String a) { }\n}\n"
)


def _caller(package: str, cls: str) -> str:
    return (
        f"package {package};\n"
        f"public class {cls} {{\n"
        '    void go(Object h) { h.tidy("x"); }\n'
        "}\n"
    )


@pytest.mark.parametrize(
    ("rel", "package", "reached"),
    [
        (f"{_MAIN}/Same.java", "app", True),
        ("src/test/java/app/SameTest.java", "app", True),
        (f"{_OTHER}/Far.java", "other", False),
    ],
)
def test_a_package_private_method_is_reached_from_its_package_only(
    tmp_path: Path,
    rel: str,
    package: str,
    reached: bool,
) -> None:
    cls = Path(rel).stem
    graph = _graph(
        tmp_path,
        {f"{_MAIN}/Helper.java": _PKG_HELPER, rel: _caller(package, cls)},
    )
    pair = (f"{rel}::{cls}.go", f"{_MAIN}/Helper.java::Helper.tidy")
    assert (pair in _edges(graph)) == reached


def test_a_file_with_no_source_root_is_never_judged_by_package(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {"a/Helper.java": _PKG_HELPER, "b/Far.java": _caller("other", "Far")},
    )
    assert ("b/Far.java::Far.go", "a/Helper.java::Helper.tidy") in _edges(
        graph
    )


_NESTED = (
    "package app;\n"
    "public class Outer {\n"
    "    private static class Inner {\n"
    "        public void ping() { }\n"
    "        static class Deeper { public void deep() { } }\n"
    "    }\n"
    "    void use(Object i) { i.ping(); i.deep(); }\n"
    "}\n"
)


def test_a_member_of_a_private_type_is_reached_from_its_own_file_only(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            f"{_MAIN}/Outer.java": _NESTED,
            f"{_MAIN}/Away.java": (
                "package app;\n"
                "public class Away {\n"
                "    void go(Object x) { x.ping(); x.deep(); }\n"
                "}\n"
            ),
        },
    )
    edges = _edges(graph)
    ping = f"{_MAIN}/Outer.java::Outer.Inner.ping"
    deep = f"{_MAIN}/Outer.java::Outer.Inner.Deeper.deep"
    assert (f"{_MAIN}/Outer.java::Outer.use", ping) in edges
    assert (f"{_MAIN}/Outer.java::Outer.use", deep) in edges
    assert (f"{_MAIN}/Away.java::Away.go", ping) not in edges
    assert (f"{_MAIN}/Away.java::Away.go", deep) not in edges


def test_a_member_of_a_package_private_type_stays_in_its_package(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            f"{_MAIN}/Hidden.java": (
                "package app;\nclass Hidden {\n    public void hide() { }\n}\n"
            ),
            f"{_OTHER}/Far.java": (
                "package other;\n"
                "public class Far {\n"
                "    void go(Object h) { h.hide(); }\n"
                "}\n"
            ),
        },
    )
    assert (
        f"{_OTHER}/Far.java::Far.go",
        f"{_MAIN}/Hidden.java::Hidden.hide",
    ) not in _edges(graph)


def test_an_interface_method_without_a_modifier_is_public(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            f"{_MAIN}/Api.java": (
                "package app;\n"
                "public interface Api {\n"
                "    void serve(String s);\n"
                "}\n"
            ),
            f"{_OTHER}/Far.java": (
                "package other;\n"
                "public class Far {\n"
                '    void go(Object a) { a.serve("x"); }\n'
                "}\n"
            ),
        },
    )
    assert (
        f"{_OTHER}/Far.java::Far.go",
        f"{_MAIN}/Api.java::Api.serve",
    ) in _edges(graph)


def test_a_method_reference_to_a_private_method_elsewhere_is_no_edge(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            f"{_MAIN}/Checker.java": (
                "package app;\n"
                "public class Checker {\n"
                "    private boolean isInstance(Object o) { return true; }\n"
                "}\n"
            ),
            f"{_MAIN}/Filter.java": (
                "package app;\n"
                "public class Filter {\n"
                "    void go(java.util.List<Object> xs) {\n"
                "        xs.stream().filter(Checker.class::isInstance);\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    target = f"{_MAIN}/Checker.java::Checker.isInstance"
    assert all(e.callee != target for e in graph.referenced)
    assert all(e.callee != target for e in graph.edges)


def test_a_vetoed_private_pick_reruns_the_ladder(tmp_path: Path) -> None:
    """A ``Shop.Desk`` parameter takes the typed-parameter rung to the
    outer class's private ``work``; the site can't reach it, so the
    ladder runs again and the nested type's public ``work`` is the sole
    candidate left."""
    graph = _graph(
        tmp_path,
        {
            f"{_OTHER}/Shop.java": (
                "package other;\n"
                "public class Shop {\n"
                "    private void work() { }\n"
                "    public static final class Desk {\n"
                "        public void work() { }\n"
                "    }\n"
                "}\n"
            ),
            f"{_MAIN}/Boss.java": (
                "package app;\n"
                "import other.Shop;\n"
                "public class Boss {\n"
                "    void go(Shop.Desk desk) { desk.work(); }\n"
                "}\n"
            ),
        },
    )
    edges = _edges(graph)
    caller = f"{_MAIN}/Boss.java::Boss.go"
    assert (caller, f"{_OTHER}/Shop.java::Shop.work") not in edges
    assert (caller, f"{_OTHER}/Shop.java::Shop.Desk.work") in edges


def test_kotlin_private_members_are_reached_from_their_file_only(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            "src/main/kotlin/app/Tools.kt": (
                "package app\n"
                "private fun secret(a: Int) {}\n"
                "private class Box {\n"
                "    fun lid(a: Int) {}\n"
                "}\n"
                "fun local() { secret(1) }\n"
            ),
            "src/main/kotlin/app/Use.kt": (
                "package app\n"
                "fun use(x: Any) {\n"
                "    secret(1)\n"
                "    x.lid(1)\n"
                "}\n"
            ),
        },
    )
    edges = _edges(graph)
    tools = "src/main/kotlin/app/Tools.kt"
    use = "src/main/kotlin/app/Use.kt::use"
    assert (f"{tools}::local", f"{tools}::secret") in edges
    assert (use, f"{tools}::secret") not in edges
    assert (use, f"{tools}::Box.lid") not in edges


_GADGET = (
    "package app;\n"
    "public class Gadget {\n"
    "    public Gadget() { }\n"
    "    private Gadget(int a) { }\n"
    "    private Gadget(int a, int b) { }\n"
    "    public Gadget(String a, String b) { }\n"
    "}\n"
)


def test_a_private_constructor_is_no_overload_from_another_file(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            f"{_MAIN}/Gadget.java": _GADGET,
            f"{_MAIN}/Maker.java": (
                "package app;\n"
                "public class Maker {\n"
                "    Object one() { return new Gadget(1); }\n"
                "    Object two(Object a, Object b) {\n"
                "        return new Gadget(a, b);\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    edges = _edges(graph)
    maker = f"{_MAIN}/Maker.java::Maker"
    cls = f"{_MAIN}/Gadget.java::Gadget"
    assert (f"{maker}.one", cls) in edges
    assert {c for c, n in edges if c == f"{maker}.one" and n != cls} == set()
    # Two arguments fit a private and a public overload; only the
    # public one is reachable, so it is no longer a tie.
    assert (f"{maker}.two", f"{cls}.Gadget#4") in edges
    assert not [row for row in graph.ambiguous if row[0].startswith(maker)]


def test_visibility_round_trips_through_map_json_and_the_cache(
    tmp_path: Path,
) -> None:
    (tmp_path / _MAIN).mkdir(parents=True)
    (tmp_path / f"{_MAIN}/Helper.java").write_text(
        "package app;\n"
        "public class Helper {\n"
        "    private void own() { }\n"
        "    void pkg() { }\n"
        "    public void open() { }\n"
        "}\n"
    )
    want = {"Helper.own": "private", "Helper.pkg": "package"}
    for _ in range(2):
        # The second run reads every file back from the extraction
        # cache, which once dropped a field it didn't know.
        assert cli.main(["map", str(tmp_path), "--quiet"]) == 0
        doc = json.loads((tmp_path / ".dekko/map.json").read_text())
        rows = {s["qualname"]: s for s in doc["symbols"]}
        assert {
            q: s["visibility"] for q, s in rows.items() if "visibility" in s
        } == want
        index = load_map(tmp_path)
        assert index is not None
        loaded = {
            s.qualname: s.visibility for s in index.symbols_by_id.values()
        }
        assert loaded["Helper.open"] is None
        assert {q: loaded[q] for q in want} == want
