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
from pathlib import Path
from typing import Any

from dekko import repo_ops
from dekko import selfcheck
from dekko.analysis import affected
from dekko.analysis import ambiguous
from dekko.analysis import contextpack
from dekko.core.resolver import PoolStalledError
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
# turn (on zed, the token net-negative was mostly this). Callers can
# always pass a larger budget explicitly.
DEFAULT_ORIENT_BUDGET = 2000

# Default token cap for the relation/usage/pack tools (get_callers,
# get_callees, query_symbol, find_usages, get_context_pack) when the
# caller passes no budget. These return a flatter, more repetitive row
# shape than an outline, so half of DEFAULT_ORIENT_BUDGET is a
# reasonable starting cap — a symbol with dozens of call sites (or
# test-file callers included) otherwise renders unbounded output that
# can exceed a naive grep for the same question (get_callers and
# find_usages both lost to grep on uncapped output).
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
    shared invalidation between them.
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
            back-to-back calls against an unchanged map — this cache
            is checked, and only refreshed on a
            ``mapfile.check_freshness`` miss, so a warm session skips
            straight to the cheap freshness check instead.
    """

    default_root: Path
    no_regen: bool
    index_cache: dict[Path, mapfile.MapIndex] = field(default_factory=dict)


# Worker count for a rev-cache-miss old-side re-parse/resolve behind
# ``impacted_tests``/``workset``. These tools used to
# call ``affected.run``/``workset.run`` without ``jobs`` at all, so they
# inherited the functions' own sequential default: a first-touch call
# on tensorflow ran single-threaded for 12+ minutes, far past any MCP
# client's patience, while the same request with all cores finishes in
# about four. Same value the CLI's ``--jobs 0`` default resolves to;
# ``resolver._pool_workers`` still scales it down to what the work
# justifies, so a small repo never pays for a pool it can't use.
_COLD_REV_JOBS = repo_ops.resolve_workers(0)


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
    silently vanished: a "47 callers"
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


# Cap on the sparse-file caveats a directory ``outline`` forwards
# through ``_with_notes``. ``outline``'s row content is already budget-capped
# (``DEFAULT_ORIENT_BUDGET``/``_outline_limit_arg``), but
# ``outline.py``'s per-file "few named symbols" caveat
# (``_sparse_note``) is emitted to stderr once per file in the
# *target directory*, independent of which files' rows actually
# survived that cap -- so on a large directory the notes alone grew
# past 8k tokens even though the visible outline content stayed
# small. Live-verified on claude-code's 1900-file ``src/``: an
# uncapped MCP call returned ~237k chars, almost entirely sparse-file
# notes for files whose own rows never made it into the output at
# all. Scoped to ``tool_outline`` alone, not folded into
# ``_with_notes`` itself -- every other tool's stderr disclosures are
# already small and already governed by the same content-level cap
# their row output uses.
_MAX_OUTLINE_NOTES = 20


def _capped_notes(err: str, max_notes: int = _MAX_OUTLINE_NOTES) -> str:
    """Keep at most ``max_notes`` lines of ``err``, footer-disclosed.

    A note about a file whose own outline rows didn't survive
    ``outline``'s row-level budget/limit fit isn't actionable anyway —
    there's nothing shown for that file to act on — so capping the
    note *count* here, independent of the row-level fit, is the right
    knob rather than trying to thread this cap back through
    ``outline.py``'s own rendering.

    Args:
        err: Raw captured stderr (``outline``'s only stderr output is
            one ``note:`` line per sparse file; see
            ``outline._sparse_note``).
        max_notes: Maximum note lines to keep.

    Returns:
        ``err`` unchanged when it has ``max_notes`` lines or fewer;
        otherwise the first ``max_notes`` lines plus a trailing
        omission line in the same "raise --X" footer shape the rest
        of the tool suite uses.
    """
    lines = err.strip().splitlines()
    if len(lines) <= max_notes:
        return err
    omitted = len(lines) - max_notes
    kept = lines[:max_notes]
    kept.append(
        f"note: {omitted} more sparse-file note(s) omitted — narrow "
        "the target (a subdirectory, or one file) to see them all"
    )
    return "\n".join(kept)


def _outline_limit_arg(args: dict) -> int:
    """Row-count cap for ``tool_outline`` — mirrors ``_limit_arg``'s
    budget/limit precedence (see its own docstring and
    ``query.effective_limit``: an explicit ``budget`` with no explicit
    ``limit`` lets the budget govern alone, since the tool's own
    default budget doesn't count as "the caller chose one"), scoped to
    ``outline``'s own row-count default (200, matching the CLI's own
    ``--limit`` default) rather than the relation tools' (``query.
    DEFAULT_LIMIT``, 50) — the two tools' row shapes and defaults have
    never been the same, only the *precedence rule* is being mirrored
    here.
    """
    limit = args.get("limit")
    return outline_mod.effective_limit(
        int(limit) if limit is not None else None, args.get("budget")
    )


def _require(args: dict, key: str) -> str:
    """Return a required string argument or raise ``ToolError``."""
    value = args.get(key)
    if value is not None and not isinstance(value, str):
        # Present but mistyped is a different mistake from absent, and
        # "missing" sends the caller hunting for a key it already sent.
        raise ToolError(
            f"argument '{key}' must be a string, got {type(value).__name__}"
        )
    if not value:
        raise ToolError(f"missing required argument '{key}'")
    return value


# Each target-taking tool's own name for its target argument. The
# names differ by tool (``find_usages`` takes ``name``,
# ``find_type_usages`` takes ``type``, ``outline`` takes ``target``),
# and agents calling several in a row guess by analogy, so any of the
# others is accepted in its place (see ``_resolve_target_alias``). Two
# different values under two of these names is an error, not a pick.
# A new target-taking tool belongs here too.
_TARGET_PARAM = {
    "query_symbol": "symbol",
    "get_callers": "symbol",
    "get_callees": "symbol",
    "get_supertypes": "symbol",
    "get_subtypes": "symbol",
    "add_note": "symbol",
    "find_usages": "name",
    "find_type_usages": "type",
    "get_context_pack": "target",
    "outline": "target",
}
_TARGET_ALIASES = ("symbol", "name", "target", "type")


def _budget_arg(args: dict, default: int | None) -> int | None:
    """A tool's ``budget`` argument: ``default`` when absent, ``0`` for
    no cap (read as uncapped by ``textutil.fit_to_budget``).

    Raises:
        ToolError: For a negative or non-integer budget.
    """
    raw = args.get("budget")
    if raw is None:
        return default
    try:
        budget = int(raw)
    except (TypeError, ValueError):
        raise ToolError(
            f"argument 'budget' must be an integer, got {raw!r}"
        ) from None
    if budget < 0:
        raise ToolError(
            f"argument 'budget' must be 0 (no cap) or more, got {budget}"
        )

    return budget


def _limit_arg(args: dict) -> int:
    """Row limit for a query-backed tool (see ``query.effective_limit``).

    An explicit ``budget`` with no explicit ``limit`` lets the budget
    govern alone, same as the CLI. The tool's *default* budget doesn't
    count: only a caller who actually chose a budget has said how much
    output they can take.
    """
    limit = args.get("limit")
    return query.effective_limit(
        int(limit) if limit is not None else None,
        _budget_arg(args, None),
    )


def _resolve_target_alias(tool_name: str, args: dict) -> dict:
    """Accept any target-argument name in place of the tool's own.

    Agents calling several target tools in a row guess the argument
    name by analogy, so ``symbol``, ``name``, ``target`` and ``type``
    are interchangeable on every tool that takes a target. Two of them
    carrying different values is a caller bug (a stale value, a
    copy-paste), and picking one would hide it behind a confident
    answer about the wrong symbol; that case errors, whichever names
    are involved. A ``null`` value counts as absent.

    Args:
        tool_name: The tool being invoked.
        args: The call's raw arguments, as received.

    Returns:
        ``args`` unchanged when no alias needs folding, or a shallow
        copy with the tool's own target argument set and every alias
        name removed.

    Raises:
        ToolError: If two target names carry different values.
    """
    primary = _TARGET_PARAM.get(tool_name)
    if primary is None:
        return args
    given = {k: args[k] for k in _TARGET_ALIASES if args.get(k) is not None}
    if not given:
        return args
    if len({str(v) for v in given.values()}) > 1:
        pairs = ", ".join(f"{k}={v!r}" for k, v in given.items())
        raise ToolError(
            f"got {pairs} naming different targets; pass one "
            f"'{primary}' argument"
        )
    if [k for k in _TARGET_ALIASES if k in args] == [primary]:
        return args

    folded = {k: v for k, v in args.items() if k not in _TARGET_ALIASES}
    folded[primary] = next(iter(given.values()))

    return folded


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
    index rebuild — the dominant cost of a reload.
    ``check_freshness`` itself still runs on every call (a cheap
    provenance/mtime comparison, not a reload), so a map regenerated
    out-of-band (another process, or this session's own
    ``refresh_map``) is never served stale: correctness comes from
    re-checking on every access, not from catching invalidation events.
    ``check_freshness`` alone can't see a regen that didn't follow a
    source change (a newer dekko rebuilding the map after an upgrade),
    so ``index_matches_disk`` checks that too: one ``stat``.

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
    if (
        cached is not None
        and mapfile.index_matches_disk(root, cached)
        and mapfile.check_freshness(root, cached).fresh
    ):
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
        this tool) and that actually hid rows, a trailing ``note:``
        line says how many — an explicit ``include_tests=false`` from
        the caller needs no such note, since that filtering was
        requested, not implicit.
    """
    explicit_include_tests = "include_tests" in args
    include_tests = bool(args.get("include_tests", default_include_tests))
    full = _index_for(ctx, args)
    index = full if include_tests else full.without_tests()
    target = _require(args, "symbol")
    limit = _limit_arg(args)
    sites = bool(args.get("sites", False))
    budget = _budget_arg(args, DEFAULT_RELATION_BUDGET)
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
    if include_tests or explicit_include_tests:
        return result

    hidden, sites = query.hidden_test_rows(full, index, action, target)
    return result + _hidden_tests_note(action, hidden, sites)


