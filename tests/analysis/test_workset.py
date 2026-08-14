"""The workset subcommand: seeds, tiered budget, and exit codes."""

import json
import subprocess
from pathlib import Path

import pytest

from dekko.integrations import cli
from dekko.analysis import diff
from dekko.render import mapfile
from dekko.integrations import server

# core() is changed by _change_core; called directly by test_core,
# transitively (via wrapper) by test_wrapper, import-only by test_ref.
BASE = {
    "src/app.py": (
        '"""The app core."""\n'
        "\n"
        "\n"
        "def core() -> int:\n"
        '    """Return the core value."""\n'
        "    return 1\n"
        "\n"
        "\n"
        "def wrapper() -> int:\n"
        "    return core()\n"
    ),
    "src/other.py": "def helper() -> int:\n    return 9\n",
    "tests/test_direct.py": (
        "from src.app import core\n"
        "\n"
        "\n"
        "def test_core():\n"
        "    assert core() == 1\n"
    ),
    "tests/test_transitive.py": (
        "from src.app import wrapper\n"
        "\n"
        "\n"
        "def test_wrapper():\n"
        "    assert wrapper() == 1\n"
    ),
}


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True
    )


def _commit_all(root: Path, message: str) -> None:
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
    _git(root, "init", "-q")
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    _commit_all(root, "base")
    assert cli.main(["map", str(root), "--quiet"]) == 0
    return root


def _change_core(root: Path) -> None:
    (root / "src/app.py").write_text(
        '"""The app core."""\n'
        "\n"
        "\n"
        "def core() -> int:\n"
        '    """Return the core value."""\n'
        "    return 2\n"
        "\n"
        "\n"
        "def wrapper() -> int:\n"
        "    return core()\n"
    )


