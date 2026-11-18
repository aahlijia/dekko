"""Orientation in doc-heavy repos: the CLI over links between docs."""

import json
import shutil
from pathlib import Path

import pytest

from dekko.integrations import cli, server

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _mapped(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    shutil.copytree(FIXTURES / name, root)
    assert cli.main(["map", str(root), "--quiet"]) == 0
    return root


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return _mapped(tmp_path, "docvault")


@pytest.fixture
def site(tmp_path: Path) -> Path:
    return _mapped(tmp_path, "docsite")


def _run(
    capsys: pytest.CaptureFixture[str], *args: str
) -> tuple[int, str, str]:
    code = cli.main(list(args))
    out = capsys.readouterr()
    return code, out.out, out.err


# --- mapping ---------------------------------------------------------------


def test_a_repo_of_only_docs_maps(vault: Path) -> None:
    assert (vault / ".dekko" / "map.json").is_file()


def test_a_repo_with_neither_code_nor_docs_still_fails(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("plain text\n")

    assert cli.main(["map", str(tmp_path), "--quiet"]) == 1


# --- docs --stale ---------------------------------------------------------


def test_stale_lists_broken_wikilinks_and_headings(
    vault: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _run(capsys, "docs", "--stale", "--root", str(vault))

    assert code == 1
    assert "Missing Note  (missing)" in out
    assert "Projects/Alpha#Nope  (no such heading)" in out


def test_stale_lists_broken_routes_and_anchors(
    site: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _run(capsys, "docs", "--stale", "--root", str(site))

    assert code == 1
    assert "/features/gone  (missing)" in out
    assert "../features/rules.mdx#nope  (no such heading)" in out


# --- outline --------------------------------------------------------------


def test_outline_of_a_doc_is_its_heading_tree(
    site: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _run(
        capsys, "outline", "docs/guide/setup.md", "--root", str(site)
    )

    assert code == 0
    lines = out.splitlines()
    assert lines[0] == (
        "outline: docs/guide/setup.md  [markdown]  "
        "2 sections · 1 code link · 2 doc links"
    )
    assert lines[1] == "  1-6  Setup  (2 doc)"
    assert lines[2] == "    7-9  Usage  (1 code)"


def test_outline_finds_a_doc_by_its_name(
    site: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _run(capsys, "outline", "setup.md", "--root", str(site))

    assert code == 0
    assert out.startswith("outline: docs/guide/setup.md")


def test_outline_of_an_ambiguous_doc_name(
    vault: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err = _run(capsys, "outline", "index.md", "--root", str(vault))

    assert code == 4
    assert "a/index.md" in err
    assert "b/index.md" in err


def test_outline_drops_the_deepest_headings_first(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = "# Top\n## A\n### a1\n### a2\n## B\n### b1\n"
    (tmp_path / "guide.md").write_text(text)
    assert cli.main(["map", str(tmp_path), "--quiet"]) == 0

    code, out, _ = _run(
        capsys, "outline", "guide.md", "--limit", "3", "--root", str(tmp_path)
    )

    assert code == 0
    headings = [line.split("  ")[-1].strip() for line in out.splitlines()[1:4]]
    assert headings == ["Top", "A", "B"]
    assert "3 of 6 omitted" in out


def test_outline_of_a_doc_as_json(
    site: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _run(
        capsys, "outline", "docs/guide/setup.md", "--json", "--root", str(site)
    )

    assert code == 0
    doc = json.loads(out)
    [entry] = doc["docs"]
    assert entry["path"] == "docs/guide/setup.md"
    assert entry["kind"] == "markdown"
    assert entry["sections"][1] == {
        "heading": "Usage",
        "level": 2,
        "line": 7,
        "end_line": 9,
        "anchor": "",
        "code_links": 1,
        "doc_links": 0,
    }


def test_outline_of_a_folder_of_docs(
    vault: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _run(capsys, "outline", "Projects", "--root", str(vault))

    assert code == 0
    assert out.splitlines()[0] == "outline: Projects — 0 files, 1 doc"
    assert "outline: Projects/Alpha.md  [markdown]" in out
    assert "Goals" in out


def test_outline_of_a_folder_lists_code_then_docs(
    site: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _run(capsys, "outline", ".", "--root", str(site))

    assert code == 0
    assert out.splitlines()[0] == "outline: . — 1 file, 6 docs"
    assert out.index("outline: app.py") < out.index("outline: README.md")


# --- summary --------------------------------------------------------------


def test_summary_of_a_doc_heavy_repo_has_a_docs_section(
    vault: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _run(capsys, "summary", "--root", str(vault))

    assert code == 0
    lines = out.splitlines()
    start = lines.index("docs: 7 files in 4 dirs (most docs first):")
    assert lines[start + 1 : start + 5] == [
        "     4  ./",
        "     1  Projects/",
        "     1  a/",
        "     1  b/",
    ]
    assert (
        "most linked docs: Home.md (2), Projects/Alpha.md (2), Beta.md (1), "
        "diagram.md (1)"
    ) in lines
    assert "entry docs: a/index.md, b/index.md" in lines
    assert "orphan docs (3): Orphan.md, a/index.md, b/index.md" in lines


def test_summary_of_a_code_repo_leaves_docs_out_unless_asked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for n in range(3):
        (tmp_path / f"m{n}.py").write_text("def f():\n    return 1\n")
    (tmp_path / "README.md").write_text("# R\n")
    assert cli.main(["map", str(tmp_path), "--quiet"]) == 0

    _, plain, _ = _run(capsys, "summary", "--root", str(tmp_path))
    _, asked, _ = _run(capsys, "summary", "--docs", "--root", str(tmp_path))

    assert "docs:" not in plain
    assert "docs: 1 file in 1 dir (most docs first):" in asked


def test_summary_docs_come_before_the_code_rankings(
    site: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, out, _ = _run(capsys, "summary", "--root", str(site))

    assert out.index("docs: 6 files") < out.index("entrypoints:")


def test_summary_tool_takes_a_docs_switch(tmp_path: Path) -> None:
    for n in range(3):
        (tmp_path / f"m{n}.py").write_text("def f():\n    return 1\n")
    (tmp_path / "README.md").write_text("# R\n")
    assert cli.main(["map", str(tmp_path), "--quiet"]) == 0
    ctx = server.Context(default_root=tmp_path, no_regen=False)

    reply = server.handle(
        ctx,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "summary", "arguments": {"docs": True}},
        },
    )

    assert reply["result"]["isError"] is False
    assert "docs: 1 file" in reply["result"]["content"][0]["text"]


# --- communities --docs ---------------------------------------------------


def _two_topics(root: Path, nav: bool = False) -> Path:
    """Two folders of three docs that link among themselves, joined by
    one link."""
    files = {}
    for topic in ("alpha", "beta"):
        names = [f"{topic}/{topic[0]}{n}.md" for n in range(3)]
        for name in names:
            others = [o for o in names if o != name]
            links = " ".join(f"[x](../{o})" for o in others)
            files[name] = f"# {name}\n\n{links}\n"
    files["alpha/a0.md"] += "Also [b0](../beta/b0.md).\n"
    if nav:
        listing = "".join(
            f"- [{n}]({n})\n" for n in sorted(files) if n.endswith(".md")
        )
        files["SUMMARY.md"] = f"# Summary\n\n{listing}"
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    assert cli.main(["map", str(root), "--quiet"]) == 0
    return root


def test_doc_communities_follow_the_links(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _two_topics(tmp_path)

    code, out, _ = _run(capsys, "communities", "--docs", "--root", str(root))

    assert code == 0
    lines = out.splitlines()
    assert lines[0] == (
        "dekko: 2 doc communities over 6 docs (0 unconnected, 0 small groups)"
    )
    assert any(line.startswith("#1 alpha  3 docs, cohesion") for line in lines)
    assert any(line.startswith("#2 beta  3 docs, cohesion") for line in lines)
    assert "   top: alpha/a0.md, alpha/a1.md, alpha/a2.md" in lines


def test_doc_communities_leave_tables_of_contents_out(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _two_topics(tmp_path, nav=True)

    _, out, _ = _run(capsys, "communities", "--docs", "--root", str(root))

    assert "over 6 docs" in out.splitlines()[0]
    assert "SUMMARY.md" not in out


def test_communities_of_one_doc(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _two_topics(tmp_path)

    code, out, _ = _run(
        capsys, "communities", "alpha/a0.md", "--root", str(root)
    )

    assert code == 0
    lines = out.splitlines()
    assert lines[0] == "alpha/a0.md is in:"
    assert lines[1].startswith("#1 alpha  3 docs")
    assert "links to other communities:" in lines
    assert "  #2 beta  1" in lines


def test_doc_communities_as_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _two_topics(tmp_path)

    code, out, _ = _run(
        capsys, "communities", "--docs", "--json", "--root", str(root)
    )

    assert code == 0
    doc = json.loads(out)
    assert doc["view"] == "docs"
    assert [c["label"] for c in doc["communities"]] == ["alpha", "beta"]
    assert doc["communities"][1]["files"] == [
        "beta/b0.md",
        "beta/b1.md",
        "beta/b2.md",
    ]


# --- trace and query docs between docs ------------------------------------


def test_trace_between_docs_follows_links(
    site: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _run(
        capsys,
        "trace",
        "README.md",
        "docs/features/rules.mdx",
        "--root",
        str(site),
    )

    assert code == 0
    assert out.splitlines() == [
        "README.md:3 -> docs/guide/setup.md:3 "
        "-> docs/features/rules.mdx#example-1"
    ]


def _toc_repo(root: Path) -> Path:
    files = {
        "SUMMARY.md": "# Summary\n\n- [a](a.md)\n- [b](b.md)\n- [c](c.md)\n",
        "a.md": "# A\n\nSee the [contents](SUMMARY.md) or [b](b.md).\n",
        "b.md": "# B\n\nNext: [c](c.md).\n",
        "c.md": "# C\n",
    }
    for rel, text in files.items():
        (root / rel).write_text(text)
    assert cli.main(["map", str(root), "--quiet"]) == 0
    return root


def test_trace_never_passes_through_a_table_of_contents(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _toc_repo(tmp_path)

    _, out, _ = _run(capsys, "trace", "a.md", "c.md", "--root", str(root))

    assert out.splitlines() == ["a.md:3 -> b.md:3 -> c.md"]


def test_trace_can_start_at_a_table_of_contents(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _toc_repo(tmp_path)

    _, out, _ = _run(
        capsys, "trace", "SUMMARY.md", "c.md", "--root", str(root)
    )

    assert out.splitlines() == ["SUMMARY.md:5 -> c.md"]


def test_trace_with_no_link_path(
    site: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err = _run(
        capsys,
        "trace",
        "docs/api/index.md",
        "README.md",
        "--root",
        str(site),
    )

    assert code == 1
    assert "no link path from docs/api/index.md to README.md" in err


def test_trace_from_a_doc_to_a_symbol_is_a_usage_error(
    site: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err = _run(
        capsys, "trace", "README.md", "main", "--root", str(site)
    )

    assert code == 2
    assert "both ends must be docs or both symbols" in err


def test_trace_between_docs_as_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _toc_repo(tmp_path)

    _, out, _ = _run(
        capsys, "trace", "a.md", "c.md", "--json", "--root", str(root)
    )

    assert json.loads(out)["paths"] == [
        [
            {"doc": "a.md", "anchor": None, "line": 3},
            {"doc": "b.md", "anchor": None, "line": 3},
            {"doc": "c.md", "anchor": None, "line": None},
        ]
    ]


def test_query_docs_on_a_doc_lists_what_links_to_it(
    site: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _run(
        capsys,
        "query",
        "docs",
        "docs/features/rules.mdx",
        "--root",
        str(site),
    )

    assert code == 0
    assert out.splitlines()[:3] == [
        "docs/SUMMARY.md:1  Summary",
        "docs/guide/setup.md:1  Setup  → #example-1",
        "docs/index.mdx:1  Welcome  → #custom-id",
    ]


def test_outline_keeps_what_fits_of_a_level_too_big_to_show_whole(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = "# Top\n" + "".join(f"## Sec {n}\n### sub {n}\n" for n in range(40))
    (tmp_path / "big.md").write_text(text)
    assert cli.main(["map", str(tmp_path), "--quiet"]) == 0

    _, out, _ = _run(
        capsys, "outline", "big.md", "--limit", "10", "--root", str(tmp_path)
    )

    headings = [
        line.split("  ")[-1].strip() for line in out.splitlines()[1:11]
    ]
    assert headings == ["Top", *(f"Sec {n}" for n in range(9))]
    assert "sub" not in out