def _hidden_tests_note(action: str, hidden: int, sites: int) -> str:
    """Trailing note for what a tool's default test filter removed.

    Empty when nothing was hidden, so "no note" means "no test rows",
    and a count tells the caller whether re-querying is worth it.

    Args:
        action: The plural relation name (``callers``, ``subtypes``).
        hidden: Distinct callers/types the default filter removed.
        sites: The call sites those account for; shown when it
            differs from ``hidden`` (a module-level test caller is one
            caller but one output row per call site).

    Returns:
        The note, with its leading blank line, or ``""``.
    """
    if hidden <= 0:
        return ""

    noun = action if hidden != 1 else action.removesuffix("s")
    detail = f" ({sites} call sites)" if sites != hidden else ""
    return (
        f"\n\nnote: {hidden} test-file {noun}{detail} excluded by "
        "default for this tool; pass include_tests=true to see them."
    )


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
    limit = _limit_arg(args)
    budget = _budget_arg(args, DEFAULT_RELATION_BUDGET)
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
    limit = _limit_arg(args)
    budget = _budget_arg(args, DEFAULT_RELATION_BUDGET)
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
        Same silent-default disclosure rule as ``_relation_tool``: when
        ``include_tests`` was defaulted to false for this tool, the
        caller didn't say so, and rows were hidden, a trailing
        ``note:`` line says how many.
    """
    explicit_include_tests = "include_tests" in args
    include_tests = bool(args.get("include_tests", default_include_tests))
    full = _index_for(ctx, args)
    index = full if include_tests else full.without_tests()
    target = _require(args, "symbol")
    transitive = bool(args.get("transitive", False))
    relation = args.get("relation")
    budget = _budget_arg(args, DEFAULT_RELATION_BUDGET)
    code, out, err = _capture(
        lambda: query.run(
            index,
            action,
            target,
            as_json=False,
            limit=_limit_arg(args),
            budget=budget,
            transitive=transitive,
            relation=relation,
        )
    )
    if code != 0:
        raise ToolError(err.strip() or out.strip() or f"exit {code}")
    result = _with_notes(out, err, fallback=f"(no {action} for {target})")
    if include_tests or explicit_include_tests:
        return result

    hidden, sites = query.hidden_test_rows(
        full,
        index,
        action,
        target,
        transitive=transitive,
        relation=relation,
    )
    return result + _hidden_tests_note(action, hidden, sites)


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
    budget = _budget_arg(args, DEFAULT_RELATION_BUDGET)
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
    limit = _outline_limit_arg(args)
    budget = _budget_arg(args, DEFAULT_ORIENT_BUDGET)
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
    return _with_notes(out, _capped_notes(err))


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
    budget = _budget_arg(args, None)
    # ``None`` lets ``unused.run`` apply its defaults, including the
    # suspects section's own cap, which any explicit limit replaces.
    given = args.get("limit") is not None or budget is not None
    limit = _limit_arg(args) if given else None
    suspect = bool(args.get("suspect", False))
    code, out, err = _capture(
        lambda: unused.run(
            index,
            tuple(roots),
            as_json=False,
            limit=limit,
            budget=budget,
            suspect=suspect,
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
    budget = _budget_arg(args, affected.DEFAULT_BUDGET)
    code, out, err = _capture(
        lambda: affected.run(
            root,
            rev,
            as_json=False,
            limit=limit,
            budget=budget,
            jobs=_COLD_REV_JOBS,
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
    # ranking ever saw them (the exclusion hint) — mirrors
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
    budget = _budget_arg(args, search.DEFAULT_BUDGET)
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
    budget = _budget_arg(args, workset_mod.DEFAULT_BUDGET)
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
            jobs=_COLD_REV_JOBS,
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
    budget = _budget_arg(args, 500)
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
    budget = _budget_arg(args, DEFAULT_ORIENT_BUDGET)
    return _summary_text(ctx, args, budget=budget)


def tool_lean(ctx: Context, args: dict) -> str:
    """Budget-capped navigation map of the whole repo."""
    index = _index_for(ctx, args)
    root = _root_of(ctx, args)
    budget = _budget_arg(args, None)
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
        # Same reply the CLI gives: the candidate rows (with the
        # ':LINE' form that picks one) or the closest-match list, not
        # a bare count the agent can't act on without a second call.
        _, _, err = _capture(
            lambda: query.report_unresolved(target, candidates, index)
        )
        raise ToolError(err.strip() or f"no symbol matches '{target}'")
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
    budget = _budget_arg(args, None)
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

    Thin MCP-surface wrapper around the shared signal-naming logic in
    ``mapfile.describe_version_stale`` — kept here so callers in this
    module don't need to import the parts-builder directly, and so the
    MCP surface can grow its own wording later without touching
    ``mapfile.py`` again.

    Args:
        fresh: A freshness verdict with ``reason == "version"``.

    Returns:
        A one-line ``"stale (...)"`` prefix naming which signal(s)
        fired and their built-vs-running values.
    """
    return mapfile.describe_version_stale(fresh)


