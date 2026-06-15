"""The summary digest and the MCP resource that serves it."""

import json
from pathlib import Path

import pytest

from dekko import cli
from dekko import server

from conftest import RepoFactory

SRC = {
    "src/app.py": (
        '"""The application core."""\n'
        "\n"
        "\n"
        "def helper():\n"
        "    return 1\n"
        "\n"
        "\n"
        "def main():\n"
        "    helper()\n"
    ),
    "src/util/__init__.py": '"""Utility helpers."""\n',
    "src/util/io.py": "def read():\n    return 2\n",
    "tests/test_app.py": "def test_main():\n    pass\n",
}


def _summary(root: Path, *argv: str) -> int:
    return cli.main(["summary", "--root", str(root), *argv])


def test_text_digest(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SRC)
    assert _summary(root) == 0
    out = capsys.readouterr().out
    assert "files," in out and "symbols," in out and "edges" in out
    assert "directories" in out
    assert "src/app.py — The application core." not in out  # purpose is dir
    assert "src/util/  — Utility helpers." in out
    assert "entrypoints:" in out
    assert "main()" in out


def test_json_shape(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SRC)
    assert _summary(root, "--json") == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["files"] >= 4
    dirs = {d["path"]: d for d in doc["directories"]}
    assert dirs["src/util"]["purpose"] == "Utility helpers."
    assert any(e["id"] == "src/app.py::main" for e in doc["entrypoints"])
    assert "parse_errors" in doc


def test_directory_purpose_prefers_index_file(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SRC)
    assert _summary(root, "--json") == 0
    doc = json.loads(capsys.readouterr().out)
    src = next(d for d in doc["directories"] if d["path"] == "src")
    # src/app.py has a module doc; src/ has no __init__, so the first
    # doc'd file supplies the purpose.
    assert src["purpose"] == "The application core."


def test_cross_dir_edges_counted(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(
        {
            "a/one.py": "def f():\n    return 1\n",
            "b/two.py": "from a.one import f\n\n\ndef g():\n    return f()\n",
        }
    )
    assert _summary(root, "--json") == 0
    doc = json.loads(capsys.readouterr().out)
    dirs = {d["path"]: d for d in doc["directories"]}
    assert dirs["a"]["cross_edges"] == 1
    assert dirs["b"]["cross_edges"] == 1


def test_no_tests_filter(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SRC)
    assert _summary(root, "--no-tests", "--json") == 0
    doc = json.loads(capsys.readouterr().out)
    assert all(d["path"] != "tests" for d in doc["directories"])


def _request(root: Path, method: str, params: dict) -> dict:
    ctx = server.Context(default_root=root, no_regen=False)
    msg = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    return server.handle(ctx, msg)


def test_mcp_resources_list_and_read(make_mapped_repo: RepoFactory) -> None:
    root = make_mapped_repo(SRC)
    listed = _request(root, "resources/list", {})["result"]
    uris = {r["uri"] for r in listed["resources"]}
    assert "dekko://summary" in uris

    read = _request(root, "resources/read", {"uri": "dekko://summary"})[
        "result"
    ]
    text = read["contents"][0]["text"]
    assert "symbols," in text
    assert read["contents"][0]["uri"] == "dekko://summary"


def test_mcp_resources_read_unknown_uri(make_mapped_repo: RepoFactory) -> None:
    root = make_mapped_repo(SRC)
    resp = _request(root, "resources/read", {"uri": "dekko://nope"})
    assert "error" in resp


def test_mcp_summary_tool(make_mapped_repo: RepoFactory) -> None:
    root = make_mapped_repo(SRC)
    resp = _request(
        root,
        "tools/call",
        {"name": "summary", "arguments": {}},
    )
    result = resp["result"]
    assert not result["isError"]
    assert "directories" in result["content"][0]["text"]


def test_initialize_advertises_resources() -> None:
    resp = server.handle(
        server.Context(default_root=Path("."), no_regen=False),
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )
    assert resp["result"]["capabilities"]["resources"] == {}


def test_summary_tool_registered() -> None:
    assert "summary" in {t["name"] for t in server.TOOLS}
