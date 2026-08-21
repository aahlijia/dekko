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
from concurrent.futures.process import BrokenProcessPool
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Any

from dekko import repo_ops
from dekko.analysis import affected
from dekko.analysis import ambiguous
from dekko.analysis import contextpack
from dekko.storage import ledger as ledger_mod
from dekko.render import mapfile
from dekko.storage import notes as notes_mod
from dekko.analysis import outline as outline_mod
from dekko.analysis import query
from dekko.analysis import relevance
from dekko.render import render_lean
from dekko.analysis import search
from dekko.analysis import stats
from dekko.analysis import summary
from dekko.analysis import trace
from dekko.analysis import unused
from dekko.analysis import workset as workset_mod

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

    ``index_cache`` is intentionally independent of ``daemon.py``'s own
    ``_WarmCache``: this module never imports or talks to ``daemon.py``
    (no ``try_daemon``/socket round trip), so an MCP session and a
    ``dekko daemon start``-ed background process for the same root each
    hold their own separate warm copy of the index in memory, with no
    shared invalidation between them (round-12 master report §3.7/§4.4).
    That's a deliberate, self-contained design for MCP's own
    already-long-lived process, not a stub someone forgot to wire up to
    the daemon — but it does mean ``dekko daemon status``'s cache
    counters never reflect MCP tool-call activity, and, on a repo where
    both are active at once, the map is held warm twice rather than
    shared.

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


def _with_notes(out: str, err: str, fallback: str = "") -> str:
    """Append a successful run's stderr disclosure notes to its result.

    ``_capture()``-based tool handlers reuse the CLI's renderers,
    which print ambiguous-call-count, coverage, and budget-floor
    disclosure notes to stderr rather than stdout even on an
    otherwise-successful (exit 0) run — the CLI shows a human both
    streams, so this loses nothing there. An MCP tool result is
    stdout-only, so without this call every one of those notes
    silently vanished (round-12 master report §3.1): a "47 callers"
    answer looked identical whether or not another 1,385 call sites
    were resolved ambiguously and excluded.

    Args:
        out: Captured stdout from a successful run.
        err: Captured stderr from the same run.
        fallback: Text to use when ``out`` is empty (e.g. "(no
            matches)"); notes are still appended after it.

    Returns:
        ``out`` (or ``fallback``) with any stderr notes appended,
        separated by a blank line.
    """
    text = out.strip() or fallback
    notes = err.strip()
    if not notes:
        return text
    return f"{text}\n\n{notes}" if text else notes


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
        index, code = repo_ops.load_or_regen(root, ctx.no_regen)
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
        Rendered text result, or a placeholder when there are none. When
        ``include_tests`` was silently defaulted to false (the caller
        omitted the argument and ``default_include_tests`` is false for
        this tool), a trailing ``note:`` line discloses the exclusion —
        an explicit ``include_tests=false`` from the caller needs no
        such note, since that filtering was requested, not implicit.
    """
    explicit_include_tests = "include_tests" in args
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
    result = _with_notes(out, err, fallback=f"(no {action} for {target})")
    if not include_tests and not explicit_include_tests:
        result += (
            f"\n\nnote: test-file {action} excluded by default for "
            "this tool; pass include_tests=true to see them."
        )
    return result


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
    return _with_notes(out, err)


def tool_find_type_usages(ctx: Context, args: dict) -> str:
    """Symbols that use a type as a parameter or return type."""
    index = _index_for(ctx, args)
    name = _require(args, "type")
    exact = bool(args.get("exact", False))
    limit = int(args.get("limit", 50))
    budget = args.get("budget")
    budget = int(budget) if budget is not None else DEFAULT_RELATION_BUDGET
    code, out, err = _capture(
        lambda: query.run(
            index,
            "type",
            name,
            as_json=False,
            limit=limit,
            budget=budget,
            exact=exact,
        )
    )
    if code != 0:
        raise ToolError(err.strip() or out.strip() or f"exit {code}")
    return _with_notes(out, err)


def _heritage_tool(
    ctx: Context,
    action: str,
    args: dict,
    default_include_tests: bool = True,
) -> str:
    """Run the supertypes/subtypes query action and return text.

    Mirrors ``_relation_tool``'s shape (index load, error handling,
    fallback text), plus the two heritage-specific arguments
    (``transitive``/``relation``) neither ``callers``/``callees`` nor
    ``symbol`` need.

    Args:
        ctx: Server-wide settings.
        action: ``"supertypes"`` or ``"subtypes"``.
        args: The tool call's raw arguments.
        default_include_tests: Value to use for ``include_tests`` when
            the caller omits it — see ``_relation_tool``.

    Returns:
        Rendered text result, or a placeholder when there are none.
    """
    include_tests = bool(args.get("include_tests", default_include_tests))
    index = _index_for(ctx, args, include_tests=include_tests)
    target = _require(args, "symbol")
    transitive = bool(args.get("transitive", False))
    relation = args.get("relation")
    budget = args.get("budget")
    budget = int(budget) if budget is not None else DEFAULT_RELATION_BUDGET
    code, out, err = _capture(
        lambda: query.run(
            index,
            action,
            target,
            as_json=False,
            limit=int(args.get("limit", 50)),
            budget=budget,
            transitive=transitive,
            relation=relation,
        )
    )
    if code != 0:
        raise ToolError(err.strip() or out.strip() or f"exit {code}")
    return _with_notes(out, err, fallback=f"(no {action} for {target})")


def tool_get_supertypes(ctx: Context, args: dict) -> str:
    """What a type extends/implements/impl-for's — one hop by default."""
    return _heritage_tool(ctx, "supertypes", args)


