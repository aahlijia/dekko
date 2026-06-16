"""A hand-rolled MCP server exposing the map over stdio.

``dekko serve --mcp`` speaks the Model Context Protocol as
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

from . import affected
from . import contextpack
from . import ledger as ledger_mod
from . import mapfile
from . import notes as notes_mod
from . import outline as outline_mod
from . import query
from . import relevance
from . import render_lean
from . import stats
from . import summary
from . import trace
from . import unused
from . import workset as workset_mod

SERVER_NAME = "dekko"
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


def _task_of(ctx: Context, args: dict) -> relevance.TaskContext | None:
    """Build a task context from a tool's ``task`` argument, or ``None``."""
    text = args.get("task")
    if not (isinstance(text, str) and text):
        return None
    return relevance.task_context(text, _root_of(ctx, args))


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
    sites = bool(args.get("sites", False))
    budget = args.get("budget")
    budget = int(budget) if budget is not None else None
    code, out, err = _capture(
        lambda: query.run(
            index,
            action,
            target,
            as_json=False,
            limit=limit,
            sites=sites,
            budget=budget,
        )
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


def tool_find_usages(ctx: Context, args: dict) -> str:
    """Symbols that reference an external (out-of-repo) name."""
    index = _index_for(ctx, args)
    name = _require(args, "name")
    limit = int(args.get("limit", 50))
    budget = args.get("budget")
    budget = int(budget) if budget is not None else None
    code, out, err = _capture(
        lambda: query.run(
            index, "uses", name, as_json=False, limit=limit, budget=budget
        )
    )
    if code != 0:
        raise ToolError(err.strip() or out.strip() or f"exit {code}")
    return out.strip()


def tool_get_context_pack(ctx: Context, args: dict) -> str:
    """Minimal signature neighborhood for editing a symbol or file."""
    index = _index_for(ctx, args)
    target = _require(args, "target")
    hops = int(args.get("hops", 1))
    budget = args.get("budget")
    budget = int(budget) if budget is not None else None
    with_source = bool(args.get("with_source", False))
    root = _root_of(ctx, args)
    task = _task_of(ctx, args)
    code, out, err = _capture(
        lambda: contextpack.run(
            index,
            target,
            hops=hops,
            budget=budget,
            as_json=False,
            root=root,
            with_source=with_source,
            task=task,
        )
    )
    if code != 0:
        raise ToolError(err.strip() or out.strip() or f"exit {code}")
    return out.strip()


def tool_outline(ctx: Context, args: dict) -> str:
    """Structural outline of a file or directory (signatures, no bodies)."""
    index = _index_for(ctx, args)
    target = _require(args, "target")
    limit = int(args.get("limit", 200))
    budget = args.get("budget")
    budget = int(budget) if budget is not None else None
    root = _root_of(ctx, args)
    code, out, err = _capture(
        lambda: outline_mod.run(
            index,
            target,
            root=root,
            budget=budget,
            limit=limit,
            as_json=False,
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
    budget = args.get("budget")
    budget = int(budget) if budget is not None else None
    code, out, err = _capture(
        lambda: unused.run(
            index, tuple(roots), as_json=False, limit=limit, budget=budget
        )
    )
    if code not in (0, 1):
        raise ToolError(err.strip() or out.strip() or f"exit {code}")
    return out.strip() or "(no unused symbols)"


def tool_impacted_tests(ctx: Context, args: dict) -> str:
    """Test files impacted by changes since a git rev."""
    root = _root_of(ctx, args)
    rev = args.get("rev")
    rev = rev if isinstance(rev, str) and rev else None
    limit = int(args.get("limit", 8))
    budget = args.get("budget")
    budget = int(budget) if budget is not None else None
    code, out, err = _capture(
        lambda: affected.run(
            root, rev, as_json=False, limit=limit, budget=budget
        )
    )
    if code == affected.EXIT_ERROR:
        raise ToolError(err.strip() or out.strip() or f"exit {code}")
    return out.strip() or "(no impacted tests)"


def tool_workset(ctx: Context, args: dict) -> str:
    """One budgeted bundle for a change or symbol."""
    root = _root_of(ctx, args)
    rev = args.get("rev")
    rev = rev if isinstance(rev, str) and rev else None
    symbol = args.get("symbol")
    symbol = symbol if isinstance(symbol, str) and symbol else None
    if rev is not None and symbol is not None:
        raise ToolError("give 'rev' or 'symbol', not both")
    budget = args.get("budget")
    budget = int(budget) if budget is not None else workset_mod.DEFAULT_BUDGET
    packs = int(args.get("packs", workset_mod.DEFAULT_PACKS))
    task = _task_of(ctx, args)
    code, out, err = _capture(
        lambda: workset_mod.run(
            root,
            rev,
            symbol,
            budget=budget,
            packs=packs,
            as_json=False,
            no_regen=False,
            task=task,
        )
    )
    if code != 0:
        raise ToolError(err.strip() or out.strip() or f"exit {code}")
    return out.strip()


def tool_stats(ctx: Context, args: dict) -> str:
    """Fan-in/out hotspots, largest files, language mix."""
    index = _index_for(ctx, args)
    top = int(args.get("top", 10))
    code, out, err = _capture(lambda: stats.run(index, top, as_json=False))
    if code != 0:
        raise ToolError(err.strip() or out.strip() or f"exit {code}")
    return out.strip()


def _summary_text(ctx: Context, args: dict) -> str:
    """Render the repo digest, reused by the tool and the resource."""
    index = _index_for(ctx, args)
    code, out, err = _capture(lambda: summary.run(index, as_json=False))
    if code != 0:
        raise ToolError(err.strip() or out.strip() or f"exit {code}")
    return out.strip()


def tool_summary(ctx: Context, args: dict) -> str:
    """Compact repo digest: directories, hotspots, entry points."""
    return _summary_text(ctx, args)


def tool_lean(ctx: Context, args: dict) -> str:
    """Budget-capped navigation map of the whole repo."""
    index = _index_for(ctx, args)
    root = _root_of(ctx, args)
    budget = args.get("budget")
    budget = int(budget) if budget is not None else None
    task = _task_of(ctx, args)
    dense = bool(args.get("dense", False))
    code, out, err = _capture(
        lambda: render_lean.run(
            index, root, budget=budget, as_json=False, task=task,
            dense=dense,
        )
    )
    if code != 0:
        raise ToolError(err.strip() or out.strip() or f"exit {code}")
    return out.strip()


def tool_add_note(ctx: Context, args: dict) -> str:
    """Anchor a durable note to a symbol."""
    index = _index_for(ctx, args)
    target = _require(args, "symbol")
    text = _require(args, "text")
    sym, candidates = query.resolve_target(index, target)
    if sym is None:
        if candidates:
            raise ToolError(f"'{target}' is ambiguous ({len(candidates)})")
        raise ToolError(f"no symbol matches '{target}'")
    notes_mod.add(_root_of(ctx, args), sym.id, text)
    return f"noted {sym.id}"


def tool_list_notes(ctx: Context, args: dict) -> str:
    """List notes for a symbol, or all notes in the repo."""
    root = _root_of(ctx, args)
    target = args.get("symbol")
    all_notes = notes_mod.load(root)
    if isinstance(target, str) and target:
        index = _index_for(ctx, args)
        sym, _ = query.resolve_target(index, target)
        if sym is None:
            raise ToolError(f"no symbol matches '{target}'")
        records = all_notes.get(sym.id, [])
        if not records:
            return f"(no notes for {sym.id})"
        return "\n".join(f"{sym.id}: {r.get('text', '')}" for r in records)
    if not any(all_notes.values()):
        return "(no notes)"
    lines = []
    for sym_id, records in sorted(all_notes.items()):
        lines += [f"{sym_id}: {r.get('text', '')}" for r in records]
    return "\n".join(lines)


def tool_ledger(ctx: Context, args: dict) -> str:
    """What this session has already put in context (from the transcript)."""
    root = _root_of(ctx, args)
    transcript = args.get("transcript")
    transcript = (
        Path(transcript) if isinstance(transcript, str) and transcript
        else None
    )
    session = args.get("session")
    session = session if isinstance(session, str) and session else None
    budget = args.get("budget")
    budget = int(budget) if budget is not None else None
    code, out, err = _capture(
        lambda: ledger_mod.run(
            root, transcript, session, budget, as_json=False
        )
    )
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
_SITES_PROP = {
    "type": "boolean",
    "description": "One row per call site (path:line of each call "
    "expression) instead of one per definition",
}
_BUDGET_PROP = {
    "type": "integer",
    "description": "Approximate token budget; lowest-relevance rows are "
    "dropped to fit and a cost footer is appended",
}
_TASK_PROP = {
    "type": "string",
    "description": "Rank output by relevance to this task description, "
    "blended with structural centrality and the working diff",
}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "query_symbol",
        "description": "Signature, kind, location, doc, fan-in/out, and "
        "notes for one symbol — the fast way to learn what a symbol is "
        "without reading its file.",
        "inputSchema": {
            "type": "object",
            "properties": {"symbol": _SYMBOL_PROP, "root": _ROOT_PROP},
            "required": ["symbol"],
        },
        "handler": tool_query_symbol,
    },
    {
        "name": "get_callers",
        "description": "Every symbol (and module-level site) that calls "
        "a symbol — exact call edges, unlike grep, which can't tell a "
        "call from a same-named string. Set sites=true for the precise "
        "path:line of each call. Use for impact analysis before a "
        "change.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": _SYMBOL_PROP,
                "sites": _SITES_PROP,
                "budget": _BUDGET_PROP,
                "root": _ROOT_PROP,
            },
            "required": ["symbol"],
        },
        "handler": tool_get_callers,
    },
    {
        "name": "get_callees",
        "description": "Every in-repo symbol a symbol calls (set "
        "sites=true for call-site lines) — what this code depends on, "
        "without reading its body.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": _SYMBOL_PROP,
                "sites": _SITES_PROP,
                "budget": _BUDGET_PROP,
                "root": _ROOT_PROP,
            },
            "required": ["symbol"],
        },
        "handler": tool_get_callees,
    },
    {
        "name": "find_usages",
        "description": "List the symbols that reference an external "
        "(out-of-repo) name, e.g. a stdlib or third-party function, "
        "with call sites.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Base identifier of the external "
                    "reference (e.g. 'run' for subprocess.run, 'Path')",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max result lines (default 50)",
                },
                "budget": _BUDGET_PROP,
                "root": _ROOT_PROP,
            },
            "required": ["name"],
        },
        "handler": tool_find_usages,
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
                "with_source": {
                    "type": "boolean",
                    "description": "Inline the target's source body and "
                    "hop-1 call-site lines (default false; counts "
                    "against budget)",
                },
                "task": _TASK_PROP,
                "root": _ROOT_PROP,
            },
            "required": ["target"],
        },
        "handler": tool_get_context_pack,
    },
    {
        "name": "outline",
        "description": "A file's (or directory's) structural outline — "
        "signatures + doc lines, no bodies — at roughly a tenth the cost "
        "of reading it. Prefer this before reading a file to learn what "
        "it contains.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Mapped file path or directory "
                    "(suffix-matched); a directory rolls up its files",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max symbol rows (default 200)",
                },
                "budget": _BUDGET_PROP,
                "root": _ROOT_PROP,
            },
            "required": ["target"],
        },
        "handler": tool_outline,
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
                "budget": _BUDGET_PROP,
                "root": _ROOT_PROP,
            },
        },
        "handler": tool_find_unused,
    },
    {
        "name": "impacted_tests",
        "description": "Test files a runner should exercise after a "
        "change: reverse call-graph reachability from changed symbols "
        "plus an import-edge fallback (leads, not verdicts — static "
        "analysis misses fixtures and dynamic dispatch).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "rev": {
                    "type": "string",
                    "description": "Git rev to compare against (default: "
                    "the commit the map was generated at, else HEAD)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max impacted symbols per test file "
                    "(default 8)",
                },
                "budget": _BUDGET_PROP,
                "root": _ROOT_PROP,
            },
        },
        "handler": tool_impacted_tests,
    },
    {
        "name": "workset",
        "description": "Task work-set: for a change (git rev) or a "
        "symbol, bundle the touched files' outlines plus call-graph "
        "packs for the most central touched symbols under one token "
        "budget. One call replaces affected + N outlines + N packs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "rev": {
                    "type": "string",
                    "description": "Git rev to bundle changes against "
                    "(default: the commit the map was generated at, else "
                    "HEAD); omit when using 'symbol'",
                },
                "symbol": {
                    "type": "string",
                    "description": "Seed from a symbol instead of a diff "
                    "(name, Class.method, file.py:name); not with 'rev'",
                },
                "budget": {
                    "type": "integer",
                    "description": "Shared token budget for the whole "
                    "bundle (default 6000)",
                },
                "packs": {
                    "type": "integer",
                    "description": "Top-centrality touched symbols to "
                    "deep-pack (default 5)",
                },
                "task": _TASK_PROP,
                "root": _ROOT_PROP,
            },
        },
        "handler": tool_workset,
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
        "name": "summary",
        "description": "Compact repo digest (~40 lines): counts, "
        "language mix, per-directory rollup with coupling and purpose, "
        "load-bearing/orchestrating symbols, entry points, parse "
        "errors. Read this before exploring an unfamiliar repo.",
        "inputSchema": {
            "type": "object",
            "properties": {"root": _ROOT_PROP},
        },
        "handler": tool_summary,
    },
    {
        "name": "lean",
        "description": "A budget-capped navigation map of the whole "
        "repo: every file with its purpose, symbols (signatures on the "
        "most central, names on the rest), and module dependency edges, "
        "shed in priority order to fit a token cap. Denser than "
        "`summary`, far cheaper than reading MAP.md — read it to orient "
        "before exploring. The header reports what was elided and how to "
        "recover it (`outline`, `context`, `query`).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "budget": {
                    "type": "integer",
                    "description": "Hard token cap (default scales with "
                    "repo size)",
                },
                "dense": {
                    "type": "boolean",
                    "description": "Terser skin: signatures only on the "
                    "most-central symbols, names for the rest",
                },
                "task": _TASK_PROP,
                "root": _ROOT_PROP,
            },
        },
        "handler": tool_lean,
    },
    {
        "name": "add_note",
        "description": "Anchor a durable note to a symbol. Notes are "
        "committed to .dekko/notes.json and shown on the symbol's card "
        "and in its context pack. Use after a non-obvious change so the "
        "rationale survives.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": _SYMBOL_PROP,
                "text": {
                    "type": "string",
                    "description": "The note text",
                },
                "root": _ROOT_PROP,
            },
            "required": ["symbol", "text"],
        },
        "handler": tool_add_note,
    },
    {
        "name": "list_notes",
        "description": "List notes anchored to a symbol, or every note "
        "in the repo when no symbol is given.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Symbol to list notes for (omit for "
                    "all notes)",
                },
                "root": _ROOT_PROP,
            },
        },
        "handler": tool_list_notes,
    },
    {
        "name": "ledger",
        "description": "What this session has already loaded into context "
        "(files read, symbols seen, tokens consumed), projected from the "
        "session transcript. Use it to avoid re-fetching context the "
        "agent already holds.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "transcript": {
                    "type": "string",
                    "description": "Session JSONL path (default: the "
                    "latest transcript for this repo under ~/.claude)",
                },
                "session": {
                    "type": "string",
                    "description": "Session id to resolve when discovering",
                },
                "budget": {
                    "type": "integer",
                    "description": "Report remaining tokens vs this budget",
                },
                "root": _ROOT_PROP,
            },
        },
        "handler": tool_ledger,
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
                    "description": "Ignore the .dekko cache (cold rebuild)",
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

