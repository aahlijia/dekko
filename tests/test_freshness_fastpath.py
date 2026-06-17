"""The mtime/size fast path in check_freshness skips redundant hashing."""

from pathlib import Path

import pytest

from dekko import mapfile

from conftest import RepoFactory

SRC = {
    "a.py": "def f() -> int:\n    return 1\n",
    "b.py": "def g() -> int:\n    return 2\n",
}


def _count_hashes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every path that gets content-hashed during a check."""
    hashed: list[str] = []
    real = mapfile._file_hash

    def spy(path: Path) -> str:
        hashed.append(path.name)
        return real(path)

    monkeypatch.setattr(mapfile, "_file_hash", spy)
    return hashed


def test_unchanged_tree_hashes_nothing(
    make_mapped_repo: RepoFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_mapped_repo(SRC)
    index = mapfile.load_map(root)
    assert index is not None

    hashed = _count_hashes(monkeypatch)
    fresh = mapfile.check_freshness(root, index)
    assert fresh.fresh
    assert hashed == []  # every file matched on (mtime, size)


def test_only_touched_file_is_hashed(
    make_mapped_repo: RepoFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_mapped_repo(SRC)
    (root / "a.py").write_text("def f() -> int:\n    return 99\n")
    index = mapfile.load_map(root)
    assert index is not None

    hashed = _count_hashes(monkeypatch)
    fresh = mapfile.check_freshness(root, index)
    assert not fresh.fresh
    assert fresh.changed == ["a.py"]
    assert hashed == ["a.py"]  # b.py's stat was unchanged → not hashed


def test_legacy_map_without_stat_hashes_all(
    make_mapped_repo: RepoFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_mapped_repo(SRC)
    index = mapfile.load_map(root)
    assert index is not None
    # Simulate a pre-fast-path map: drop the recorded stat signatures.
    index.provenance.pop("stat", None)

    hashed = _count_hashes(monkeypatch)
    fresh = mapfile.check_freshness(root, index)
    assert fresh.fresh
    assert sorted(hashed) == ["a.py", "b.py"]  # full fallback


def test_provenance_records_stat(make_mapped_repo: RepoFactory) -> None:
    import json

    root = make_mapped_repo(SRC)
    doc = json.loads((root / ".dekko" / "map.json").read_text())
    stat = doc["provenance"]["stat"]
    assert set(stat) == {"a.py", "b.py"}
    assert all(len(sig) == 2 for sig in stat.values())
