"""``dekko summary`` lists the largest communities; MAP.md does not."""

import json
from pathlib import Path

import pytest
from conftest import RepoFactory

from dekko.integrations import cli


def _chain_package(name: str) -> dict[str, str]:
    """Three files in ``name/`` calling down a chain."""
    return {
        f"{name}/__init__.py": "",
        f"{name}/a.py": (
            f"from {name}.b import b\n\n\ndef a():\n    b()\n    b()\n"
        ),
        f"{name}/b.py": (
            f"from {name}.c import c\n\n\ndef b():\n    c()\n    c()\n"
        ),
        f"{name}/c.py": "def c():\n    return 1\n",
    }


@pytest.fixture
def root(make_mapped_repo: RepoFactory) -> Path:
    sources: dict[str, str] = {}
    for i in range(9):
        sources.update(_chain_package(f"pkg{i}"))
    return make_mapped_repo(sources)


def test_summary_lists_at_most_eight_communities(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["summary", "--root", str(root), "--budget", "0"]) == 0
    lines = capsys.readouterr().out.splitlines()

    start = lines.index("communities (largest first):")
    rows = []
    for line in lines[start + 1 :]:
        if not line.startswith("  #"):
            break
        rows.append(line)
    assert len(rows) == 8
    assert rows[0].startswith("  #1 pkg")
    assert rows[0].endswith("3 files, cohesion 1.00")
    assert lines.index("directories (files/symbols, int+cross edges):") < start


def test_summary_json_has_community_rows(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["summary", "--root", str(root), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)

    assert len(doc["communities"]) == 8
    assert set(doc["communities"][0]) == {"id", "label", "files", "cohesion"}


def test_summary_leaves_the_section_out_for_an_old_map(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = root / ".dekko" / "map.json"
    doc = json.loads(path.read_text())
    del doc["communities"]
    path.write_text(json.dumps(doc))

    assert cli.main(["summary", "--root", str(root), "--no-regen"]) == 0

    assert "communities (largest first)" not in capsys.readouterr().out


def test_map_md_has_no_communities_section(root: Path) -> None:
    text = (root / ".dekko" / "MAP.md").read_text()

    assert "communities (largest first)" not in text
