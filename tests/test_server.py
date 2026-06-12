"""The hand-rolled MCP server: protocol handling and tool dispatch."""

import io
import json
from pathlib import Path

import pytest

from dekko import cli
from dekko import server

from conftest import RepoFactory

SRC = {
    "a.py": "def f() -> int:\n    return 1\n",
    "b.py": "from a import f\n\n\ndef g() -> int:\n    return f()\n",
}


def _ctx(root: Path) -> server.Context:
    return server.Context(default_root=root, no_regen=False)


def _call(ctx: server.Context, name: str, arguments: dict) -> dict:
    """Issue one tools/call and return its result block."""
    msg = {
        "jsonrpc": "2.0",
        "id": 9,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    return server.handle(ctx, msg)["result"]


def test_initialize_echoes_protocol_and_names() -> None:
    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {}},
    }
    resp = server.handle(_ctx(Path(".")), msg)
    result = resp["result"]
    assert result["protocolVersion"] == "2025-03-26"
    assert result["capabilities"] == {"tools": {}}
    assert result["serverInfo"]["name"] == "dekko"


def test_initialized_notification_is_silent() -> None:
    msg = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    assert server.handle(_ctx(Path(".")), msg) is None


def test_ping() -> None:
    msg = {"jsonrpc": "2.0", "id": 5, "method": "ping"}
    assert server.handle(_ctx(Path(".")), msg)["result"] == {}


def test_unknown_method_is_error() -> None:
    msg = {"jsonrpc": "2.0", "id": 6, "method": "no/such"}
    resp = server.handle(_ctx(Path(".")), msg)
    assert resp["error"]["code"] == server.METHOD_NOT_FOUND


def test_tools_list_exposes_the_read_surface() -> None:
    resp = server.handle(
        _ctx(Path(".")),
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
        },
    )
    names = {t["name"] for t in resp["result"]["tools"]}
    assert names == {
        "query_symbol",
        "get_callers",
        "get_callees",
        "get_context_pack",
        "trace_path",
        "find_unused",
        "stats",
        "map_status",
        "refresh_map",
    }
    for tool in resp["result"]["tools"]:
        assert set(tool) == {"name", "description", "inputSchema"}


def test_query_symbol_tool(make_mapped_repo: RepoFactory) -> None:
    ctx = _ctx(make_mapped_repo(SRC))
    result = _call(ctx, "query_symbol", {"symbol": "f"})
    assert result["isError"] is False
    assert "f() -> int" in result["content"][0]["text"]


def test_get_callers_tool(make_mapped_repo: RepoFactory) -> None:
    ctx = _ctx(make_mapped_repo(SRC))
    result = _call(ctx, "get_callers", {"symbol": "f"})
    assert result["isError"] is False
    assert "g() -> int" in result["content"][0]["text"]


def test_get_context_pack_tool(make_mapped_repo: RepoFactory) -> None:
    ctx = _ctx(make_mapped_repo(SRC))
    result = _call(ctx, "get_context_pack", {"target": "g", "hops": 1})
    text = result["content"][0]["text"]
    assert result["isError"] is False
    assert "context: b.py:g" in text


def test_trace_path_tool(make_mapped_repo: RepoFactory) -> None:
    ctx = _ctx(make_mapped_repo(SRC))
    result = _call(ctx, "trace_path", {"from": "g", "to": "f"})
    text = result["content"][0]["text"]
    assert result["isError"] is False
    assert "g -> " in text and text.rstrip().endswith("f")


def test_trace_path_no_path_is_not_error(
    make_mapped_repo: RepoFactory,
) -> None:
    ctx = _ctx(make_mapped_repo(SRC))
    # f does not reach g (edge runs g -> f)
    result = _call(ctx, "trace_path", {"from": "f", "to": "g"})
    assert result["isError"] is False
    assert "no call path" in result["content"][0]["text"].lower()


def test_trace_path_missing_argument_is_error(
    make_mapped_repo: RepoFactory,
) -> None:
    ctx = _ctx(make_mapped_repo(SRC))
    result = _call(ctx, "trace_path", {"from": "g"})
    assert result["isError"] is True
    assert "missing required argument 'to'" in result["content"][0]["text"]


def test_find_unused_tool(make_mapped_repo: RepoFactory) -> None:
    ctx = _ctx(make_mapped_repo(SRC))
    # g has no inbound calls and is not a root → a dead-code lead
    result = _call(ctx, "find_unused", {})
    text = result["content"][0]["text"]
    assert result["isError"] is False
    assert "g" in text


def test_stats_tool(make_mapped_repo: RepoFactory) -> None:
    ctx = _ctx(make_mapped_repo(SRC))
    result = _call(ctx, "stats", {"top": 3})
    text = result["content"][0]["text"]
    assert result["isError"] is False
    assert "files" in text and "symbols" in text


def test_missing_argument_is_tool_error(
    make_mapped_repo: RepoFactory,
) -> None:
    ctx = _ctx(make_mapped_repo(SRC))
    result = _call(ctx, "query_symbol", {})
    assert result["isError"] is True
    assert "missing required argument 'symbol'" in result["content"][0]["text"]


def test_not_found_is_tool_error_not_doubled(
    make_mapped_repo: RepoFactory,
) -> None:
    ctx = _ctx(make_mapped_repo(SRC))
    result = _call(ctx, "query_symbol", {"symbol": "ghost"})
    text = result["content"][0]["text"]
    assert result["isError"] is True
    assert text.startswith("dekko: no symbol matches")  # single prefix


def test_unknown_tool_is_error(make_mapped_repo: RepoFactory) -> None:
    ctx = _ctx(make_mapped_repo(SRC))
    resp = server.handle(
        ctx,
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "bogus", "arguments": {}},
        },
    )
    assert resp["error"]["code"] == server.INVALID_PARAMS


def test_map_status_and_refresh(make_mapped_repo: RepoFactory) -> None:
    root = make_mapped_repo(SRC)
    ctx = _ctx(root)
    assert "fresh" in _call(ctx, "map_status", {})["content"][0]["text"]

    (root / "a.py").write_text("def f() -> int:\n    return 2\nY = 1\n")
    # map_status reads the on-disk map, which is now stale
    assert "stale" in _call(ctx, "map_status", {})["content"][0]["text"]

    refreshed = _call(ctx, "refresh_map", {})
    assert refreshed["isError"] is False
    assert "mapped" in refreshed["content"][0]["text"]
    assert "fresh" in _call(ctx, "map_status", {})["content"][0]["text"]


def test_serve_loop_frames_messages(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    lines = (
        '{"jsonrpc":"2.0","id":1,"method":"ping"}\n'
        "not json\n"
        '{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
        '{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n'
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(lines))
    assert cli.main(["serve", "--mcp", "--root", "."]) == 0

    out = [json.loads(ln) for ln in capsys.readouterr().out.splitlines()]
    # ping result, parse error, tools/list — the notification is silent
    assert out[0] == {"jsonrpc": "2.0", "id": 1, "result": {}}
    assert out[1]["error"]["code"] == server.PARSE_ERROR
    assert out[2]["id"] == 2 and "tools" in out[2]["result"]


def test_serve_requires_mcp(capsys: pytest.CaptureFixture) -> None:
    assert cli.main(["serve"]) == 2
    assert "requires --mcp" in capsys.readouterr().err
