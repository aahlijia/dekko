"""The .lidar incremental cache: creation, reuse, and --full."""

from pathlib import Path

import pytest

from lidar_map import cache as cache_mod
from lidar_map import cli

from conftest import RepoFactory

SRC = {
    "a.py": "def f() -> int:\n    return 1\n",
    "b.py": "def g() -> int:\n    return 2\n",
}


def _count_extractions(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Patch ``extract_one`` to record every file it parses."""
    parsed: list[str] = []
    real = cli.extract_one

    def spy(root: Path, rel: str):  # noqa: ANN202
        parsed.append(rel)
        return real(root, rel)

    monkeypatch.setattr(cli, "extract_one", spy)
    return parsed


def test_cache_created_and_ignored(make_mapped_repo: RepoFactory) -> None:
    root = make_mapped_repo(SRC)
    cache_file = root / cache_mod.CACHE_DIR / cache_mod.CACHE_FILE
    assert cache_file.is_file()
    assert (root / cache_mod.CACHE_DIR / ".gitignore").read_text() == "*\n"
    assert ".lidar/" in (root / ".gitignore").read_text().splitlines()

    entries = cache_mod.load(root)
    assert set(entries) == {"a.py", "b.py"}


def test_unchanged_files_are_reused(
    make_mapped_repo: RepoFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_mapped_repo(SRC)
    parsed = _count_extractions(monkeypatch)

    assert cli.main(["map", str(root), "--quiet"]) == 0
    assert parsed == []  # nothing changed → no re-parsing


def test_only_changed_files_reparse(
    make_mapped_repo: RepoFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_mapped_repo(SRC)
    (root / "a.py").write_text("def f() -> int:\n    return 99\n")
    parsed = _count_extractions(monkeypatch)

    assert cli.main(["map", str(root), "--quiet"]) == 0
    assert parsed == ["a.py"]


def test_full_forces_cold_rebuild(
    make_mapped_repo: RepoFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_mapped_repo(SRC)
    parsed = _count_extractions(monkeypatch)

    assert cli.main(["map", str(root), "--quiet", "--full"]) == 0
    assert sorted(parsed) == ["a.py", "b.py"]


def test_no_json_skips_cache(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text(SRC["a.py"])
    assert cli.main(["map", str(tmp_path), "--quiet", "--no-json"]) == 0
    assert not (tmp_path / cache_mod.CACHE_DIR).exists()


def test_gitignore_entry_not_duplicated(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(SRC)
    cli.main(["map", str(root), "--quiet"])
    lines = (root / ".gitignore").read_text().splitlines()
    assert lines.count(".lidar/") == 1


def test_reused_map_matches_cold_map(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(SRC)
    incremental = (root / "map.json").read_text()
    assert cli.main(["map", str(root), "--quiet", "--full"]) == 0
    # symbols/edges are identical; only the generated_at stamp differs.
    assert '"symbols"' in incremental
    cold = (root / "map.json").read_text()

    def _strip(text: str) -> str:
        return "\n".join(
            ln for ln in text.splitlines() if "generated_at" not in ln
        )

    assert _strip(incremental) == _strip(cold)
