"""Orientation in doc-heavy repos: the CLI over links between docs."""

import json
import shutil
from pathlib import Path

import pytest

from dekko.integrations import cli

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
