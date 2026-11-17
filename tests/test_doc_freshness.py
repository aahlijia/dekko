"""Doc files count toward map freshness, like the code they describe."""

import json
import os
from pathlib import Path

import pytest
from conftest import RepoFactory

from dekko.integrations import cli
from dekko.render import mapfile

SOURCES = {
    "app.py": "def run_app():\n    return 1\n",
    "README.md": "# App\n\nCall `run_app`.\n",
}


def _freshness(root: Path) -> mapfile.Freshness:
    index = mapfile.load_map(root)
    assert index is not None
    return mapfile.check_freshness(root, index)


def _bump(path: Path, text: str) -> None:
    """Rewrite ``path`` with a different size and a later mtime."""
    path.write_text(text)
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))


def test_a_fresh_map_records_doc_hashes(make_mapped_repo: RepoFactory) -> None:
    root = make_mapped_repo(SOURCES)

    prov = mapfile.load_map(root).provenance

    assert set(prov["doc_files"]) == {"README.md"}
    assert set(prov["doc_stat"]) == {"README.md"}
    assert "README.md" not in prov["files"]
    assert _freshness(root).fresh


def test_editing_a_doc_stales_the_map(make_mapped_repo: RepoFactory) -> None:
    root = make_mapped_repo(SOURCES)

    _bump(root / "README.md", "# App\n\nCall `run_app` twice.\n")

    fresh = _freshness(root)
    assert not fresh.fresh
    assert fresh.changed == ["README.md"]


def test_adding_and_removing_docs_stales_the_map(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(SOURCES)

    (root / "GUIDE.md").write_text("# Guide\n")
    assert _freshness(root).added == ["GUIDE.md"]

    (root / "GUIDE.md").unlink()
    (root / "README.md").unlink()
    assert _freshness(root).removed == ["README.md"]


def test_an_excluded_doc_never_stales_the_map(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(SOURCES)
    assert (
        cli.main(["map", str(root), "--quiet", "--exclude", "drafts/*"]) == 0
    )

    (root / "drafts").mkdir()
    (root / "drafts" / "plan.md").write_text("# Plan\n")

    assert _freshness(root).fresh


def test_a_map_without_doc_hashes_is_stale_once(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(SOURCES)
    for name in ("map.json", "provenance.json"):
        path = root / ".dekko" / name
        if not path.exists():
            continue
        doc = json.loads(path.read_text())
        prov = doc.get("provenance", doc)
        prov.pop("doc_files", None)
        prov.pop("doc_stat", None)
        path.write_text(json.dumps(doc))

    assert _freshness(root).added == ["README.md"]

    assert cli.main(["map", str(root), "--quiet"]) == 0
    assert _freshness(root).fresh


def test_a_doc_only_edit_is_not_an_unchanged_map_run(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_mapped_repo(SOURCES)
    _bump(root / "README.md", "# App\n\nNow `run_app` and more.\n")

    assert cli.main(["map", str(root)]) == 0

    assert "nothing written" not in capsys.readouterr().out
    index = mapfile.load_map(root)
    assert index.doc_links[0].text == "run_app"
    assert _freshness(root).fresh
