"""A stale map's ``diff``/``affected`` re-map is written as the map.

The re-map is the expensive part of a stale-map call, and the map on
disk is what makes the next call cheap, so the two commands write what
they built instead of throwing it away. ``--no-regen``, a tree that was
never mapped, a held regen lock and a failed write all leave the map as
it was, and the answer stands either way.
"""

import contextlib
import json
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from dekko import repo_ops
from dekko.analysis import affected
from dekko.analysis import diff
from dekko.analysis import workset
from dekko.integrations import cli
from dekko.integrations import server
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


def _commit(root: Path, message: str) -> None:
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
        message,
    )


def _head(root: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _write_base(root: Path) -> None:
    _git(root, "init", "-q")
    for name, text in BASE.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    _commit(root, "base")


@pytest.fixture
def stale_root(tmp_path: Path) -> Path:
    """A committed, mapped repo with one uncommitted edit: a stale map."""
    _write_base(tmp_path)
    assert cli.main(["map", str(tmp_path), "--quiet"]) == 0
    (tmp_path / "a.py").write_text(EDIT)
    return tmp_path


def _map_bytes(root: Path) -> bytes:
    return (root / ".dekko" / "map.json").read_bytes()


def _is_fresh(root: Path) -> bool:
    prov = mapfile.load_provenance(root)
    return mapfile.check_freshness_provenance(root, prov).fresh


def _count_remaps(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Record every in-memory snapshot and every regen, by root."""
    calls: list[Path] = []
    real_snapshot = diff.snapshot
    real_run_map = repo_ops.run_map

    def snapshot_spy(root: Path, *args: Any, **kwargs: Any) -> diff.Snapshot:
        calls.append(root)
        return real_snapshot(root, *args, **kwargs)

    def run_map_spy(args: Any, *rest: Any, **kwargs: Any) -> int:
        calls.append(Path(args.map_dir))
        return real_run_map(args, *rest, **kwargs)

    monkeypatch.setattr(diff, "snapshot", snapshot_spy)
    monkeypatch.setattr(repo_ops, "run_map", run_map_spy)
    return calls


@pytest.mark.parametrize("command", ["diff", "affected"])
def test_a_stale_map_is_written_fresh(
    stale_root: Path, capsys: pytest.CaptureFixture, command: str
) -> None:
    run = diff.run if command == "diff" else affected.run

    assert run(stale_root, None, False, 10) == 1
    assert "f" in capsys.readouterr().out
    assert _is_fresh(stale_root)


def test_the_written_map_is_the_one_dekko_map_writes(
    stale_root: Path, capsys: pytest.CaptureFixture
) -> None:
    diff.run(stale_root, None, False, 10)
    persisted = _map_bytes(stale_root)
    (stale_root / ".dekko" / "map.json").unlink()

    assert cli.main(["map", str(stale_root), "--quiet", "--full"]) == 0
    assert _map_bytes(stale_root) == persisted


def test_no_regen_leaves_the_map_untouched(
    stale_root: Path, capsys: pytest.CaptureFixture
) -> None:
    path = stale_root / ".dekko" / "map.json"
    before = (path.read_bytes(), path.stat().st_mtime_ns)

    assert diff.run(stale_root, None, False, 10, no_regen=True) == 1
    assert affected.run(stale_root, None, False, 10, no_regen=True) == 1
    assert "f" in capsys.readouterr().out
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_cli_no_regen_reaches_the_command(
    stale_root: Path, capsys: pytest.CaptureFixture
) -> None:
    before = _map_bytes(stale_root)

    code = cli.main(["diff", "--root", str(stale_root), "--no-regen"])

    assert code == 1
    assert _map_bytes(stale_root) == before
    assert not _is_fresh(stale_root)


def test_a_never_mapped_tree_stays_unmapped(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    _write_base(tmp_path)
    (tmp_path / "a.py").write_text(EDIT)

    assert diff.run(tmp_path, None, False, 10) == 1
    assert not (tmp_path / ".dekko" / "map.json").exists()


def test_a_held_regen_lock_skips_the_write(
    stale_root: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @contextlib.contextmanager
    def held(_root: Path) -> Iterator[bool]:
        yield False

    monkeypatch.setattr(repo_ops.filelock, "try_regen_lock", held)
    before = _map_bytes(stale_root)

    assert diff.run(stale_root, None, False, 10) == 1
    assert "f" in capsys.readouterr().out
    assert _map_bytes(stale_root) == before


def test_a_failed_write_keeps_the_answer(
    stale_root: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def read_only(*_args: object, **_kwargs: object) -> list[Path]:
        raise PermissionError("read-only file system")

    monkeypatch.setattr(repo_ops, "write_map", read_only)

    code = diff.run(stale_root, None, False, 10)

    captured = capsys.readouterr()
    assert code == 1
    assert "a.py" in captured.out
    assert "could not update the map" in captured.err
    assert not _is_fresh(stale_root)


def test_an_outdated_long_lived_process_never_writes(
    stale_root: Path,
) -> None:
    current = repo_ops.load_current_side(stale_root)
    assert diff._persist_hook(stale_root, current) is not None

    current.freshness.process_outdated = True

    assert diff._persist_hook(stale_root, current) is None


def test_one_remap_serves_affected_diff_and_workset(
    stale_root: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _count_remaps(monkeypatch)

    assert affected.run(stale_root, None, False, 10) == 1
    assert diff.run(stale_root, None, False, 10) == 1
    code = workset.run(stale_root, None, None, None, 2, False, False)

    assert code == 0
    # The old side's export of the rev is mapped too; count the tree's.
    assert [root for root in calls if root == stale_root] == [stale_root]


def test_workset_defaults_to_the_maps_commit_like_diff(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    _write_base(tmp_path)
    assert cli.main(["map", str(tmp_path), "--quiet"]) == 0
    mapped_at = _head(tmp_path)
    (tmp_path / "a.py").write_text(EDIT)
    _commit(tmp_path, "edit f")

    assert workset.run(tmp_path, None, None, None, 2, True, False) == 0
    doc = json.loads(capsys.readouterr().out)

    assert doc["seed"]["rev"] == mapped_at
    assert doc["seed"]["touched_files"] == ["a.py"]


def test_mcp_workset_honors_no_regen(stale_root: Path) -> None:
    ctx = server.Context(default_root=stale_root, no_regen=True)
    before = _map_bytes(stale_root)

    with pytest.raises(server.ToolError, match="stale"):
        server.tool_workset(ctx, {})
    assert _map_bytes(stale_root) == before


def test_mcp_impacted_tests_honors_no_regen(stale_root: Path) -> None:
    ctx = server.Context(default_root=stale_root, no_regen=True)
    before = _map_bytes(stale_root)

    out = server.tool_impacted_tests(ctx, {})

    assert "test_b.py" in out
    assert _map_bytes(stale_root) == before
