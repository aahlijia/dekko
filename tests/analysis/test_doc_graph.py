"""The link graph over a repo's docs: nav files, orphans, entry docs."""

import shutil
from pathlib import Path

from dekko.analysis.doc_graph import DocGraph
from dekko.integrations import cli
from dekko.render.mapfile import MapIndex, load_map

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _index(tmp_path: Path, name: str) -> MapIndex:
    root = tmp_path / name
    shutil.copytree(FIXTURES / name, root)
    assert cli.main(["map", str(root), "--quiet"]) == 0
    index = load_map(root)
    assert index is not None
    return index


def _write(root: Path, files: dict[str, str]) -> MapIndex:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    assert cli.main(["map", str(root), "--quiet"]) == 0
    index = load_map(root)
    assert index is not None
    return index


def test_a_summary_file_is_nav(tmp_path: Path) -> None:
    graph = DocGraph.from_index(_index(tmp_path, "docsite"))

    assert graph.nav == {"docs/SUMMARY.md"}


def test_a_page_that_is_mostly_a_link_list_is_nav(tmp_path: Path) -> None:
    pages = {f"docs/p{n}.md": f"# P{n}\n" for n in range(12)}
    listing = "# Pages\n\n" + "".join(
        f"- [P{n}](p{n}.md)\n" for n in range(12)
    )
    short = "# Few\n\n" + "".join(f"- [P{n}](p{n}.md)\n" for n in range(9))
    prose = "# Prose\n\n" + "".join(
        f"See [P{n}](p{n}.md) for part {n}.\n" for n in range(12)
    )
    index = _write(
        tmp_path,
        {
            **pages,
            "docs/pages.md": listing,
            "docs/few.md": short,
            "docs/prose.md": prose,
        },
    )

    assert DocGraph.from_index(index).nav == {"docs/pages.md"}


def test_edges_count_resolved_links_only(tmp_path: Path) -> None:
    graph = DocGraph.from_index(_index(tmp_path, "docvault"))

    assert graph.edges == {
        ("Home.md", "Projects/Alpha.md"): 2,
        ("Home.md", "Beta.md"): 1,
        ("Home.md", "diagram.md"): 1,
        ("Projects/Alpha.md", "Home.md"): 1,
        ("Beta.md", "Projects/Alpha.md"): 1,
        ("Orphan.md", "Home.md"): 1,
    }


def test_orphans_have_no_doc_linking_to_them(tmp_path: Path) -> None:
    graph = DocGraph.from_index(_index(tmp_path, "docsite"))

    assert graph.orphans == ["README.md", "docs/SUMMARY.md"]


def test_entry_docs(tmp_path: Path) -> None:
    graph = DocGraph.from_index(_index(tmp_path, "docsite"))

    assert graph.entries == [
        "README.md",
        "docs/SUMMARY.md",
        "docs/api/index.md",
        "docs/index.mdx",
    ]


def test_a_doc_linking_out_widely_and_seldom_linked_is_an_entry(
    tmp_path: Path,
) -> None:
    pages = {f"p{n}.md": f"# P{n}\n" for n in range(5)}
    start = "# Start\n\n" + " ".join(f"[P{n}](p{n}.md)" for n in range(5))

    index = _write(tmp_path, {**pages, "start.md": start + "\n"})

    assert DocGraph.from_index(index).entries == ["start.md"]


def test_most_linked_counts_linking_docs_and_skips_nav(tmp_path: Path) -> None:
    graph = DocGraph.from_index(_index(tmp_path, "docsite"))

    assert graph.most_linked(2) == [
        ("docs/features/rules.mdx", 2),
        ("docs/api/index.md", 1),
    ]


def test_cluster_edges_leave_nav_files_out(tmp_path: Path) -> None:
    graph = DocGraph.from_index(_index(tmp_path, "docsite"))

    assert not any(
        "SUMMARY" in a or "SUMMARY" in b for a, b in graph.cluster_edges
    )
    assert (
        "docs/guide/setup.md",
        "docs/features/rules.mdx",
    ) in graph.cluster_edges


def test_test_docs_are_left_out_unless_asked(tmp_path: Path) -> None:
    index = _write(
        tmp_path,
        {"a.md": "# A\n[b](b.md)\n", "b.md": "# B\n", "tests/t.md": "# T\n"},
    )

    assert DocGraph.from_index(index).docs == ["a.md", "b.md"]
    assert "tests/t.md" in DocGraph.from_index(index, tests=True).docs