_SUMMARY_URI = "dekko://summary"
RESOURCES: list[dict[str, str]] = [
    {
        "uri": _SUMMARY_URI,
        "name": "Repo summary",
        "description": "Compact digest of the mapped repository "
        "(counts, directories, hotspots, entry points).",
        "mimeType": "text/plain",
    }
]


def _prefixed(message: str) -> str:
    """Ensure a tool error message carries a single ``dekko:`` prefix."""
    return message if message.startswith("dekko:") else f"dekko: {message}"


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
            "capabilities": {"tools": {}, "resources": {}},
            "serverInfo": {
                "name": SERVER_NAME,
                "version": _pkg_version("dekko"),
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
        text, is_error = f"dekko: internal error: {exc}", True
    return _ok(
        req_id,
        {"content": [{"type": "text", "text": text}], "isError": is_error},
    )


def _handle_resources_list(req_id: Any) -> dict:
    """Answer ``resources/list`` with the published resources."""
    return _ok(req_id, {"resources": RESOURCES})


def _handle_resources_read(ctx: Context, req_id: Any, params: dict) -> dict:
    """Answer ``resources/read`` for a known resource URI."""
    uri = params.get("uri")
    if uri != _SUMMARY_URI:
        return _err(req_id, INVALID_PARAMS, f"unknown resource '{uri}'")
    try:
        text = _summary_text(ctx, {})
    except ToolError as exc:
        return _err(req_id, INTERNAL_ERROR, _prefixed(str(exc)))
    return _ok(
        req_id,
        {"contents": [{"uri": uri, "mimeType": "text/plain", "text": text}]},
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
    if method == "resources/list":
        return _handle_resources_list(req_id)
    if method == "resources/read":
        return _handle_resources_read(ctx, req_id, params)
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
