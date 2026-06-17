"""Context pack v2: doc lines, --with-source, call-site excerpts."""

import json
from pathlib import Path

import pytest

from dekko import cli
from dekko import server
from dekko.textutil import estimate_tokens

from conftest import RepoFactory

SRC = {
    "src/app.py": (
        '"""App module."""\n'
        "\n"
        "\n"
        "def helper():\n"
        '    """Add one."""\n'
        "    a = 1\n"
        "    a += 1\n"
        "    a += 2\n"
        "    a += 3\n"
        "    a += 4\n"
        "    a += 5\n"
        "    return a\n"
        "\n"
        "\n"
        "def main():\n"
        '    """Run the app."""\n'
        "    helper()\n"
        "    helper()\n"
    ),
}


def _context(root: Path, *argv: str) -> int:
    return cli.main(["context", "helper", "--root", str(root), *argv])


def test_doc_lines_render_by_default(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SRC)
    assert _context(root) == 0
    out = capsys.readouterr().out
    assert "doc: Add one." in out
    assert "doc: Run the app." in out
    assert "source:" not in out
    assert "> 17:" not in out


def test_with_source_inlines_body_and_sites(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SRC)
    assert _context(root, "--with-source") == 0
    out = capsys.readouterr().out
    assert "source:" in out
    assert "def helper():" in out
    assert "return a" in out
    assert "> 17: helper()" in out
    assert "> 18: helper()" in out


def test_with_source_json_shape(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SRC)
    assert _context(root, "--with-source", "--json") == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["target"]["doc"] == "Add one."
    assert "def helper():" in doc["source"]
    assert doc["source_truncated"] is False
    caller = next(n for n in doc["neighbors"] if n["direction"] == "caller")
    assert caller["doc"] == "Run the app."
    assert caller["sites"] == [
        {"line": 17, "text": "helper()"},
        {"line": 18, "text": "helper()"},
    ]


def test_json_without_source_has_no_source_key(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SRC)
    assert _context(root, "--json") == 0
    doc = json.loads(capsys.readouterr().out)
    assert "source" not in doc
    assert all("sites" not in n for n in doc["neighbors"])


def test_budget_truncates_source_keeps_signature(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SRC)
    assert _context(root) == 0
    baseline = capsys.readouterr().out
    budget = estimate_tokens(baseline) + 10

    assert _context(root, "--with-source", f"--budget={budget}") == 0
    out = capsys.readouterr().out
    assert "… (source truncated)" in out
    assert "helper()" in out  # signature survives
    assert "src/app.py:4-12" in out  # location survives


def test_file_mode_never_inlines_source(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SRC)
    code = cli.main(
        ["context", "src/app.py", "--root", str(root), "--with-source"]
    )
    assert code == 0
    assert "source:" not in capsys.readouterr().out


def test_mcp_context_pack_with_source(make_mapped_repo: RepoFactory) -> None:
    root = make_mapped_repo(SRC)
    ctx = server.Context(default_root=root, no_regen=False)
    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "get_context_pack",
            "arguments": {"target": "helper", "with_source": True},
        },
    }
    result = server.handle(ctx, msg)["result"]
    assert not result["isError"]
    text = result["content"][0]["text"]
    assert "source:" in text
    assert "def helper():" in text
