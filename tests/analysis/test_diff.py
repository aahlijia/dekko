"""The diff subcommand: added/removed/changed symbols and exit codes."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from dekko.integrations import cli
from dekko.analysis import diff
from dekko.render import mapfile

BASE = {
    "a.py": "def f() -> int:\n    return 1\n",
    "b.py": "from a import f\n\n\ndef g() -> int:\n    return f()\n",
}


def _git(root: Path, *args: str) -> None:
    """Run a git command in ``root``, raising on failure."""
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
    )


def _commit_all(root: Path, message: str) -> None:
    """Stage and commit everything currently in the tree."""
    _git(root, "add", "-A")
    _git(
        root,
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "commit",
        "-m",
        message,
    )


def _repo(root: Path, files: dict[str, str]) -> Path:
    """Create a committed git repo and map it."""
    _git(root, "init", "-q")
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    _commit_all(root, "base")
    assert cli.main(["map", str(root), "--quiet"]) == 0
    return root


def test_diff_clean_tree_is_empty(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    root = _repo(tmp_path, BASE)
    assert cli.main(["diff", "--root", str(root)]) == 0
    assert "no symbol changes" in capsys.readouterr().out


def test_diff_detects_added_removed_changed(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    root = _repo(tmp_path, BASE)
    # change f, add h, remove g (by replacing b.py's body)
    (root / "a.py").write_text("def f() -> int:\n    return 2\n")
    (root / "c.py").write_text("def h() -> int:\n    return 3\n")
    (root / "b.py").write_text("X = 1\n")

    assert cli.main(["diff", "--root", str(root)]) == 1
    out = capsys.readouterr().out
    assert "1 changed, 1 added, 1 removed" in out
    assert "~ a.py:1" in out  # f changed
    assert "+ c.py:1" in out  # h added
    assert "- b.py:4" in out  # g removed


def test_diff_reports_impacted_callers(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    root = _repo(tmp_path, BASE)
    (root / "a.py").write_text("def f() -> int:\n    return 42\n")

    assert cli.main(["diff", "--root", str(root)]) == 1
    out = capsys.readouterr().out
    assert "called by: b.py:4 g" in out


def test_diff_json(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    root = _repo(tmp_path, BASE)
    (root / "a.py").write_text("def f() -> int:\n    return 2\n")

    assert cli.main(["diff", "--root", str(root), "--json"]) == 1
    doc = json.loads(capsys.readouterr().out)
    assert [d["id"] for d in doc["changed"]] == ["a.py::f"]
    assert doc["changed"][0]["callers"] == ["b.py:4 g"]
    assert doc["added"] == []
    assert doc["removed"] == []


def test_diff_explicit_rev(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    root = _repo(tmp_path, BASE)
    (root / "a.py").write_text("def f() -> int:\n    return 2\n")
    _commit_all(root, "change f")

    # worktree now matches HEAD, but differs from HEAD~1
    assert cli.main(["diff", "HEAD", "--root", str(root)]) == 0
    capsys.readouterr()
    assert cli.main(["diff", "HEAD~1", "--root", str(root)]) == 1
    assert "~ a.py:1" in capsys.readouterr().out


def test_diff_bad_rev(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    root = _repo(tmp_path, BASE)
    assert cli.main(["diff", "nope-not-a-rev", "--root", str(root)]) == 2
    assert "cannot export git rev" in capsys.readouterr().err


def test_diff_uses_no_tar_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """export_rev extracts via stdlib tarfile, never the tar binary."""
    root = _repo(tmp_path, BASE)
    (root / "a.py").write_text("def f() -> int:\n    return 2\n")

    real_run = subprocess.run
    calls: list[list[str]] = []

    def spy(cmd: list[str], *args: object, **kwargs: object):  # noqa: ANN202
        calls.append(list(cmd))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(diff.subprocess, "run", spy)
    assert cli.main(["diff", "--root", str(root)]) == 1
    # No `tar` subprocess; the snapshot still comes from `git archive`.
    assert not any(c and c[0] == "tar" for c in calls)
    assert any(c[:2] == ["git", "-C"] and "archive" in c for c in calls)


def test_diff_unaffected_by_gitignore_pattern_matching_tracked_dir(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """Investigation-1.6: an unanchored ``.gitignore`` pattern that
    happens to match an already-tracked path must not make the
    old-side snapshot silently drop it. ``git`` never retroactively
    untracks a path a later ignore rule happens to match, but the
    old-side fallback (a bare filesystem walk of the ``git archive``
    extraction, which has no ``.git/`` to ask) can't tell tracked from
    untracked and used to prune it anyway, surfacing as phantom
    "added" symbols on a genuinely clean tree (awesome-go's
    ``.github/scripts/check-*`` vs. a root ``check-*`` ignore pattern).
    """
    root = tmp_path
    _git(root, "init", "-q")
    (root / "foo-bar").mkdir()
    (root / "foo-bar" / "main.py").write_text(
        "def helper() -> int:\n    return 1\n"
    )
    _commit_all(root, "add foo-bar (tracked before any ignore rule)")
    (root / ".gitignore").write_text("foo-*\n")
    _commit_all(root, "add colliding ignore pattern")
    assert cli.main(["map", str(root), "--quiet"]) == 0

    assert cli.main(["diff", "--root", str(root)]) == 0
    assert "no symbol changes" in capsys.readouterr().out


def test_snapshot_from_index_matches_full_snapshot(tmp_path: Path) -> None:
    """2.2: ``snapshot_from_index`` must match a full tree-sitter
    re-parse's symbol/caller/import tables for an unchanged tree — the
    regression guard against subtly diverging from the "real"
    extraction path while eliminating its redundant cost."""
    root = _repo(tmp_path, BASE)
    index = mapfile.load_map(root)
    assert index is not None

    full = diff.snapshot(root, None, (), 1_000_000)
    from_index = diff.snapshot_from_index(index, root)

    assert set(from_index.symbols) == set(full.symbols)
    for sym_id, sym in full.symbols.items():
        assert from_index.symbols[sym_id].qualname == sym.qualname
        assert from_index.symbols[sym_id].start_line == sym.start_line
    assert from_index.callers == full.callers
    assert from_index.body == full.body
    assert set(from_index.imports) == set(full.imports)


def test_snapshot_new_side_falls_back_when_index_is_stale(
    tmp_path: Path,
) -> None:
    """A loaded index that predates a working-tree change must not be
    trusted outright — ``snapshot_new_side`` should fall back to a real
    re-parse rather than silently reporting stale symbols."""
    root = _repo(tmp_path, BASE)
    index = mapfile.load_map(root)
    assert index is not None
    (root / "c.py").write_text("def h() -> int:\n    return 3\n")

    stale = diff.snapshot_from_index(index, root)
    assert "c.py::h" not in stale.symbols  # index predates the new file

    fresh = diff.snapshot_new_side(root, None, (), 1_000_000, index)
    assert "c.py::h" in fresh.symbols  # fell back to a real re-parse


def test_diff_run_reuses_index_for_new_side_when_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2.2 primary fix: with a fresh loaded index, the new (working-
    tree) side must not pay for a second full tree-sitter re-parse —
    only the old (git-archive) side calls the full ``snapshot()``
    path."""
    root = _repo(tmp_path, BASE)
    calls: list[Path] = []
    real_snapshot = diff.snapshot

    def spy(root_arg: Path, *args: object, **kwargs: object) -> diff.Snapshot:
        calls.append(Path(root_arg))
        return real_snapshot(root_arg, *args, **kwargs)

    monkeypatch.setattr(diff, "snapshot", spy)
    assert cli.main(["diff", "--root", str(root)]) == 0
    assert len(calls) == 1
    assert calls[0] != root


