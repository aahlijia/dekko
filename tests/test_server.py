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
    assert result["capabilities"] == {"tools": {}, "resources": {}}
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
    # trace_path/find_unused/stats/lean/ledger are CLI-only (E5 trim,
    # 2026-07-10): the MCP surface pays schema rent in context tokens
    # on every session, and agents never reached for them live.
    assert names == {
        "query_symbol",
        "get_callers",
        "get_callees",
        "find_usages",
        "get_context_pack",
        "outline",
        "impacted_tests",
        "workset",
        "summary",
        "add_note",
        "list_notes",
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


def test_omitted_root_echoes_resolved_default(
    make_mapped_repo: RepoFactory,
) -> None:
    # Bug #1/B1: four independent evaluators hit the same failure on
    # four different repos — omitting `root` silently resolves
    # against the server's cwd, and a wrong-repo answer looks
    # identical in shape to a correct one. Every successful reply
    # that used the default root must now echo it, so this is
    # visually obvious rather than silently wrong.
    root = make_mapped_repo(SRC)
    ctx = _ctx(root)
    result = _call(ctx, "query_symbol", {"symbol": "f"})
    text = result["content"][0]["text"]
    assert result["isError"] is False
    assert f"root: {root}" in text
    assert "f() -> int" in text


def test_explicit_root_suppresses_the_default_note(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(SRC)
    ctx = _ctx(root)
    result = _call(ctx, "query_symbol", {"symbol": "f", "root": str(root)})
    text = result["content"][0]["text"]
    assert result["isError"] is False
    assert "no 'root' argument was given" not in text
    assert text.startswith("f() -> int")


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


# trace_path/find_unused/stats left the MCP surface (E5 trim) but their
# handlers stay callable — direct-call coverage below keeps them honest.


def test_trace_path_handler(make_mapped_repo: RepoFactory) -> None:
    ctx = _ctx(make_mapped_repo(SRC))
    text = server.tool_trace_path(ctx, {"from": "g", "to": "f"})
    assert "g -> " in text and text.rstrip().endswith("f")


def test_trace_path_handler_no_path_is_not_error(
    make_mapped_repo: RepoFactory,
) -> None:
    ctx = _ctx(make_mapped_repo(SRC))
    # f does not reach g (edge runs g -> f)
    text = server.tool_trace_path(ctx, {"from": "f", "to": "g"})
    assert "no call path" in text.lower()


def test_trace_path_handler_missing_argument_raises(
    make_mapped_repo: RepoFactory,
) -> None:
    ctx = _ctx(make_mapped_repo(SRC))
    with pytest.raises(
        server.ToolError, match="missing required argument 'to'"
    ):
        server.tool_trace_path(ctx, {"from": "g"})


def test_trace_path_tool_is_cli_only(make_mapped_repo: RepoFactory) -> None:
    ctx = _ctx(make_mapped_repo(SRC))
    resp = server.handle(
        ctx,
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {"name": "trace_path", "arguments": {}},
        },
    )
    assert "error" in resp


def test_find_unused_handler(make_mapped_repo: RepoFactory) -> None:
    ctx = _ctx(make_mapped_repo(SRC))
    # g has no inbound calls and is not a root → a dead-code lead
    assert "g" in server.tool_find_unused(ctx, {})


def test_stats_handler(make_mapped_repo: RepoFactory) -> None:
    ctx = _ctx(make_mapped_repo(SRC))
    text = server.tool_stats(ctx, {"top": 3})
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


def test_query_symbol_tool_reports_unsupported_coverage_gap(
    make_mapped_repo: RepoFactory,
) -> None:
    # F3: MCP-surface twin of
    # test_query.py::test_query_callers_reports_unsupported_coverage_gap
    # — proves query.report_unresolved()'s stderr coverage note (the
    # "may be incomplete" caveat on a not-found reply, added for the
    # 2026-07-31 Astro-repo eval finding) actually folds into the
    # server.ToolError message across the MCP tool-call path
    # (server._capture -> _relation_tool), not just the CLI's direct
    # stderr print. Closes the synthesis report's "unresolved" flag on
    # Improvement #6 — source-reading during the design pass found the
    # wiring already correct; only this end-to-end test was missing.
    root = make_mapped_repo(
        dict(SRC, **{"Card.astro": "---\nconst x = 1;\n---\n"})
    )
    ctx = _ctx(root)
    with pytest.raises(server.ToolError, match="no parser for: astro"):
        server.tool_query_symbol(ctx, {"symbol": "ghost"})


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


def test_map_status_reports_unsupported_coverage(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(
        dict(SRC, **{"Card.astro": "---\nconst x = 1;\n---\n"})
    )
    ctx = _ctx(root)
    text = _call(ctx, "map_status", {})["content"][0]["text"]
    assert "fresh" in text
    assert "no parser for: astro" in text


def test_map_status_reports_version_stale(
    make_mapped_repo: RepoFactory,
) -> None:
    # Bug #1: map_status is the MCP-facing surface of the same
    # freshness check as `dekko status` — it must also call out a
    # version-stale map with an actionable message, not a generic
    # content diff (there is none to show).
    root = make_mapped_repo(SRC)
    map_path = root / ".dekko" / "map.json"
    doc = json.loads(map_path.read_text())
    doc["provenance"]["tool_version"] = "0.0.0-stale"
    map_path.write_text(json.dumps(doc))

    ctx = _ctx(root)
    text = _call(ctx, "map_status", {})["content"][0]["text"]
    assert "stale (version)" in text
    assert "built by dekko 0.0.0-stale, running" in text
    assert "call refresh_map" in text


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


def test_outline_tool_defaults_budget(
    monkeypatch: pytest.MonkeyPatch, make_mapped_repo: RepoFactory
) -> None:
    # No caller budget → DEFAULT_ORIENT_BUDGET, not unbounded (a large
    # repo's outline otherwise front-loads the agent's context).
    ctx = _ctx(make_mapped_repo(SRC))
    seen: dict = {}

    def fake_run(index, target, *, root, budget, limit, as_json):  # noqa: ANN001, ANN202
        seen["budget"] = budget
        print("outline")
        return 0

    monkeypatch.setattr(server.outline_mod, "run", fake_run)
    assert _call(ctx, "outline", {"target": "a.py"})["isError"] is False
    assert seen["budget"] == server.DEFAULT_ORIENT_BUDGET

    assert (
        _call(ctx, "outline", {"target": "a.py", "budget": 9000})["isError"]
        is False
    )
    assert seen["budget"] == 9000


def test_summary_tool_defaults_budget(
    monkeypatch: pytest.MonkeyPatch, make_mapped_repo: RepoFactory
) -> None:
    ctx = _ctx(make_mapped_repo(SRC))
    seen: dict = {}

    def fake_run(index, as_json, budget=None):  # noqa: ANN001, ANN202
        seen["budget"] = budget
        print("digest")
        return 0

    monkeypatch.setattr(server.summary, "run", fake_run)
    assert _call(ctx, "summary", {})["isError"] is False
    assert seen["budget"] == server.DEFAULT_ORIENT_BUDGET


def test_get_callers_tool_defaults_budget(
    monkeypatch: pytest.MonkeyPatch, make_mapped_repo: RepoFactory
) -> None:
    # No caller budget → DEFAULT_RELATION_BUDGET, not unbounded — the
    # 2026-07-31 eval lost to grep on get_callers because uncapped
    # output dumped every call site with no default cap.
    ctx = _ctx(make_mapped_repo(SRC))
    seen: dict = {}

    def fake_run(
        index,  # noqa: ANN001
        action,  # noqa: ANN001
        target,  # noqa: ANN001
        as_json,  # noqa: ANN001
        limit,  # noqa: ANN001
        sites=False,  # noqa: ANN001
        notes=True,  # noqa: ANN001
        budget=None,  # noqa: ANN001
    ) -> int:
        seen["budget"] = budget
        print("callers")
        return 0

    monkeypatch.setattr(server.query, "run", fake_run)
    assert _call(ctx, "get_callers", {"symbol": "f"})["isError"] is False
    assert seen["budget"] == server.DEFAULT_RELATION_BUDGET

    assert (
        _call(ctx, "get_callers", {"symbol": "f", "budget": 9000})["isError"]
        is False
    )
    assert seen["budget"] == 9000


def test_find_usages_tool_defaults_budget(
    monkeypatch: pytest.MonkeyPatch, make_mapped_repo: RepoFactory
) -> None:
    ctx = _ctx(make_mapped_repo(SRC))
    seen: dict = {}

    def fake_run(
        index,  # noqa: ANN001
        action,  # noqa: ANN001
        target,  # noqa: ANN001
        as_json,  # noqa: ANN001
        limit,  # noqa: ANN001
        budget=None,  # noqa: ANN001
    ) -> int:
        seen["budget"] = budget
        print("uses")
        return 0

    monkeypatch.setattr(server.query, "run", fake_run)
    assert _call(ctx, "find_usages", {"name": "f"})["isError"] is False
    assert seen["budget"] == server.DEFAULT_RELATION_BUDGET

    assert (
        _call(ctx, "find_usages", {"name": "f", "budget": 9000})["isError"]
        is False
    )
    assert seen["budget"] == 9000


def test_get_context_pack_tool_defaults_budget(
    monkeypatch: pytest.MonkeyPatch, make_mapped_repo: RepoFactory
) -> None:
    ctx = _ctx(make_mapped_repo(SRC))
    seen: dict = {}

    def fake_run(
        index,  # noqa: ANN001
        target,  # noqa: ANN001
        hops,  # noqa: ANN001
        budget,  # noqa: ANN001
        as_json,  # noqa: ANN001
        root=None,  # noqa: ANN001
        with_source=False,  # noqa: ANN001
        task=None,  # noqa: ANN001
    ) -> int:
        seen["budget"] = budget
        print("pack")
        return 0

    monkeypatch.setattr(server.contextpack, "run", fake_run)
    assert _call(ctx, "get_context_pack", {"target": "g"})["isError"] is False
    assert seen["budget"] == server.DEFAULT_RELATION_BUDGET

    assert (
        _call(ctx, "get_context_pack", {"target": "g", "budget": 9000})[
            "isError"
        ]
        is False
    )
    assert seen["budget"] == 9000


def test_get_callers_budget_caps_real_output(
    make_mapped_repo: RepoFactory,
) -> None:
    # Real (unmocked) end-to-end proof: a target with many callers,
    # capped to a tiny explicit budget, is actually truncated rather
    # than dumping every call site.
    files = {"target.py": "def shared() -> int:\n    return 1\n"}
    for i in range(20):
        files[f"caller_{i}.py"] = (
            "from target import shared\n\n\n"
            f"def caller_with_a_fairly_long_name_{i}() -> int:\n"
            "    return shared()\n"
        )
    ctx = _ctx(make_mapped_repo(files))

    uncapped = _call(
        ctx, "get_callers", {"symbol": "shared", "budget": 100000}
    )["content"][0]["text"]
    capped = _call(ctx, "get_callers", {"symbol": "shared", "budget": 30})[
        "content"
    ][0]["text"]
    assert len(capped) < len(uncapped)
    assert "omitted" in capped


def test_get_callers_default_budget_caps_many_callers(
    make_mapped_repo: RepoFactory,
) -> None:
    # Same shape, but relying on the *default* budget (no caller-given
    # value at all) — this is the failure mode from the 2026-07-31
    # eval, where get_callers lost to grep because nothing capped it.
    # Padded names push the total well past DEFAULT_RELATION_BUDGET
    # (800) while staying under the 50-row count limit, so it's the
    # budget default — not the pre-existing row cap — doing the work.
    pad = "z" * 60
    files = {"target.py": "def shared() -> int:\n    return 1\n"}
    for i in range(30):
        files[f"caller_{i}.py"] = (
            "from target import shared\n\n\n"
            f"def caller_with_a_long_padded_name_{pad}_{i}() -> int:\n"
            "    return shared()\n"
        )
    ctx = _ctx(make_mapped_repo(files))
    result = _call(ctx, "get_callers", {"symbol": "shared"})
    text = result["content"][0]["text"]
    assert result["isError"] is False
    assert "omitted" in text


def test_get_callers_excludes_test_callers_by_default(
    make_mapped_repo: RepoFactory,
) -> None:
    files = {
        "a.py": "def f() -> int:\n    return 1\n",
        "b.py": "from a import f\n\n\ndef g() -> int:\n    return f()\n",
        "tests/test_a.py": (
            "from a import f\n\n\ndef test_f() -> None:\n    assert f() == 1\n"
        ),
    }
    ctx = _ctx(make_mapped_repo(files))

    default_text = _call(ctx, "get_callers", {"symbol": "f"})["content"][0][
        "text"
    ]
    assert "g() -> int" in default_text
    assert "test_f" not in default_text

    with_tests_text = _call(
        ctx, "get_callers", {"symbol": "f", "include_tests": True}
    )["content"][0]["text"]
    assert "g() -> int" in with_tests_text
    assert "test_f" in with_tests_text


def test_get_callees_and_query_symbol_include_tests_by_default(
    make_mapped_repo: RepoFactory,
) -> None:
    # get_callers diverges from the CLI's opt-in --no-tests default
    # (test callers are noise for impact analysis); get_callees and
    # query_symbol keep the inclusive default so this isn't a silent,
    # blanket change to every relation tool.
    files = {
        "a.py": "def f() -> int:\n    return 1\n",
        "tests/test_a.py": (
            "from a import f\n\n\ndef test_f() -> None:\n    assert f() == 1\n"
        ),
    }
    ctx = _ctx(make_mapped_repo(files))
    callees_text = _call(ctx, "get_callees", {"symbol": "test_f"})["content"][
        0
    ]["text"]
    assert "f() -> int" in callees_text
