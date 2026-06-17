"""The affected subcommand: impacted test selection and exit codes."""

import json
import subprocess
from pathlib import Path

import pytest

from dekko import cli
from dekko import server

# core() is called directly by one test, transitively (via wrapper) by
# another, only imported by a third, and unrelated to a fourth.
BASE = {
    "src/app.py": (
        "def core() -> int:\n"
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
    "tests/test_import_only.py": (
        "from src.app import core\n"
        "\n"
        "\n"
        "REF = core\n"
        "\n"
        "\n"
        "def test_ref():\n"
        "    assert REF is not None\n"
    ),
    "tests/test_unrelated.py": (
        "from src.other import helper\n"
        "\n"
        "\n"
        "def test_helper():\n"
        "    assert helper() == 9\n"
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
        "def core() -> int:\n"
        "    return 2\n"
        "\n"
        "\n"
        "def wrapper() -> int:\n"
        "    return core()\n"
    )


def test_clean_tree_has_no_impact(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    root = _repo(tmp_path, BASE)
    assert cli.main(["affected", "--root", str(root)]) == 0
    assert "no impacted tests" in capsys.readouterr().out


def test_tiers_direct_transitive_import(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    root = _repo(tmp_path, BASE)
    _change_core(root)

    assert cli.main(["affected", "--root", str(root)]) == 1
    out = capsys.readouterr().out
    assert "[direct] tests/test_direct.py" in out
    assert "[transitive] tests/test_transitive.py" in out
    assert "[import] tests/test_import_only.py" in out
    assert "test_unrelated.py" not in out


def test_pytest_hint_lists_impacted_files(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    root = _repo(tmp_path, BASE)
    _change_core(root)

    assert cli.main(["affected", "--root", str(root)]) == 1
    out = capsys.readouterr().out
    hint = next(ln for ln in out.splitlines() if ln.startswith("pytest "))
    assert "tests/test_direct.py" in hint
    assert "tests/test_import_only.py" in hint
    assert "tests/test_unrelated.py" not in hint


def test_json_shape(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    root = _repo(tmp_path, BASE)
    _change_core(root)

    assert cli.main(["affected", "--root", str(root), "--json"]) == 1
    doc = json.loads(capsys.readouterr().out)
    by_path = {i["path"]: i for i in doc["impacted"]}
    assert by_path["tests/test_direct.py"]["tier"] == "direct"
    assert by_path["tests/test_transitive.py"]["tier"] == "transitive"
    assert by_path["tests/test_import_only.py"]["tier"] == "import"
    assert by_path["tests/test_direct.py"]["symbols"][0]["id"] == (
        "tests/test_direct.py::test_core"
    )
    assert doc["command"].startswith("pytest ")


def test_editing_a_test_marks_it_direct(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    root = _repo(tmp_path, BASE)
    (root / "tests/test_direct.py").write_text(
        "from src.app import core\n"
        "\n"
        "\n"
        "def test_core():\n"
        "    assert core() == 1  # tweaked\n"
    )
    assert cli.main(["affected", "--root", str(root)]) == 1
    out = capsys.readouterr().out
    assert "[direct] tests/test_direct.py" in out
    # Nothing else changed, so no other test file is impacted.
    assert "test_transitive.py" not in out
    assert "test_import_only.py" not in out


def test_bad_rev(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    root = _repo(tmp_path, BASE)
    assert cli.main(["affected", "nope-not-a-rev", "--root", str(root)]) == 2
    assert "cannot export git rev" in capsys.readouterr().err


def test_mcp_impacted_tests(tmp_path: Path) -> None:
    root = _repo(tmp_path, BASE)
    _change_core(root)
    ctx = server.Context(default_root=root, no_regen=False)
    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "impacted_tests", "arguments": {}},
    }
    result = server.handle(ctx, msg)["result"]
    assert not result["isError"]
    text = result["content"][0]["text"]
    assert "tests/test_direct.py" in text


def test_impacted_tests_in_tool_list() -> None:
    names = {t["name"] for t in server.TOOLS}
    assert "impacted_tests" in names
