"""File discovery tests."""

from pathlib import Path

from dekko.walker import discover


def _touch(path: Path, content: str = "x = 1\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_discover_filters(tmp_path: Path) -> None:
    _touch(tmp_path / "src" / "app.py")
    _touch(tmp_path / "src" / "core.rs")
    _touch(tmp_path / "node_modules" / "pkg" / "x.py")
    _touch(tmp_path / "notes.txt")
    _touch(tmp_path / "gen" / "schema_pb2.py")
    _touch(tmp_path / "big.py", "x = 1\n" * 100)

    files, skipped = discover(tmp_path, max_file_size=50)
    assert files == ["src/app.py", "src/core.rs"]
    reasons = dict(skipped)
    assert reasons["gen/schema_pb2.py"] == "generated"
    assert reasons["big.py"] == "too large"


def test_discover_reports_known_unsupported_language(tmp_path: Path) -> None:
    # .astro has no parser at all (not even a Tier-2 attempt): unlike
    # ordinary non-code files (notes.txt), it must be surfaced in
    # ``skipped`` rather than silently dropped, so a caller can warn
    # that map coverage is incomplete (2026-07-31 eval, gitaustin repo:
    # a whole Astro site was mapped with zero warning).
    _touch(tmp_path / "src" / "app.py")
    _touch(tmp_path / "src" / "Card.astro", "---\nconst x = 1;\n---\n")
    _touch(tmp_path / "notes.txt")

    files, skipped = discover(tmp_path)
    assert files == ["src/app.py"]
    reasons = dict(skipped)
    assert reasons["src/Card.astro"] == "no parser (astro)"
    assert "notes.txt" not in reasons


def test_discover_unsupported_language_respects_subpath(
    tmp_path: Path,
) -> None:
    _touch(tmp_path / "src" / "app.py")
    _touch(tmp_path / "other" / "Card.astro", "---\n---\n")

    files, skipped = discover(tmp_path, subpath="src")
    assert files == ["src/app.py"]
    assert not any(path == "other/Card.astro" for path, _ in skipped)


def test_discover_unsupported_language_respects_excludes(
    tmp_path: Path,
) -> None:
    _touch(tmp_path / "src" / "app.py")
    _touch(tmp_path / "src" / "Card.astro", "---\n---\n")

    files, skipped = discover(tmp_path, excludes=("*.astro",))
    assert files == ["src/app.py"]
    reasons = dict(skipped)
    assert reasons["src/Card.astro"] == "excluded"


def test_discover_subpath_and_excludes(tmp_path: Path) -> None:
    _touch(tmp_path / "src" / "app.py")
    _touch(tmp_path / "src" / "skip_me.py")
    _touch(tmp_path / "other" / "b.py")

    files, skipped = discover(
        tmp_path,
        subpath="src",
        excludes=("skip_*.py",),
    )
    assert files == ["src/app.py"]
    assert ("src/skip_me.py", "excluded") in skipped
