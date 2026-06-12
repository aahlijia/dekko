"""A hand-rolled MCP server exposing the map over stdio.

``lidar serve --mcp`` speaks the Model Context Protocol as
newline-delimited JSON-RPC 2.0 on stdin/stdout, with **no SDK
dependency**. It exposes the read surface (query, context, status) plus
an explicit refresh as MCP tools so an agent can ask "who calls X?"
without reading MAP.md.

Only JSON-RPC messages may touch stdout — every tool reuses the CLI's
renderers under captured stdout/stderr so their output is returned in
the tool result rather than leaking onto the protocol channel.
"""

import io
import json
import sys
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Any

from . import contextpack
from . import mapfile
from . import query
from . import stats
from . import trace
from . import unused

SERVER_NAME = "lidar"
PROTOCOL_VERSION = "2025-06-18"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class ToolError(Exception):
    """A tool failed in a way the agent should see as an error result."""


@dataclass
class Context:
    """Server-wide settings shared across tool calls.

    Attributes:
        default_root: Root used when a tool omits ``root``.
        no_regen: Fail instead of regenerating a stale map on reads.
    """

    default_root: Path
    no_regen: bool


def _capture(fn: Callable[[], int]) -> tuple[int, str, str]:
    """Run ``fn`` with stdout/stderr captured.

    Returns:
        ``(exit_code, stdout, stderr)`` with the streams as strings.
    """
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = fn()
    return code, out.getvalue(), err.getvalue()


def _require(args: dict, key: str) -> str:
    """Return a required string argument or raise ``ToolError``."""
    value = args.get(key)
    if not isinstance(value, str) or not value:
        raise ToolError(f"missing required argument '{key}'")
    return value


def _root_of(ctx: Context, args: dict) -> Path:
    """Resolve the target root from a tool's ``root`` argument."""
    root = args.get("root")
    if isinstance(root, str) and root:
        return Path(root).resolve()
    return ctx.default_root


def _index_for(ctx: Context, args: dict) -> mapfile.MapIndex:
    """Load (auto-regenerating) the map for a tool call."""
    from . import cli

    root = _root_of(ctx, args)
    index, code = cli._load_or_regen(root, ctx.no_regen)
    if index is None:
        raise ToolError(f"no usable map under {root} (exit {code})")
    return index


def _relation_tool(ctx: Context, action: str, args: dict) -> str:
    """Run a query action (symbol/callers/callees) and return text."""
    index = _index_for(ctx, args)
    target = _require(args, "symbol")
    limit = int(args.get("limit", 50))
    code, out, err = _capture(
        lambda: query.run(index, action, target, as_json=False, limit=limit)
    )
    if code != 0:
        raise ToolError(err.strip() or out.strip() or f"exit {code}")
    return out.strip() or f"(no {action} for {target})"


def tool_query_symbol(ctx: Context, args: dict) -> str:
    """Signature card for one symbol."""
    return _relation_tool(ctx, "symbol", args)


def tool_get_callers(ctx: Context, args: dict) -> str:
    """Symbols (and module-level sites) that call the target."""
    return _relation_tool(ctx, "callers", args)


def tool_get_callees(ctx: Context, args: dict) -> str:
    """Symbols the target calls."""
    return _relation_tool(ctx, "callees", args)


def tool_get_context_pack(ctx: Context, args: dict) -> str:
    """Minimal signature neighborhood for editing a symbol or file."""
    index = _index_for(ctx, args)
    target = _require(args, "target")
    hops = int(args.get("hops", 1))
    budget = args.get("budget")
    budget = int(budget) if budget is not None else None
    code, out, err = _capture(
        lambda: contextpack.run(
            index, target, hops=hops, budget=budget, as_json=False
        )
    )
    if code != 0:
        raise ToolError(err.strip() or out.strip() or f"exit {code}")
    return out.strip()


def tool_trace_path(ctx: Context, args: dict) -> str:
    """Shortest call path(s) from one symbol to another."""
    index = _index_for(ctx, args)
    frm = _require(args, "from")
    to = _require(args, "to")
    max_paths = int(args.get("max_paths", 3))
    code, out, err = _capture(
        lambda: trace.run(index, frm, to, max_paths=max_paths, as_json=False)
    )
    if code == trace.EXIT_NO_PATH:
        return out.strip() or err.strip() or f"no path from {frm} to {to}"
    if code != 0:
        raise ToolError(err.strip() or out.strip() or f"exit {code}")
    return out.strip()


