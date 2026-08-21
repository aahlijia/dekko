"""The .dekko incremental cache: creation, reuse, and --full."""

from pathlib import Path

import pytest

from dekko import repo_ops
from dekko.core.model import FileMap
from dekko.render import mapfile
from dekko.render.mapfile import _file_hash
from dekko.storage import cache as cache_mod
from dekko.integrations import cli

from conftest import RepoFactory

SRC = {
    "a.py": "def f() -> int:\n    return 1\n",
    "b.py": "def g() -> int:\n    return 2\n",
}


def _count_extractions(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Patch ``extract_one`` to record every file it parses."""
    parsed: list[str] = []
    real = repo_ops.extract_one

    def spy(root: Path, rel: str):  # noqa: ANN202
        parsed.append(rel)
        return real(root, rel)

    monkeypatch.setattr(repo_ops, "extract_one", spy)
    return parsed


def test_cache_created_and_ignored(make_mapped_repo: RepoFactory) -> None:
    root = make_mapped_repo(SRC)
    cache_file = root / cache_mod.CACHE_DIR / cache_mod.CACHE_FILE
    assert cache_file.is_file()
    inner = (root / cache_mod.CACHE_DIR / ".gitignore").read_text()
    # Generated files ignored; the ignore file, notes, and the
    # persistent .dekkoignore are tracked.
    assert inner.splitlines() == [
        "*",
        "!.gitignore",
        "!notes.json",
        "!.dekkoignore",
    ]
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


def test_spec_change_invalidates_cache(
    make_mapped_repo: RepoFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A dev iteration or hotfix can change what the extractor pulls
    # out of a file without bumping the released version string — the
    # cache must still invalidate on that, not just on a real version
    # bump (bug #1: the tool-version check alone is too coarse).
    root = make_mapped_repo(SRC)
    monkeypatch.setattr(cache_mod, "spec_fingerprint", lambda: "deadbeef")
    assert cache_mod.load(root) == {}

    parsed = _count_extractions(monkeypatch)
    assert cli.main(["map", str(root), "--quiet"]) == 0
    assert sorted(parsed) == ["a.py", "b.py"]


def test_header_dispatch_heuristic_change_invalidates_cache(
    make_mapped_repo: RepoFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 18's tensorflow finding: ``.h`` was always parsed with the
    C grammar, even for a genuine C++ header, silently dropping every
    ``class``/``namespace``/``template`` construct and producing wrong
    call/heritage resolution downstream. The fix
    (``repo_ops._resolve_header_spec``) content-sniffs a ``.h`` file
    and swaps to the C++ grammar when warranted -- but that dispatch
    logic lives outside any ``LanguageSpec``, so it had to be folded
    into ``spec_fingerprint`` by hand
    (``languages._HEADER_DISPATCH_HEURISTIC_VERSION``), or a
    ``.dekko/`` cache built under the *old* (always-C) dispatch logic
    would keep silently reusing a stale, wrongly-C-parsed ``FileMap``
    for every ``.h`` file forever after an upgrade -- the exact
    silent-wrong-answer failure mode the fix was meant to close, just
    relocated to the upgrade boundary. This simulates exactly that
    boundary: a hand-built cache entry standing in for what a pre-fix
    ``dekko map`` run would have produced.
    """
    src = {
        "widget.h": (
            "namespace demo {\n"
            "class Widget {\n"
            " public:\n"
            "  void Spin();\n"
            "};\n"
            "}  // namespace demo\n"
        ),
    }
    root = make_mapped_repo(src)

    # Hand-write a cache entry mimicking a pre-fix build: `widget.h`
    # parsed with the C grammar (no `class`/`namespace` visible to it
    # at all, hence no symbols extracted), tagged with a spec_hash
    # standing in for what a build predating this fix would have
    # computed (the fingerprint without the heuristic version marker
    # folded in).
    stale_filemap = FileMap(path="widget.h", language="c", symbols=[])
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cache_mod, "spec_fingerprint", lambda: "pre-fix-hash")
        cache = cache_mod.IncrementalCache({})
        cache.entries["widget.h"] = {
            "hash": _file_hash(root / "widget.h"),
            "file": cache_mod._filemap_to_dict(stale_filemap),
        }
        cache_mod.save(root, cache)

    # Sanity check: the hand-built entry really is stale under the
    # *current* fingerprint (not a no-op fixture) -- ``load`` discards
    # the whole cache on a spec_hash mismatch.
    assert cache_mod.load(root) == {}

    parsed = _count_extractions(monkeypatch)
    assert cli.main(["map", str(root), "--quiet"]) == 0
    assert parsed == ["widget.h"]  # re-extracted, not served from cache

    index = mapfile.load_map(root)
    assert index is not None
    assert index.languages_by_path["widget.h"] == "cpp"


