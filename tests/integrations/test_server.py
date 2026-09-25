"""The hand-rolled MCP server: protocol handling and tool dispatch."""

import io
import json
import os
import sys
from concurrent.futures.process import BrokenProcessPool
from itertools import combinations
from pathlib import Path

import pytest

from dekko import repo_ops
from dekko import selfcheck
from dekko.analysis import query
from dekko.core.resolver import PoolStalledError
from dekko.integrations import cli
from dekko.render import mapfile
from dekko.integrations import server

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
    # trace_path/find_unused/stats/lean/ledger are CLI-only: the MCP
    # surface pays schema rent in context tokens
    # on every session, and agents never reached for them live.
    assert names == {
        "search_code",
        "query_symbol",
        "get_callers",
        "get_callees",
        "find_usages",
        "find_type_usages",
        "get_supertypes",
        "get_subtypes",
        "get_context_pack",
        "outline",
        "impacted_tests",
        "workset",
        "check_ambiguous",
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
    # The same failure showed up on four different repos — omitting
    # `root` silently resolves
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


@pytest.mark.parametrize(
    ("symbol", "is_error"),
    [("f", False), ("ghost", True)],
)
def test_default_root_note_is_exactly_one_short_line(
    make_mapped_repo: RepoFactory,
    symbol: str,
    is_error: bool,
) -> None:
    # The note rides on every reply that omits `root`, success or
    # error, so its wording is pinned: any growth is paid on every call.
    root = make_mapped_repo(SRC)
    ctx = _ctx(root)
    result = _call(ctx, "query_symbol", {"symbol": symbol})
    text = result["content"][0]["text"]
    assert result["isError"] is is_error
    first, _ = text.split("\n", 1)
    assert first == f"(default root: {root})"


def test_explicit_root_suppresses_the_default_note(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(SRC)
    ctx = _ctx(root)
    result = _call(ctx, "query_symbol", {"symbol": "f", "root": str(root)})
    text = result["content"][0]["text"]
    assert result["isError"] is False
    assert "default root:" not in text
    assert text.startswith("f() -> int")


def test_index_for_caches_across_calls_when_unchanged(
    make_mapped_repo: RepoFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A warm session must not re-parse map.json on every
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
    # other_dir is nested under tmp_path purely as a fixture
    # convenience -- it's meant to be a second, wholly unrelated root,
    # not a subtree of root_a, so --force-new-root opts out of the
    # orphan-root guard that would otherwise (rightly)
    # flag this exact directory shape.
    assert (
        cli.main(["map", str(other_dir), "--quiet", "--force-new-root"]) == 0
    )

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


@pytest.mark.parametrize(
    ("tool", "value"),
    [
        ("query_symbol", "f"),
        ("get_callers", "f"),
        ("get_callees", "g"),
    ],
)
def test_symbol_alias_matches_symbol_argument(
    make_mapped_repo: RepoFactory, tool: str, value: str
) -> None:
    # Agents guessed `name` instead of
    # `symbol` on these tools since `find_usages` itself uses `name`.
    # `name` must resolve to an identical result as `symbol`.
    ctx = _ctx(make_mapped_repo(SRC))
    by_symbol = _call(ctx, tool, {"symbol": value})
    by_name = _call(ctx, tool, {"name": value})
    assert by_name == by_symbol


def test_conflicting_primary_and_alias_is_an_error(
    make_mapped_repo: RepoFactory,
) -> None:
    # Two different values is a caller bug (a stale value, a
    # copy-paste). The tool's own name used to win silently, which hid
    # the bug behind a confident answer about the wrong symbol. Now it
    # errors like any other conflicting pair, naming both values.
    ctx = _ctx(make_mapped_repo(SRC))
    result = _call(
        ctx, "query_symbol", {"symbol": "f", "name": "wrong_target"}
    )
    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert "symbol='f'" in text
    assert "name='wrong_target'" in text
    assert "pass one 'symbol' argument" in text


@pytest.mark.parametrize("tool", sorted(server._TARGET_PARAM))
@pytest.mark.parametrize(
    ("first", "second"), list(combinations(server._TARGET_ALIASES, 2))
)
def test_every_target_tool_rejects_every_conflicting_pair(
    tool: str, first: str, second: str
) -> None:
    # The whole matrix, whichever names carry the two values, the
    # tool's own included. The function is pure, so no repo is needed.
    primary = server._TARGET_PARAM[tool]
    with pytest.raises(server.ToolError) as excinfo:
        server._resolve_target_alias(tool, {first: "x", second: "y"})
    message = str(excinfo.value)
    assert "naming different targets" in message
    assert f"pass one '{primary}' argument" in message
    assert f"{first}='x'" in message
    assert f"{second}='y'" in message


@pytest.mark.parametrize("tool", sorted(server._TARGET_PARAM))
def test_conflicting_pair_is_an_error_reply_on_every_tool(
    make_mapped_repo: RepoFactory, tool: str
) -> None:
    ctx = _ctx(make_mapped_repo(SRC))
    primary = server._TARGET_PARAM[tool]
    alias = next(a for a in server._TARGET_ALIASES if a != primary)
    args = {primary: "a.py", alias: "f"}
    if tool == "add_note":
        args["text"] = "t"
    result = _call(ctx, tool, args)
    assert result["isError"] is True
    assert "naming different targets" in result["content"][0]["text"]


@pytest.mark.parametrize(
    "args",
    [
        {"symbol": "f", "name": "f"},
        {"name": "f", "target": "f"},
        {"symbol": "f", "name": "f", "target": "f", "type": "f"},
        {"symbol": None, "name": "f"},
        {"symbol": "f", "name": None, "type": None},
    ],
)
def test_agreeing_or_null_aliases_fold_to_the_primary(args: dict) -> None:
    # Hedging with the same value under two names is the reason the
    # aliases exist; a JSON null is an absent argument, not a value.
    args = {**args, "root": "/r"}
    folded = server._resolve_target_alias("query_symbol", args)
    assert folded == {"symbol": "f", "root": "/r"}


def test_agreeing_aliases_answer_like_the_plain_call(
    make_mapped_repo: RepoFactory,
) -> None:
    ctx = _ctx(make_mapped_repo(SRC))
    plain = _call(ctx, "query_symbol", {"symbol": "f"})
    hedged = _call(ctx, "query_symbol", {"symbol": "f", "name": "f"})
    assert hedged == plain
    assert hedged["isError"] is False


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("query_symbol", {"symbol": "f", "root": "/r"}),
        ("query_symbol", {"symbol": None}),
        ("query_symbol", {"root": "/r"}),
        ("search_code", {"query": "x", "name": "y", "symbol": "z"}),
    ],
)
def test_untouched_calls_return_the_same_arguments_object(
    tool: str, args: dict
) -> None:
    # No fold needed: the tool's own name alone, nothing to fold, or a
    # tool that takes no target. The handler sees exactly what was sent
    # (a missing or null primary is still the handler's error to give).
    assert server._resolve_target_alias(tool, args) is args


@pytest.mark.parametrize(
    ("tool", "alias"),
    [
        ("get_context_pack", "name"),
        ("get_context_pack", "symbol"),
        ("outline", "symbol"),
        ("find_usages", "symbol"),
        ("get_callers", "target"),
    ],
)
def test_target_aliases_work_across_tools(
    make_mapped_repo: RepoFactory, tool: str, alias: str
) -> None:
    """Each target-taking tool accepts the other tools' names for its
    target, since agents calling several in a row guess by analogy."""
    ctx = _ctx(make_mapped_repo(SRC))
    target = "a.py" if tool == "outline" else "f"
    text = _call(ctx, tool, {alias: target})["content"][0]["text"]
    # ``find_usages`` rejects the internal ``f`` on its merits; what
    # matters is that no tool reports its target argument missing.
    assert "missing required argument" not in text
    assert "f" in text or "a.py" in text


def test_find_type_usages_accepts_symbol(
    make_mapped_repo: RepoFactory,
) -> None:
    ctx = _ctx(
        make_mapped_repo(
            {
                "app.py": "class Config:\n    pass\n\n\n"
                "def load(c: Config) -> None:\n    pass\n"
            }
        )
    )
    result = _call(ctx, "find_type_usages", {"symbol": "Config"})
    assert result["isError"] is False
    assert "load" in result["content"][0]["text"]


def test_conflicting_target_aliases_are_an_error(
    make_mapped_repo: RepoFactory,
) -> None:
    ctx = _ctx(make_mapped_repo(SRC))
    result = _call(ctx, "get_context_pack", {"name": "f", "symbol": "g"})
    assert result["isError"] is True
    assert "pass one 'target' argument" in result["content"][0]["text"]


def test_find_type_usages_tool(make_mapped_repo: RepoFactory) -> None:
    files = {
        "app.py": (
            "class Config:\n"
            "    pass\n"
            "\n"
            "\n"
            "def start(cfg: Config) -> None:\n"
            "    pass\n"
        ),
    }
    ctx = _ctx(make_mapped_repo(files))
    result = _call(ctx, "find_type_usages", {"type": "Config"})
    assert result["isError"] is False
    assert "start(cfg: Config) -> None" in result["content"][0]["text"]


def test_find_type_usages_tool_exact_passthrough(
    make_mapped_repo: RepoFactory,
) -> None:
    files = {
        "app.py": (
            "from typing import Optional\n"
            "\n"
            "\n"
            "class Config:\n"
            "    pass\n"
            "\n"
            "\n"
            "def start(cfg: Optional[Config] = None) -> None:\n"
            "    pass\n"
        ),
    }
    ctx = _ctx(make_mapped_repo(files))
    loose = _call(ctx, "find_type_usages", {"type": "Config"})
    assert loose["isError"] is False
    assert "start" in loose["content"][0]["text"]

    exact = _call(ctx, "find_type_usages", {"type": "Config", "exact": True})
    assert exact["isError"] is True


PY_HERITAGE = {
    "base.py": "class Animal:\n    pass\n",
    "dog.py": ("from base import Animal\n\n\nclass Dog(Animal):\n    pass\n"),
}


def test_get_supertypes_tool(make_mapped_repo: RepoFactory) -> None:
    ctx = _ctx(make_mapped_repo(PY_HERITAGE))
    result = _call(ctx, "get_supertypes", {"symbol": "Dog"})
    assert result["isError"] is False
    assert "class Animal" in result["content"][0]["text"]


def test_get_subtypes_tool(make_mapped_repo: RepoFactory) -> None:
    ctx = _ctx(make_mapped_repo(PY_HERITAGE))
    result = _call(ctx, "get_subtypes", {"symbol": "Animal"})
    assert result["isError"] is False
    assert "class Dog" in result["content"][0]["text"]


@pytest.mark.parametrize(
    ("tool", "value"),
    [
        ("get_supertypes", "Dog"),
        ("get_subtypes", "Animal"),
    ],
)
def test_symbol_alias_matches_symbol_for_heritage_tools(
    make_mapped_repo: RepoFactory, tool: str, value: str
) -> None:
    ctx = _ctx(make_mapped_repo(PY_HERITAGE))
    by_symbol = _call(ctx, tool, {"symbol": value})
    by_name = _call(ctx, tool, {"name": value})
    assert by_name == by_symbol


def test_symbol_alias_matches_symbol_for_add_note(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(SRC)
    by_symbol = _call(
        _ctx(root), "add_note", {"symbol": "f", "text": "note one"}
    )
    by_name = _call(_ctx(root), "add_note", {"name": "f", "text": "note two"})
    assert by_symbol["isError"] is False
    assert by_name["isError"] is False
    assert "noted" in by_symbol["content"][0]["text"]
    assert "noted" in by_name["content"][0]["text"]


def test_get_supertypes_transitive_passthrough(
    make_mapped_repo: RepoFactory,
) -> None:
    files = {
        "a.py": (
            "class A:\n    pass\n\n\nclass B(A):\n    pass\n\n\nclass C(B):\n"
            "    pass\n"
        ),
    }
    ctx = _ctx(make_mapped_repo(files))
    one_hop = _call(ctx, "get_supertypes", {"symbol": "C"})
    assert one_hop["isError"] is False
    assert "class B" in one_hop["content"][0]["text"]
    assert "class A" not in one_hop["content"][0]["text"]

    transitive = _call(
        ctx, "get_supertypes", {"symbol": "C", "transitive": True}
    )
    assert transitive["isError"] is False
    text = transitive["content"][0]["text"]
    assert "class B" in text
    assert "class A" in text


def test_get_supertypes_relation_passthrough(
    make_mapped_repo: RepoFactory,
) -> None:
    files = {
        "Shapes.java": (
            "class Base {}\n"
            "interface IFoo {}\n"
            "class Foo extends Base implements IFoo {}\n"
        ),
    }
    ctx = _ctx(make_mapped_repo(files))
    result = _call(
        ctx, "get_supertypes", {"symbol": "Foo", "relation": "implements"}
    )
    assert result["isError"] is False
    text = result["content"][0]["text"]
    assert "IFoo" in text
    assert "Base" not in text


def test_get_subtypes_excludes_test_files_by_default(
    make_mapped_repo: RepoFactory,
) -> None:
    files = dict(
        PY_HERITAGE,
        **{
            "tests/test_dog.py": (
                "from base import Animal\n"
                "\n"
                "\n"
                "class FakeAnimal(Animal):\n"
                "    pass\n"
            )
        },
    )
    ctx = _ctx(make_mapped_repo(files))
    default_text = _call(ctx, "get_subtypes", {"symbol": "Animal"})["content"][
        0
    ]["text"]
    assert "Dog" in default_text
    assert "FakeAnimal" not in default_text

    included = _call(
        ctx, "get_subtypes", {"symbol": "Animal", "include_tests": True}
    )["content"][0]["text"]
    assert "FakeAnimal" in included


def test_get_subtypes_note_counts_the_hidden_test_subtypes(
    make_mapped_repo: RepoFactory,
) -> None:
    files = dict(
        PY_HERITAGE,
        **{
            "tests/test_dog.py": (
                "from base import Animal\n\n\n"
                "class FakeAnimal(Animal):\n    pass\n\n\n"
                "class StubAnimal(Animal):\n    pass\n"
            )
        },
    )
    ctx = _ctx(make_mapped_repo(files))

    default = _text(ctx, "get_subtypes", {"symbol": "Animal"})
    assert "note: 2 test-file subtypes excluded by default" in default

    for explicit in (True, False):
        text = _text(
            ctx,
            "get_subtypes",
            {"symbol": "Animal", "include_tests": explicit},
        )
        assert "excluded by default" not in text


def test_get_subtypes_no_note_when_no_test_subtypes_exist(
    make_mapped_repo: RepoFactory,
) -> None:
    ctx = _ctx(make_mapped_repo(PY_HERITAGE))
    assert "excluded by default" not in _text(
        ctx, "get_subtypes", {"symbol": "Animal"}
    )


def test_get_subtypes_note_follows_the_transitive_walk(
    make_mapped_repo: RepoFactory,
) -> None:
    """A test subtype two hops down is hidden only from a transitive
    walk, so only that walk's note counts it."""
    files = dict(
        PY_HERITAGE,
        **{
            "tests/test_dog.py": (
                "from dog import Dog\n\n\nclass FakeDog(Dog):\n    pass\n"
            )
        },
    )
    ctx = _ctx(make_mapped_repo(files))
    assert "excluded by default" not in _text(
        ctx, "get_subtypes", {"symbol": "Animal"}
    )
    assert "note: 1 test-file subtype excluded" in _text(
        ctx, "get_subtypes", {"symbol": "Animal", "transitive": True}
    )


def test_get_supertypes_tool_rust_impl(
    make_mapped_repo: RepoFactory,
) -> None:
    files = {
        "shapes.rs": (
            "pub trait Shape {}\n"
            "\n"
            "pub struct Circle;\n"
            "\n"
            "impl Shape for Circle {}\n"
        ),
    }
    ctx = _ctx(make_mapped_repo(files))
    result = _call(ctx, "get_supertypes", {"symbol": "Circle"})
    assert result["isError"] is False
    assert "trait Shape" in result["content"][0]["text"]


def test_get_subtypes_tool_cpp_multiple_inheritance(
    make_mapped_repo: RepoFactory,
) -> None:
    files = {
        "shapes.cpp": (
            "class Base1 {};\n"
            "class Base2 {};\n"
            "class Derived : public Base1, private Base2 {\n"
            "public:\n"
            "};\n"
        ),
    }
    ctx = _ctx(make_mapped_repo(files))
    result = _call(ctx, "get_subtypes", {"symbol": "Base1"})
    assert result["isError"] is False
    assert "class Derived" in result["content"][0]["text"]


def test_get_supertypes_tool_schema_shape() -> None:
    tool = next(t for t in server.TOOLS if t["name"] == "get_supertypes")
    assert set(tool) == {"name", "description", "inputSchema", "handler"}
    schema = tool["inputSchema"]
    assert schema["required"] == ["symbol"]
    props = schema["properties"]
    assert set(props) == {
        "symbol",
        "transitive",
        "relation",
        "budget",
        "include_tests",
        "root",
    }
    assert props["relation"]["enum"] == list(query.HERITAGE_RELATIONS)


def test_get_subtypes_tool_schema_shape() -> None:
    tool = next(t for t in server.TOOLS if t["name"] == "get_subtypes")
    assert set(tool) == {"name", "description", "inputSchema", "handler"}
    schema = tool["inputSchema"]
    assert schema["required"] == ["symbol"]
    props = schema["properties"]
    assert set(props) == {
        "symbol",
        "transitive",
        "relation",
        "budget",
        "include_tests",
        "root",
    }
    assert props["relation"]["enum"] == list(query.HERITAGE_RELATIONS)


def test_symbol_alias_tools_schema_unchanged() -> None:
    # The `name` alias is resolved at the dispatch chokepoint, not in
    # `inputSchema` — `required` must still read exactly `["symbol"]`
    # for every `symbol` tool in `_TARGET_PARAM`, and the shared
    # `_SYMBOL_PROP` description should document the alias.
    alias_tools = {
        "query_symbol",
        "get_callers",
        "get_callees",
        "get_supertypes",
        "get_subtypes",
        "add_note",
    }
    for tool in server.TOOLS:
        if tool["name"] not in alias_tools:
            continue
        schema = tool["inputSchema"]
        assert schema["required"] == ["symbol"] or schema["required"] == [
            "symbol",
            "text",
        ]
        description = schema["properties"]["symbol"]["description"]
        assert "'name', 'target' and 'type' are also accepted" in description
        assert "two of them with different values is an error" in description


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


def test_find_unused_handler_kinds_unexposed(
    make_mapped_repo: RepoFactory,
) -> None:
    # No MCP schema/tool change for --kinds —
    # find_unused stays CLI-only and always runs the unchanged
    # default ("callables") kind, ignoring any stray "kinds" argument.
    assert not any(t["name"] == "find_unused" for t in server.TOOLS)
    ctx = _ctx(make_mapped_repo(SRC))
    out = server.tool_find_unused(ctx, {"kinds": "types"})
    assert "g" in out


SUSPECT_SRC = {
    "foo.py": (
        "class Foo:\n"
        "    def has(self) -> bool:\n"
        "        return True\n\n"
        "    def check(self) -> bool:\n"
        "        return self.has()\n"
    ),
    "a.py": "def has() -> bool:\n    return True\n",
    "b.py": "def has() -> bool:\n    return False\n",
    "c.py": "def caller() -> bool:\n    return has()\n",
}


def test_find_unused_handler_suspect_forwards_to_unused_run(
    make_mapped_repo: RepoFactory,
) -> None:
    # tool_find_unused gains a `suspect` arg forwarded to unused.run, alongside
    # (but not fixing) the pre-existing --kinds omission above.
    ctx = _ctx(make_mapped_repo(SUSPECT_SRC))
    default_out = server.tool_find_unused(ctx, {})
    assert "suspects:" not in default_out

    suspect_out = server.tool_find_unused(ctx, {"suspect": True})
    assert "suspects:" in suspect_out
    assert "dekko ambiguous --name has" in suspect_out


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
    # An error reply that defaulted the root now
    # carries the root line first, like a success reply always did --
    # a wrong-repo query's likeliest outcome IS a not-found error.
    assert text.startswith("(default root: ")
    body = text.split("\n", 1)[1]
    assert body.startswith("dekko: no symbol matches")  # single prefix
    assert body.count("dekko:") == 1


def test_error_with_explicit_root_has_no_root_line(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(SRC)
    result = _call(
        _ctx(root), "query_symbol", {"symbol": "ghost", "root": str(root)}
    )
    assert result["isError"] is True
    assert result["content"][0]["text"].startswith("dekko: no symbol")


def test_query_symbol_tool_reports_unsupported_coverage_gap(
    make_mapped_repo: RepoFactory,
) -> None:
    # MCP-surface twin of
    # test_query.py::test_query_callers_reports_unsupported_coverage_gap
    # — proves query.report_unresolved()'s stderr coverage note (the
    # "may be incomplete" caveat on a not-found reply, added after a
    # whole Astro site went unmapped silently) actually folds into the
    # server.ToolError message across the MCP tool-call path
    # (server._capture -> _relation_tool), not just the CLI's direct
    # stderr print. The wiring was already correct; only this
    # end-to-end test was missing.
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


def test_map_status_fresh_again_after_an_edit_is_reverted(
    make_mapped_repo: RepoFactory,
) -> None:
    """Editing a file makes the map stale; restoring its exact content
    makes it fresh again, even though the file's mtime has moved. A
    long-lived server must decide on content, not on the timestamp.
    """
    root = make_mapped_repo(SRC)
    ctx = _ctx(root)
    target = root / "a.py"
    original = target.read_text()

    target.write_text(original + "# probe\n")
    edited = _text(ctx, "map_status", {})
    assert "stale" in edited and "a.py" in edited

    target.write_text(original)
    reverted = _text(ctx, "map_status", {})
    assert "fresh" in reverted
    assert "stale" not in reverted


def _as_long_lived(
    monkeypatch: pytest.MonkeyPatch, installed: tuple[str, str] | None
) -> None:
    """Make this test process a long-lived server whose *installed*
    dekko (what the child-interpreter arbiter reports) is ``installed``.
    """
    selfcheck.mark_long_lived()
    monkeypatch.setattr(selfcheck, "_ask_child", lambda: installed)


def _stamp_spec(root: Path, spec_hash: str) -> bytes:
    """Rewrite the map's recorded spec_hash; return the new bytes."""
    map_path = root / ".dekko" / "map.json"
    doc = json.loads(map_path.read_text())
    doc["provenance"]["spec_hash"] = spec_hash
    map_path.write_text(json.dumps(doc))
    return map_path.read_bytes()


def test_refresh_map_delegates_when_process_outdated(
    make_mapped_repo: RepoFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An outdated server's in-process regen used to re-extract with
    # stale code and re-stamp "fresh", which once only got a caveat.
    # Now the cause is gone: an outdated
    # process hands the regen to the installed dekko and never
    # extracts in-process.
    root = make_mapped_repo(SRC)
    _stamp_spec(root, "deadbeef")
    _as_long_lived(monkeypatch, (selfcheck.loaded_version(), "deadbeef"))
    delegated: list[tuple[Path, bool]] = []

    def fake_delegated(root: Path, full: bool, quiet: bool) -> int:
        delegated.append((root, full))
        return 0

    def no_in_process(*_a: object, **_k: object) -> int:
        raise AssertionError("outdated process must not extract in-process")

    monkeypatch.setattr(repo_ops, "_delegated_regen", fake_delegated)
    monkeypatch.setattr(repo_ops, "run_map", no_in_process)

    refreshed = _call(_ctx(root), "refresh_map", {"full": True})
    assert refreshed["isError"] is False
    assert delegated == [(root, True)]
    text = refreshed["content"][0]["text"]
    assert "running outdated code" in text
    assert "restart the dekko MCP server" in text


def test_refresh_map_regens_in_process_when_map_is_the_stale_party(
    make_mapped_repo: RepoFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The installed code agrees with this server: the *map* is old.
    # Regenerate in-process as always, and don't tell anyone to
    # restart a server that is perfectly current.
    root = make_mapped_repo(SRC)
    _stamp_spec(root, "deadbeef")
    _as_long_lived(
        monkeypatch, (selfcheck.loaded_version(), selfcheck.loaded_spec())
    )

    def no_delegation(*_a: object, **_k: object) -> int:
        raise AssertionError("a current process must regen in-process")

    monkeypatch.setattr(repo_ops, "_delegated_regen", no_delegation)
    refreshed = _call(_ctx(root), "refresh_map", {})
    assert refreshed["isError"] is False
    text = refreshed["content"][0]["text"]
    assert "restart" not in text
    assert "outdated" not in text
    prov = json.loads((root / ".dekko" / "map.json").read_text())["provenance"]
    assert prov["spec_hash"] == selfcheck.loaded_spec()


def test_refresh_map_no_caveat_when_not_self_stale(
    make_mapped_repo: RepoFactory,
) -> None:
    # Regression guard: the common case (reason == "content", or
    # already fresh) must not gain the restart caveat — only a
    # pre-regen reason == "version" verdict should trigger it.
    root = make_mapped_repo(SRC)
    (root / "a.py").write_text("def f() -> int:\n    return 2\nY = 1\n")

    ctx = _ctx(root)
    refreshed = _call(ctx, "refresh_map", {})
    assert refreshed["isError"] is False
    text = refreshed["content"][0]["text"]
    assert "restart the dekko MCP server process" not in text


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
    # map_status is the MCP-facing surface of the same
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
    # Nothing says this process is outdated, so the
    # map is the stale party and regenerating is the real fix. (It
    # used to say "restart" unconditionally, because nothing could
    # tell the two cases apart.)
    assert "call refresh_map" in text
    assert "restart the dekko MCP server process" not in text


def test_map_status_both_old_says_restart(
    make_mapped_repo: RepoFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Identical ``tool_version`` on both sides while
    # the extractor spec drifted must name ``spec_hash`` explicitly.
    # Here the installed dekko matches neither this process nor the
    # map: the process IS outdated (restart it) and the map is old too.
    root = make_mapped_repo(SRC)
    _stamp_spec(root, "deadbeef")
    _as_long_lived(monkeypatch, (selfcheck.loaded_version(), "feedface"))

    text = _call(_ctx(root), "map_status", {})["content"][0]["text"]
    assert "stale (spec_hash)" in text
    assert "tool_version:" not in text
    assert "deadbeef" in text
    assert "same version string" in text
    assert "restart the dekko MCP server process" in text
    assert "call refresh_map" not in text
    assert "running outdated code" in text


def test_outdated_server_serves_the_installed_codes_map_untouched(
    make_mapped_repo: RepoFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # THE stale-server regression (seen on tensorflow, and it rewrote
    # dekko's own tracked map). A current CLI built
    # this map (spec "deadbeef" stands in for the installed spec);
    # this server is older. It used to call the map stale, re-extract
    # with its old code, and stamp its old spec over it -- which the
    # CLI then called stale and rewrote, forever. It must now leave
    # every byte alone, answer from it, and say it needs a restart.
    root = make_mapped_repo(SRC)
    before = _stamp_spec(root, "deadbeef")
    _as_long_lived(monkeypatch, (selfcheck.loaded_version(), "deadbeef"))

    def no_regen(*_a: object, **_k: object) -> int:
        raise AssertionError("must not regenerate a map that is current")

    monkeypatch.setattr(repo_ops, "regen_map", no_regen)
    ctx = _ctx(root)

    status = _call(ctx, "map_status", {})["content"][0]["text"]
    assert "\nfresh (" in status  # after the default-root line
    assert "stale" not in status
    assert "running outdated code" in status

    summary = _call(ctx, "summary", {})
    assert summary["isError"] is False
    assert "running outdated code" in summary["content"][0]["text"]

    assert (root / ".dekko" / "map.json").read_bytes() == before


def test_outdated_server_drops_its_cached_index_for_the_rebuilt_map(
    make_mapped_repo: RepoFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The server loaded this root *before* dekko was upgraded, so it
    # holds an index whose provenance is its own. A newer CLI then
    # rebuilds the map. The held copy still looks "fresh" against the
    # source tree; it must be dropped for the map that is now on disk.
    root = make_mapped_repo(SRC)
    ctx = _ctx(root)
    assert _call(ctx, "summary", {})["isError"] is False
    held = ctx.index_cache[root]

    _stamp_spec(root, "deadbeef")  # "rebuilt by the newer dekko"
    _as_long_lived(monkeypatch, (selfcheck.loaded_version(), "deadbeef"))
    monkeypatch.setattr(
        repo_ops,
        "regen_map",
        lambda *_a, **_k: pytest.fail("must not regenerate"),
    )

    assert _call(ctx, "summary", {})["isError"] is False
    assert ctx.index_cache[root] is not held
    assert ctx.index_cache[root].provenance["spec_hash"] == "deadbeef"


def test_outdated_server_delegates_regen_on_real_content_change(
    make_mapped_repo: RepoFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An outdated server still has to notice edited source. It does,
    # via the content leg -- and hands the rebuild to the installed
    # dekko rather than extracting with its own stale code.
    root = make_mapped_repo(SRC)
    _stamp_spec(root, "deadbeef")
    _as_long_lived(monkeypatch, (selfcheck.loaded_version(), "deadbeef"))
    (root / "a.py").write_text("def f() -> int:\n    return 2\nY = 1\n")
    delegated: list[Path] = []

    def fake_delegated(root: Path, full: bool, quiet: bool) -> int:
        delegated.append(root)
        return 1  # the child failed: the old map must still be served

    monkeypatch.setattr(repo_ops, "_delegated_regen", fake_delegated)
    summary = _call(_ctx(root), "summary", {})
    assert delegated == [root]
    assert summary["isError"] is False


def test_unprovable_identity_never_writes(
    make_mapped_repo: RepoFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The arbiter child failed (broken install, timeout). A server
    # that can't prove it is current must not author shared state.
    root = make_mapped_repo(SRC)
    before = _stamp_spec(root, "deadbeef")
    _as_long_lived(monkeypatch, None)

    summary = _call(_ctx(root), "summary", {})
    assert summary["isError"] is False
    assert "could not be identified" in summary["content"][0]["text"]
    assert (root / ".dekko" / "map.json").read_bytes() == before


def test_one_shot_process_never_consults_the_arbiter(
    make_mapped_repo: RepoFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Not long-lived (no ``serve()``): current by construction. The
    # child interpreter must never be spawned, and a mismatched map is
    # regenerated in-process exactly as before.
    root = make_mapped_repo(SRC)
    _stamp_spec(root, "deadbeef")

    def no_child() -> None:
        raise AssertionError("a one-shot process must not spawn the arbiter")

    monkeypatch.setattr(selfcheck, "_ask_child", no_child)
    summary = _call(_ctx(root), "summary", {})
    assert summary["isError"] is False
    assert "outdated" not in summary["content"][0]["text"]
    prov = json.loads((root / ".dekko" / "map.json").read_text())["provenance"]
    assert prov["spec_hash"] == selfcheck.loaded_spec()


def test_tool_call_reports_too_new_doc_version_clearly(
    make_mapped_repo: RepoFactory,
) -> None:
    # A long-lived MCP server process (this test's Context stands in
    # for one) that predates a doc-version bump must not surface the
    # opaque "internal error: expected string or bytes-like object,
    # got 'int'" that a downstream shape mismatch would otherwise
    # raise first. mapfile.load_map() now raises
    # MapFormatTooNewError instead, and the MCP tool-call path must
    # translate it into an actionable "restart the server" message
    # rather than falling through to the generic internal-error catch.
    root = make_mapped_repo(SRC)
    map_path = root / ".dekko" / "map.json"
    doc = json.loads(map_path.read_text())
    doc["version"] = mapfile.MAP_DOC_VERSION + 1
    map_path.write_text(json.dumps(doc))

    ctx = _ctx(root)
    result = _call(ctx, "map_status", {})
    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert "restart" in text.lower()
    assert "dekko serve --mcp" in text
    assert "internal error" not in text


def test_tool_call_reports_malformed_doc_version_clearly(
    make_mapped_repo: RepoFactory,
) -> None:
    # A corrupted/mid-write map.json with a non-numeric "version"
    # (null here) must not fall through to the generic "internal
    # error" catch-all with an opaque TypeError message — it needs
    # its own actionable text distinct from the "too new" case, since
    # restarting the server won't fix a broken file.
    root = make_mapped_repo(SRC)
    map_path = root / ".dekko" / "map.json"
    doc = json.loads(map_path.read_text())
    doc["version"] = None
    map_path.write_text(json.dumps(doc))

    ctx = _ctx(root)
    result = _call(ctx, "map_status", {})
    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert "version" in text.lower()
    assert "dekko map" in text
    assert "internal error" not in text


def test_tool_call_reports_persistent_broken_pool_clearly(
    make_mapped_repo: RepoFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A BrokenProcessPool that survives resolver.py's own
    # reduced-parallelism retry (persistent, not transient, sibling
    # multiprocessing contention on the host machine) must not fall
    # through to the generic "internal error: {exc}" catch-all -- it
    # gets its own actionable message pointing at the likely cause and
    # a workaround (`dekko map --jobs 1`), mirroring how
    # MapFormatTooNewError/MapFormatInvalidError are already handled
    # just above this in server.py.
    root = make_mapped_repo(SRC)

    def _raise(*args: object, **kwargs: object) -> None:
        raise BrokenProcessPool(
            "A process in the process pool was terminated abruptly"
        )

    monkeypatch.setattr(repo_ops, "load_or_regen", _raise)

    ctx = _ctx(root)  # fresh Context -> empty index_cache -> forces load
    result = _call(ctx, "query_symbol", {"symbol": "f"})
    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert "process-pool" in text or "process pool" in text
    assert "dekko map --jobs 1" in text
    assert "internal error" not in text


def test_tool_call_reports_stalled_pool_worker_clearly(
    make_mapped_repo: RepoFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A stalled process-pool worker (one that never
    # returns a result at all -- cline's reproduced 6+ minute hang at
    # 0% CPU from a worker resolving the wrong Python interpreter) now
    # surfaces as resolver.PoolStalledError instead of hanging the MCP
    # server indefinitely. Same "point at the fix, don't fall through
    # to the generic internal-error bucket" shape as the
    # BrokenProcessPool test just above.
    root = make_mapped_repo(SRC)

    def _raise(*args: object, **kwargs: object) -> None:
        raise PoolStalledError(
            "process pool made no progress during file extraction "
            "within 600s -- a worker likely failed to start or "
            "stalled. Retry with --jobs 1, or after system load has "
            "subsided."
        )

    monkeypatch.setattr(repo_ops, "load_or_regen", _raise)

    ctx = _ctx(root)  # fresh Context -> empty index_cache -> forces load
    result = _call(ctx, "query_symbol", {"symbol": "f"})
    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert "process pool" in text
    assert "--jobs 1" in text
    assert "internal error" not in text


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


def test_outline_tool_limit_budget_precedence(
    monkeypatch: pytest.MonkeyPatch, make_mapped_repo: RepoFactory
) -> None:
    # The limit/budget half of
    # the fix: mirrors _limit_arg's precedence (query.effective_limit)
    # onto outline's own row-count default (200), not the relation
    # tools' (50) -- neither default is being changed, only which one
    # wins when the caller gives an explicit budget but no limit.
    ctx = _ctx(make_mapped_repo(SRC))
    seen: dict = {}

    def fake_run(index, target, *, root, budget, limit, as_json):  # noqa: ANN001, ANN202
        seen["limit"] = limit
        print("outline")
        return 0

    monkeypatch.setattr(server.outline_mod, "run", fake_run)

    # Neither given: outline's own 200-row default.
    assert _call(ctx, "outline", {"target": "a.py"})["isError"] is False
    assert seen["limit"] == 200

    # Explicit budget alone: budget governs, rows unbounded.
    assert (
        _call(ctx, "outline", {"target": "a.py", "budget": 9000})["isError"]
        is False
    )
    assert seen["limit"] == query.NO_ROW_LIMIT

    # Explicit limit always wins, budget or not.
    assert (
        _call(
            ctx,
            "outline",
            {"target": "a.py", "budget": 9000, "limit": 5},
        )["isError"]
        is False
    )
    assert seen["limit"] == 5


def test_outline_tool_default_budget_discloses_partial_view(
    make_mapped_repo: RepoFactory,
) -> None:
    """The MCP tool's default budget trims a large file, and its reply's
    savings line has to say so rather than read like the whole outline.
    """
    source = "".join(
        f"def func_{i}(alpha: int, beta: str) -> None:\n    pass\n\n\n"
        for i in range(400)
    )
    ctx = _ctx(make_mapped_repo({"big.py": source}))
    result = _call(ctx, "outline", {"target": "big.py"})
    assert result["isError"] is False
    text = result["content"][0]["text"]
    line = next(ln for ln in text.splitlines() if ln.startswith("full ≈"))
    assert "partial: " in line
    assert " of 400 symbols; complete outline ≈ " in line


def test_outline_tool_caps_directory_sparse_notes(
    monkeypatch: pytest.MonkeyPatch, make_mapped_repo: RepoFactory
) -> None:
    # The actual measured
    # regression: outline.py's per-file sparse-file caveat is written
    # to stderr once per file in the target DIRECTORY, independent of
    # outline's own row-level --limit/--budget fit -- on claude-code's
    # 1900-file src/, this alone produced a ~237k-char MCP response.
    # _with_notes forwards ALL of stderr unconditionally, so the cap
    # has to happen before that call.
    ctx = _ctx(make_mapped_repo(SRC))

    def fake_run(index, target, *, root, budget, limit, as_json):  # noqa: ANN001, ANN202
        print("outline: many-files — 30 files")
        for i in range(30):
            print(f"  note: file_{i}.ts — sparse", file=sys.stderr)
        return 0

    monkeypatch.setattr(server.outline_mod, "run", fake_run)
    result = _call(ctx, "outline", {"target": "."})
    assert result["isError"] is False
    text = result["content"][0]["text"]
    note_lines = [ln for ln in text.splitlines() if "— sparse" in ln]
    assert len(note_lines) == server._MAX_OUTLINE_NOTES
    assert "10 more sparse-file note(s) omitted" in text


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
    # No caller budget → DEFAULT_RELATION_BUDGET, not unbounded —
    # get_callers once lost to grep because uncapped output dumped
    # every call site with no default cap.
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
    # No caller budget -> affected.DEFAULT_BUDGET, not unbounded (a
    # single tensorflow commit rendered ~124K uncapped tokens
    # with no --budget default at all on either the CLI or this tool).
    ctx = _ctx(make_mapped_repo(SRC))
    seen: dict = {}

    def fake_run(
        root,  # noqa: ANN001
        rev,  # noqa: ANN001
        as_json,  # noqa: ANN001
        limit,  # noqa: ANN001
        budget=None,  # noqa: ANN001
        jobs=None,  # noqa: ANN001
    ) -> int:
        seen["budget"] = budget
        seen["jobs"] = jobs
        print("impacted")
        return 0

    monkeypatch.setattr(server.affected, "run", fake_run)
    assert _call(ctx, "impacted_tests", {})["isError"] is False
    assert seen["budget"] == server.affected.DEFAULT_BUDGET
    # Never the function's own sequential default.
    assert seen["jobs"] == (os.cpu_count() or 1)

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


def test_find_type_usages_tool_defaults_budget(
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
        exact=False,  # noqa: ANN001
    ) -> int:
        seen["budget"] = budget
        seen["exact"] = exact
        print("type")
        return 0

    monkeypatch.setattr(server.query, "run", fake_run)
    basic = _call(ctx, "find_type_usages", {"type": "Config"})
    assert basic["isError"] is False
    assert seen["budget"] == server.DEFAULT_RELATION_BUDGET
    assert seen["exact"] is False

    result = _call(
        ctx,
        "find_type_usages",
        {"type": "Config", "budget": 9000, "exact": True},
    )
    assert result["isError"] is False
    assert seen["budget"] == 9000
    assert seen["exact"] is True


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
    # value at all) — the failure mode where get_callers lost to
    # grep because nothing capped it.
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


CALLERS_WITH_TESTS = {
    "a.py": "def f() -> int:\n    return 1\n",
    "b.py": (
        "from a import f\n\n\n"
        "def g1() -> int:\n    return f()\n\n\n"
        "def g2() -> int:\n    return f()\n\n\n"
        "def g3() -> int:\n    return f()\n"
    ),
    "tests/test_a.py": (
        "from a import f\n\n\n"
        "def test_f() -> None:\n    assert f() == 1\n\n\n"
        "def test_f_again() -> None:\n    assert f() == 1\n"
    ),
}


def _text(ctx: server.Context, name: str, arguments: dict) -> str:
    return _call(ctx, name, arguments)["content"][0]["text"]


def test_get_callers_note_counts_the_hidden_test_callers(
    make_mapped_repo: RepoFactory,
) -> None:
    """A caller who never mentions ``include_tests`` is told how many
    test-file callers this tool's default dropped, so it can tell
    "nothing hidden" from "go look". A caller who explicitly asks for
    either value gets no note: they already know what they asked for.
    """
    ctx = _ctx(make_mapped_repo(CALLERS_WITH_TESTS))

    default = _text(ctx, "get_callers", {"symbol": "f"})
    assert all(g in default for g in ("g1()", "g2()", "g3()"))
    assert "test_f" not in default
    assert (
        "note: 2 test-file callers excluded by default for this tool"
        in default
    )

    opt_in = _text(ctx, "get_callers", {"symbol": "f", "include_tests": True})
    assert "test_f()" in opt_in and "test_f_again()" in opt_in
    assert "excluded by default" not in opt_in

    opt_out = _text(
        ctx, "get_callers", {"symbol": "f", "include_tests": False}
    )
    assert "test_f" not in opt_out
    assert "excluded by default" not in opt_out


def test_get_callers_note_uses_the_singular_for_one(
    make_mapped_repo: RepoFactory,
) -> None:
    files = dict(CALLERS_WITH_TESTS)
    files["tests/test_a.py"] = (
        "from a import f\n\n\ndef test_f() -> None:\n    assert f() == 1\n"
    )
    ctx = _ctx(make_mapped_repo(files))
    assert "note: 1 test-file caller excluded" in _text(
        ctx, "get_callers", {"symbol": "f"}
    )


def test_get_callers_note_counts_module_level_call_sites(
    make_mapped_repo: RepoFactory,
) -> None:
    """A test file calling the target from its top level is one caller
    that renders as one row per call site, so the note gives both."""
    files = {
        "a.py": "def f() -> int:\n    return 1\n",
        "b.py": "from a import f\n\n\ndef g() -> int:\n    return f()\n",
        "tests/test_a.py": "from a import f\n\nf()\nf()\nf()\n",
    }
    ctx = _ctx(make_mapped_repo(files))
    assert (
        "note: 1 test-file caller (3 call sites) excluded by default"
        in _text(ctx, "get_callers", {"symbol": "f"})
    )


def test_get_callers_no_note_when_no_test_callers_exist(
    make_mapped_repo: RepoFactory,
) -> None:
    """Nothing hidden means nothing to disclose."""
    files = {
        "a.py": "def f() -> int:\n    return 1\n",
        "b.py": "from a import f\n\n\ndef g() -> int:\n    return f()\n",
    }
    ctx = _ctx(make_mapped_repo(files))
    assert "excluded by default" not in _text(
        ctx, "get_callers", {"symbol": "f"}
    )


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
    """A Java package segment literally named `test`
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
    """``query.run`` prints its
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
    ``get_callees``."""
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
    """``render_lean.run`` prints a
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


def test_check_ambiguous_tool(make_mapped_repo: RepoFactory) -> None:
    ctx = _ctx(make_mapped_repo(AMBIGUOUS_CALL))
    result = _call(ctx, "check_ambiguous", {})
    assert result["isError"] is False
    text = result["content"][0]["text"]
    assert "1 ambiguous call sites" in text
    assert "target" in text


def test_check_ambiguous_tool_no_ambiguity(
    make_mapped_repo: RepoFactory,
) -> None:
    files = {"a.py": "def main() -> int:\n    return 1\n"}
    ctx = _ctx(make_mapped_repo(files))
    result = _call(ctx, "check_ambiguous", {})
    assert result["isError"] is False
    assert "no ambiguous call sites" in result["content"][0]["text"]


def test_check_ambiguous_tool_defaults_budget(
    monkeypatch: pytest.MonkeyPatch, make_mapped_repo: RepoFactory
) -> None:
    ctx = _ctx(make_mapped_repo(AMBIGUOUS_CALL))
    seen: dict = {}

    def fake_run(
        index,  # noqa: ANN001
        by,  # noqa: ANN001
        name,  # noqa: ANN001
        top,  # noqa: ANN001
        limit,  # noqa: ANN001
        budget,  # noqa: ANN001
        as_json,  # noqa: ANN001
    ) -> int:
        seen["by"] = by
        seen["name"] = name
        seen["top"] = top
        seen["limit"] = limit
        seen["budget"] = budget
        print("ambiguous")
        return 0

    monkeypatch.setattr(server.ambiguous, "run", fake_run)
    basic = _call(ctx, "check_ambiguous", {})
    assert basic["isError"] is False
    assert seen["by"] is None
    assert seen["name"] is None
    assert seen["top"] == 5
    assert seen["limit"] == 10
    assert seen["budget"] == 500

    result = _call(ctx, "check_ambiguous", {"top": 3, "budget": 9000})
    assert result["isError"] is False
    assert seen["top"] == 3
    assert seen["limit"] == 6
    assert seen["budget"] == 9000


def test_check_ambiguous_tool_schema_has_no_drilldown_params() -> None:
    # The MCP surface is deliberately narrower than the CLI (design
    # doc: summary-only, no --by/--name drill-down) — a schema-shape
    # assertion, not just a behavioral one, so a later "helpful"
    # addition of a 'by'/'name' parameter without revisiting the
    # token-budget tradeoff regresses loudly here.
    tool = next(t for t in server.TOOLS if t["name"] == "check_ambiguous")
    props = set(tool["inputSchema"]["properties"])
    assert props == {"top", "budget", "root"}


def test_require_distinguishes_wrong_type_from_missing() -> None:
    with pytest.raises(server.ToolError, match="must be a string, got int"):
        server._require({"symbol": 42}, "symbol")
    with pytest.raises(server.ToolError, match="missing required argument"):
        server._require({}, "symbol")
    with pytest.raises(server.ToolError, match="missing required argument"):
        server._require({"symbol": ""}, "symbol")


def _many_callers_repo(make_mapped_repo: RepoFactory) -> Path:
    callers = "".join(
        f"def c{i}() -> int:\n    return f()\n\n" for i in range(60)
    )
    return make_mapped_repo(
        {
            "a.py": "def f() -> int:\n    return 1\n",
            "b.py": f"from a import f\n\n{callers}",
        }
    )


def test_budget_zero_uncaps_a_tool(make_mapped_repo: RepoFactory) -> None:
    """``budget: 0`` returns every row instead of the one row a
    literal zero budget used to keep."""
    ctx = _ctx(_many_callers_repo(make_mapped_repo))
    result = _call(ctx, "get_callers", {"symbol": "f", "budget": 0})
    assert not result.get("isError")
    text = result["content"][0]["text"]
    assert sum("b.py:" in ln for ln in text.splitlines()) == 60


def test_negative_budget_is_a_tool_error(
    make_mapped_repo: RepoFactory,
) -> None:
    ctx = _ctx(_many_callers_repo(make_mapped_repo))
    result = _call(ctx, "get_callers", {"symbol": "f", "budget": -1})
    assert result["isError"]
    assert "budget" in result["content"][0]["text"]
