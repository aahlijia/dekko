"""The disk-backed rev-cache: round-trip, eviction, isolation."""

import subprocess
from pathlib import Path

from dekko.storage import revcache
from dekko.analysis.diff import Snapshot
from dekko.core.model import Import, Param, Symbol


def _git(root: Path, *args: str) -> None:
    """Run a git command in ``root``, raising on failure."""
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
    )


def _repo_with_one_commit(root: Path) -> None:
    """A minimal committed git repo, just enough for ``resolve_sha``."""
    (root / "a.py").write_text("x = 1\n")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(
        root,
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "commit",
        "-m",
        "base",
    )


def _symbol(sym_id: str, name: str) -> Symbol:
    return Symbol(
        id=sym_id,
        name=name,
        qualname=name,
        kind="function",
        path="a.py",
        language="python",
        params=[Param(name="x", type="int")],
        returns="int",
        start_line=1,
        end_line=2,
    )


def _snapshot(name: str) -> Snapshot:
    sym = _symbol(f"a.py::{name}", name)
    return Snapshot(
        symbols={sym.id: sym},
        callers={sym.id: ["a.py::caller"]},
        body={sym.id: "deadbeef"},
        imports={"a.py": [Import(path="a.py", name="os", source="os")]},
    )


def test_load_missing_entry_is_a_cache_miss(tmp_path: Path) -> None:
    assert revcache.load(tmp_path, "0" * 40) is None


def test_save_then_load_round_trips_a_snapshot(tmp_path: Path) -> None:
    sha = "1" * 40
    snap = _snapshot("f")
    revcache.save(tmp_path, sha, snap)

    loaded = revcache.load(tmp_path, sha)
    assert loaded is not None
    assert set(loaded.symbols) == {"a.py::f"}
    assert loaded.symbols["a.py::f"].qualname == "f"
    assert loaded.symbols["a.py::f"].params[0].type == "int"
    assert loaded.callers == snap.callers
    assert loaded.body == snap.body
    assert loaded.imports["a.py"][0].source == "os"


def test_two_revs_do_not_leak_into_each_other(tmp_path: Path) -> None:
    sha_a, sha_b = "a" * 40, "b" * 40
    revcache.save(tmp_path, sha_a, _snapshot("f"))
    revcache.save(tmp_path, sha_b, _snapshot("g"))

    loaded_a = revcache.load(tmp_path, sha_a)
    loaded_b = revcache.load(tmp_path, sha_b)
    assert loaded_a is not None and loaded_b is not None
    assert set(loaded_a.symbols) == {"a.py::f"}
    assert set(loaded_b.symbols) == {"a.py::g"}


def test_corrupt_entry_is_a_miss_not_a_crash(tmp_path: Path) -> None:
    sha = "c" * 40
    cache_dir = tmp_path / ".dekko" / "rev-cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / f"{sha}.json").write_bytes(b"not json{{{")
    assert revcache.load(tmp_path, sha) is None


def test_eviction_caps_entries_and_drops_oldest_accessed(
    tmp_path: Path,
) -> None:
    shas = [f"{i:040d}" for i in range(revcache.MAX_ENTRIES + 5)]
    for sha in shas:
        revcache.save(tmp_path, sha, _snapshot("f"))

    cache_dir = tmp_path / ".dekko" / "rev-cache"
    remaining = {p.stem for p in cache_dir.glob("*.json")}
    assert len(remaining) == revcache.MAX_ENTRIES
    # The earliest-written (and never re-touched) entries are the ones
    # evicted; the most recently written survive.
    assert shas[0] not in remaining
    assert shas[-1] in remaining


def test_resolve_sha_unknown_rev_returns_none(tmp_path: Path) -> None:
    assert revcache.resolve_sha(tmp_path, "not-a-real-rev") is None


# ---------------------------------------------------------------------
# Round-24 §2 fix: has_entry() -- a resolvable-rev/on-disk-cache-file
# composition, used by try_daemon() to pick a rev-cache-miss-aware
# client timeout instead of guessing every request is a fast hit.
# ---------------------------------------------------------------------


def test_has_entry_true_for_resolvable_rev_with_cache_file(
    tmp_path: Path,
) -> None:
    _repo_with_one_commit(tmp_path)
    sha = revcache.resolve_sha(tmp_path, "HEAD")
    assert sha is not None
    revcache.save(tmp_path, sha, _snapshot("f"))
    assert revcache.has_entry(tmp_path, "HEAD") is True


def test_has_entry_false_for_resolvable_rev_with_no_cache_file(
    tmp_path: Path,
) -> None:
    _repo_with_one_commit(tmp_path)
    assert revcache.has_entry(tmp_path, "HEAD") is False


def test_has_entry_false_for_unresolvable_rev(tmp_path: Path) -> None:
    assert revcache.has_entry(tmp_path, "not-a-real-rev") is False
