"""Doc queries: which docs name a symbol, what a doc refers to, rot."""

import json
import shutil
from pathlib import Path

import pytest

from dekko.integrations import cli

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "docs"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    shutil.copytree(FIXTURE, root)
    assert cli.main(["map", str(root), "--quiet"]) == 0
    return root


def _run(
    capsys: pytest.CaptureFixture[str], *args: str
) -> tuple[int, str, str]:
    code = cli.main(list(args))
    out = capsys.readouterr()
    return code, out.out, out.err


# --- query docs -----------------------------------------------------------


def test_docs_for_a_symbol(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _run(
        capsys, "query", "docs", "MapIndex.load", "--root", str(repo)
    )

    assert code == 0
    assert "README.md:1  App  [EXTRACTED]" in out
    assert "docs/guide.rst:5  Loading  [EXTRACTED]" in out
    assert "Building" not in out


def test_docs_rank_extracted_before_inferred(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, out, _ = _run(capsys, "query", "docs", "discover", "--root", str(repo))

    rows = [line for line in out.splitlines() if "  [" in line]
    assert rows[0].startswith("README.md:5  Layout  [EXTRACTED]")
    assert any("[INFERRED]" in r for r in rows[1:])


def test_docs_list_an_ambiguous_mention(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, out, _ = _run(
        capsys,
        "query",
        "docs",
        "src/app/other.py:Config.save",
        "--root",
        str(repo),
    )

    assert "README.md:5  Layout  [AMBIGUOUS]" in out
    assert "1 of 2" in out


def test_docs_for_a_file(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _run(
        capsys, "query", "docs", "src/app/walker.py", "--root", str(repo)
    )

    assert code == 0
    assert "README.md:5  Layout" in out
    assert "CHANGELOG.md:3  0.2.0" in out
    assert out.index("README.md") < out.index("CHANGELOG.md")


def test_docs_json(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _, out, _ = _run(
        capsys, "query", "docs", "MapIndex.load", "--root", str(repo), "--json"
    )

    doc = json.loads(out)
    assert doc["target"] == "src/app/index.py::MapIndex.load"
    first = doc["sections"][0]
    assert set(first) >= {"path", "line", "heading", "tier", "refs"}


def test_docs_without_tests_drops_test_docs(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, with_tests, _ = _run(
        capsys, "query", "docs", "tests/test_walker.py", "--root", str(repo)
    )
    code, _, err = _run(
        capsys,
        "query",
        "docs",
        "tests/test_walker.py",
        "--root",
        str(repo),
        "--no-tests",
    )

    assert "tests/README.md" in with_tests
    assert code != 0 or "no doc section" in err


def test_docs_for_a_symbol_no_doc_names(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = _run(
        capsys, "query", "docs", "helper_only_in_code", "--root", str(repo)
    )

    assert code == 0
    assert "no doc section names" in err
    assert "[" not in out


def test_docs_unknown_target(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, _ = _run(
        capsys, "query", "docs", "NoSuchThing", "--root", str(repo)
    )

    assert code == 3


def test_docs_on_a_map_without_a_doc_index(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = repo / ".dekko" / "map.json"
    doc = json.loads(path.read_text())
    for key in ("doc_sections", "doc_links", "doc_dangling"):
        del doc[key]
    path.write_text(json.dumps(doc))

    code, _, err = _run(
        capsys, "query", "docs", "discover", "--root", str(repo), "--no-regen"
    )

    assert code == 1
    assert "predates the doc index" in err


def test_docs_budget_trims_rows(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, out, _ = _run(
        capsys,
        "query",
        "docs",
        "discover",
        "--root",
        str(repo),
        "--limit",
        "1",
    )

    assert len([line for line in out.splitlines() if "  [" in line]) == 1


# --- query refs -----------------------------------------------------------


def test_refs_of_a_doc(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = _run(
        capsys, "query", "refs", "README.md", "--root", str(repo)
    )

    assert code == 0
    assert "App (README.md:1)" in out
    assert (
        "MapIndex.load -> src/app/index.py::MapIndex.load  [EXTRACTED]" in out
    )
    assert "Config.save -> ?" in out
    assert "src/app/index.py::Config.save" in out


def test_refs_of_one_section_by_heading_or_slug(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, by_text, _ = _run(
        capsys, "query", "refs", "docs/guide.rst#building", "--root", str(repo)
    )
    _, by_slug, _ = _run(
        capsys, "query", "refs", "guide.rst#Building", "--root", str(repo)
    )

    assert "MapIndex.build" in by_text
    assert "MapIndex.load" not in by_text
    assert by_text == by_slug


def test_refs_show_stale_references(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, out, _ = _run(
        capsys, "query", "refs", "CHANGELOG.md", "--root", str(repo)
    )

    assert "stale: src/app/old_walker.py (missing)" in out


def test_refs_of_an_unknown_doc(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err = _run(
        capsys, "query", "refs", "READ.md", "--root", str(repo)
    )

    assert code == 3
    assert "not an indexed doc" in err
    assert "README.md" in err


# --- docs --stale ---------------------------------------------------------


def _plant(root: Path) -> None:
    (root / "docs" / "rot.md").write_text(
        "# Rot\n\n"
        "See [gone](../src/app/gone.py) and `src/app/walker.py:400`.\n"
        "Call `MapIndex.lod` or [MapIndex.build](../src/app/index.py#L8).\n"
    )
    assert cli.main(["map", str(root), "--quiet"]) == 0


def test_stale_is_silent_on_a_clean_tree(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = _run(capsys, "docs", "--stale", "--root", str(repo))

    assert code == 0
    assert out == ""
    assert "1 in changelogs" in err


def test_stale_reports_each_kind_of_rot(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _plant(repo)

    code, out, _ = _run(capsys, "docs", "--stale", "--root", str(repo))

    assert code == 1
    assert "docs/rot.md" in out
    assert "../src/app/gone.py  (missing)" in out
    assert "src/app/walker.py:400  (past end of file)" in out
    assert "MapIndex.lod  (no such member)" in out
    assert "../src/app/index.py:8  (moved: now src/app/index.py:10)" in out
    assert "CHANGELOG" not in out


def test_stale_all_includes_changelogs(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _run(
        capsys, "docs", "--stale", "--all", "--root", str(repo)
    )

    assert code == 1
    assert "CHANGELOG.md" in out
    assert "src/app/old_walker.py  (missing)" in out


def test_stale_narrows_to_paths(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _plant(repo)

    code, out, _ = _run(
        capsys, "docs", "--stale", "README.md", "--root", str(repo)
    )

    assert code == 0
    assert out == ""


def test_stale_json(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _plant(repo)

    code, out, _ = _run(
        capsys, "docs", "--stale", "--json", "--root", str(repo)
    )

    doc = json.loads(out)
    assert code == 1
    assert doc["count"] == 4
    assert {r["why"] for r in doc["stale"]} == {
        "missing",
        "past-end",
        "no-member",
        "moved",
    }
    assert {"path", "line", "heading", "text", "why", "now"} <= set(
        doc["stale"][0]
    )


def test_docs_needs_an_action(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err = _run(capsys, "docs", "--root", str(repo))

    assert code == 2
    assert "--stale" in err


def test_docs_is_routed_through_the_daemon() -> None:
    from dekko.daemon import daemon

    assert "docs" in daemon._DAEMON_ELIGIBLE


# --- docs: lines in symbol cards and context packs --------------------------


def test_symbol_card_lists_doc_sections(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, out, _ = _run(
        capsys, "query", "symbol", "MapIndex.load", "--root", str(repo)
    )

    assert "  docs: README.md:1 App, docs/guide.rst:5 Loading" in out


def test_symbol_card_docs_in_json(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, out, _ = _run(
        capsys,
        "query",
        "symbol",
        "MapIndex.load",
        "--root",
        str(repo),
        "--json",
    )

    docs = json.loads(out)["docs"]
    assert docs[0] == {
        "path": "README.md",
        "line": 1,
        "heading": "App",
        "tier": "EXTRACTED",
    }


def test_symbol_card_has_no_docs_line_without_docs(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, out, _ = _run(
        capsys, "query", "symbol", "helper_only_in_code", "--root", str(repo)
    )

    assert "docs:" not in out


def test_symbol_card_caps_doc_sections(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    extra = "".join(f"\n## Part {i}\n\nUse `discover`.\n" for i in range(4))
    (repo / "docs" / "more.md").write_text("# More\n" + extra)
    assert cli.main(["map", str(repo), "--quiet"]) == 0

    _, out, _ = _run(
        capsys, "query", "symbol", "discover", "--root", str(repo)
    )

    (line,) = [x for x in out.splitlines() if x.startswith("  docs:")]
    assert line.count(",") == 2
    assert line.endswith("more")


def test_context_pack_lists_doc_sections(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, out, _ = _run(capsys, "context", "MapIndex.load", "--root", str(repo))

    assert "  docs: README.md:1 App, docs/guide.rst:5 Loading" in out


def test_context_pack_docs_in_json(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, out, _ = _run(
        capsys, "context", "MapIndex.load", "--root", str(repo), "--json"
    )

    assert json.loads(out)["docs"] == [
        "README.md:1 App",
        "docs/guide.rst:5 Loading",
    ]


def test_a_tight_budget_drops_docs_before_neighbors(repo: Path) -> None:
    from dekko.analysis import contextpack
    from dekko.render.mapfile import load_map

    index = load_map(repo)
    (sym,) = index.symbols_by_qualname["MapIndex.load"]
    pack = contextpack.build_pack(index, sym, hops=1, all_imports=True)
    pack.entries = [
        contextpack.PackEntry(
            index.symbols_by_qualname["discover"][0], 1, "caller"
        )
    ]
    docs, pack.docs = pack.docs, []
    budget = contextpack._estimate_tokens(pack)
    pack.docs = docs

    contextpack.trim_to_budget(index, pack, budget)

    assert pack.docs == []
    assert len(pack.entries) == 1


def test_workset_pack_lists_doc_sections(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, out, _ = _run(
        capsys, "workset", "--symbol", "MapIndex.load", "--root", str(repo)
    )

    assert "docs: README.md:1 App" in out


def test_mcp_query_symbol_carries_docs(repo: Path) -> None:
    from dekko.integrations import server

    ctx = server.Context(default_root=repo, no_regen=False)
    text = server._relation_tool(ctx, "symbol", {"symbol": "MapIndex.load"})

    assert "docs: README.md:1 App" in text
