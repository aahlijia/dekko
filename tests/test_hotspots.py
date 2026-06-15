"""B5: trust line, largest-files overview, and churn x fan-in hotspots."""

import re
import subprocess
from pathlib import Path


from dekko import cli, mapfile, summary
from dekko.model import CallGraph, Edge, FileMap, Symbol
from dekko.render_md import render_markdown


def _sym(path: str, name: str) -> Symbol:
    return Symbol(
        id=f"{path}::{name}",
        name=name,
        qualname=name,
        kind="function",
        path=path,
        language="python",
        start_line=1,
        end_line=2,
    )


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "Test")


# --- largest files ---------------------------------------------------------


def test_largest_files_in_summary_doc() -> None:
    fm = FileMap(
        path="big.py",
        language="python",
        symbols=[_sym("big.py", "f"), _sym("big.py", "g")],
    )
    index = mapfile.index_from_maps([fm], CallGraph(), "demo")
    doc = summary.compute(index)
    assert doc["largest_files"][0] == {"path": "big.py", "symbols": 2}


def test_largest_files_linked_in_overview() -> None:
    fm = FileMap(
        path="big.py", language="python", symbols=[_sym("big.py", "f")]
    )
    text = render_markdown([fm], CallGraph(), "demo")
    overview = text.split("## Contents")[0]
    assert "**Largest files**" in overview
    hrefs = re.findall(r"Largest files.*?\n\n(- .+)", overview, re.DOTALL)
    assert hrefs and "big.py" in hrefs[0]


# --- trust line ------------------------------------------------------------


def test_trust_line_cold_then_warm(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def f() -> int:\n    return 1\n")
    (tmp_path / "b.py").write_text("def g() -> int:\n    return 2\n")

    assert cli.main(["map", str(tmp_path), "--quiet", "--full"]) == 0
    cold = (tmp_path / ".dekko" / "MAP.md").read_text()
    line = next(ln for ln in cold.splitlines() if ln.startswith("*Mapped "))
    assert "(cache: 0 reused / 2 parsed)" in line
    assert re.search(r"Mapped 2 files in \d+ ms", line)

    assert cli.main(["map", str(tmp_path), "--quiet"]) == 0
    warm = (tmp_path / ".dekko" / "MAP.md").read_text()
    warm_line = next(
        ln for ln in warm.splitlines() if ln.startswith("*Mapped ")
    )
    assert "(cache: 2 reused / 0 parsed)" in warm_line


# --- churn x fan-in hotspots ----------------------------------------------


def _hotspot_index() -> mapfile.MapIndex:
    """Three files where ``a.py::core`` has fan-in 2 (b and c call it)."""
    fa = FileMap("a.py", "python", symbols=[_sym("a.py", "core")])
    fb = FileMap("b.py", "python", symbols=[_sym("b.py", "g")])
    fc = FileMap("c.py", "python", symbols=[_sym("c.py", "h")])
    graph = CallGraph(
        edges=[
            Edge(caller="b.py::g", callee="a.py::core", lines=[2]),
            Edge(caller="c.py::h", callee="a.py::core", lines=[2]),
        ]
    )
    return mapfile.index_from_maps([fa, fb, fc], graph, "demo")


def test_churn_hotspots_empty_without_git(tmp_path: Path) -> None:
    # tmp_path is not a git repo → best-effort returns nothing.
    assert summary.churn_hotspots(_hotspot_index(), tmp_path) == []


def test_churn_hotspots_present_and_ordered(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    # a.py (fan-in 2) churns 3 times; b.py (fan-in 0) churns once.
    for i in range(3):
        (tmp_path / "a.py").write_text(f"# rev {i}\n")
        _git(tmp_path, "add", "a.py")
        _git(tmp_path, "commit", "-q", "-m", f"a {i}")
    (tmp_path / "b.py").write_text("# b\n")
    _git(tmp_path, "add", "b.py")
    _git(tmp_path, "commit", "-q", "-m", "b")

    rows = summary.churn_hotspots(_hotspot_index(), tmp_path)
    # Only files with churn *and* fan-in qualify: a.py, not b.py (no
    # fan-in) and not c.py (no churn).
    assert [r["path"] for r in rows] == ["a.py"]
    assert rows[0]["churn"] == 3
    assert rows[0]["fan_in"] == 2


def test_overview_hotspots_omitted_without_git(tmp_path: Path) -> None:
    # A real root is passed, but tmp_path has no git history, so the
    # churn section is omitted rather than rendered empty.
    files = [
        FileMap("a.py", "python", symbols=[_sym("a.py", "core")]),
        FileMap("b.py", "python", symbols=[_sym("b.py", "g")]),
    ]
    graph = CallGraph(
        edges=[Edge(caller="b.py::g", callee="a.py::core", lines=[2])]
    )
    text = render_markdown(files, graph, "demo", root=tmp_path)
    assert "**Hotspots**" not in text