def test_body_hashes_read_each_file_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """4.4: a file with several symbols must be read+split exactly
    once per snapshot build, not once per symbol — the regression
    test that actually catches a reintroduction of the O(total
    symbols) file-read bug (output-equality alone wouldn't, since the
    memoized and unmemoized versions produce byte-identical hashes)."""
    files = {
        "multi.py": (
            "def f() -> int:\n    return 1\n\n\n"
            "def g() -> int:\n    return 2\n\n\n"
            "def h() -> int:\n    return 3\n"
        ),
    }
    root = _repo(tmp_path, files)
    index = mapfile.load_map(root)
    assert index is not None

    calls: list[Path] = []
    real_read_text = Path.read_text

    def spy(self: Path, *args: object, **kwargs: object) -> str:
        if self.name == "multi.py":
            calls.append(self)
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", spy)

    from_index = diff.snapshot_from_index(index, root)
    assert len(from_index.symbols) == 3
    assert len(calls) == 1  # one read for all three symbols

    calls.clear()
    full = diff.snapshot(root, None, (), 1_000_000)
    assert len(full.symbols) == 3
    assert len(calls) == 1  # one read for all three symbols

    assert from_index.body == full.body


def test_diff_jobs_flag_reaches_old_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-12 master report §3.3: ``old_snapshot()``'s rev-cache-miss
    re-parse/resolve used to always run single-threaded no matter what
    ``--jobs`` was passed, because ``dekko diff`` never had a
    ``--jobs`` flag to begin with -- a separate, unparallelized code
    path from ``dekko map --full --jobs``. ``dekko diff --jobs N``
    must now reach ``diff.old_snapshot`` with the resolved worker
    count (``0`` maps to "all cores" via ``repo_ops.resolve_workers``,
    same as ``map``)."""
    root = _repo(tmp_path, BASE)

    seen_jobs: list[int] = []
    real_old_snapshot = diff.old_snapshot

    def spy(*args: object, **kwargs: object) -> diff.Snapshot | None:
        seen_jobs.append(kwargs["jobs"])
        return real_old_snapshot(*args, **kwargs)

    monkeypatch.setattr(diff, "old_snapshot", spy)
    assert cli.main(["diff", "--root", str(root), "--jobs", "0"]) == 0
    assert len(seen_jobs) == 1
    assert seen_jobs[0] == (os.cpu_count() or 1)


def test_maybe_warn_sequential_fires_above_threshold(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Round-15 finding: a jobs=1 rev-cache-miss over enough files gets
    a stderr disclosure note before the slow single-threaded work
    starts, mirroring ``render_lean.run``'s own floor-disclosure
    pattern."""
    monkeypatch.setattr(diff, "_SEQUENTIAL_DISCLOSURE_THRESHOLD", 3)
    diff._maybe_warn_sequential(1, ["a.py", "b.py", "c.py"])
    err = capsys.readouterr().err
    assert "single-threaded resolve on 3 files" in err
    assert "--jobs 0" in err


