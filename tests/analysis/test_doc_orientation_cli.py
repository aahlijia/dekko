"""Orientation in doc-heavy repos: the CLI over links between docs."""

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
