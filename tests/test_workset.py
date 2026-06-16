"""The workset subcommand: seeds, tiered budget, and exit codes."""

import json
import subprocess
from pathlib import Path

import pytest

from dekko import cli
from dekko import server

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
    assert result["content"][0]["text"].startswith("workset:")


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
    assert len(server.TOOLS) == 16