def test_maybe_warn_sequential_silent_below_threshold(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Small repos stay silent -- the note is only for the cases
    where a single-threaded resolve would plausibly take a while."""
    monkeypatch.setattr(diff, "_SEQUENTIAL_DISCLOSURE_THRESHOLD", 10)
    diff._maybe_warn_sequential(1, ["a.py", "b.py", "c.py"])
    assert capsys.readouterr().err == ""


def test_maybe_warn_sequential_silent_when_parallel(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """``--jobs 0``/``-N`` (resolved to >1 workers) never needs the
    disclosure -- the whole point is a sequential-only cost."""
    monkeypatch.setattr(diff, "_SEQUENTIAL_DISCLOSURE_THRESHOLD", 1)
    diff._maybe_warn_sequential(4, ["a.py", "b.py", "c.py"])
    assert capsys.readouterr().err == ""


def test_maybe_warn_sequential_silent_when_candidates_unknown(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """An unresolvable rev (``candidates is None``) has nothing to
    count -- ``snapshot()`` will fall back to its own discovery and
    fail/succeed on its own terms; no note to print here."""
    monkeypatch.setattr(diff, "_SEQUENTIAL_DISCLOSURE_THRESHOLD", 1)
    diff._maybe_warn_sequential(1, None)
    assert capsys.readouterr().err == ""


def test_old_snapshot_disclosure_note_on_a_real_cache_miss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """End-to-end: a real ``dekko diff`` rev-cache miss at the default
    ``--jobs 1`` prints the note when the repo is (per a lowered
    threshold) "large"; a rev-cache *hit* on a second identical call
    prints nothing, since ``old_snapshot`` returns before
    ``_maybe_warn_sequential`` is ever reached."""
    monkeypatch.setattr(diff, "_SEQUENTIAL_DISCLOSURE_THRESHOLD", 1)
    root = _repo(tmp_path, BASE)
    (root / "a.py").write_text("def f() -> int:\n    return 2\n")
    _commit_all(root, "change f")

    assert cli.main(["diff", "HEAD~1", "--root", str(root)]) == 1
    first_err = capsys.readouterr().err
    assert "single-threaded resolve" in first_err
    assert "--jobs 0" in first_err

    assert cli.main(["diff", "HEAD~1", "--root", str(root)]) == 1
    assert "single-threaded resolve" not in capsys.readouterr().err


def test_diff_rev_cache_hit_skips_reexport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-08 §2.6: a second ``diff`` call against the same rev must
    reuse the cached old-side snapshot instead of paying the
    export/tarfile-extract/re-parse cost again."""
    root = _repo(tmp_path, BASE)
    (root / "a.py").write_text("def f() -> int:\n    return 2\n")
    _commit_all(root, "change f")

    calls: list[str] = []
    real_export_rev = diff.export_rev

    def spy(root_arg: Path, rev: str, dest: Path) -> bool:
        calls.append(rev)
        return real_export_rev(root_arg, rev, dest)

    monkeypatch.setattr(diff, "export_rev", spy)

    assert cli.main(["diff", "HEAD~1", "--root", str(root)]) == 1
    assert len(calls) == 1  # cache miss: real export

    assert cli.main(["diff", "HEAD~1", "--root", str(root)]) == 1
    assert len(calls) == 1  # cache hit: no second export

    cache_dir = root / ".dekko" / "rev-cache"
    assert list(cache_dir.glob("*.json"))


def test_diff_rev_cache_is_correct_not_just_fast(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """The cached (second-call) snapshot must reproduce the same diff
    result as the uncached first call — a fast-but-wrong cache would
    be worse than none."""
    root = _repo(tmp_path, BASE)
    (root / "a.py").write_text("def f() -> int:\n    return 2\n")
    (root / "c.py").write_text("def h() -> int:\n    return 3\n")
    _commit_all(root, "change f, add h")

    assert cli.main(["diff", "HEAD~1", "--root", str(root), "--json"]) == 1
    first = json.loads(capsys.readouterr().out)

    assert cli.main(["diff", "HEAD~1", "--root", str(root), "--json"]) == 1
    second = json.loads(capsys.readouterr().out)

    assert first == second
    assert [d["id"] for d in first["changed"]] == ["a.py::f"]
    assert [d["id"] for d in first["added"]] == ["c.py::h"]
