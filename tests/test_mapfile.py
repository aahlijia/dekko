"""map.json round-trip, provenance, and freshness checks."""

import json
from pathlib import Path

from dekko import mapfile

from conftest import RepoFactory

CHAIN = {
    "a.py": (
        "def helper(x: int) -> int:\n"
        "    return x + 1\n"
        "\n"
        "\n"
        "def main() -> None:\n"
        "    helper(1)\n"
    )
}


def test_load_round_trip(make_mapped_repo: RepoFactory) -> None:
    root = make_mapped_repo(CHAIN)
    index = mapfile.load_map(root)
    assert index is not None
    helper = index.symbols_by_qualname["helper"][0]
    assert helper.path == "a.py"
    assert helper.params[0].type == "int"
    main_id = index.symbols_by_qualname["main"][0].id
    assert helper.id in index.calls_out[main_id]
    assert main_id in index.calls_in[helper.id]


def test_provenance_written(make_mapped_repo: RepoFactory) -> None:
    root = make_mapped_repo(CHAIN)
    doc = json.loads((root / ".dekko" / "map.json").read_text())
    assert doc["version"] == 3
    prov = doc["provenance"]
    assert prov["tool_version"]
    assert set(prov["files"]) == {"a.py"}


def test_freshness_transitions(make_mapped_repo: RepoFactory) -> None:
    root = make_mapped_repo(CHAIN)
    index = mapfile.load_map(root)
    assert mapfile.check_freshness(root, index).fresh

    (root / "a.py").write_text(CHAIN["a.py"] + "\n\nX = 1\n")
    fresh = mapfile.check_freshness(root, index)
    assert not fresh.fresh
    assert fresh.changed == ["a.py"]

    (root / "b.py").write_text("def extra() -> None:\n    pass\n")
    fresh = mapfile.check_freshness(root, index)
    assert fresh.added == ["b.py"]


def test_removed_file_detected(make_mapped_repo: RepoFactory) -> None:
    root = make_mapped_repo(
        dict(CHAIN, **{"b.py": "def extra() -> None:\n    pass\n"})
    )
    index = mapfile.load_map(root)
    (root / "b.py").unlink()
    fresh = mapfile.check_freshness(root, index)
    assert not fresh.fresh
    assert fresh.removed == ["b.py"]


def test_v1_map_is_always_stale(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(CHAIN)
    doc = json.loads((root / ".dekko" / "map.json").read_text())
    doc["version"] = 1
    del doc["provenance"]
    (root / ".dekko" / "map.json").write_text(json.dumps(doc))

    index = mapfile.load_map(root)
    assert index is not None
    assert not mapfile.check_freshness(root, index).fresh


def test_missing_map_loads_none(tmp_path: Path) -> None:
    assert mapfile.load_map(tmp_path) is None