def test_rev_seed_bundles_change(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    root = _repo(tmp_path, BASE)
    _change_core(root)

    assert cli.main(["workset", "--root", str(root)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("workset:")
    assert "1 symbols" in out
    assert "pytest " in out
    assert "src/app.py" in out
    # The depth tier carries core's pack (its callers).
    assert "packs:" in out
    assert "callers:" in out


def test_rev_seed_loads_current_index_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2.4: a rev-seeded ``workset`` call must load the current-tree
    map once, not twice, when the on-disk map is already fresh (no
    auto-regen in the way). ``workset.run()`` loads a fresh index via
    ``load_or_regen`` before calling ``seed_from_rev``; before this
    fix, ``affected.changes()`` (called from ``seed_from_rev``)
    redundantly reloaded the same map.json itself instead of reusing
    the index it was already handed. Commits the change and remaps
    first so the working tree matches the on-disk map exactly (a
    stale map would trigger its own separate auto-regen load, muddying
    the count this test is checking)."""
    root = _repo(tmp_path, BASE)
    _change_core(root)
    _commit_all(root, "change core")
    assert cli.main(["map", str(root), "--quiet"]) == 0

    calls: list[Path] = []
    real_load_map = mapfile.load_map

    def spy(root_arg: Path) -> mapfile.MapIndex | None:
        calls.append(root_arg)
        return real_load_map(root_arg)

    monkeypatch.setattr(mapfile, "load_map", spy)
    assert cli.main(["workset", "HEAD~1", "--root", str(root)]) == 0
    assert len(calls) == 1


def test_rev_seed_jobs_flag_reaches_old_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-12 master report §3.3: a rev-seeded ``dekko workset``
    shares ``diff``/``affected``'s rev-cache-miss old-side re-parse/
    resolve path, which used to always run single-threaded regardless
    of ``--jobs`` because ``dekko workset`` never had that flag.
    ``dekko workset REV --jobs N`` must now reach
    ``diff.old_snapshot`` with the resolved worker count."""
    root = _repo(tmp_path, BASE)
    _change_core(root)

    seen_jobs: list[int] = []
    real_old_snapshot = diff.old_snapshot

    def spy(*args: object, **kwargs: object) -> diff.Snapshot | None:
        seen_jobs.append(kwargs["jobs"])
        return real_old_snapshot(*args, **kwargs)

    monkeypatch.setattr(diff, "old_snapshot", spy)
    assert (
        cli.main(
            ["workset", "--root", str(root), "--jobs", "3"],
        )
        == 0
    )
    assert seen_jobs == [3]


def test_symbol_seed_needs_no_git(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    root = _repo(tmp_path, BASE)
    assert cli.main(["workset", "--symbol", "core", "--root", str(root)]) == 0
    out = capsys.readouterr().out
    assert "symbol src/app.py:core" in out
    # Reverse-BFS reaches the direct and transitive tests, no import tier.
    assert "tests/test_direct.py" in out
    assert "tests/test_transitive.py" in out


def test_symbol_not_found(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    root = _repo(tmp_path, BASE)
    code = cli.main(["workset", "--symbol", "nope", "--root", str(root)])
    assert code == 3
    assert "no symbol matches" in capsys.readouterr().err


def test_symbol_ambiguous(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    files = {
        "src/a.py": "def dup() -> int:\n    return 1\n",
        "src/b.py": "def dup() -> int:\n    return 2\n",
    }
    root = _repo(tmp_path, files)
    code = cli.main(["workset", "--symbol", "dup", "--root", str(root)])
    assert code == 4
    assert "ambiguous" in capsys.readouterr().err


def test_rev_and_symbol_are_mutually_exclusive(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    root = _repo(tmp_path, BASE)
    code = cli.main(
        ["workset", "HEAD", "--symbol", "core", "--root", str(root)]
    )
    assert code == 2
    assert "not both" in capsys.readouterr().err


def test_clean_tree_is_empty_bundle(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    root = _repo(tmp_path, BASE)
    assert cli.main(["workset", "--root", str(root)]) == 0
    out = capsys.readouterr().out
    assert "0 symbols" in out
    assert "0 impacted tests" in out


def test_bad_rev(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    root = _repo(tmp_path, BASE)
    code = cli.main(["workset", "nope-not-a-rev", "--root", str(root)])
    assert code == 2
    assert "cannot export git rev" in capsys.readouterr().err


def test_packs_zero_skips_depth_tier(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    root = _repo(tmp_path, BASE)
    code = cli.main(
        ["workset", "--symbol", "core", "--packs", "0", "--root", str(root)]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "packs:" not in out
    assert "files:" in out


def test_tight_budget_keeps_breadth_drops_detail(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    root = _repo(tmp_path, BASE)
    code = cli.main(
        ["workset", "--symbol", "core", "--budget", "1", "--root", str(root)]
    )
    assert code == 0
    out = capsys.readouterr().out
    # Manifest always prints; the floor keeps one breadth row.
    assert out.startswith("workset:")
    assert "files:" in out
    assert "detail:" not in out
    footer = out.strip().splitlines()[-1]
    assert "omitted" in footer
    assert "raise --budget" in footer


def test_budget_is_deterministic(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    root = _repo(tmp_path, BASE)
    _change_core(root)
    args = ["workset", "--budget", "120", "--root", str(root)]
    assert cli.main(args) == 0
    first = capsys.readouterr().out
    assert cli.main(args) == 0
    second = capsys.readouterr().out
    assert first == second


def test_json_shape(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    root = _repo(tmp_path, BASE)
    _change_core(root)
    assert cli.main(["workset", "--root", str(root), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["seed"]["mode"] == "rev"
    assert "src/app.py" in doc["seed"]["touched_files"]
    assert doc["pytest"].startswith("pytest ")
    paths = {o["path"] for o in doc["outlines"]}
    assert "src/app.py" in paths
    assert doc["packs"][0]["target"]["signature"].startswith("core")
    assert {"tokens", "returned", "total"} <= doc["meta"].keys()


def test_impacted_tests_tier_shown_in_text(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    root = _repo(tmp_path, BASE)
    _change_core(root)
    code = cli.main(["workset", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "impacted tests:" in out
    assert "tests/test_direct.py  [direct]" in out


def test_json_impacted_tests_total_matches_seed(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    root = _repo(tmp_path, BASE)
    _change_core(root)
    code = cli.main(["workset", "--root", str(root), "--json"])
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["impacted_tests_total"] == 2
    assert len(doc["impacted_tests"]) == 2


def test_impacted_tests_respect_budget_when_numerous(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    # tensorflow's bug (B6): impacted_tests used to bypass token-
    # budget accounting entirely — a real ~1,500-impact repo dumped
    # every path verbatim, 3.6x over the stated budget. Many impacted
    # tests under a modest budget must now truncate like every other
    # section, with the meter reflecting the omission.
    files = dict(BASE)
    for i in range(60):
        files[f"tests/test_extra_{i}.py"] = (
            "from src.app import core\n\n\n"
            f"def test_extra_{i}():\n"
            "    assert core() == 2\n"
        )
    root = _repo(tmp_path, files)
    _change_core(root)
    code = cli.main(
        ["workset", "--root", str(root), "--budget", "150", "--json"]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["impacted_tests_total"] == 62
    assert len(doc["impacted_tests"]) < doc["impacted_tests_total"]
    assert doc["meta"]["truncated_by"] is not None


def test_mcp_workset_tool(tmp_path: Path) -> None:
    root = _repo(tmp_path, BASE)
    _change_core(root)
    ctx = server.Context(default_root=root, no_regen=False)
    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "workset", "arguments": {}},
    }
    result = server.handle(ctx, msg)["result"]
    assert not result["isError"]
    # No explicit `root` argument: the reply is prefixed with the
    # resolved default root (bug #1/B1) ahead of the usual manifest.
    assert "workset:" in result["content"][0]["text"]


def test_mcp_workset_rejects_both_seeds(tmp_path: Path) -> None:
    root = _repo(tmp_path, BASE)
    ctx = server.Context(default_root=root, no_regen=False)
    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "workset",
            "arguments": {"rev": "HEAD", "symbol": "core"},
        },
    }
    result = server.handle(ctx, msg)["result"]
    assert result["isError"]
    assert "not both" in result["content"][0]["text"]


def test_workset_registered() -> None:
    assert "workset" in cli.SUBCOMMANDS
    names = {t["name"] for t in server.TOOLS}
    assert "workset" in names
