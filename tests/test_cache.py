"""The .dekko incremental cache: creation, reuse, and --full."""

from pathlib import Path

import pytest

from dekko import cache as cache_mod
from dekko import cli

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
    inner = (root / cache_mod.CACHE_DIR / ".gitignore").read_text()
    # Generated files ignored; the ignore file and notes are tracked.
    assert inner.splitlines() == ["*", "!.gitignore", "!notes.json"]
    # The repo .gitignore is intentionally not touched (a blanket
    # .dekko/ there would make notes.json impossible to track).
    assert not (root / ".gitignore").exists()

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


def test_version_change_invalidates_cache(
    make_mapped_repo: RepoFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_mapped_repo(SRC)
    # Simulate upgrading dekko: the on-disk cache was written by an
    # older version, so every file must re-parse (extractor logic may
    # have changed).
    monkeypatch.setattr(cache_mod, "_tool_version", lambda: "0.0.0-test")
    assert cache_mod.load(root) == {}

    parsed = _count_extractions(monkeypatch)
    assert cli.main(["map", str(root), "--quiet"]) == 0
    assert sorted(parsed) == ["a.py", "b.py"]


def test_parallel_extraction_matches_sequential(
    make_mapped_repo: RepoFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Force the process-pool path on a small repo, then confirm the
    # output is byte-identical to a sequential cold rebuild.
    root = make_mapped_repo(SRC)
    monkeypatch.setattr(cli, "_PARALLEL_MIN", 1)

    assert (
        cli.main(["map", str(root), "--quiet", "--full", "--jobs", "2"]) == 0
    )
    parallel = (root / ".dekko" / "map.json").read_text()
    parallel_md = (root / ".dekko" / "MAP.md").read_text()

    assert (
        cli.main(["map", str(root), "--quiet", "--full", "--jobs", "1"]) == 0
    )
    sequential = (root / ".dekko" / "map.json").read_text()

    def _strip(text: str) -> str:
        return "\n".join(
            ln for ln in text.splitlines() if "generated_at" not in ln
        )

    assert _strip(parallel) == _strip(sequential)

    # The trust line carries wall-clock timing, which differs run to
    # run; strip it before comparing the structural MAP.md output.
    def _strip_md(text: str) -> str:
        return "\n".join(
            ln for ln in text.splitlines() if not ln.startswith("*Mapped ")
        )

    assert _strip_md(parallel_md) == _strip_md(
        (root / ".dekko" / "MAP.md").read_text()
    )


def test_no_json_skips_cache(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text(SRC["a.py"])
    assert cli.main(["map", str(tmp_path), "--quiet", "--no-json"]) == 0
    assert not (tmp_path / cache_mod.CACHE_DIR / cache_mod.CACHE_FILE).exists()


def test_map_run_leaves_repo_gitignore_untouched(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(SRC)
    (root / ".gitignore").write_text("node_modules/\n")
    cli.main(["map", str(root), "--quiet"])
    # The repo .gitignore is never modified by a map run.
    assert (root / ".gitignore").read_text() == "node_modules/\n"


def test_existing_dekko_dir_leaves_inner_gitignore_untouched(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(SRC)
    # Remove the inner ignore but keep the .dekko/ directory.
    (root / cache_mod.CACHE_DIR / ".gitignore").unlink()

    assert cli.main(["map", str(root), "--quiet"]) == 0

    # .dekko/ already existed, so a map run does not re-create it.
    assert not (root / cache_mod.CACHE_DIR / ".gitignore").exists()


def test_ensure_notes_tracked_migrates_legacy_ignore(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(SRC)
    inner = root / cache_mod.CACHE_DIR / ".gitignore"
    inner.write_text("*\n")  # legacy pre-notes form

    cache_mod.ensure_notes_tracked(root)

    assert inner.read_text().splitlines() == [
        "*",
        "!.gitignore",
        "!notes.json",
    ]


def test_ensure_notes_tracked_keeps_custom_ignore(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(SRC)
    inner = root / cache_mod.CACHE_DIR / ".gitignore"
    inner.write_text("*\n!custom\n")  # user-customized

    cache_mod.ensure_notes_tracked(root)

    assert inner.read_text() == "*\n!custom\n"


def test_reused_map_matches_cold_map(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(SRC)
    incremental = (root / ".dekko" / "map.json").read_text()
    assert cli.main(["map", str(root), "--quiet", "--full"]) == 0
    # symbols/edges are identical; only the generated_at stamp differs.
    assert '"symbols"' in incremental
    cold = (root / ".dekko" / "map.json").read_text()

    def _strip(text: str) -> str:
        return "\n".join(
            ln for ln in text.splitlines() if "generated_at" not in ln
        )

    assert _strip(incremental) == _strip(cold)