def tool_find_unused(ctx: Context, args: dict) -> str:
    """Symbols with no inbound calls (dead-code leads)."""
    index = _index_for(ctx, args)
    roots = args.get("roots") or []
    if not isinstance(roots, list):
        raise ToolError("'roots' must be a list of path globs")
    limit = int(args.get("limit", 50))
    code, out, err = _capture(
        lambda: unused.run(index, tuple(roots), as_json=False, limit=limit)
    )
    if code not in (0, 1):
        raise ToolError(err.strip() or out.strip() or f"exit {code}")
    return out.strip() or "(no unused symbols)"


def tool_stats(ctx: Context, args: dict) -> str:
    """Fan-in/out hotspots, largest files, language mix."""
    index = _index_for(ctx, args)
    top = int(args.get("top", 10))
    code, out, err = _capture(lambda: stats.run(index, top, as_json=False))
    if code != 0:
        raise ToolError(err.strip() or out.strip() or f"exit {code}")
    return out.strip()


def tool_map_status(ctx: Context, args: dict) -> str:
    """Whether the map on disk is fresh, with what changed if stale."""
    root = _root_of(ctx, args)
    index = mapfile.load_map(root)
    if index is None:
        return f"no map.json under {root} (call refresh_map)"
    fresh = mapfile.check_freshness(root, index)
    if fresh.fresh:
        prov = index.provenance or {}
        commit = (prov.get("git_commit") or "no git")[:12]
        return f"fresh ({len(prov.get('files', {}))} files, commit {commit})"
    parts = [f"stale: {len(fresh.changed)} changed"]
    parts.append(f"{len(fresh.added)} added")
    parts.append(f"{len(fresh.removed)} removed")
    detail = ", ".join(parts)
    changed = ", ".join((fresh.changed + fresh.added + fresh.removed)[:10])
    return f"{detail}\n{changed}" if changed else detail


def tool_refresh_map(ctx: Context, args: dict) -> str:
    """Regenerate the map (optionally a full, uncached rebuild)."""
    from . import cli

    root = _root_of(ctx, args)
    full = bool(args.get("full", False))
    code, out, err = _capture(
        lambda: cli.regen_map(root, full=full, quiet=False)
    )
    if code != 0:
        raise ToolError(err.strip() or out.strip() or f"exit {code}")
    return out.strip() or "map refreshed"


_ROOT_PROP = {
    "type": "string",
    "description": "Repo root containing map.json (default: server cwd)",
}
_SYMBOL_PROP = {
    "type": "string",
    "description": "Symbol: name, Class.method, or file.py:name",
}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "query_symbol",
        "description": "Signature, kind, location, and fan-in/out of a "
        "symbol.",
        "inputSchema": {
            "type": "object",
            "properties": {"symbol": _SYMBOL_PROP, "root": _ROOT_PROP},
            "required": ["symbol"],
        },
        "handler": tool_query_symbol,
    },
    {
        "name": "get_callers",
        "description": "List the symbols and module-level sites that call "
        "a symbol.",
        "inputSchema": {
            "type": "object",
            "properties": {"symbol": _SYMBOL_PROP, "root": _ROOT_PROP},
            "required": ["symbol"],
        },
        "handler": tool_get_callers,
    },
    {
        "name": "get_callees",
        "description": "List the symbols a symbol calls.",
        "inputSchema": {
            "type": "object",
            "properties": {"symbol": _SYMBOL_PROP, "root": _ROOT_PROP},
            "required": ["symbol"],
        },
        "handler": tool_get_callees,
    },
    {
        "name": "get_context_pack",
        "description": "Compact signature neighborhood (callers/callees "
        "within N hops) for editing a symbol or file. Token-budgetable.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Symbol or repo-relative file path",
                },
                "hops": {
                    "type": "integer",
                    "description": "Neighborhood radius (default 1)",
                },
                "budget": {
                    "type": "integer",
                    "description": "Approx token budget for the pack",
                },
                "root": _ROOT_PROP,
            },
            "required": ["target"],
        },
        "handler": tool_get_context_pack,
    },
    {
        "name": "trace_path",
        "description": "Shortest call path(s) between two symbols "
        "(how one reaches the other). Empty when there is no path.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "from": {
                    "type": "string",
                    "description": "Source symbol (name, Class.method, "
                    "file.py:name)",
                },
                "to": {
                    "type": "string",
                    "description": "Destination symbol (name, "
                    "Class.method, file.py:name)",
                },
                "max_paths": {
                    "type": "integer",
                    "description": "Max distinct shortest paths (default 3)",
                },
                "root": _ROOT_PROP,
            },
            "required": ["from", "to"],
        },
        "handler": tool_trace_path,
    },
    {
        "name": "find_unused",
        "description": "Symbols with no inbound calls (dead-code leads, "
        "not verdicts). Entry points and exported symbols are excluded.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "roots": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Extra path globs whose symbols are "
                    "always treated as roots",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max result lines (default 50)",
                },
                "root": _ROOT_PROP,
            },
        },
        "handler": tool_find_unused,
    },
    {
        "name": "stats",
        "description": "File/symbol/edge totals, language mix, top "
        "fan-in/out, and largest files.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "top": {
                    "type": "integer",
                    "description": "Entries per ranked list (default 10)",
                },
                "root": _ROOT_PROP,
            },
        },
        "handler": tool_stats,
    },
    {
        "name": "map_status",
        "description": "Report whether map.json is fresh or stale.",
        "inputSchema": {
            "type": "object",
            "properties": {"root": _ROOT_PROP},
        },
        "handler": tool_map_status,
    },
    {
        "name": "refresh_map",
        "description": "Regenerate the map; set full=true to ignore the "
        "cache and re-parse every file.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "full": {
                    "type": "boolean",
                    "description": "Ignore the .lidar cache (cold rebuild)",
                },
                "root": _ROOT_PROP,
            },
        },
        "handler": tool_refresh_map,
    },
]

