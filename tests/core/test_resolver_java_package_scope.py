"""A Java simple type name the ladder can't decide is its own package's.

Java looks a simple type name up in the enclosing classes, then the
single-type imports, then the file's own package, then the on-demand
imports. No rung read the package, so ``new Conv()`` with one ``Conv``
beside the caller and another elsewhere was ambiguous.
"""

import json
from pathlib import Path

from dekko.core.model import CallGraph
from dekko.core.resolver import resolve
from dekko.integrations import cli
from dekko.repo_ops import map_repository

_LB = "src/main/java/org/lb"
_L4 = "src/main/java/org/l4"
_CONV_LB = f"{_LB}/Conv.java"
_CONV_L4 = f"{_L4}/Conv.java"
_CONVS = {
    _CONV_LB: "package org.lb;\npublic class Conv { public Conv() { } }\n",
    _CONV_L4: "package org.l4;\npublic class Conv { public Conv() { } }\n",
}


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


def _use(package: str, body: str, imports: str = "") -> str:
    return f"package {package};\n{imports}class Use {{\n{body}}}\n"


def _callees(graph: CallGraph, caller: str) -> set[str]:
    return {e.callee for e in graph.edges if e.caller == caller}


def _ambiguous(graph: CallGraph, caller: str) -> list[str]:
    return [name for c, name, _ in graph.ambiguous if c == caller]


def test_a_construction_takes_its_own_packages_type(tmp_path: Path) -> None:
    use = f"{_LB}/Use.java"
    graph = _graph(
        tmp_path,
        {**_CONVS, use: _use("org.lb", "    void run() { new Conv(); }\n")},
    )
    caller = f"{use}::Use.run"
    assert _callees(graph, caller) == {
        f"{_CONV_LB}::Conv",
        f"{_CONV_LB}::Conv.Conv",
    }
    assert _ambiguous(graph, caller) == []


def test_an_import_still_decides(tmp_path: Path) -> None:
    use = f"{_LB}/Use.java"
    graph = _graph(
        tmp_path,
        {
            **_CONVS,
            use: _use(
                "org.lb",
                "    void run() { new Conv(); }\n",
                imports="import org.l4.Conv;\n",
            ),
        },
    )
    assert _callees(graph, f"{use}::Use.run") == {
        f"{_CONV_L4}::Conv",
        f"{_CONV_L4}::Conv.Conv",
    }


def test_the_package_is_read_across_source_sets(tmp_path: Path) -> None:
    use = "src/test/java/org/lb/UseTest.java"
    graph = _graph(
        tmp_path,
        {
            **_CONVS,
            use: (
                "package org.lb;\nclass UseTest {\n"
                "    void run() { new Conv(); }\n}\n"
            ),
        },
    )
    assert f"{_CONV_LB}::Conv" in _callees(graph, f"{use}::UseTest.run")


def test_a_constructor_reference_takes_its_own_packages_type(
    tmp_path: Path,
) -> None:
    use = f"{_LB}/Use.java"
    graph = _graph(
        tmp_path,
        {
            **_CONVS,
            use: _use(
                "org.lb",
                "    void run() { java.util.function.Supplier<Conv> s ="
                " Conv::new; }\n",
            ),
        },
    )
    referenced = {
        e.callee for e in graph.referenced if e.caller == f"{use}::Use.run"
    }
    assert referenced == {f"{_CONV_LB}::Conv", f"{_CONV_LB}::Conv.Conv"}


def test_two_in_the_package_or_none_stay_ambiguous(tmp_path: Path) -> None:
    twin = "src/test/java/org/lb/Conv.java"
    other = "src/main/java/org/x/Use.java"
    lb_use = f"{_LB}/Use.java"
    graph = _graph(
        tmp_path,
        {
            **_CONVS,
            twin: "package org.lb;\npublic class Conv { }\n",
            lb_use: _use("org.lb", "    void run() { new Conv(); }\n"),
            other: _use("org.x", "    void run() { new Conv(); }\n"),
        },
    )
    assert _ambiguous(graph, f"{lb_use}::Use.run") == ["Conv"]
    assert _ambiguous(graph, f"{other}::Use.run") == ["Conv"]


def test_a_nested_type_elsewhere_is_no_rival(tmp_path: Path) -> None:
    use = f"{_LB}/Use.java"
    outer = f"{_L4}/Outer.java"
    graph = _graph(
        tmp_path,
        {
            _CONV_LB: _CONVS[_CONV_LB],
            outer: (
                "package org.l4;\npublic class Outer {\n"
                "    public static class Conv { public Conv() { } }\n}\n"
            ),
            use: _use("org.lb", "    void run() { new Conv(); }\n"),
        },
    )
    assert f"{_CONV_LB}::Conv" in _callees(graph, f"{use}::Use.run")


def test_a_file_with_no_source_root_is_not_judged(tmp_path: Path) -> None:
    use = "lb/Use.java"
    graph = _graph(
        tmp_path,
        {
            "lb/Conv.java": "package lb;\npublic class Conv { }\n",
            "l4/Conv.java": "package l4;\npublic class Conv { }\n",
            use: _use("lb", "    void run() { new Conv(); }\n"),
        },
    )
    assert _ambiguous(graph, f"{use}::Use.run") == ["Conv"]


def _graph_json(root: Path) -> dict:
    doc = json.loads((root / ".dekko/map.json").read_text())
    keys = ("edges", "ambiguous", "external", "referenced", "symbols")
    return {k: doc.get(k) for k in keys}


def test_a_new_package_type_reaches_an_incremental_map(
    tmp_path: Path,
) -> None:
    use = f"{_LB}/Use.java"
    for rel, text in {
        _CONV_L4: _CONVS[_CONV_L4],
        f"{_L4}/Other.java": "package org.l4;\nclass Other { }\n",
        use: _use(
            "org.lb",
            "    void run() { new Conv(); }\n",
            imports="import org.l4.*;\n",
        ),
    }.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    assert cli.main(["map", str(tmp_path), "--quiet"]) == 0
    (tmp_path / _CONV_LB).write_text(_CONVS[_CONV_LB])

    assert cli.main(["map", str(tmp_path), "--quiet"]) == 0
    incremental = _graph_json(tmp_path)
    assert cli.main(["map", str(tmp_path), "--quiet", "--full"]) == 0
    assert incremental == _graph_json(tmp_path)

    doc = json.loads((tmp_path / ".dekko/map.json").read_text())
    ids = doc["ids"]
    callees = {
        ids[e["callee"]]
        for e in doc["edges"]
        if ids[e["caller"]] == f"{use}::Use.run"
    }
    assert f"{_CONV_LB}::Conv" in callees