def _version_stale_action(fresh: mapfile.Freshness) -> str:
    """Suggested next step for a ``reason == "version"`` verdict.

    ``refresh_map`` regenerates *in-process*, using whatever extractor
    code this same MCP server process already has loaded — Python
    doesn't hot-reload imported modules, so if *this* process is the
    stale party, an in-process regen cannot pick up the newer code.
    Previously nothing could tell which party was stale, so this
    always said "restart". Now ``fresh.process_outdated`` carries the
    answer (``selfcheck.classify`` asked the disk): an outdated process
    says restart, and a current process looking at a genuinely old map
    says regenerate.

    Args:
        fresh: A freshness verdict with ``reason == "version"``.

    Returns:
        A short "next step" suffix, no leading punctuation.
    """
    if fresh.process_outdated:
        return "restart the dekko MCP server process"

    # The installed code agrees with this process,
    # so the *map* is the stale party and a regen is the real fix.
    # Any read tool will do it automatically; ``refresh_map`` forces it.
    return "call refresh_map (any read tool also regenerates it)"


def tool_map_status(ctx: Context, args: dict) -> str:
    """Whether the map on disk is fresh, with what changed if stale.

    Reads the small provenance sidecar first (``mapfile.
    load_provenance``), the same cheap path ``dekko status`` and
    ``dekko doctor`` take: this tool's whole point is the staleness
    fact *without* paying for anything else, and a full ``load_map``
    parses the entire ``map.json`` (hundreds of MB on a large repo) to
    answer a question the sidecar already holds. Falls back to the
    full load only for a map written before the sidecar existed.
    ``check_version=True`` keeps the too-new / malformed ``map.json``
    detection ``load_map`` gave this tool: those errors propagate to
    ``_handle_tools_call``, which already turns them into the restart
    and regenerate instructions.
    """
    root = _root_of(ctx, args)
    prov = mapfile.load_provenance(root, check_version=True)
    if prov is not None:
        fresh = mapfile.check_freshness_provenance(root, prov)
    else:
        index = mapfile.load_map(root)
        if index is None:
            return f"no map.json under {root} (call refresh_map)"
        fresh = mapfile.check_freshness(root, index)
        prov = index.provenance
    note = mapfile.format_unsupported(prov)
    if fresh.fresh:
        prov = prov or {}
        commit = (prov.get("git_commit") or "no git")[:12]
        n = len(prov.get("files", {}))
        status = f"fresh ({n} files, commit {commit})"
        return f"{status}\n{note}" if note else status
    if fresh.reason == "version":
        return (
            f"{_version_stale_detail(fresh)} — {_version_stale_action(fresh)}"
        )
    parts = [f"stale: {len(fresh.changed)} changed"]
    parts.append(f"{len(fresh.added)} added")
    parts.append(f"{len(fresh.removed)} removed")
    detail = ", ".join(parts)
    if note:
        detail = f"{detail}\n{note}"
    changed = ", ".join((fresh.changed + fresh.added + fresh.removed)[:10])
    return f"{detail}\n{changed}" if changed else detail