_HANDLERS: dict[str, Callable[[Context, dict], str]] = {
    t["name"]: t["handler"] for t in TOOLS
}


def _prefixed(message: str) -> str:
    """Ensure a tool error message carries a single ``lidar:`` prefix."""
    return message if message.startswith("lidar:") else f"lidar: {message}"


def _ok(req_id: Any, result: dict) -> dict:
    """Build a JSON-RPC success response."""
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _err(req_id: Any, code: int, message: str) -> dict:
    """Build a JSON-RPC error response."""
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": code, "message": message},
    }


def _handle_initialize(req_id: Any, params: dict) -> dict:
    """Answer the lifecycle ``initialize`` handshake."""
    requested = params.get("protocolVersion")
    version = requested if isinstance(requested, str) else PROTOCOL_VERSION
    return _ok(
        req_id,
        {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": {
                "name": SERVER_NAME,
                "version": _pkg_version("lidar-map"),
            },
        },
    )


def _handle_tools_list(req_id: Any) -> dict:
    """Answer ``tools/list`` with the public tool schemas."""
    listed = [
        {k: t[k] for k in ("name", "description", "inputSchema")}
        for t in TOOLS
    ]
    return _ok(req_id, {"tools": listed})


def _handle_tools_call(ctx: Context, req_id: Any, params: dict) -> dict:
    """Dispatch a ``tools/call`` to a registered handler."""
    name = params.get("name")
    handler = _HANDLERS.get(name)
    if handler is None:
        return _err(req_id, INVALID_PARAMS, f"unknown tool '{name}'")
    args = params.get("arguments") or {}
    try:
        text = handler(ctx, args)
        is_error = False
    except ToolError as exc:
        text, is_error = _prefixed(str(exc)), True
    except Exception as exc:  # surface any tool crash as an error result
        text, is_error = f"lidar: internal error: {exc}", True
    return _ok(
        req_id,
        {"content": [{"type": "text", "text": text}], "isError": is_error},
    )


def handle(ctx: Context, msg: dict) -> dict | None:
    """Route one JSON-RPC message, returning a response or ``None``.

    Notifications (no ``id``) and the ``initialized`` notice yield
    ``None``; requests yield a response dict.
    """
    method = msg.get("method")
    req_id = msg.get("id")
    params = msg.get("params") or {}
    if req_id is None and method != "ping":
        return None  # a notification: acknowledge nothing
    if method == "initialize":
        return _handle_initialize(req_id, params)
    if method == "tools/list":
        return _handle_tools_list(req_id)
    if method == "tools/call":
        return _handle_tools_call(ctx, req_id, params)
    if method == "ping":
        return _ok(req_id, {})
    return _err(req_id, METHOD_NOT_FOUND, f"unknown method '{method}'")


def _send(message: dict) -> None:
    """Write one newline-delimited JSON-RPC message to stdout."""
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def serve(root: Path, no_regen: bool = False) -> int:
    """Run the stdio MCP loop until stdin closes.

    Args:
        root: Default repository root for tools that omit ``root``.
        no_regen: Fail instead of regenerating a stale map on reads.

    Returns:
        Process exit code (0 on clean shutdown).
    """
    ctx = Context(default_root=root.resolve(), no_regen=no_regen)
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            _send(_err(None, PARSE_ERROR, "parse error"))
            continue
        response = handle(ctx, msg)
        if response is not None:
            _send(response)
    return 0
