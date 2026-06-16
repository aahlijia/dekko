"""Symbol-anchored notes: CRUD, rendering, orphans, MCP, committability."""

import json
from pathlib import Path

import pytest

from dekko import cli
from dekko import notes as notes_mod
from dekko import server

from conftest import RepoFactory

SRC = {
    "src/app.py": (
        "def helper():\n    return 1\n\n\ndef main():\n    helper()\n"
    ),
}


def _cli(root: Path, *argv: str) -> int:
    return cli.main([*argv, "--root", str(root)])


def test_add_persists_and_renders_in_query(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SRC)
    assert _cli(root, "note", "add", "helper", "keep it pure") == 0
    capsys.readouterr()

    assert _cli(root, "query", "symbol", "helper") == 0
    assert "note: keep it pure" in capsys.readouterr().out

    notes = notes_mod.load(root)
    assert notes["src/app.py::helper"][0]["text"] == "keep it pure"
    assert "created" in notes["src/app.py::helper"][0]


def test_notes_render_in_context_and_respect_flag(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SRC)
    assert _cli(root, "note", "add", "helper", "a caveat") == 0
    capsys.readouterr()

    assert _cli(root, "context", "helper") == 0
    assert "note: a caveat" in capsys.readouterr().out

    assert _cli(root, "context", "helper", "--no-notes") == 0
    assert "note:" not in capsys.readouterr().out

    assert _cli(root, "query", "symbol", "helper", "--no-notes") == 0
    assert "note:" not in capsys.readouterr().out


def test_rm_by_index_and_all(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SRC)
    _cli(root, "note", "add", "helper", "first")
    _cli(root, "note", "add", "helper", "second")
    capsys.readouterr()

    assert _cli(root, "note", "rm", "helper", "1") == 0
    capsys.readouterr()
    remaining = notes_mod.load(root)["src/app.py::helper"]
    assert [r["text"] for r in remaining] == ["second"]

    assert _cli(root, "note", "rm", "helper") == 0
    assert "src/app.py::helper" not in notes_mod.load(root)


def test_list_all_and_orphaned(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SRC)
    _cli(root, "note", "add", "helper", "live note")
    # Anchor a note to an id that is not in the map (simulated orphan).
    notes = notes_mod.load(root)
    notes["src/app.py::gone"] = [{"text": "stale", "created": "x"}]
    notes_mod.save(root, notes)
    capsys.readouterr()

    assert _cli(root, "note", "list", "--orphaned", "--json") == 0
    orphans = json.loads(capsys.readouterr().out)
    assert "src/app.py::gone" in orphans
    assert "src/app.py::helper" not in orphans


def test_notes_committable_gitignore(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SRC)
    _cli(root, "note", "add", "helper", "x")
    inner = (root / ".dekko" / ".gitignore").read_text().splitlines()
    assert "!notes.json" in inner
    assert (root / ".dekko" / "notes.json").is_file()


def test_unresolved_target_exits_not_found(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SRC)
    assert _cli(root, "note", "add", "nope_missing", "x") == 3
    assert "no symbol matches" in capsys.readouterr().err


def _call(root: Path, name: str, arguments: dict) -> dict:
    ctx = server.Context(default_root=root, no_regen=False)
    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    return server.handle(ctx, msg)["result"]


def test_mcp_add_and_list_notes(make_mapped_repo: RepoFactory) -> None:
    root = make_mapped_repo(SRC)
    added = _call(root, "add_note", {"symbol": "helper", "text": "mcp note"})
    assert not added["isError"]
    assert "src/app.py::helper" in added["content"][0]["text"]

    listed = _call(root, "list_notes", {"symbol": "helper"})
    assert not listed["isError"]
    assert "mcp note" in listed["content"][0]["text"]


def test_notes_tools_registered() -> None:
    names = {t["name"] for t in server.TOOLS}
    assert {"add_note", "list_notes"} <= names
