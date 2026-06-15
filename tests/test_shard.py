"""Sharded MAP.md: mode matrix, auto threshold, orphans, link resolution."""

import posixpath
import re
from pathlib import Path

import pytest

from dekko import cli, render_md
from dekko.model import CallGraph, Edge, FileMap, Symbol


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


def _files() -> tuple[list[FileMap], CallGraph]:
    files = [
        FileMap(
            path="a/x.py", language="python", symbols=[_sym("a/x.py", "f")]
        ),
        FileMap(
            path="b/y.py", language="python", symbols=[_sym("b/y.py", "g")]
        ),
    ]
    graph = CallGraph(
        edges=[Edge(caller="a/x.py::f", callee="b/y.py::g", lines=[1])]
    )
    return files, graph


def _multi_repo(tmp_path: Path) -> Path:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "x.py").write_text(
        "from b.y import g\n\n\ndef f():\n    return g()\n"
    )
    (tmp_path / "b" / "y.py").write_text("def g():\n    return 1\n")
    return tmp_path


def test_never_emits_single_page() -> None:
    files, graph = _files()
    pages = render_md.render_map(files, graph, "demo", "never")
    assert [name for name, _ in pages] == ["MAP.md"]


def test_always_emits_index_plus_dir_pages() -> None:
    files, graph = _files()
    names = [
        name
        for name, _ in render_md.render_map(files, graph, "demo", "always")
    ]
    assert names[0] == "MAP.md"
    assert "map/a.md" in names
    assert "map/b.md" in names


def test_auto_single_under_threshold() -> None:
    files, graph = _files()
    pages = render_md.render_map(files, graph, "demo", "auto")
    assert len(pages) == 1


def test_auto_shards_over_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(render_md, "_SHARD_LINE_LIMIT", 1)
    files, graph = _files()
    pages = render_md.render_map(files, graph, "demo", "auto")
    assert len(pages) > 1


def _resolve_page(current: str, page_part: str) -> str:
    """Resolve a link's page part relative to the page it appears on."""
    if page_part == "":
        return current
    cur_dir = posixpath.dirname(current)
    return posixpath.normpath(posixpath.join(cur_dir, page_part))


def test_sharded_links_resolve_across_pages() -> None:
    files, graph = _files()
    pages = render_md.render_map(files, graph, "demo", "always")
    anchors_by_page = {
        name: set(re.findall(r'<a id="([a-z0-9-]+)">', content))
        for name, content in pages
    }
    checked = 0
    for name, content in pages:
        for target in re.findall(r"\]\(([^)]+)\)", content):
            if "#" not in target or target.startswith("http"):
                continue
            page_part, _, anchor = target.partition("#")
            resolved = _resolve_page(name, page_part)
            assert anchor in anchors_by_page.get(resolved, set()), (
                f"{name}: dangling link {target} -> {resolved}"
            )
            checked += 1
    assert checked > 0, "no cross-page links exercised"


def test_shard_never_leaves_no_map_dir(tmp_path: Path) -> None:
    root = _multi_repo(tmp_path)
    assert cli.main(["map", str(root), "--shard", "never", "--quiet"]) == 0
    assert (root / ".dekko" / "MAP.md").exists()
    assert not (root / ".dekko" / "map").exists()


def test_shard_always_writes_pages(tmp_path: Path) -> None:
    root = _multi_repo(tmp_path)
    assert cli.main(["map", str(root), "--shard", "always", "--quiet"]) == 0
    map_dir = root / ".dekko" / "map"
    assert map_dir.is_dir()
    assert (map_dir / "a.md").exists()
    assert (map_dir / "b.md").exists()
    index = (root / ".dekko" / "MAP.md").read_text()
    assert "map/a.md#" in index


def test_orphan_pages_cleaned_on_rename(tmp_path: Path) -> None:
    root = _multi_repo(tmp_path)
    assert cli.main(["map", str(root), "--shard", "always", "--quiet"]) == 0
    assert (root / ".dekko" / "map" / "a.md").exists()

    (root / "a").rename(root / "c")
    assert (
        cli.main(["map", str(root), "--shard", "always", "--quiet", "--full"])
        == 0
    )
    assert not (root / ".dekko" / "map" / "a.md").exists()
    assert (root / ".dekko" / "map" / "c.md").exists()


def test_output_file_forces_single(tmp_path: Path) -> None:
    root = _multi_repo(tmp_path)
    out = tmp_path / "CUSTOM.md"
    assert (
        cli.main(
            [
                "map",
                str(root),
                "--shard",
                "always",
                "--output",
                str(out),
                "--quiet",
            ]
        )
        == 0
    )
    assert out.exists()
    assert not (out.parent / "map").exists()