def tool_get_subtypes(ctx: Context, args: dict) -> str:
    """What extends/implements/impl-for's a type — one hop by default."""
    return _heritage_tool(ctx, "subtypes", args, default_include_tests=False)


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
    return _with_notes(out, err)


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
    return _with_notes(out, err)


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
    return _with_notes(out, err)


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
    return _with_notes(out, err, fallback="(no unused symbols)")


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
    return _with_notes(out, err, fallback="(no impacted tests)")


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
    return _with_notes(out, err, fallback="(no matches)")


def tool_workset(ctx: Context, args: dict) -> str:
    """One budgeted bundle for a change or symbol."""
    root = _root_of(ctx, args)
    rev = args.get("rev")
    rev = rev if isinstance(rev, str) and rev else None
    symbol = args.get("symbol")
    symbol = symbol if isinstance(symbol, str) and symbol else None
    if rev is not None and symbol is not None:
        raise ToolError("give 'rev' or 'symbol', not both")
    type_impact = bool(args.get("type_impact", False))
    if type_impact and symbol is None:
        raise ToolError(
            "'type_impact' requires 'symbol' (a rev diff has no single "
            "target type)"
        )
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
            type_impact=type_impact,
        )
    )
    if code != 0:
        raise ToolError(err.strip() or out.strip() or f"exit {code}")
    return _with_notes(out, err)


def tool_stats(ctx: Context, args: dict) -> str:
    """Fan-in/out hotspots, largest files, language mix."""
    index = _index_for(ctx, args)
    top = int(args.get("top", 10))
    code, out, err = _capture(lambda: stats.run(index, top, as_json=False))
    if code != 0:
        raise ToolError(err.strip() or out.strip() or f"exit {code}")
    return _with_notes(out, err)


def tool_check_ambiguous(ctx: Context, args: dict) -> str:
    """Repo-wide resolver-trust summary: how ambiguous call resolution is.

    Deliberately narrower than the CLI (no ``--by``/``--name``
    drill-down parameters — those stay CLI-only, reachable via
    ``dekko ambiguous --by name`` for an agent with shell access) and
    a tighter default budget (500 vs. ``DEFAULT_RELATION_BUDGET``'s
    800), since this tool's whole value is being a *cheap* sanity
    check before trusting ``get_callers``/``get_callees``/``workset``,
    not a full report.
    """
    index = _index_for(ctx, args)
    top = int(args.get("top", 5))
    budget = args.get("budget")
    budget = int(budget) if budget is not None else 500
    code, out, err = _capture(
        lambda: ambiguous.run(
            index,
            by=None,
            name=None,
            top=top,
            limit=top * 2,
            budget=budget,
            as_json=False,
        )
    )
    if code != 0:
        raise ToolError(err.strip() or out.strip() or f"exit {code}")
    return _with_notes(out, err)


