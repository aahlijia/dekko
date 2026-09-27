"""A stale map's new side, kept for the next call in long-lived processes.

``diff``/``affected`` re-map a stale working tree in memory and never
write the map. A long-lived process (the daemon, the MCP server) keeps
that result and reuses it while the tree holds the same content; a
one-shot CLI process keeps nothing.
"""

import subprocess
from pathlib import Path

import pytest

from dekko import repo_ops
from dekko import selfcheck
from dekko.analysis import affected
from dekko.analysis import diff
from dekko.integrations import cli
from dekko.render import mapfile

BASE = {
    "a.py": "def f() -> int:\n    return 1\n",
    "b.py": "from a import f\n\n\ndef g() -> int:\n    return f()\n",
    "tests/test_b.py": (
        "from b import g\n\n\ndef test_g() -> None:\n    assert g() == 1\n"
    ),
}
EDIT = "def f() -> int:\n    return 7\n"


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def stale_root(tmp_path: Path) -> Path:
    """A committed, mapped repo with one uncommitted edit: a stale map."""
    _git(tmp_path, "init", "-q")
    for name, text in BASE.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    _git(tmp_path, "add", "-A")
    _git(
        tmp_path,
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "commit",
        "-m",
        "base",
    )
    assert cli.main(["map", str(tmp_path), "--quiet"]) == 0
    (tmp_path / "a.py").write_text(EDIT)
    return tmp_path


def _count_snapshots(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Record every in-memory re-map ``diff.snapshot`` performs."""
    calls: list[Path] = []
    real = diff.snapshot

    def spy(root: Path, *args: object, **kwargs: object) -> diff.Snapshot:
        calls.append(root)
        return real(root, *args, **kwargs)

    monkeypatch.setattr(diff, "snapshot", spy)
    return calls


def _pair(root: Path) -> tuple[diff.Snapshot, diff.Snapshot]:
    current = repo_ops.load_current_side(root)
    pair = diff.snapshot_pair(root, current.provenance["git_commit"], current)
    assert pair is not None
    return pair


def test_long_lived_repeat_call_reuses_the_new_side(
    stale_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selfcheck.mark_long_lived()
    _, first = _pair(stale_root)
    calls = _count_snapshots(monkeypatch)

    _, second = _pair(stale_root)

    assert second is first
    assert calls == []


def test_one_shot_process_keeps_nothing(
    stale_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pair(stale_root)
    calls = _count_snapshots(monkeypatch)

    _pair(stale_root)

    assert diff._new_side_memo is None
    assert calls == [stale_root]


@pytest.mark.parametrize(
    "move",
    ["edit_again", "revert", "add_file", "remove_file"],
)
def test_any_tree_change_misses(
    stale_root: Path, monkeypatch: pytest.MonkeyPatch, move: str
) -> None:
    selfcheck.mark_long_lived()
    _, first = _pair(stale_root)
    if move == "edit_again":
        (stale_root / "a.py").write_text("def f() -> int:\n    return 8\n")
    elif move == "revert":
        (stale_root / "a.py").write_text(BASE["a.py"])
        # Leave one change so the map stays stale and the memo is
        # consulted rather than dropped.
        (stale_root / "b.py").write_text(BASE["b.py"] + "\n")
    elif move == "add_file":
        (stale_root / "c.py").write_text("def h() -> int:\n    return 3\n")
    else:
        (stale_root / "b.py").unlink()
    calls = _count_snapshots(monkeypatch)

    _, second = _pair(stale_root)

    assert calls == [stale_root]
    assert second is not first
    cold = diff.snapshot(stale_root, None, (), 1_000_000)
    assert second.body == cold.body
    assert second.callers == cold.callers


def test_a_rewritten_map_misses(
    stale_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``dekko map`` from anywhere replaces what the memo was judged
    against; a second edit keeps the tree stale against the new map."""
    selfcheck.mark_long_lived()
    _pair(stale_root)
    assert cli.main(["map", str(stale_root), "--quiet"]) == 0
    (stale_root / "b.py").write_text(BASE["b.py"] + "\n")
    calls = _count_snapshots(monkeypatch)

    _pair(stale_root)

    assert calls == [stale_root]


def test_a_fresh_map_drops_the_entry(stale_root: Path) -> None:
    selfcheck.mark_long_lived()
    _pair(stale_root)
    assert diff._new_side_memo is not None

    assert cli.main(["map", str(stale_root), "--quiet"]) == 0
    _pair(stale_root)

    assert diff._new_side_memo is None


def test_version_stale_map_is_not_kept(
    stale_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A version verdict lists no files, so there is no key to check a
    later tree against."""
    selfcheck.mark_long_lived()
    stale = mapfile.Freshness(fresh=False, reason="version")
    current = repo_ops.CurrentSide(
        index=None,
        provenance=repo_ops.load_current_side(stale_root).provenance,
        freshness=stale,
    )

    diff.snapshot_pair(stale_root, current.provenance["git_commit"], current)

    assert diff._new_side_memo is None


def test_an_edit_during_the_build_misses_next_time(
    stale_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The key is hashed before the re-map reads a byte. An edit that
    lands in between leaves the snapshot newer than the key, so the
    next call must rebuild rather than serve it as current."""
    selfcheck.mark_long_lived()
    real = diff.snapshot
    mid_build = "def f() -> int:\n    return 9\n"

    def edit_then_snapshot(
        root: Path, *args: object, **kwargs: object
    ) -> diff.Snapshot:
        (root / "a.py").write_text(mid_build)
        return real(root, *args, **kwargs)

    monkeypatch.setattr(diff, "snapshot", edit_then_snapshot)
    _pair(stale_root)
    monkeypatch.setattr(diff, "snapshot", real)
    calls = _count_snapshots(monkeypatch)

    _pair(stale_root)

    assert calls == [stale_root]


def test_fresh_verdict_is_not_rechecked(
    stale_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``snapshot_pair`` hands its verdict down instead of re-running
    the full freshness check on the index."""
    assert cli.main(["map", str(stale_root), "--quiet"]) == 0
    current = repo_ops.load_current_side(stale_root)
    assert current.fresh

    def refuse(root: Path, index: object) -> None:
        raise AssertionError("freshness already judged")

    monkeypatch.setattr(mapfile, "check_freshness", refuse)

    assert diff.snapshot_pair(stale_root, "HEAD", current) is not None


@pytest.mark.parametrize("command", ["diff", "affected"])
def test_memo_hit_output_matches_the_first_call(
    stale_root: Path, capsys: pytest.CaptureFixture, command: str
) -> None:
    selfcheck.mark_long_lived()
    run = diff.run if command == "diff" else affected.run

    for as_json in (False, True):
        first_code = run(stale_root, None, as_json, 10)
        first = capsys.readouterr().out
        second_code = run(stale_root, None, as_json, 10)
        second = capsys.readouterr().out

        assert diff._new_side_memo is not None
        assert second_code == first_code
        assert second == first
