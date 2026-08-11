"""The hand-rolled MCP server: protocol handling and tool dispatch."""

import io
import json
from pathlib import Path

import pytest

from dekko import cli
from dekko import mapfile
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
        "search_code",
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


def test_index_for_caches_across_calls_when_unchanged(
    make_mapped_repo: RepoFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-08 §2.6: a warm session must not re-parse map.json on every
    tool call — repeated calls against an unchanged map should hit the
    in-process cache and skip ``mapfile.load_map`` entirely."""
    root = make_mapped_repo(SRC)
    ctx = _ctx(root)

    calls = []
    real_load_map = mapfile.load_map

    def spy(root_arg: Path) -> mapfile.MapIndex | None:
        calls.append(root_arg)
        return real_load_map(root_arg)

    monkeypatch.setattr(mapfile, "load_map", spy)

    result1 = _call(ctx, "query_symbol", {"symbol": "f"})
    assert result1["isError"] is False
    assert len(calls) == 1  # cache miss: first call loads the map

    result2 = _call(ctx, "get_callers", {"symbol": "f"})
    assert result2["isError"] is False
    assert len(calls) == 1  # cache hit: no second load_map call

    result3 = _call(ctx, "query_symbol", {"symbol": "g"})
    assert result3["isError"] is False
    assert len(calls) == 1  # still cached


def test_index_for_busts_cache_when_map_goes_stale(
    make_mapped_repo: RepoFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cached index must never be served once the working tree has
    actually moved on — correctness depends on ``check_freshness``
    catching this every call, not on observing the change event. A new
    symbol added after the first (caching) call must be invisible
    until the cache is actually refreshed, then visible right after —
    a weaker check (unchanged text) would pass even if invalidation
    were silently broken."""
    root = make_mapped_repo(SRC)
    ctx = _ctx(root)

    calls = []
    real_load_map = mapfile.load_map

    def spy(root_arg: Path) -> mapfile.MapIndex | None:
        calls.append(root_arg)
        return real_load_map(root_arg)

    monkeypatch.setattr(mapfile, "load_map", spy)

    result1 = _call(ctx, "query_symbol", {"symbol": "f"})
    assert result1["isError"] is False
    assert len(calls) == 1  # cache miss: populates the cache

    not_yet = _call(ctx, "query_symbol", {"symbol": "brand_new"})
    assert not_yet["isError"] is True  # cache still serving the old index
    assert len(calls) == 1  # served from cache, no reload triggered

    (root / "a.py").write_text(
        "def f() -> int:\n    return 1\n\n\ndef brand_new() -> int:\n"
        "    return 2\n"
    )
    before_refresh = len(calls)
    result2 = _call(ctx, "query_symbol", {"symbol": "brand_new"})
    assert result2["isError"] is False  # staleness detected, cache refreshed
    assert len(calls) > before_refresh  # a reload actually happened
    assert "brand_new() -> int" in result2["content"][0]["text"]

    after_refresh = len(calls)
    result3 = _call(ctx, "query_symbol", {"symbol": "brand_new"})
    assert result3["isError"] is False
    assert len(calls) == after_refresh  # re-cached after the refresh


def test_index_for_caches_per_root(
    make_mapped_repo: RepoFactory, tmp_path: Path
) -> None:
    """Two distinct roots must not share (or clobber) one cache entry."""
    root_a = make_mapped_repo(SRC)
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    (other_dir / "z.py").write_text("def only_here() -> int:\n    return 1\n")
    assert cli.main(["map", str(other_dir), "--quiet"]) == 0

    ctx = server.Context(default_root=root_a, no_regen=False)
    result_a = _call(ctx, "query_symbol", {"symbol": "f", "root": str(root_a)})
    assert result_a["isError"] is False
    result_b = _call(
        ctx, "query_symbol", {"symbol": "only_here", "root": str(other_dir)}
    )
    assert result_b["isError"] is False
    assert len(ctx.index_cache) == 2


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


_SEARCH_SRC = {
    "src/auth.py": ('"""Authentication."""\ndef login() -> None:\n    pass\n'),
    "src/db.py": '"""Database access."""\ndef connect() -> None:\n    pass\n',
}


def test_search_code_tool(make_mapped_repo: RepoFactory) -> None:
    ctx = _ctx(make_mapped_repo(_SEARCH_SRC))
    result = _call(ctx, "search_code", {"query": "login flow"})
    assert result["isError"] is False
    assert "login" in result["content"][0]["text"]


def test_search_code_tool_missing_query_is_tool_error(
    make_mapped_repo: RepoFactory,
) -> None:
    ctx = _ctx(make_mapped_repo(_SEARCH_SRC))
    result = _call(ctx, "search_code", {})
    assert result["isError"] is True
    assert "missing required argument 'query'" in result["content"][0]["text"]


def test_search_code_tool_zero_hits_is_not_error(
    make_mapped_repo: RepoFactory,
) -> None:
    ctx = _ctx(make_mapped_repo(_SEARCH_SRC))
    result = _call(ctx, "search_code", {"query": "xyzxyzxyz"})
    assert result["isError"] is False
    assert "(no matches)" in result["content"][0]["text"]


def test_search_code_tool_defaults_budget(
    monkeypatch: pytest.MonkeyPatch, make_mapped_repo: RepoFactory
) -> None:
    ctx = _ctx(make_mapped_repo(_SEARCH_SRC))
    seen: dict = {}

    def fake_run(
        index,  # noqa: ANN001
        query_text,  # noqa: ANN001
        kinds=None,  # noqa: ANN001
        limit=15,  # noqa: ANN001
        budget=None,  # noqa: ANN001
        as_json=False,  # noqa: ANN001
        root=None,  # noqa: ANN001
        scorer_name="lexical",  # noqa: ANN001
        excluded_test_count=0,  # noqa: ANN001
    ) -> int:
        seen["budget"] = budget
        print("search")
        return 0

    monkeypatch.setattr(server.search, "run", fake_run)
    assert _call(ctx, "search_code", {"query": "login"})["isError"] is False
    assert seen["budget"] == server.search.DEFAULT_BUDGET

    assert (
        _call(ctx, "search_code", {"query": "login", "budget": 9000})[
            "isError"
        ]
        is False
    )
    assert seen["budget"] == 9000


def test_search_code_tool_forwards_scorer_arg(
    monkeypatch: pytest.MonkeyPatch, make_mapped_repo: RepoFactory
) -> None:
    ctx = _ctx(make_mapped_repo(_SEARCH_SRC))
    seen: dict = {}

    def fake_run(
        index,  # noqa: ANN001
        query_text,  # noqa: ANN001
        kinds=None,  # noqa: ANN001
        limit=15,  # noqa: ANN001
        budget=None,  # noqa: ANN001
        as_json=False,  # noqa: ANN001
        root=None,  # noqa: ANN001
        scorer_name="lexical",  # noqa: ANN001
        excluded_test_count=0,  # noqa: ANN001
    ) -> int:
        seen["scorer_name"] = scorer_name
        seen["root"] = root
        print("search")
        return 0

    monkeypatch.setattr(server.search, "run", fake_run)
    assert _call(ctx, "search_code", {"query": "login"})["isError"] is False
    assert seen["scorer_name"] == "lexical"
    assert seen["root"] is not None

    assert (
        _call(ctx, "search_code", {"query": "login", "scorer": "embedding"})[
            "isError"
        ]
        is False
    )
    assert seen["scorer_name"] == "embedding"


def test_search_code_tool_embedding_scorer_unavailable_is_tool_error(
    monkeypatch: pytest.MonkeyPatch, make_mapped_repo: RepoFactory
) -> None:
    ctx = _ctx(make_mapped_repo(_SEARCH_SRC))
    monkeypatch.setattr(server.search.embedding, "available", lambda: False)
    result = _call(
        ctx, "search_code", {"query": "login", "scorer": "embedding"}
    )
    assert result["isError"] is True
    assert "dekko[search]" in result["content"][0]["text"]


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


def test_map_status_reports_spec_hash_stale_distinctly(
    make_mapped_repo: RepoFactory,
) -> None:
    # round-09 §2.3: a long-lived ``dekko serve`` process can have an
    # identical ``tool_version`` string on both sides while still
    # running stale extractor code — the old message only ever
    # printed ``tool_version``, so this case read as the
    # self-contradictory "built by dekko 0.21.3, running 0.21.3" with
    # no explanation. The message must name ``spec_hash`` explicitly
    # and must not claim a ``tool_version`` mismatch that didn't
    # happen.
    root = make_mapped_repo(SRC)
    map_path = root / ".dekko" / "map.json"
    doc = json.loads(map_path.read_text())
    doc["provenance"]["spec_hash"] = "deadbeef"
    map_path.write_text(json.dumps(doc))

    ctx = _ctx(root)
    text = _call(ctx, "map_status", {})["content"][0]["text"]
    assert "stale (spec_hash)" in text
    assert "tool_version:" not in text
    assert "deadbeef" in text
    assert "same version string" in text
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


def test_impacted_tests_tool_defaults_budget(
    monkeypatch: pytest.MonkeyPatch, make_mapped_repo: RepoFactory
) -> None:
    # No caller budget -> affected.DEFAULT_BUDGET, not unbounded (round-08
    # eval: a single tensorflow commit rendered ~124K uncapped tokens
    # with no --budget default at all on either the CLI or this tool).
    ctx = _ctx(make_mapped_repo(SRC))
    seen: dict = {}

    def fake_run(
        root,  # noqa: ANN001
        rev,  # noqa: ANN001
        as_json,  # noqa: ANN001
        limit,  # noqa: ANN001
        budget=None,  # noqa: ANN001
    ) -> int:
        seen["budget"] = budget
        print("impacted")
        return 0

    monkeypatch.setattr(server.affected, "run", fake_run)
    assert _call(ctx, "impacted_tests", {})["isError"] is False
    assert seen["budget"] == server.affected.DEFAULT_BUDGET

    assert _call(ctx, "impacted_tests", {"budget": 9000})["isError"] is False
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


def test_get_callers_discloses_silent_test_exclusion(
    make_mapped_repo: RepoFactory,
) -> None:
    """Round-11 §6: a caller who never mentions ``include_tests`` gets
    a ``note:`` disclosing that test-file callers were dropped by this
    tool's own default (which diverges from the CLI's). A caller who
    explicitly asks for either value gets no such note — they already
    know what they asked for."""
    files = {
        "a.py": "def f() -> int:\n    return 1\n",
        "b.py": "from a import f\n\n\ndef g() -> int:\n    return f()\n",
    }
    ctx = _ctx(make_mapped_repo(files))

    silent_default = _call(ctx, "get_callers", {"symbol": "f"})["content"][0][
        "text"
    ]
    assert "excluded by default for this tool" in silent_default

    explicit_opt_out = _call(
        ctx, "get_callers", {"symbol": "f", "include_tests": False}
    )["content"][0]["text"]
    assert "excluded by default for this tool" not in explicit_opt_out

    explicit_opt_in = _call(
        ctx, "get_callers", {"symbol": "f", "include_tests": True}
    )["content"][0]["text"]
    assert "excluded by default for this tool" not in explicit_opt_in


def test_get_callees_never_discloses_test_exclusion(
    make_mapped_repo: RepoFactory,
) -> None:
    """``get_callees``' default is already ``include_tests=True``, so
    nothing is silently excluded and the disclosure note never fires."""
    files = {
        "a.py": "def f() -> int:\n    return 1\n",
        "b.py": "from a import f\n\n\ndef g() -> int:\n    return f()\n",
    }
    ctx = _ctx(make_mapped_repo(files))
    text = _call(ctx, "get_callees", {"symbol": "g"})["content"][0]["text"]
    assert "excluded by default" not in text


def test_get_callers_resolves_java_package_named_test(
    make_mapped_repo: RepoFactory,
) -> None:
    """Round-11 §3: a Java package segment literally named `test`
    (org.springframework.boot.test, under src/main/) used to make
    classify.is_test_path() misclassify the *definition's own file* as
    a test file, so MCP's default (without_tests()) filtering removed
    the target symbol itself and get_callers returned "no symbol
    matches" even though the CLI (include_tests=True by default)
    resolved it fine. Reproduces the exact spring-boot repro shape."""
    path = (
        "core/spring-boot-test/src/main/java/org/springframework/boot/"
        "test/context/runner/AbstractApplicationContextRunner.java"
    )
    files = {
        path: (
            "package org.springframework.boot.test.context.runner;\n"
            "\n"
            "class AbstractApplicationContextRunner {\n"
            "    void withUserConfiguration() {\n"
            "    }\n"
            "}\n"
        ),
        path.replace("AbstractApplicationContextRunner.java", "Caller.java"): (
            "package org.springframework.boot.test.context.runner;\n"
            "\n"
            "class Caller {\n"
            "    void run() {\n"
            "        AbstractApplicationContextRunner runner =\n"
            "            new AbstractApplicationContextRunner();\n"
            "        runner.withUserConfiguration();\n"
            "    }\n"
            "}\n"
        ),
    }
    ctx = _ctx(make_mapped_repo(files))
    result = _call(
        ctx, "get_callers", {"symbol": f"{path}:withUserConfiguration"}
    )
    text = result["content"][0]["text"]
    assert result["isError"] is False
    assert "no symbol matches" not in text
    assert "Caller.run" in text


AMBIGUOUS_CALL = {
    "a.py": "def target() -> int:\n    return 1\n",
    "b.py": "def target() -> int:\n    return 2\n",
    "c.py": "def caller() -> int:\n    return target()\n",
}


def test_get_callers_discloses_ambiguous_call_sites_over_mcp(
    make_mapped_repo: RepoFactory,
) -> None:
    """Round-12 master report §3.1: ``query.run`` prints its
    "N additional call site(s) ... resolved ambiguously — not counted
    here" disclosure to stderr on an otherwise-successful (exit 0)
    run. The CLI shows both streams to a human, but every
    ``_capture()``-based MCP tool handler used to return only
    ``out.strip()``, silently discarding that note — an MCP-only
    caller's "no callers" answer looked complete even though a real
    ambiguous call site existed. ``get_callers`` must now surface it.
    """
    ctx = _ctx(make_mapped_repo(AMBIGUOUS_CALL))
    text = _call(ctx, "get_callers", {"symbol": "a.py:target"})["content"][0][
        "text"
    ]
    assert "(no callers of" in text
    assert "resolved ambiguously" in text
    assert "not counted here" in text


def test_get_callees_discloses_ambiguous_outgoing_calls_over_mcp(
    make_mapped_repo: RepoFactory,
) -> None:
    """Same fix, outgoing-call direction (``ambiguous_out``) via
    ``get_callees`` — round-12 master report §3.1."""
    ctx = _ctx(make_mapped_repo(AMBIGUOUS_CALL))
    text = _call(ctx, "get_callees", {"symbol": "c.py:caller"})["content"][0][
        "text"
    ]
    assert "(no callees of" in text
    assert "resolved ambiguously" in text
    assert "not counted here" in text


def test_lean_discloses_budget_floor_note(
    make_mapped_repo: RepoFactory,
) -> None:
    """Round-12 master report §3.1: ``render_lean.run`` prints a
    "requested budget N is below this repo's ~M-token path-only
    floor" note to stderr on success when a caller's ``budget`` is
    too tight to honor. ``tool_lean`` (not currently a registered MCP
    tool, exercised directly like ``tool_trace_path``/``tool_stats``
    elsewhere in this file) must not silently drop that note."""
    files = {
        "a.py": "def f() -> int:\n    return 1\n",
        "b.py": "from a import f\n\n\ndef g() -> int:\n    return f()\n",
    }
    ctx = _ctx(make_mapped_repo(files))
    text = server.tool_lean(ctx, {"budget": 1})
    assert "path-only floor" in text