def _summary_text(ctx: Context, args: dict, budget: int | None = None) -> str:
    """Render the repo digest, reused by the tool and the resource."""
    index = _index_for(ctx, args)
    code, out, err = _capture(
        lambda: summary.run(index, as_json=False, budget=budget)
    )
    if code != 0:
        raise ToolError(err.strip() or out.strip() or f"exit {code}")
    return _with_notes(out, err)


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
    return _with_notes(out, err)


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
    return f"noted {sym.id} ({sym.path}:{sym.start_line})"


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
    return _with_notes(out, err)


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
    root = _root_of(ctx, args)
    full = bool(args.get("full", False))
    code, out, err = _capture(
        lambda: repo_ops.regen_map(root, full=full, quiet=False)
    )
    if code != 0:
        raise ToolError(err.strip() or out.strip() or f"exit {code}")
    return _with_notes(out, err, fallback="map refreshed")


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
                    "enum": list(search.SCORER_CHOICES),
                    "description": "Relevance scorer: 'lexical' "
                    "(default, BM25, always available), 'embedding' "
                    "(hashing-trick embedding), or 'both' (fuses "
                    "lexical + embedding rankings via reciprocal rank "
                    "fusion) — 'embedding' and 'both' only work if the "
                    "server was installed with the dekko[search] "
                    "extra",
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
        "without reading its body. Walks the resolved call graph "
        "directly instead of grepping the body for names that look "
        "like calls.",
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
        "with call sites — one call gets every real call site across "
        "the repo, where grepping the bare name also pulls in imports, "
        "comments, and unrelated same-named locals you'd have to "
        "hand-filter.",
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
        "name": "find_type_usages",
        "description": "Every function/method that uses a type as a "
        "parameter or return type — for 'what breaks if I change this "
        "struct/class's shape' questions the call graph alone can't "
        "answer, since a function can use a type without calling "
        "anything on it. Matches the bare type name inside wrapper "
        "syntax (Optional[Config], Vec<Config>, Config | None all match "
        "'Config') unless exact=true. Only functions/methods carry "
        "typed params/returns — struct/class fields typed with the "
        "target type are not covered.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "description": "Type/class/struct/interface name "
                    "to search for, e.g. 'Config'",
                },
                "exact": {
                    "type": "boolean",
                    "description": "Match the declared type text "
                    "exactly instead of the bare identifier inside "
                    "wrapper syntax (default false)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max result lines (default 50)",
                },
                "budget": _BUDGET_PROP,
                "root": _ROOT_PROP,
            },
            "required": ["type"],
        },
        "handler": tool_find_type_usages,
    },
    {
        "name": "get_supertypes",
        "description": "What a class/interface/struct/trait extends, "
        "implements, or is impl'd for — its own declared heritage. Set "
        "transitive=true for the full ancestor chain/DAG (multiple "
        "inheritance and multi-interface implementation both fan out, "
        "not a single line). Covers Python/JavaScript/TypeScript/Java/"
        "Rust/C++. Go struct embedding is not extracted (only answers "
        "composition, not interface satisfaction, so not worth the "
        "confusion) and Go's structural interface satisfaction has no "
        "declaring syntax to extract at all — no tree-sitter query can "
        "see it.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": _SYMBOL_PROP,
                "transitive": {
                    "type": "boolean",
                    "description": "Full ancestor chain/DAG instead of "
                    "one hop (default false)",
                },
                "relation": {
                    "type": "string",
                    "enum": list(query.HERITAGE_RELATIONS),
                    "description": "Filter to one heritage-relation "
                    "kind ('embeds' is Go struct embedding, not "
                    "extracted, and never appears in current "
                    "results)",
                },
                "budget": _BUDGET_PROP,
                "include_tests": _INCLUDE_TESTS_PROP,
                "root": _ROOT_PROP,
            },
            "required": ["symbol"],
        },
        "handler": tool_get_supertypes,
    },
    {
        "name": "get_subtypes",
        "description": "What extends, implements, or is impl'd for a "
        "class/interface/struct/trait — the 'if I change this, who's "
        "affected' blast-radius question for type declarations. Set "
        "transitive=true for every direct and indirect implementor, "
        "not just direct ones. Does not include each implementor's own "
        "callers — pair with get_callers on individual results for "
        "that. Test-file subtypes are excluded by default — set "
        "include_tests=true to see them.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": _SYMBOL_PROP,
                "transitive": {
                    "type": "boolean",
                    "description": "Every direct and indirect "
                    "implementor instead of just direct ones (default "
                    "false)",
                },
                "relation": {
                    "type": "string",
                    "enum": list(query.HERITAGE_RELATIONS),
                    "description": "Filter to one heritage-relation "
                    "kind ('embeds' is Go struct embedding, not "
                    "extracted, and never appears in current "
                    "results)",
                },
                "budget": _BUDGET_PROP,
                "include_tests": _INCLUDE_TESTS_PROP,
                "root": _ROOT_PROP,
            },
            "required": ["symbol"],
        },
        "handler": tool_get_subtypes,
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
        "analysis misses fixtures and dynamic dispatch). More reliable "
        "than grepping test files for the changed symbol's name, which "
        "misses indirect callers and matches unrelated same-named text.",
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
        "budget. One call replaces affected + N outlines + N packs — "
        "and grepping a diff for touched names then reading each file "
        "whole to work it. Set type_impact=true when the target is a "
        "class/interface/struct/trait to also union in every type-usage "
        "site and implementor into the touched set — the full blast "
        "radius of changing a shared type's shape, not just its direct "
        "callers.",
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
                "type_impact": {
                    "type": "boolean",
                    "description": "Also include type-usage sites and "
                    "implementors in the touched set (only meaningful "
                    "when 'symbol' is a class/interface/struct/trait; "
                    "no-op otherwise). Requires 'symbol'. default false",
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
        "name": "check_ambiguous",
        "description": "Repo-wide resolver-trust summary: total ambiguous "
        "call sites, the ambiguous rate, and the top colliding names/files. "
        "Run this before leaning on get_callers/get_callees/workset for an "
        "impact-analysis decision on a repo with generic/common method "
        "names — a low ambiguous rate means the call graph is trustworthy "
        "as-is; a high one concentrated in a few files means spot-check "
        "those files' call sites by hand before trusting the graph there.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "top": {
                    "type": "integer",
                    "description": "Top-N entries per ranking (default 5)",
                },
                "budget": {
                    "type": "integer",
                    "description": "Approx token budget (default 500)",
                },
                "root": _ROOT_PROP,
            },
        },
        "handler": tool_check_ambiguous,
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
    except mapfile.MapFormatTooNewError:
        # This MCP server process has been running since before the
        # map.json on disk was regenerated in a newer on-disk format
        # (e.g. after a `dekko` upgrade) — its in-memory parsing code
        # predates that format and can't safely read it. Restarting
        # the process (not the repo) is the fix, so say that plainly
        # instead of surfacing whatever opaque shape-mismatch error
        # would otherwise fire first (see
        # .features/fixes/stale-map-json-mcp-crash.md).
        text, is_error = (
            "dekko: this MCP server process was started before "
            "map.json was last regenerated in a newer format (e.g. "
            "after a dekko upgrade), so it can no longer read it. "
            "Restart the MCP server (dekko serve --mcp) to pick up "
            "the current version.",
            True,
        )
    except mapfile.MapFormatInvalidError:
        # map.json's "version" field itself is malformed (null,
        # non-numeric, ...) — the document is corrupted or was read
        # mid-write, not merely newer than this process understands.
        # Restarting the server won't fix a broken file, so point at
        # regenerating the map instead (see
        # .features/fixes/stale-map-json-mcp-crash.md).
        text, is_error = (
            'dekko: map.json\'s "version" field is missing or '
            "invalid, so it can't be read. The file may be "
            "corrupted or truncated. Regenerate it with `dekko map` "
            "(or call refresh_map).",
            True,
        )
    except BrokenProcessPool:
        # A process pool broke twice in a row (once on the first
        # attempt, once on round 17's reduced-parallelism retry —
        # see resolver.py's run_pooled_with_retry) -- persistent, not
        # transient, contention. Most often another concurrent
        # ``dekko`` process on this machine (e.g. a heavy `dekko map
        # --jobs 0`) is oversubscribing the CPU badly enough that
        # even a small worker pool can't start up. Point at the fix
        # instead of surfacing the raw "A process in the process pool
        # was terminated abruptly..." text.
        text, is_error = (
            "dekko: map regeneration failed twice due to a process-pool "
            "failure (often caused by heavy CPU/multiprocessing load from "
            "another concurrent dekko process on this machine). Try again "
            "once system load drops, or run `dekko map --jobs 1` manually "
            "against this repo to avoid the parallel pool entirely.",
            True,
        )
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
