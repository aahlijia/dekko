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
from dataclasses import dataclass, field
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
from . import search
from . import stats
from . import summary
from . import trace
from . import unused
from . import workset as workset_mod

SERVER_NAME = "dekko"
PROTOCOL_VERSION = "2025-06-18"

# Default token cap for the orientation tools (summary/outline) when the
# caller passes no budget. Their output scales with repo size — a large
# monorepo's un-capped summary renders ~30k chars, and because agents
# call these tools FIRST, that cost is re-read as cache on every later
# turn (2026-07-10 eval: the zed net-negative was mostly this). Callers
# can always pass a larger budget explicitly.
DEFAULT_ORIENT_BUDGET = 2000

# Default token cap for the relation/usage/pack tools (get_callers,
# get_callees, query_symbol, find_usages, get_context_pack) when the
# caller passes no budget. These return a flatter, more repetitive row
# shape than an outline, so half of DEFAULT_ORIENT_BUDGET is a
# reasonable starting cap — a symbol with dozens of call sites (or
# test-file callers included) otherwise renders unbounded output that
# can exceed a naive grep for the same question (2026-07-31 eval:
# get_callers/find_usages both lost to grep on uncapped output).
# Callers can always pass a larger budget explicitly. Sourced from
# ``query.DEFAULT_RELATION_BUDGET`` (which the CLI's ``dekko query``
# now also falls back to) so the two surfaces can't drift apart again
# the way they did when only the MCP path enforced this default.
DEFAULT_RELATION_BUDGET = query.DEFAULT_RELATION_BUDGET

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
        index_cache: In-process cache of the last loaded (unfiltered)
            index per resolved root, reused across tool calls in this
            server session. A long-lived MCP session used to pay the
            full ``map.json`` parse + index-rebuild cost
            (``mapfile.load_map``) on *every single* tool call, even
            back-to-back calls against an unchanged map (round-08
            §2.6) — this cache is checked, and only refreshed on a
            ``mapfile.check_freshness`` miss, so a warm session skips
            straight to the cheap freshness check instead.
    """

    default_root: Path
    no_regen: bool
    index_cache: dict[Path, mapfile.MapIndex] = field(default_factory=dict)


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


def _index_for(
    ctx: Context, args: dict, include_tests: bool = True
) -> mapfile.MapIndex:
    """Load (auto-regenerating) the map for a tool call.

    Checks ``ctx.index_cache`` first: a cached index for this root that
    ``mapfile.check_freshness`` still reports fresh is reused outright,
    skipping ``map.json``'s JSON parse and the full symbol/call-graph
    index rebuild — the dominant cost of a reload, per round-08 §2.6.
    ``check_freshness`` itself still runs on every call (a cheap
    provenance/mtime comparison, not a reload), so a map regenerated
    out-of-band (another process, or this session's own
    ``refresh_map``) is never served stale: correctness comes from
    re-checking on every access, not from catching invalidation events.

    Args:
        ctx: Server-wide settings.
        args: The tool call's raw arguments (for ``root``).
        include_tests: When false, apply ``MapIndex.without_tests()``
            (mirrors the CLI's ``--no-tests`` flag) so test-path
            symbols, edges, and external calls are dropped before the
            tool sees the index. Applied fresh on every call — the
            cache holds only the unfiltered index, since filtering is
            already a cheap view rebuild, not a reload.

    Returns:
        The loaded (optionally filtered) map index.
    """
    root = _root_of(ctx, args)
    cached = ctx.index_cache.get(root)
    if cached is not None and mapfile.check_freshness(root, cached).fresh:
        index = cached
    else:
        from . import cli

        index, code = cli._load_or_regen(root, ctx.no_regen)
        if index is None:
            raise ToolError(f"no usable map under {root} (exit {code})")
        ctx.index_cache[root] = index
    if not include_tests:
        index = index.without_tests()
    return index


def _relation_tool(
    ctx: Context,
    action: str,
    args: dict,
    default_include_tests: bool = True,
) -> str:
    """Run a query action (symbol/callers/callees) and return text.

    Args:
        ctx: Server-wide settings.
        action: One of ``query.ACTIONS`` (``symbol``/``callers``/
            ``callees``).
        args: The tool call's raw arguments.
        default_include_tests: Value to use for ``include_tests`` when
            the caller omits it — tools whose own descriptions pitch
            them as impact-analysis (e.g. ``get_callers``) pass
            ``False`` here since test callers are usually noise.

    Returns:
        Rendered text result, or a placeholder when there are none.
    """
    include_tests = bool(args.get("include_tests", default_include_tests))
    index = _index_for(ctx, args, include_tests=include_tests)
    target = _require(args, "symbol")
    limit = int(args.get("limit", 50))
    sites = bool(args.get("sites", False))
    budget = args.get("budget")
    budget = int(budget) if budget is not None else DEFAULT_RELATION_BUDGET
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
    return _relation_tool(ctx, "callers", args, default_include_tests=False)


def tool_get_callees(ctx: Context, args: dict) -> str:
    """Symbols the target calls."""
    return _relation_tool(ctx, "callees", args)


def tool_find_usages(ctx: Context, args: dict) -> str:
    """Symbols that reference an external (out-of-repo) name."""
    index = _index_for(ctx, args)
    name = _require(args, "name")
    limit = int(args.get("limit", 50))
    budget = args.get("budget")
    budget = int(budget) if budget is not None else DEFAULT_RELATION_BUDGET
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
    budget = int(budget) if budget is not None else DEFAULT_RELATION_BUDGET
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
    budget = int(args.get("budget", DEFAULT_ORIENT_BUDGET))
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
    budget = int(budget) if budget is not None else affected.DEFAULT_BUDGET
    code, out, err = _capture(
        lambda: affected.run(
            root, rev, as_json=False, limit=limit, budget=budget
        )
    )
    if code == affected.EXIT_ERROR:
        raise ToolError(err.strip() or out.strip() or f"exit {code}")
    return out.strip() or "(no impacted tests)"


def tool_search_code(ctx: Context, args: dict) -> str:
    """Free-text relevance search over symbol names, docs, signatures."""
    query_text = _require(args, "query")
    include_tests = bool(args.get("include_tests", False))
    # Load unfiltered first so a not-``include_tests`` call can report
    # how many test-path symbols ``.without_tests()`` dropped before
    # ranking ever saw them (round-08 §2.2's exclusion hint) — mirrors
    # ``cli.run_search``'s own before/after count.
    index = _index_for(ctx, args, include_tests=True)
    excluded_test_count = 0
    if not include_tests:
        filtered = index.without_tests()
        excluded_test_count = len(index.symbols_by_id) - len(
            filtered.symbols_by_id
        )
        index = filtered
    limit = int(args.get("limit", search.DEFAULT_LIMIT))
    budget = args.get("budget")
    budget = int(budget) if budget is not None else search.DEFAULT_BUDGET
    kinds = search.parse_kinds(args.get("kind"))
    scorer_name = args.get("scorer") or search.DEFAULT_SCORER
    code, out, err = _capture(
        lambda: search.run(
            index,
            query_text,
            kinds=kinds,
            limit=limit,
            budget=budget,
            as_json=False,
            root=_root_of(ctx, args),
            scorer_name=scorer_name,
            excluded_test_count=excluded_test_count,
        )
    )
    if code != 0:
        raise ToolError(err.strip() or out.strip() or f"exit {code}")
    return out.strip() or "(no matches)"


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


def _summary_text(ctx: Context, args: dict, budget: int | None = None) -> str:
    """Render the repo digest, reused by the tool and the resource."""
    index = _index_for(ctx, args)
    code, out, err = _capture(
        lambda: summary.run(index, as_json=False, budget=budget)
    )
    if code != 0:
        raise ToolError(err.strip() or out.strip() or f"exit {code}")
    return out.strip()


def tool_summary(ctx: Context, args: dict) -> str:
    """Compact repo digest: directories, hotspots, entry points."""
    budget = int(args.get("budget", DEFAULT_ORIENT_BUDGET))
    return _summary_text(ctx, args, budget=budget)


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
            index,
            root,
            budget=budget,
            as_json=False,
            task=task,
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
        Path(transcript)
        if isinstance(transcript, str) and transcript
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


def _version_stale_detail(fresh: mapfile.Freshness) -> str:
    """Explain *which* staleness signal fired for a "version" verdict.

    ``Freshness.reason == "version"`` collapses two independent
    signals (``tool_version`` mismatch, ``spec_hash`` mismatch) into
    one string. A long-lived ``dekko serve`` process can have an
    identical ``tool_version`` on both sides — Python doesn't hot-
    reload already-imported modules, so a ``uv tool install
    --reinstall`` after the server started has no effect on that
    process's own ``spec_fingerprint()`` output until it restarts —
    which used to read as a self-contradictory "built by dekko 0.21.3,
    running 0.21.3" with no explanation of what was actually stale
    (round-09 §2.3). This names the differentiator explicitly using
    the raw values ``mapfile._freshness_from_provenance`` already
    computed.

    Args:
        fresh: A freshness verdict with ``reason == "version"``.

    Returns:
        A one-line ``"stale (...)"`` prefix naming which signal(s)
        fired and their built-vs-running values.
    """
    which = "+".join(
        name
        for name, stale in (
            ("version", fresh.version_stale),
            ("spec_hash", fresh.spec_stale),
        )
        if stale
    )
    parts: list[str] = []
    if fresh.version_stale:
        parts.append(
            f"tool_version: built by dekko {fresh.built_version}, "
            f"running {fresh.running_version}"
        )
    if fresh.spec_stale:
        built_hash = (fresh.built_spec_hash or "unknown")[:12]
        running_hash = (fresh.running_spec_hash or "unknown")[:12]
        spec_detail = (
            f"spec_hash: map built with extractor spec {built_hash}, "
            f"this process is running spec {running_hash}"
        )
        if not fresh.version_stale:
            spec_detail += (
                f" (same version string {fresh.running_version} on "
                "both sides — this is a long-lived process running "
                "older/different extractor code than what's on disk; "
                "restart it)"
            )
        parts.append(spec_detail)
    return f"stale ({which}): " + "; ".join(parts)


def tool_map_status(ctx: Context, args: dict) -> str:
    """Whether the map on disk is fresh, with what changed if stale."""
    root = _root_of(ctx, args)
    index = mapfile.load_map(root)
    if index is None:
        return f"no map.json under {root} (call refresh_map)"
    fresh = mapfile.check_freshness(root, index)
    note = mapfile.format_unsupported(index.provenance)
    if fresh.fresh:
        prov = index.provenance or {}
        commit = (prov.get("git_commit") or "no git")[:12]
        n = len(prov.get("files", {}))
        status = f"fresh ({n} files, commit {commit})"
        return f"{status}\n{note}" if note else status
    if fresh.reason == "version":
        return f"{_version_stale_detail(fresh)} — call refresh_map"
    parts = [f"stale: {len(fresh.changed)} changed"]
    parts.append(f"{len(fresh.added)} added")
    parts.append(f"{len(fresh.removed)} removed")
    detail = ", ".join(parts)
    if note:
        detail = f"{detail}\n{note}"
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
    "description": "Symbol: name, Class.method, or file.py:name. If the "
    "reply says the target is ambiguous (an overload set sharing the "
    "same file+name), append ':LINE' from one of the printed candidate "
    "rows, e.g. file.py:Class.method:42, to pick that one",
}
_SITES_PROP = {
    "type": "boolean",
    "description": "One row per call site (path:line of each call "
    "expression) instead of one per definition",
}
_BUDGET_PROP = {
    "type": "integer",
    "description": "Approximate token budget (default 800); "
    "lowest-relevance rows are dropped to fit and a cost footer is "
    "appended",
}
_INCLUDE_TESTS_PROP = {
    "type": "boolean",
    "description": "Include results from test files (default: false — "
    "test-file callers are usually noise for impact analysis; set "
    "true to include them)",
}
_TASK_PROP = {
    "type": "string",
    "description": "Rank output by relevance to this task description, "
    "blended with structural centrality and the working diff",
}

# The agent-facing tool surface. Deliberately smaller than the CLI:
# every schema below is sent to the model on each session, so each entry
# pays rent in context tokens. trace_path / find_unused / stats / lean /
# ledger are CLI-only — diagnostic/operator surface with no observed
# agent usage (2026-07-10 eval transcripts) — their handlers remain
# callable and `dekko <cmd>` unaffected.
TOOLS: list[dict[str, Any]] = [
    {
        "name": "search_code",
        "description": "Rank symbols by free-text relevance to a "
        "natural-language description — for when you know what the code "
        "should do but not its name. Matches against names, signatures, "
        "and doc lines with BM25-style scoring, not substring matching. "
        "Falls back to zero hits (not an error) when nothing matches; try "
        "broader or different terms. Use query_symbol/get_callers instead "
        "once you have an exact name.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Free-text description of the code "
                    "you're looking for",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max hits (default 15)",
                },
                "budget": {
                    "type": "integer",
                    "description": "Token budget for the output (default 800)",
                },
                "kind": {
                    "type": "string",
                    "description": "Comma-separated symbol kinds to "
                    "restrict to (function, method, class, ...)",
                },
                "include_tests": {
                    "type": "boolean",
                    "description": "Include test-path symbols "
                    "(default: false)",
                },
                "scorer": {
                    "type": "string",
                    "enum": ["lexical", "embedding"],
                    "description": "Relevance scorer: 'lexical' "
                    "(default, BM25, always available) or 'embedding' "
                    "(hashing-trick embedding; only works if the "
                    "server was installed with the dekko[search] "
                    "extra)",
                },
                "root": _ROOT_PROP,
            },
            "required": ["query"],
        },
        "handler": tool_search_code,
    },
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
        "change. Test-file callers are excluded by default — set "
        "include_tests=true to see them.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": _SYMBOL_PROP,
                "sites": _SITES_PROP,
                "budget": _BUDGET_PROP,
                "include_tests": _INCLUDE_TESTS_PROP,
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
        "within N hops) for editing a symbol or file. Token-budgetable. "
        'Example: target="awardXp", task="who calls this".',
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
                    "description": "Approx token budget for the pack "
                    "(default 800)",
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
                "budget": {
                    "type": "integer",
                    "description": "Approximate token budget (default "
                    "2000); lowest-relevance rows are dropped to fit "
                    "and a cost footer is appended",
                },
                "root": _ROOT_PROP,
            },
            "required": ["target"],
        },
        "handler": tool_outline,
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
        "name": "summary",
        "description": "Compact repo digest (~40 lines): counts, "
        "language mix, per-directory rollup with coupling and purpose, "
        "load-bearing/orchestrating symbols, entry points, parse "
        "errors. Read this before exploring an unfamiliar repo.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "budget": {
                    "type": "integer",
                    "description": "Approximate token cap (default "
                    "2000); trailing sections are shed to fit and a "
                    "footer reports the omission",
                },
                "root": _ROOT_PROP,
            },
        },
        "handler": tool_summary,
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


def _with_default_root_note(ctx: Context, args: dict, text: str) -> str:
    """Prefix a successful reply with the root it actually resolved to.

    Four independent evaluators (2026 fable token-usage tests, bug
    #1/B1) hit the same failure on four different repos/languages:
    omitting ``root`` silently resolves against the server's cwd —
    often dekko's own project, not the repo an agent meant to query —
    and a wrong-repo answer otherwise looks identical in shape to a
    correct one. Requiring ``root`` on every call would be a bigger
    ergonomics regression, and guessing "does this answer plausibly
    belong to the target repo" has its own false-negative risk, so
    this takes the report's own minimum-viable fix: echo the resolved
    root on every reply that used the default, so a wrong-repo answer
    is visually obvious immediately instead of only discovered later.

    Args:
        ctx: Server-wide settings (for the actual default root).
        args: The tool call's raw arguments.
        text: The handler's already-rendered reply text.

    Returns:
        ``text`` prefixed with a one-line root note when ``root`` was
        omitted; unchanged when the caller passed one explicitly.
    """
    root = args.get("root")
    if isinstance(root, str) and root:
        return text
    return (
        f"(root: {ctx.default_root} — no 'root' argument was given; "
        "pass one to target a different repo)\n"
    ) + text


def _handle_tools_call(ctx: Context, req_id: Any, params: dict) -> dict:
    """Dispatch a ``tools/call`` to a registered handler."""
    name = params.get("name")
    handler = _HANDLERS.get(name)
    if handler is None:
        return _err(req_id, INVALID_PARAMS, f"unknown tool '{name}'")
    args = params.get("arguments") or {}
    try:
        text = _with_default_root_note(ctx, args, handler(ctx, args))
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
    """Answer ``resources/read`` for a known resource URI.

    Deliberately unbudgeted, unlike ``tool_summary``: a resource is
    fetched by reference on demand, not re-sent as cache on every
    conversation turn the way a tool result is, so the token-bloat
    concern that justified capping the tool doesn't apply here. See
    ``test_mcp_summary_resource_stays_unbudgeted``.
    """
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