def tool_refresh_map(ctx: Context, args: dict) -> str:
    """Regenerate the map (optionally a full, uncached rebuild).

    An outdated server's in-process regen re-extracts with stale code
    and re-stamps the result "fresh", which dekko used to settle for
    disclosing. This removes the problem instead:
    ``repo_ops.regen_map`` hands the whole regen to the installed dekko
    whenever this process is outdated (``selfcheck.process_outdated``),
    so the map is always built by current code. The standing
    outdated-server note on every reply (``_with_outdated_note``)
    covers the disclosure.
    """
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
    "rows, e.g. file.py:Class.method:42, to pick that one. 'name', "
    "'target' and 'type' are also accepted as aliases for this argument; "
    "two of them with different values is an error.",
}
_SITES_PROP = {
    "type": "boolean",
    "description": "One row per call site (path:line of each call "
    "expression) instead of one per definition",
}
_BUDGET_PROP = {
    "type": "integer",
    "description": "Approximate token budget (default 800; 0 = no "
    "cap); "
    "lowest-relevance rows are dropped to fit and a cost footer is "
    "appended",
}
_INCLUDE_TESTS_PROP = {
    "type": "boolean",
    "description": "Include results from test files (default: false — "
    "test-file callers are usually noise for impact analysis; set "
    "true to include them)",
}
# get_supertypes defaults the other way: a type's own declared
# heritage is the same whether or not test files are in the index, so
# there is nothing to filter and no reason to surprise a caller by
# dropping a test-only base class. Only the tools that walk *inbound*
# relations (get_callers, get_subtypes) default to excluding tests.
_INCLUDE_TESTS_DEFAULT_TRUE_PROP = {
    "type": "boolean",
    "description": "Include results from test files (default: true; "
    "set false to drop test-path types from the index first)",
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
# agent usage in real agent transcripts. Their ``tool_*`` handler
# functions above are kept and exercised directly by tests, but they
# are not registered in ``_HANDLERS`` (built from this list), so an
# MCP client cannot reach them; `dekko <cmd>` is unaffected.
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
                    "description": "Token budget for the output (default "
                    "800; 0 = no cap)",
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
        "description": "List the symbols that call into an external "
        "(out-of-repo) name, with call sites and a summary line "
        "(site/file counts, top members used, importing-file count). "
        "Ask by function ('run' finds subprocess.run), by import "
        "binding ('chalk' finds every chalk.red/chalk.dim call; 'np' "
        "finds np.array), or by module ('numpy', 'node:path', 'fs' "
        "find calls through whatever the file bound them to). Calls "
        "only: type-position, JSX and property reads are not recorded, "
        "and the summary says how many files import the name so a low "
        "count isn't read as the whole story.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "An external name: a function's "
                    "own name ('run'), an imported binding ('chalk', "
                    "'np', 'React'), or a module ('numpy', 'node:path'). "
                    "'symbol', 'target' and 'type' are also accepted; two "
                    "with different values is an error.",
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
        "description": "Every parameter or return annotation that "
        "uses a type — for 'what breaks if I change this "
        "struct/class's shape' questions the call graph alone can't "
        "answer, since a function can use a type without calling "
        "anything on it. Covers every function-shaped site: named "
        "functions/methods, and (TS/TSX) callbacks and returned arrow "
        "functions, function-typed interface members, method and "
        "overload signatures, reported under their enclosing "
        "definition. Matches the bare type name inside wrapper "
        "syntax (Optional[Config], Vec<Config>, Config | None all match "
        "'Config') unless exact=true. Struct/class fields, generic "
        "arguments and JSX typed with the target are not covered.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "description": "Type/class/struct/interface name "
                    "to search for, e.g. 'Config'. 'symbol', 'name' and "
                    "'target' are also accepted; two with different "
                    "values is an error.",
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
                "include_tests": _INCLUDE_TESTS_DEFAULT_TRUE_PROP,
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
                    "description": "Symbol or repo-relative file path. "
                    "'symbol', 'name' and 'type' are also accepted; two "
                    "with different values is an error.",
                },
                "hops": {
                    "type": "integer",
                    "description": "Neighborhood radius (default 1)",
                },
                "budget": {
                    "type": "integer",
                    "description": "Approx token budget for the pack "
                    "(default 800; 0 = no cap)",
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
                    "(suffix-matched); a directory rolls up its files. "
                    "'symbol', 'name' and 'type' are also accepted; two "
                    "with different values is an error.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max symbol rows (default 200; if "
                    "omitted and budget is set explicitly, budget "
                    "governs instead — the 200 default doesn't stack "
                    "with it)",
                },
                "budget": {
                    "type": "integer",
                    "description": "Approximate token budget (default "
                    "2000; 0 = no cap); lowest-relevance rows are "
                    "dropped to fit and a cost footer is appended. On a "
                    "directory "
                    "target, sparse-file caveats are separately capped "
                    "and disclosed if truncated",
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
                "budget": {
                    "type": "integer",
                    "description": "Approximate token budget (default "
                    f"{affected.DEFAULT_BUDGET}; 0 = no cap); "
                    "weakest-tier test files are dropped first to fit",
                },
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
                    "bundle (default 6000; 0 = no cap)",
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
                    "description": "Approx token budget (default 500; 0 = "
                    "no cap)",
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
                    "2000; 0 = no cap); trailing sections are shed to fit "
                    "and a "
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
                "version": selfcheck.loaded_version(),
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

    Agents on four different repos/languages hit the same failure:
    omitting ``root`` silently resolves against the server's cwd —
    often dekko's own project, not the repo an agent meant to query —
    and a wrong-repo answer otherwise looks identical in shape to a
    correct one. Requiring ``root`` on every call would be a bigger
    ergonomics regression, and guessing "does this answer plausibly
    belong to the target repo" has its own false-negative risk, so
    this takes the minimum-viable fix: echo the resolved
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
        args = _resolve_target_alias(name, args)
        text = _with_default_root_note(ctx, args, handler(ctx, args))
        is_error = False
    except ToolError as exc:
        # The root line used to be applied only to
        # successful replies. The likeliest outcome of asking one
        # repo's question of another repo's map is a not-found error
        # with plausible closest-matches from the wrong repo -- the
        # one reply shape that carried no root.
        text = _with_default_root_note(ctx, args, _prefixed(str(exc)))
        is_error = True
    except mapfile.MapFormatTooNewError:
        # This MCP server process has been running since before the
        # map.json on disk was regenerated in a newer on-disk format
        # (e.g. after a `dekko` upgrade) — its in-memory parsing code
        # predates that format and can't safely read it. Restarting
        # the process (not the repo) is the fix, so say that plainly
        # instead of surfacing whatever opaque shape-mismatch error
        # would otherwise fire first.
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
        # regenerating the map instead.
        text, is_error = (
            'dekko: map.json\'s "version" field is missing or '
            "invalid, so it can't be read. The file may be "
            "corrupted or truncated. Regenerate it with `dekko map` "
            "(or call refresh_map).",
            True,
        )
    except BrokenProcessPool:
        # A process pool broke twice in a row (once on the first
        # attempt, once on the reduced-parallelism retry —
        # see resolver.py's run_pooled_with_retry), so a worker
        # crashed under ``spawn`` too. Point at the fix instead of
        # surfacing the raw "A process in the process pool was
        # terminated abruptly..." text, without guessing a cause.
        text, is_error = (
            "dekko: map regeneration failed twice because a process-pool "
            "worker crashed. Try again, or run `dekko map --jobs 1` "
            "manually against this repo to avoid the parallel pool "
            "entirely.",
            True,
        )
    except PoolStalledError as exc:
        # A process-pool worker never returned a result within
        # resolver.POOL_RESULT_TIMEOUT_S (a spawned
        # worker resolving a completely different Python interpreter
        # than its own parent hung indefinitely at 0% CPU with no
        # error). Same "point at the fix" shape as the
        # BrokenProcessPool handler above, since the underlying cause
        # and remedy are the same -- this just catches the "hung, not
        # crashed" half of that failure family.
        text, is_error = (f"dekko: {exc}", True)
    except Exception as exc:  # surface any tool crash as an error result
        text, is_error = f"dekko: internal error: {exc}", True
    return _ok(
        req_id,
        {
            "content": [{"type": "text", "text": _with_outdated_note(text)}],
            "isError": is_error,
        },
    )


def _with_outdated_note(text: str) -> str:
    """Append the "this server is outdated" note when it is.

    On every reply, success or error, for as long as the condition
    holds: it is abnormal, the fix is one action (restart), and an
    agent that saw it once forty calls ago has forgotten. Costs two
    ``stat`` calls when nothing changed on disk (``selfcheck``'s memo).
    Never lets a failure in the check itself break a tool reply.
    """
    try:
        outdated = selfcheck.process_outdated()
    except Exception:  # the note is advisory; the reply is not
        return text
    if not outdated:
        return text

    return f"{text}\nnote: {selfcheck.outdated_note('server')}"


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
    # Long-lived by definition: from here on, an identity mismatch
    # with a map means "ask the disk who is outdated", and an outdated
    # verdict means never extracting in-process again (``selfcheck``).
    selfcheck.mark_long_lived()
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