def test_parallel_extraction_matches_sequential(
    make_mapped_repo: RepoFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Force the process-pool path on a small repo, then confirm the
    # output is byte-identical to a sequential cold rebuild.
    root = make_mapped_repo(SRC)
    monkeypatch.setattr(repo_ops, "_PARALLEL_MIN", 1)

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


def test_jobs_flag_also_parallelizes_resolution(
    make_mapped_repo: RepoFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """1.4: ``--jobs`` used to parallelize extraction only —
    resolve()/resolve_refs() ran single-threaded regardless, which is
    why a one-file-edit auto-regen could cost as much as a full remap
    on a large repo (round 11 §1). ``run_map`` now threads the same
    ``--jobs`` value into ``resolve()``'s new ``workers`` parameter;
    this forces the resolution-side parallel path too (via the
    resolver's own low item-count threshold) and confirms the output
    is still byte-identical to a sequential run."""
    from dekko.core import resolver as resolver_mod

    root = make_mapped_repo(SRC)
    monkeypatch.setattr(repo_ops, "_PARALLEL_MIN", 1)
    monkeypatch.setattr(resolver_mod, "_RESOLVE_PARALLEL_MIN_ITEMS", 0)

    assert (
        cli.main(["map", str(root), "--quiet", "--full", "--jobs", "2"]) == 0
    )
    parallel = (root / ".dekko" / "map.json").read_text()

    assert (
        cli.main(["map", str(root), "--quiet", "--full", "--jobs", "1"]) == 0
    )
    sequential = (root / ".dekko" / "map.json").read_text()

    def _strip(text: str) -> str:
        return "\n".join(
            ln for ln in text.splitlines() if "generated_at" not in ln
        )

    assert _strip(parallel) == _strip(sequential)


def test_regen_map_uses_all_cores_for_resolution(
    make_mapped_repo: RepoFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """1.4: the auto-regen path (``repo_ops.regen_map``, used by every
    read subcommand's ``load_or_regen`` on a stale map) used to
    hardcode ``jobs=1`` in its synthetic ``argparse.Namespace`` -- the
    exact scenario round 11 §1 flagged (a single-file edit's auto-regen
    paying the full single-threaded resolution cost). It must now
    request all cores (``jobs=0``) so the same fix that speeds up
    ``dekko map --jobs 0`` also reaches auto-regen."""
    root = make_mapped_repo(SRC)
    seen_jobs: list[int] = []
    real_run_map = repo_ops.run_map

    def spy(args, persist_excludes: bool = True):  # noqa: ANN001, ANN202
        seen_jobs.append(args.jobs)
        return real_run_map(args, persist_excludes=persist_excludes)

    monkeypatch.setattr(repo_ops, "run_map", spy)
    assert repo_ops.regen_map(root) == 0
    assert seen_jobs == [0]


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
        "!.dekkoignore",
    ]


def test_ensure_notes_tracked_migrates_pre_dekkoignore_ignore(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(SRC)
    inner = root / cache_mod.CACHE_DIR / ".gitignore"
    inner.write_text("*\n!.gitignore\n!notes.json\n")  # pre-feature form

    cache_mod.ensure_notes_tracked(root)

    assert inner.read_text().splitlines() == [
        "*",
        "!.gitignore",
        "!notes.json",
        "!.dekkoignore",
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


def test_save_leaves_no_temp_file_and_overwrites(
    make_mapped_repo: RepoFactory,
) -> None:
    """cache.json is written atomically: no ``.tmp`` sibling survives,
    and a second ``save`` fully replaces the prior content (round-12
    master report §4.1b: no atomic write previously guarded this
    file, so a concurrent reader could observe a partial write)."""
    root = make_mapped_repo(SRC)
    cache_dir = root / cache_mod.CACHE_DIR
    cache_file = cache_dir / cache_mod.CACHE_FILE
    assert cache_file.is_file()
    leftovers = [p for p in cache_dir.iterdir() if p.name != cache_file.name]
    assert not any(cache_mod.CACHE_FILE in p.name for p in leftovers)

    cache = cache_mod.IncrementalCache(cache_mod.load(root))
    cache.entries["a.py"] = {"hash": "deadbeef", "file": {}}
    cache_mod.save(root, cache)

    reloaded = cache_mod.load(root)
    assert reloaded["a.py"]["hash"] == "deadbeef"
    assert set(reloaded) == {"a.py"}
    leftovers = [p for p in cache_dir.iterdir() if p.name != cache_file.name]
    assert not any(cache_mod.CACHE_FILE in p.name for p in leftovers)


def test_persist_dekkoignore_creates_and_dedupes(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(SRC)
    ignore_path = root / cache_mod.CACHE_DIR / cache_mod.DEKKOIGNORE_FILE
    assert not ignore_path.exists()

    cache_mod.persist_dekkoignore(root, ["*.astro", "fixtures/*"])
    assert ignore_path.read_text().splitlines() == [
        "*.astro",
        "fixtures/*",
    ]

    # Overlapping list: only the new pattern is appended.
    cache_mod.persist_dekkoignore(root, ["*.astro", "generated/*"])
    assert ignore_path.read_text().splitlines() == [
        "*.astro",
        "fixtures/*",
        "generated/*",
    ]

    # Identical list: no-op, file content unchanged.
    before = ignore_path.read_text()
    cache_mod.persist_dekkoignore(root, ["*.astro", "generated/*"])
    assert ignore_path.read_text() == before


def test_persist_dekkoignore_preserves_comments_and_order(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(SRC)
    ignore_dir = root / cache_mod.CACHE_DIR
    ignore_dir.mkdir(parents=True, exist_ok=True)
    ignore_path = ignore_dir / cache_mod.DEKKOIGNORE_FILE
    ignore_path.write_text("# hand-authored\n*.log\n")

    cache_mod.persist_dekkoignore(root, ["*.astro"])

    assert ignore_path.read_text().splitlines() == [
        "# hand-authored",
        "*.log",
        "*.astro",
    ]


def test_heritage_survives_a_cache_hit_reparse(
    make_mapped_repo: RepoFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A per-file cache round-trip that drops RawHeritage would make
    # heritage edges vanish on the second `dekko map` run even though
    # nothing changed — the cache path (`_filemap_from_dict`) has to
    # rebuild the same ``heritage`` list ``extract_file`` produced,
    # not just symbols/calls/refs/imports.
    root = make_mapped_repo(
        {
            "base.py": "class Animal:\n    pass\n",
            "dog.py": (
                "from base import Animal\n\n\nclass Dog(Animal):\n    pass\n"
            ),
        }
    )
    index = mapfile.load_map(root)
    assert index is not None
    assert index.heritage_out["dog.py::Dog"] == ["base.py::Animal"]

    # Second run: both files are unchanged, so this exercises
    # IncrementalCache.reuse()'s cache-hit path exclusively.
    parsed = _count_extractions(monkeypatch)
    assert cli.main(["map", str(root), "--quiet"]) == 0
    assert parsed == []

    reloaded = mapfile.load_map(root)
    assert reloaded is not None
    assert reloaded.heritage_out["dog.py::Dog"] == ["base.py::Animal"]
