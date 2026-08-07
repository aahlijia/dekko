"""File discovery tests."""

import subprocess
from pathlib import Path

import pytest

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


def _write_dekkoignore(root: Path, text: str) -> None:
    ignore_dir = root / ".dekko"
    ignore_dir.mkdir(parents=True, exist_ok=True)
    (ignore_dir / ".dekkoignore").write_text(text)


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True
    )


def _init_git_repo(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(
        root,
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "commit",
        "-q",
        "-m",
        "base",
    )


def test_discover_dekkoignore_present_uses_ignored_reason(
    tmp_path: Path,
) -> None:
    _touch(tmp_path / "src" / "app.py")
    _touch(tmp_path / "src" / "widget.astro", "---\n---\n")
    _write_dekkoignore(tmp_path, "*.astro\n")

    files, skipped = discover(tmp_path)
    assert files == ["src/app.py"]
    reasons = dict(skipped)
    assert reasons["src/widget.astro"] == "ignored"
    assert reasons["src/widget.astro"] != "excluded"


def test_discover_no_dekkoignore_unchanged_behavior(tmp_path: Path) -> None:
    # Regression guard: no .dekko/.dekkoignore present at all — the new
    # code path must be a no-op.
    _touch(tmp_path / "src" / "app.py")
    _touch(tmp_path / "src" / "core.rs")

    files, skipped = discover(tmp_path)
    assert files == ["src/app.py", "src/core.rs"]
    assert skipped == []


@pytest.mark.parametrize("pattern", ["gen/", "gen/*"])
def test_discover_dekkoignore_directory_patterns_prune_nested(
    tmp_path: Path, pattern: str
) -> None:
    _touch(tmp_path / "src" / "app.py")
    _touch(tmp_path / "gen" / "out.py")
    _touch(tmp_path / "gen" / "sub" / "nested.py")
    _write_dekkoignore(tmp_path, pattern + "\n")

    files, skipped = discover(tmp_path)
    assert files == ["src/app.py"]
    reasons = dict(skipped)
    assert reasons["gen/out.py"] == "ignored"
    assert reasons["gen/sub/nested.py"] == "ignored"


def test_exclude_vs_dekkoignore_diverge_on_extension_filtered_dirs(
    tmp_path: Path,
) -> None:
    # Pins the documented fnmatch/gitwildmatch divergence: a bare
    # --exclude 'gen/*.py' reaches into a nested file today (fnmatch
    # is not slash-aware), but the identical string written to
    # .dekkoignore and parsed with gitwildmatch only prunes the direct
    # child, not the nested file — because the wildcard doesn't also
    # match (and thus prune) the intermediate subdirectory.
    _touch(tmp_path / "gen" / "out.py")
    _touch(tmp_path / "gen" / "sub" / "nested.py")

    files, skipped = discover(tmp_path, excludes=("gen/*.py",))
    reasons = dict(skipped)
    assert reasons["gen/out.py"] == "excluded"
    assert reasons["gen/sub/nested.py"] == "excluded"

    _write_dekkoignore(tmp_path, "gen/*.py\n")
    files, skipped = discover(tmp_path)
    reasons = dict(skipped)
    assert reasons["gen/out.py"] == "ignored"
    assert "gen/sub/nested.py" not in reasons
    assert files == ["gen/sub/nested.py"]


def test_discover_dekkoignore_negation(tmp_path: Path) -> None:
    _touch(tmp_path / "gen" / "out.py")
    _touch(tmp_path / "gen" / "keep.py")
    _write_dekkoignore(tmp_path, "gen/*\n!gen/keep.py\n")

    files, skipped = discover(tmp_path)
    assert files == ["gen/keep.py"]
    reasons = dict(skipped)
    assert reasons["gen/out.py"] == "ignored"


def test_discover_dekkoignore_parity_git_and_walk(tmp_path: Path) -> None:
    # The .dekkoignore filter must apply identically whether discovery
    # went through `git ls-files` or the manual os.walk fallback — the
    # one thing this design guarantees isn't asymmetric between the two
    # candidate sources (unlike the root .gitignore handling).
    _touch(tmp_path / "src" / "app.py")
    _touch(tmp_path / "src" / "widget.astro", "---\n---\n")
    _write_dekkoignore(tmp_path, "*.astro\n")

    files_no_git, skipped_no_git = discover(tmp_path)

    _init_git_repo(tmp_path)
    files_git, skipped_git = discover(tmp_path)

    assert files_no_git == files_git == ["src/app.py"]
    assert dict(skipped_no_git)["src/widget.astro"] == "ignored"
    assert dict(skipped_git)["src/widget.astro"] == "ignored"


def test_discover_vendored_dir_reason_parity_git_and_walk(
    tmp_path: Path,
) -> None:
    # Regression guard: the manual os.walk fallback (used when there's
    # no .git/) must not prune vendored dirs out of the walk before
    # _classify ever sees them — that would silently reintroduce the
    # "no signal at all" gap for any non-git checkout, defeating the
    # whole point of the vendored-dir coverage note.
    _touch(tmp_path / "src" / "app.py")
    _touch(tmp_path / "third_party" / "xla" / "lib.py")

    files_no_git, skipped_no_git = discover(tmp_path)
    assert files_no_git == ["src/app.py"]
    assert (
        dict(skipped_no_git)["third_party/xla/lib.py"]
        == "vendored (third_party)"
    )

    _init_git_repo(tmp_path)
    files_git, skipped_git = discover(tmp_path)
    assert files_git == ["src/app.py"]
    assert (
        dict(skipped_git)["third_party/xla/lib.py"] == "vendored (third_party)"
    )


def test_discover_reports_vendored_directory_with_reason(
    tmp_path: Path,
) -> None:
    # Track E / 1.5: a default-excluded dir that occasionally holds
    # first-party code (tensorflow's third_party/xla is the motivating
    # case) must be recorded with a distinguishable reason, not
    # silently dropped like a VCS/cache dir.
    _touch(tmp_path / "src" / "app.py")
    _touch(tmp_path / "third_party" / "xla" / "lib.py")
    _touch(tmp_path / "vendor" / "pkg" / "mod.go", "package pkg\n")

    files, skipped = discover(tmp_path)
    assert files == ["src/app.py"]
    reasons = dict(skipped)
    assert reasons["third_party/xla/lib.py"] == "vendored (third_party)"
    assert reasons["vendor/pkg/mod.go"] == "vendored (vendor)"


def test_discover_noise_dirs_stay_silent_not_vendored(
    tmp_path: Path,
) -> None:
    # VCS metadata / tool caches are genuinely never worth a coverage
    # note (a warning about .git/ would be pure noise) — distinct from
    # the vendored-dir case above, which does get recorded.
    _touch(tmp_path / "src" / "app.py")
    _touch(tmp_path / ".git" / "objects" / "pack.py")
    _touch(tmp_path / "__pycache__" / "app.cpython-311.pyc.py")

    files, skipped = discover(tmp_path)
    assert files == ["src/app.py"]
    reasons = dict(skipped)
    assert ".git/objects/pack.py" not in reasons
    assert "__pycache__/app.cpython-311.pyc.py" not in reasons


def test_discover_flags_minified_content_without_min_filename(
    tmp_path: Path,
) -> None:
    # Track E / 3.4a: zed's junk suggestions came from a vendored
    # book.js/highlight.js bundle with no ".min." filename convention
    # — filename-pattern matching alone misses it, so a content-based
    # average-line-length heuristic must catch it.
    _touch(tmp_path / "src" / "app.py")
    minified = "x" * 5000 + "\n"
    _touch(tmp_path / "docs" / "theme" / "book.js", minified * 5)
    normal_long_lines = "\n".join(f"const line_{i} = {i};" for i in range(60))
    _touch(tmp_path / "src" / "generated_but_normal.js", normal_long_lines)

    files, skipped = discover(tmp_path)
    assert "src/app.py" in files
    assert "src/generated_but_normal.js" in files
    reasons = dict(skipped)
    assert reasons["docs/theme/book.js"] == "generated"


def test_discover_minified_check_ignores_unreadable_file(
    tmp_path: Path,
) -> None:
    # A file that can't be read for the heuristic (e.g. removed
    # between stat and read, or binary content that still passes
    # every other gate) must fail safe: never override another gate's
    # verdict by raising, only ever add a skip when the check succeeds.
    path = tmp_path / "weird.py"
    path.write_bytes(b"\xff\xfe" * 10)

    files, _skipped = discover(tmp_path)
    # Short garbled content decodes fine under errors="replace" and
    # falls well under the average-line-length threshold, so the file
    # still maps — the point of this test is that discover() never
    # raises on it.
    assert files == ["weird.py"]
