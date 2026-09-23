"""Process identity (``dekko.selfcheck``) and delegated regen.

An outdated long-lived process and a current CLI
used to rewrite each other's ``map.json`` forever, because a spec
mismatch says "different", never "older". These tests pin the pieces
that let a process find out *which* side it is.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from dekko import repo_ops
from dekko import selfcheck
from dekko.render import mapfile
from dekko.storage import cache as cache_mod

from conftest import RepoFactory

SRC = {"a.py": "def f() -> int:\n    return 1\n"}


def _me() -> tuple[str, str]:
    return (selfcheck.loaded_version(), selfcheck.loaded_spec())


# --- loaded identity is frozen ------------------------------------------


def test_loaded_version_ignores_a_later_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ``importlib.metadata.version`` reads dist-info from disk at call
    # time. A server started under 0.43.63 used to report, and stamp,
    # whatever got installed afterwards -- which is why every
    # staleness message said "same version string on both sides".
    before = selfcheck.loaded_version()
    monkeypatch.setattr(selfcheck, "_pkg_version", lambda _name: "99.0.0")
    assert selfcheck._read_version() == "99.0.0"  # the disk "moved"
    assert selfcheck.loaded_version() == before  # this process didn't


def test_provenance_and_caches_stamp_the_loaded_identity(
    make_mapped_repo: RepoFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(selfcheck, "LOADED_VERSION", "0.0.1-loaded")
    root = make_mapped_repo(SRC)
    prov = json.loads((root / ".dekko" / "map.json").read_text())["provenance"]
    assert prov["tool_version"] == "0.0.1-loaded"
    assert prov["spec_hash"] == selfcheck.loaded_spec()
    assert cache_mod._tool_version() == "0.0.1-loaded"


# --- the arbiter ---------------------------------------------------------


def test_installed_identity_asks_a_real_child_interpreter() -> None:
    # Unmocked on purpose: proves the snippet runs under this
    # interpreter and that a fresh import agrees with this process
    # (it must, nothing was reinstalled mid-test).
    assert selfcheck.installed_identity() == _me()


def test_installed_identity_is_memoized_until_the_install_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    def fake_child() -> tuple[str, str]:
        calls.append(1)
        return ("1.0.0", f"spec{len(calls)}")

    sig = [("a", 1)]
    monkeypatch.setattr(selfcheck, "_ask_child", fake_child)
    monkeypatch.setattr(selfcheck, "install_signature", lambda: tuple(sig))

    assert selfcheck.installed_identity() == ("1.0.0", "spec1")
    assert selfcheck.installed_identity() == ("1.0.0", "spec1")
    assert len(calls) == 1
    sig[0] = ("a", 2)  # a second upgrade, mid-lifetime
    assert selfcheck.installed_identity() == ("1.0.0", "spec2")
    assert len(calls) == 2


@pytest.mark.parametrize(
    ("returncode", "stdout"),
    [(1, ""), (0, "only-one-line\n"), (0, "a\nb\nc\n")],
)
def test_ask_child_rejects_anything_but_two_lines(
    monkeypatch: pytest.MonkeyPatch, returncode: int, stdout: str
) -> None:
    def fake_run(
        cmd: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(cmd, returncode, stdout, "")

    monkeypatch.setattr(selfcheck.subprocess, "run", fake_run)
    assert selfcheck._ask_child() is None


def test_ask_child_survives_a_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd: list[str], **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd, 10)

    monkeypatch.setattr(selfcheck.subprocess, "run", fake_run)
    assert selfcheck._ask_child() is None


# --- classify: the three-way verdict ------------------------------------


@pytest.mark.parametrize(
    ("installed", "expected"),
    [
        ("me", selfcheck.MAP_OLD),
        ("map", selfcheck.PROCESS_OUTDATED),
        ("other", selfcheck.BOTH_OLD),
        (None, selfcheck.UNPROVEN),
    ],
)
def test_classify(
    monkeypatch: pytest.MonkeyPatch, installed: str | None, expected: str
) -> None:
    built = (selfcheck.loaded_version(), "mapspec")
    identity = {
        "me": _me(),
        "map": built,
        "other": ("9.9.9", "feedface"),
        None: None,
    }[installed]
    monkeypatch.setattr(selfcheck, "_ask_child", lambda: identity)
    assert selfcheck.classify(*built) == expected


def test_process_outdated_is_false_for_a_one_shot_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_child() -> None:
        raise AssertionError("one-shot processes never spawn the arbiter")

    monkeypatch.setattr(selfcheck, "_ask_child", no_child)
    assert selfcheck.process_outdated() is False


def test_process_outdated_when_identity_cannot_be_proven(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selfcheck.mark_long_lived()
    monkeypatch.setattr(selfcheck, "_ask_child", lambda: None)
    assert selfcheck.process_outdated() is True
    assert "could not be identified" in selfcheck.outdated_note()


# --- freshness: who is stale? -------------------------------------------


def _stamp(root: Path, spec_hash: str) -> None:
    path = root / ".dekko" / "map.json"
    doc = json.loads(path.read_text())
    doc["provenance"]["spec_hash"] = spec_hash
    path.write_text(json.dumps(doc))


def _freshness(root: Path) -> mapfile.Freshness:
    index = mapfile.load_map(root)
    assert index is not None
    return mapfile.check_freshness(root, index)


def test_one_shot_process_still_calls_a_mismatched_map_stale(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(SRC)
    _stamp(root, "deadbeef")
    fresh = _freshness(root)
    assert (fresh.fresh, fresh.reason) == (False, "version")
    assert fresh.process_outdated is False


def test_outdated_process_judges_the_map_on_content_alone(
    make_mapped_repo: RepoFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_mapped_repo(SRC)
    _stamp(root, "deadbeef")
    selfcheck.mark_long_lived()
    monkeypatch.setattr(
        selfcheck,
        "_ask_child",
        lambda: (selfcheck.loaded_version(), "deadbeef"),
    )

    fresh = _freshness(root)
    assert fresh.fresh is True
    assert fresh.process_outdated is True

    (root / "a.py").write_text("def f() -> int:\n    return 2\n")
    fresh = _freshness(root)
    assert (fresh.fresh, fresh.reason) == (False, "content")
    assert fresh.changed == ["a.py"]
    assert fresh.process_outdated is True


def test_current_long_lived_process_regens_an_old_map_as_before(
    make_mapped_repo: RepoFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_mapped_repo(SRC)
    _stamp(root, "deadbeef")
    selfcheck.mark_long_lived()
    monkeypatch.setattr(selfcheck, "_ask_child", _me)
    fresh = _freshness(root)
    assert (fresh.fresh, fresh.reason) == (False, "version")
    assert fresh.process_outdated is False


# --- delegated regen -----------------------------------------------------


def test_delegated_regen_really_rebuilds_via_a_child(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    # Unmocked: the snippet has to actually import dekko in a fresh
    # interpreter, honor the recorded options, and write a map stamped
    # with the on-disk identity.
    root = make_mapped_repo(SRC)
    (root / "a.py").write_text("def renamed() -> int:\n    return 1\n")
    capsys.readouterr()

    assert repo_ops._delegated_regen(root, full=False, quiet=True) == 0

    index = mapfile.load_map(root)
    assert index is not None
    assert "renamed" in index.symbols_by_name
    assert index.provenance["spec_hash"] == selfcheck.loaded_spec()
    assert capsys.readouterr().out == ""  # quiet: nothing on stdout


def test_delegated_regen_reports_a_child_that_cannot_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "executable", str(tmp_path / "no-such-python"))
    assert repo_ops._delegated_regen(tmp_path, full=False, quiet=True) == 1
    assert "delegated regen failed to run" in capsys.readouterr().err


def test_regen_map_only_delegates_when_outdated(
    make_mapped_repo: RepoFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_mapped_repo(SRC)
    seen: list[Path] = []

    def fake_delegated(root: Path, full: bool, quiet: bool) -> int:
        seen.append(root)
        return 0

    monkeypatch.setattr(repo_ops, "_delegated_regen", fake_delegated)
    assert repo_ops.regen_map(root) == 0
    assert seen == []  # one-shot: in-process

    selfcheck.mark_long_lived()
    monkeypatch.setattr(selfcheck, "_ask_child", lambda: ("9.9.9", "new"))
    assert repo_ops.regen_map(root) == 0
    assert seen == [root]


# --- the CLI side: make the war visible ---------------------------------


def test_cli_flags_a_same_version_foreign_build(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    # A fixed CLI can't stop an *unfixed* old server from rewriting
    # the map. It can stop that being invisible.
    root = make_mapped_repo(SRC)
    _stamp(root, "deadbeef")
    capsys.readouterr()

    index, code = repo_ops.load_or_regen(root, no_regen=False)
    assert (index is not None, code) == (True, 0)
    err = capsys.readouterr().err
    assert "different dekko build (spec deadbeef)" in err
    assert "dekko doctor" in err


def test_no_foreign_build_note_for_an_ordinary_content_regen(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_mapped_repo(SRC)
    (root / "a.py").write_text("def f() -> int:\n    return 2\n")
    capsys.readouterr()
    repo_ops.load_or_regen(root, no_regen=False)
    assert "different dekko build" not in capsys.readouterr().err


# --- a cached index must notice map.json was replaced -------------------


def test_index_matches_disk_sees_an_out_of_band_rebuild(
    make_mapped_repo: RepoFactory,
) -> None:
    # Found by the real-process A/B, not by design review: both the
    # MCP server's and the daemon's caches validated a held index with
    # ``check_freshness`` alone, which compares the *cached* copy's
    # provenance to the source tree. A newer dekko rebuilding the map
    # with no source change was invisible, so an outdated server kept
    # answering from its old extractor's index while claiming to serve
    # "the on-disk map".
    root = make_mapped_repo(SRC)
    held = mapfile.load_map(root)
    assert held is not None
    assert mapfile.index_matches_disk(root, held) is True
    assert mapfile.check_freshness(root, held).fresh is True

    path = root / ".dekko" / "map.json"
    doc = json.loads(path.read_text())
    doc["provenance"]["spec_hash"] = "rebuilt-by-newer-dekko"
    path.write_text(json.dumps(doc))

    assert mapfile.check_freshness(root, held).fresh is True  # blind to it
    assert mapfile.index_matches_disk(root, held) is False  # not any more


def test_in_memory_index_always_matches_disk(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(SRC)
    held = mapfile.load_map(root)
    assert held is not None
    held.map_stat = None  # the ``index_from_maps`` shape
    (root / ".dekko" / "map.json").write_text("{}")
    assert mapfile.index_matches_disk(root, held) is True
