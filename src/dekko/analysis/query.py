"""Query the loaded map index: callers, callees, symbols, files.

Targets use the agreed syntax: bare ``name``, ``Class.method``,
``file.py:name``, or ``file.py:Class.method``. File qualifiers match
on the full repo-relative path or any trailing path suffix. When two
or more candidates share the same ``(path, qualname)`` — Java/C++-style
overloads — append the candidate's own ``start_line`` as a third,
colon-separated segment (``file.py:Class.method:LINE``) to pick one;
``report_unresolved`` prints this form in its ambiguity hint whenever
plain ``path:qualname`` can't narrow the set.
"""

import difflib
import io
import json
import re
import sys
from collections import deque
from collections.abc import Iterable
from contextlib import redirect_stdout

from dekko.classify import is_test_path, relevance_key
from dekko.render.mapfile import MapIndex, format_unsupported
from dekko.core import languages
from dekko.core.model import (
    TYPE_KINDS,
    CatchSite,
    EnvRead,
    ExternalCall,
    Import,
    Symbol,
)
from dekko.textutil import Meter, fit_to_budget, signature, token_footer
from dekko.core.resolver import MODULE_CALLER_SUFFIX, bare_import_source

EXIT_OK = 0
EXIT_NOT_FOUND = 3
EXIT_AMBIGUOUS = 4
# CLI-level usage error (a required TARGET missing for an action other
# than 'env --list') — mirrors ``workset.EXIT_ERROR``'s value; used by
# ``cli.run_query`` before ``run()`` is even called, not by anything
# in this module itself.
EXIT_USAGE_ERROR = 2

ACTIONS = (
    "callers",
    "callees",
    "symbol",
    "file",
    "uses",
    "type",
    "supertypes",
    "subtypes",
    "importers",
    "peers",
    "throws",
    "catches",
    "env",
    "cohesion",
)

# Valid ``--relation``/``relation=`` filter values for supertypes/
# subtypes. ``"impl"`` (Rust) and ``"embeds"`` (Go) are accepted here
# for forward compatibility with Phase 2 (not yet implemented — no
# Phase 1 extractor ever produces them), matching the design doc's
# documented CLI/MCP shape; filtering by either today always yields an
# empty result set, same as filtering by any other relation a repo's
# languages simply don't produce.
HERITAGE_RELATIONS = ("extends", "implements", "impl", "embeds")

# Default token cap for relation-shaped actions (callers/callees/uses)
# when the caller passes no budget. Without this, a high-fan-in
# symbol's full caller/callee list (or an external name's every
# reference) renders unbounded text capped only by --limit's row
# count — the 2026-07-31 eval measured ~3,524 tokens on a 469-caller
# symbol with no budget passed, over 4x the advertised default.
# Callers can always pass a larger budget explicitly.
DEFAULT_RELATION_BUDGET = 800

# Default ``peers`` threshold: a single shared callee is common noise
# (both calling ``print``/``log``); two or more is a much stronger
# peer signal. Callers can raise this via ``--min-shared``.
DEFAULT_MIN_SHARED = 2

# Default ``throws --transitive`` walk depth: call-graph reachability
# can be very deep, and "everything this function's entire call tree
# might raise" degrades toward "every exception type in the repo" on a
# sufficiently connected codebase — see the design doc's own "hard
# depth cap" requirement. Callers can raise this via ``--depth``; a
# capped walk always discloses truncation rather than silently
# stopping (mirrors ``_MAX_AMBIGUOUS_CANDIDATES``'s discipline).
DEFAULT_THROWS_DEPTH = 2

_BUDGETED_ACTIONS = (
    "callers",
    "callees",
    "uses",
    "type",
    "supertypes",
    "subtypes",
    "importers",
    "peers",
    "throws",
    "catches",
    "env",
    "cohesion",
)

# Identifier-token pattern for the default (non-``--exact``) ``type``
# match: the queried name must appear as a whole token inside the raw,
# unparsed type text (``Optional[Config]``, ``Vec<Config>``, ``*Config``,
# ``Config | None``), not merely as a substring — this is what keeps
# ``Config`` from matching ``ConfigManager``/``AppConfig``. Same
# word-boundary discipline as ``_is_qualname_near_miss``, applied to
# type text instead of qualnames.
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def paths_matching(
    index: MapIndex, path: str, pool: Iterable[str] | None = None
) -> list[str]:
    """File paths equal to ``path`` or ending in ``/path``.

    Args:
        index: Loaded map index.
        path: Bare filename or path suffix to match.
        pool: Path universe to search — defaults to
            ``index.symbols_by_path`` (symbol-bearing files only, the
            behavior every existing caller relies on). Pass
            ``index.languages_by_path`` for a wider universe that
            includes zero-symbol files (barrel/re-export files) — see
            ``deps.py``'s ``_run_file``.
    """
    universe = index.symbols_by_path if pool is None else pool
    if path in universe:
        return [path]
    suffix = "/" + path
    return sorted(p for p in universe if p.endswith(suffix))


def resolve_target(
    index: MapIndex, target: str
) -> tuple[Symbol | None, list[Symbol]]:
    """Resolve a target string to a symbol.

    A ``::`` separator (the Rust/C++ habit agents fall into, e.g.
    ``file.py::name`` or ``Class::method``) is not part of the target
    grammar but is retried as both the ``path:qualname`` and the
    ``Class.method`` reading before giving up — a dead-end here ejects
    an agent into grep/Read, the exact cost the map exists to avoid.

    Args:
        index: Loaded map index.
        target: Bare name, qualname, ``path:qualname``, or
            ``path:qualname:line`` form — the trailing ``:line`` picks
            one candidate out of an overload set that shares the same
            ``(path, qualname)`` (see ``_resolve_exact``).

    Returns:
        ``(match, candidates)``: a unique match (or ``None``) plus all
        candidates considered. No candidates means not found; several
        with no match means ambiguous.
    """
    match, candidates = _resolve_exact(index, target)
    if not candidates and "::" in target:
        for variant in (
            target.replace("::", ":"),
            target.replace("::", "."),
        ):
            match, candidates = _resolve_exact(index, variant)
            if candidates:
                break
    return match, candidates


def _resolve_exact(
    index: MapIndex, target: str
) -> tuple[Symbol | None, list[Symbol]]:
    """Resolve one target reading against the documented grammar.

    A target with 2+ ``:`` separators whose final segment is all
    digits is read as ``path:qualname:line`` — the line qualifier an
    agent copies verbatim from a candidate row printed by
    ``report_unresolved`` to disambiguate same-file, same-qualname
    overloads (Java/C++ overload sets, round-08 §2.5) that plain
    ``path:qualname`` can never tell apart, since the resolution key
    is identical across every overload. Matched by exact
    ``start_line`` only — no fuzzy "nearest line". A stale or
    hand-typed line number that matches zero or more than one
    candidate is silently ignored (falls back to the unfiltered
    ``path:qualname`` candidate pool) rather than raising a distinct
    error, so ``report_unresolved`` handles it the same way it always
    has.
    """
    line = None
    body = target
    if target.count(":") >= 2:
        head, _, tail = target.rpartition(":")
        if tail.isdigit():
            body, line = head, int(tail)
    if ":" in body:
        path_part, _, qual = body.rpartition(":")
        pool = [
            s
            for p in paths_matching(index, path_part)
            for s in index.symbols_by_path[p]
            if s.qualname == qual or s.name == qual
        ]
        candidates = pool
        if line is not None:
            narrowed = [s for s in pool if s.start_line == line]
            if len(narrowed) == 1:
                candidates = narrowed
    else:
        # Merge both pools (deduped by id) instead of short-circuiting
        # on whichever is non-empty first: a bare name can be both a
        # unique top-level symbol's full qualname (no "." in it) *and*
        # the shared bare name of several unrelated nested methods
        # (whose qualnames are `Foo.name`/`Bar.name`, never bare
        # `name`) — picking only the qualname hit silently ignores the
        # real collision (bug #1.4).
        qual = index.symbols_by_qualname.get(target) or []
        name = index.symbols_by_name.get(target) or []
        candidates = list({s.id: s for s in (*qual, *name)}.values())
    if len(candidates) == 1:
        return candidates[0], candidates
    return None, candidates


def _related(
    index: MapIndex, sym: Symbol, direction: str
) -> tuple[list[Symbol], list[str]]:
    """Adjacent symbols plus module-level pseudo-callers.

    Args:
        index: Loaded map index.
        sym: Resolved target symbol.
        direction: ``"callers"`` or ``"callees"``.

    Returns:
        ``(symbols, module_paths)`` where module_paths are files whose
        top level calls the target.
    """
    adjacency = index.calls_in if direction == "callers" else index.calls_out
    symbols: list[Symbol] = []
    modules: list[str] = []
    for sid in adjacency.get(sym.id, []):
        if sid.endswith(MODULE_CALLER_SUFFIX):
            modules.append(sid[: -len(MODULE_CALLER_SUFFIX)])
        elif sid in index.symbols_by_id:
            symbols.append(index.symbols_by_id[sid])
    return symbols, modules


def _sym_line(sym: Symbol) -> str:
    """One-line text rendering of a symbol."""
    return f"{sym.path}:{sym.start_line}  {signature(sym)}"


def _sym_json(index: MapIndex, sym: Symbol) -> dict:
    """Structured rendering of a symbol."""
    return {
        "id": sym.id,
        "kind": sym.kind,
        "path": sym.path,
        "line": sym.start_line,
        "signature": signature(sym),
    }


def _emit_lines(
    lines: list[str], budget: int | None, limit: int, prefix: str = ""
) -> Meter:
    """Print rows trimmed to the caps and return the cost meter.

    ``prefix`` holds non-droppable leading text (a summary/header line,
    printed once up front) — it counts toward the reported token cost
    but, critically, *not* toward ``Meter.total``/the "N of TOTAL
    omitted" row count, which must reflect data rows only. Passing a
    header inside ``lines`` instead would inflate that count by one
    (or more, for multi-line headers) whenever truncation kicks in.
    """
    kept, meter = fit_to_budget(lines, budget, limit, prefix=prefix)
    if prefix:
        print(prefix)
    for line in kept:
        print(line)
    return meter


def _fit_entries(
    entries: list[dict], budget: int | None, limit: int
) -> tuple[list[dict], Meter]:
    """Trim JSON result entries to the caps, metered on their JSON cost."""
    serialized = [json.dumps(e) for e in entries]
    kept, meter = fit_to_budget(serialized, budget, limit)
    return entries[: len(kept)], meter


_MAX_SUGGESTIONS = 5
# Edit-distance fuzzy-suggestion tuning (bug #3.4b): a name shorter
# than this floor is only ever eligible via the exact/prefix/substring
# tiers, never the fuzzy edit-distance tier, and the cutoff itself is
# raised from difflib's permissive 0.6 default to trim genuinely
# unrelated matches while still surfacing real near-typos (tuned
# against claude-buddy's `buddyStateDr` case).
_MIN_FUZZY_NAME_LEN = 3
_FUZZY_CUTOFF = 0.72
# round-13 claude-buddy.md: a single-character candidate name (a
# throwaway loop variable like `B`/`D`) is a coincidental substring of
# almost any sufficiently long, unrelated needle -- `totallyMade
# UpSymbolXYZ123` contains a "b" (from "symbol") and a "d" (from
# "made") purely by chance, so the substring tier surfaced both as
# "closest matches" for a query with no real relationship to either.
# Distinct from ``_MIN_FUZZY_NAME_LEN`` (which only gates the fuzzy
# edit-distance tier, and is deliberately looser so short symbol names
# stay reachable via a genuine prefix/substring match): this floor
# only gates the "tiny candidate happens to appear inside a much
# longer needle" direction of the substring tier, not the reverse
# (a short *query* matching inside a longer real candidate name stays
# fully eligible, and 2+ character names are unaffected either way).
_MIN_SUBSTRING_CANDIDATE_LEN = 2
# Cap on how many ambiguous candidates ``report_unresolved`` prints
# unconditionally. Without this, a very-high-cardinality bare-name
# collision (zed's 99 same-named ``fn main`` candidates across a Rust
# workspace — bug #10/B10) dumps every candidate path unconditionally,
# ~1,110 tokens for a list an agent almost never reads past the first
# handful of before narrowing the target with a ``path:`` qualifier.
_MAX_AMBIGUOUS_CANDIDATES = 20


def _close_names(needle: str, names: list[str]) -> list[str]:
    """Names close to ``needle``: exact (case-insensitive) first, then
    prefix, then substring either way, then edit-distance for typos.
    Deterministic, capped."""
    low = needle.lower()
    if not low:
        return []
    scored: list[tuple[int, str]] = []
    rest: list[str] = []
    for name in names:
        cand = name.lower()
        if cand == low:
            scored.append((0, name))
        elif cand.startswith(low) or low.startswith(cand):
            scored.append((1, name))
        elif low in cand or (
            cand in low and len(cand) >= _MIN_SUBSTRING_CANDIDATE_LEN
        ):
            scored.append((2, name))
        else:
            rest.append(name)
    scored.sort()
    out = [name for _, name in scored[:_MAX_SUGGESTIONS]]
    if len(out) < _MAX_SUGGESTIONS and rest:
        # Edit-distance matching is inherently biased toward short
        # strings, so a single-letter symbol (`B`, `t`, `A`, `D`) wins
        # this tier disproportionately even when it isn't a real
        # match. A length floor keeps such names eligible only via the
        # stricter exact/prefix/substring tiers above, and a higher
        # cutoff (0.72 vs difflib's default-ish 0.6) trims genuinely
        # unrelated names that a permissive cutoff still let through.
        by_low = {}
        for name in sorted(rest):
            if len(name) >= _MIN_FUZZY_NAME_LEN:
                by_low.setdefault(name.lower(), name)
        out += [
            by_low[m]
            for m in difflib.get_close_matches(
                low,
                list(by_low),
                _MAX_SUGGESTIONS - len(out),
                _FUZZY_CUTOFF,
            )
        ]
    return out


def _is_qualname_near_miss(qual: str, sym: Symbol) -> bool:
    """Whether ``sym`` looks like ``qual`` missing a namespace/module.

    True when the symbol's real qualname *is* the requested qualname,
    or ends with it after a ``.`` segment separator — the exact shape
    of a C++ "forgot the namespace" or a Rust/Java "forgot the outer
    module" guess (master report #8, round 11: ``ClientSession.Run``
    failing to resolve when the real qualname is
    ``tensorflow.ClientSession.Run``). Requiring a preceding ``.``
    (not a bare substring) avoids matching an unrelated qualname that
    merely ends with the same trailing characters. Only meaningful
    when ``qual`` itself has a container segment (a bare name has
    nothing to be "missing a prefix" from).
    """
    return "." in qual and (
        sym.qualname == qual or sym.qualname.endswith("." + qual)
    )


def _suggest_symbols(index: MapIndex, target: str) -> list[Symbol]:
    """Symbols worth offering for a target that resolved to nothing.

    Matches the qualname part of the target (and its last segment)
    against the name index, so a wrong or stale path qualifier still
    finds the right symbol. Candidates whose real qualname is the
    requested qualname with a namespace/module prefix missing (see
    ``_is_qualname_near_miss``) rank first; within each tier,
    production code ranks before test code.
    """
    qual = target.rpartition(":")[2]
    seen: dict[str, Symbol] = {}
    for needle in dict.fromkeys((qual, qual.rpartition(".")[2])):
        for name in _close_names(needle, list(index.symbols_by_name)):
            for sym in index.symbols_by_name[name]:
                seen.setdefault(sym.id, sym)
    ranked = sorted(
        seen.values(),
        key=lambda s: (
            not _is_qualname_near_miss(qual, s),
            is_test_path(s.path),
            s.path,
            s.qualname,
        ),
    )
    return ranked[:_MAX_SUGGESTIONS]


def render_candidates(candidates: list[Symbol]) -> list[str]:
    """Render an ambiguous candidate list: rows, cap note, overload hint.

    Factored out of ``report_unresolved`` so the same cap/hint
    rendering can be reused by ``ambiguous.py``'s ``--name`` drill-down
    (every caller's own full candidate set for one colliding name)
    without duplicating the ``_MAX_AMBIGUOUS_CANDIDATES`` cap logic or
    the same-``(path, qualname)`` overload hint. Candidates are shown
    production code first, test code last (presentation only —
    resolution itself is unchanged).

    Args:
        candidates: The 2+ same-name candidates that could not be
            disambiguated.

    Returns:
        Text rows: one per candidate (capped at
        ``_MAX_AMBIGUOUS_CANDIDATES``), an optional "... +N more" row,
        and an optional "can't disambiguate" hint row when every
        candidate shares ``(path, qualname)``.
    """
    ranked = sorted(
        candidates, key=lambda s: (is_test_path(s.path), s.path, s.qualname)
    )
    rows = [
        f"  {sym.path}:{sym.start_line}  {signature(sym)}"
        for sym in ranked[:_MAX_AMBIGUOUS_CANDIDATES]
    ]
    if len(ranked) > _MAX_AMBIGUOUS_CANDIDATES:
        more = len(ranked) - _MAX_AMBIGUOUS_CANDIDATES
        sample = ranked[0]
        # Build the hint from the sample's own qualname, not the raw
        # ``target`` string — ``target`` may already be a
        # ``path:qualname[:LINE]`` form (e.g. when path+qualname alone
        # still matched an overload set), and prepending ``sample.path``
        # to an already-qualified target duplicated the path segment
        # (round-15 finding).
        rows.append(
            f"  … +{more} more (qualify with "
            f"`{sample.path}:{sample.qualname}` to narrow)"
        )
    if len({(s.path, s.qualname) for s in candidates}) == 1:
        # Every candidate shares (path, qualname) — an overload set a
        # plain `file.py:qualname` qualifier can never narrow, since
        # that's exactly the key they collide on. The line-number
        # qualifier (round-08 §2.5) is the only escape hatch; point at
        # it directly with a real candidate's own line as an example.
        sample = ranked[0]
        rows.append(
            "  … path+qualname alone can't disambiguate these (same "
            "file, same name) — append `:LINE` from a row above, e.g. "
            f"`{sample.path}:{sample.qualname}:{sample.start_line}`"
        )
    return rows


def report_unresolved(
    target: str,
    candidates: list[Symbol],
    index: MapIndex | None = None,
) -> int:
    """Explain a failed resolution and return the exit code.

    Ambiguous candidates are listed production code first, test code
    last (presentation only — resolution itself is unchanged). When an
    ``index`` is given, a not-found reply names the closest symbols so
    the caller can retry inside the map instead of falling back to
    grep (the 2026-07-10 eval transcripts show a bare not-found ejects
    agents into reading whole files).

    Always prints plain text to stderr, regardless of ``--json`` —
    this is a deliberate, project-wide contract (round-12 §3.15/§6),
    not an oversight specific to this function. Every CLI error path
    behaves the same way (see ``docs/cli.md``'s ``--json`` section):
    ``--json`` governs the shape of successful (exit 0) output only. A
    caller should check the exit code first (``EXIT_AMBIGUOUS``/
    ``EXIT_NOT_FOUND`` here) and only parse stdout as JSON when it is
    0. This function is shared by ``query``, ``trace``, ``workset``,
    and ``contextpack`` — do not "fix" it as a one-off without also
    revisiting the other three call sites and the documented contract.
    """
    if not candidates:
        print(f"dekko: no symbol matches '{target}'", file=sys.stderr)
        suggestions = _suggest_symbols(index, target) if index else []
        if suggestions:
            print("closest matches:", file=sys.stderr)
            for sym in suggestions:
                print(
                    f"  {sym.path}:{sym.start_line}  {signature(sym)}",
                    file=sys.stderr,
                )
        coverage = _coverage_note(index) if index else None
        if coverage:
            print(f"  note: {coverage}", file=sys.stderr)
        return EXIT_NOT_FOUND
    print(f"dekko: '{target}' is ambiguous; candidates:", file=sys.stderr)
    for row in render_candidates(candidates):
        print(row, file=sys.stderr)
    return EXIT_AMBIGUOUS


def _edge_key(action: str, sym: Symbol, other_id: str) -> tuple[str, str]:
    """The ``edge_lines`` key for a relation row."""
    if action == "callers":
        return (other_id, sym.id)
    return (sym.id, other_id)


def _site_rows(
    index: MapIndex, action: str, sym: Symbol, other: Symbol
) -> list[str]:
    """One row per call site for a relation, or a def-line fallback.

    Caller rows locate the call in the caller's file; callee rows
    locate it in the target's own file. Maps written before doc
    version 3 have no site lines and fall back to the symbol row.
    """
    lines = index.edge_lines.get(_edge_key(action, sym, other.id), [])
    if not lines:
        return [_sym_line(other)]
    site_path = other.path if action == "callers" else sym.path
    return [f"{site_path}:{line}  {signature(other)}" for line in lines]


def _module_rows(
    index: MapIndex, action: str, sym: Symbol, path: str, sites: bool
) -> list[str]:
    """Rows for a module-level pseudo-caller.

    Always attempts the per-site line lookup, regardless of ``sites``
    (unlike ``_site_rows``, whose named-caller default deliberately
    stays gated on the flag). "path  (module level)" with several
    distinct anonymous-callback call sites in the same file is
    genuinely ambiguous in a way the named-caller default isn't, and
    the real per-line data is already sitting in ``index.edge_lines``
    whenever it was recorded — round 22 claude-buddy.md §2.3. Falls
    back to the bare form only when the map predates per-site line
    tracking, or this specific edge has no recorded site line.
    """
    module_id = f"{path}{MODULE_CALLER_SUFFIX}"
    lines = index.edge_lines.get(_edge_key(action, sym, module_id), [])
    if lines:
        return [f"{path}:{line}  (module level)" for line in lines]
    return [f"{path}  (module level)"]


def _coverage_note(index: MapIndex) -> str | None:
    """Caveat text when the map skipped confirmed-unsupported files.

    Qualifies a "no results" answer as "no results among parsed
    files" rather than unconditional truth — a symbol only used in a
    file dekko can't parse (e.g. ``.astro``) would otherwise read as a
    confident false negative (2026-07-31 eval, gitaustin/Astro repo).

    Args:
        index: Loaded map index.

    Returns:
        A one-line caveat, or ``None`` when nothing was skipped.
    """
    note = format_unsupported(index.provenance)
    return f"{note} — this answer may be incomplete" if note else None


def _referenced_entries(
    index: MapIndex, sym: Symbol, sites: bool = False
) -> list[dict]:
    """JSON-shaped rows for symbols that reference (not call) ``sym``.

    A callback wired up by reference and never itself called (bug
    #2b — see ``model.RawRef``) is invisible to ``calls_in``; this is
    the ``get_callers`` surface for it, so "referenced_in nonzero, no
    called-in" doesn't read as "nothing uses this."

    When ``sites`` is true, each entry's ``"sites"`` key carries the
    actual reference-site lines (from ``index.ref_lines``) instead of
    only the referencer's own definition line — mirroring
    ``_print_relation_json``'s call-edge ``"sites"`` handling.
    """
    entries: list[dict] = []
    for rid in index.referenced_in.get(sym.id, []):
        if rid.endswith(MODULE_CALLER_SUFFIX):
            entries.append(
                {
                    "id": rid,
                    "path": rid[: -len(MODULE_CALLER_SUFFIX)],
                    "module_level": True,
                }
            )
            continue
        other = index.symbols_by_id.get(rid)
        if other is not None:
            entry = _sym_json(index, other)
            if sites:
                entry["sites"] = index.ref_lines.get((rid, sym.id), [])
            entries.append(entry)
    return entries


def _referenced_rows(
    index: MapIndex, sym: Symbol, sites: bool = False
) -> list[str]:
    """Text rows for symbols that reference (not call) ``sym``.

    When ``sites`` is true and a reference-site line is recorded (doc
    version 4+ maps), one row per actual reference line is emitted
    instead of the referencer's own definition line — the read-side
    fix for showing e.g. ``file.ts:680`` (the reference) rather than
    ``file.ts:209`` (the enclosing function's definition line). Falls
    back to the definition-line row when no site line is recorded
    (pre-v4 maps, or ``sites=False``), same graceful-degradation
    contract ``_site_rows`` already uses for call edges.
    """
    rows: list[str] = []
    for rid in index.referenced_in.get(sym.id, []):
        if rid.endswith(MODULE_CALLER_SUFFIX):
            path = rid[: -len(MODULE_CALLER_SUFFIX)]
            rows.append(f"{path}  (module level)")
            continue
        other = index.symbols_by_id.get(rid)
        if other is None:
            continue
        lines = index.ref_lines.get((rid, sym.id), []) if sites else []
        if lines:
            rows += [
                f"{other.path}:{line}  {signature(other)}" for line in lines
            ]
        else:
            rows.append(_sym_line(other))
    return rows


def _print_relation_json(
    index: MapIndex,
    action: str,
    sym: Symbol,
    symbols: list[Symbol],
    modules: list[str],
    sites: bool,
    budget: int | None,
    limit: int,
    coverage: str | None,
    ambig_in: int,
    ambig_out: int,
) -> None:
    """JSON rendering for ``_run_relation`` (callers/callees)."""
    entries = []
    for s in symbols:
        entry = _sym_json(index, s)
        if sites:
            entry["sites"] = index.edge_lines.get(
                _edge_key(action, sym, s.id), []
            )
        entries.append(entry)
    kept, meter = _fit_entries(entries, budget, limit)
    doc = {
        "action": action,
        "target": sym.id,
        "results": kept,
        "module_level": modules,
        "meta": meter.as_dict(),
    }
    if coverage:
        doc["coverage_warning"] = coverage
    if ambig_in:
        doc["ambiguous_in"] = ambig_in
    if ambig_out:
        doc["ambiguous_out"] = ambig_out
    if action == "callers" and not entries and not modules:
        referenced = _referenced_entries(index, sym, sites)
        if referenced:
            doc["referenced_not_called"] = referenced
    print(json.dumps(doc, indent=2))


def _run_relation(
    index: MapIndex,
    action: str,
    sym: Symbol,
    as_json: bool,
    limit: int,
    budget: int | None,
    sites: bool = False,
) -> tuple[int, Meter | None]:
    """Execute callers/callees for a resolved symbol."""
    symbols, modules = _related(index, sym, action)
    symbols.sort(key=lambda s: relevance_key(s, index))
    coverage = _coverage_note(index)
    # Ambiguous calls never become a resolved edge (see resolver.py's
    # module docstring), so a symbol's calls_in/calls_out can look
    # exhaustive when name-collision candidates were actually dropped.
    # ambig_in is meaningful for "who calls this" (candidates this
    # symbol could have been ambiguously called as); ambig_out is the
    # outgoing-side counterpart for "what does this call" (names this
    # symbol itself called ambiguously) — round-09 §2.1 part A flagged
    # that only the callers direction disclosed this gap.
    ambig_in = (
        len(index.ambiguous_in.get(sym.id, [])) if action == "callers" else 0
    )
    ambig_out = (
        len(index.ambiguous_out.get(sym.id, [])) if action == "callees" else 0
    )
    if as_json:
        _print_relation_json(
            index,
            action,
            sym,
            symbols,
            modules,
            sites,
            budget,
            limit,
            coverage,
            ambig_in,
            ambig_out,
        )
        return EXIT_OK, None
    lines: list[str] = []
    for s in symbols:
        lines += _site_rows(index, action, sym, s) if sites else [_sym_line(s)]
    for path in modules:
        lines += _module_rows(index, action, sym, path, sites)
    if ambig_in:
        print(
            f"  note: {ambig_in} additional call site(s) named "
            f"'{sym.name}' resolved ambiguously — not counted here",
            file=sys.stderr,
        )
    if ambig_out:
        print(
            f"  note: {ambig_out} outgoing call(s) from this symbol "
            "resolved ambiguously (name matched 2+ candidates) — not "
            "counted here",
            file=sys.stderr,
        )
    if not lines:
        referenced = (
            _referenced_rows(index, sym, sites) if action == "callers" else []
        )
        if referenced:
            # A callback wired up by reference but never called (bug
            # #2b) must not read as "nothing uses this" just because
            # calls_in is empty.
            print("referenced (not called):")
            for row in referenced:
                print(f"  {row}")
            return EXIT_OK, None
        print(f"(no {action} of {sym.id})")
        if coverage:
            print(f"  note: {coverage}", file=sys.stderr)
        return EXIT_OK, None
    return EXIT_OK, _emit_lines(lines, budget, limit)


def _peer_relevance_key(
    index: MapIndex, sym_id: str
) -> tuple[bool, int, str, int]:
    """Sort key for a peer id, module-level-pseudo-caller aware.

    ``calls_out``'s keys (the side ``peers`` iterates) can be a
    module-level pseudo-caller id (``path::<module>``) alongside real
    symbol ids — ``relevance_key`` only accepts a ``Symbol``, so this
    builds the same ``(is_test, -degree, path, line)`` tuple shape by
    hand for the module-level case rather than looking it up in
    ``symbols_by_id`` and KeyError-ing, mirroring ``ambiguous.py``'s
    own ``_caller_path`` suffix-stripping for the identical id shape.
    """
    if sym_id.endswith(MODULE_CALLER_SUFFIX):
        path = sym_id[: -len(MODULE_CALLER_SUFFIX)]
        return (is_test_path(path), 0, path, 0)
    return relevance_key(index.symbols_by_id[sym_id], index)


def _shared_callee_names(index: MapIndex, shared: set[str]) -> list[str]:
    """Bare qualnames for a shared-callee id set, sorted.

    ``calls_out``'s callee side is always a resolved symbol id, never
    a module-level pseudo-caller (only the caller/key side of an edge
    can be module-level — a call can't target a module), so every id
    here is safe to look up directly.
    """
    return sorted(
        index.symbols_by_id[cid].qualname
        for cid in shared
        if cid in index.symbols_by_id
    )


def _peer_row(index: MapIndex, sym_id: str, shared: list[str]) -> str:
    """One text row for a ``peers`` hit."""
    if sym_id.endswith(MODULE_CALLER_SUFFIX):
        path = sym_id[: -len(MODULE_CALLER_SUFFIX)]
        head = f"{path}  (module level)"
    else:
        head = _sym_line(index.symbols_by_id[sym_id])
    return f"{head}  shares: {', '.join(shared)} ({len(shared)})"


def _peer_entry(index: MapIndex, sym_id: str, shared: list[str]) -> dict:
    """One JSON entry for a ``peers`` hit."""
    if sym_id.endswith(MODULE_CALLER_SUFFIX):
        path = sym_id[: -len(MODULE_CALLER_SUFFIX)]
        entry = {"id": sym_id, "path": path, "module_level": True}
    else:
        entry = _sym_json(index, index.symbols_by_id[sym_id])
    entry["shared_callees"] = shared
    entry["shared_count"] = len(shared)
    return entry


def _run_peers_empty(
    sym: Symbol, min_shared: int, base_size: int, ambig_out: int
) -> None:
    """Print the empty-result note for ``peers`` (text mode only).

    ``base_size == 0`` covers two different situations that must not
    share the same "leaf function" wording: a symbol with genuinely no
    outgoing calls, and one whose only outgoing call(s) resolved
    ambiguously (name matched 2+ candidates) and so aren't counted in
    ``calls_out`` at all — the same ambiguous-call case ``query
    symbol``/``query callees`` already disclose via ``ambig_out``.
    Calling the latter a "leaf function" asserts something the
    symbol's own body contradicts.
    """
    print(f"(no peers of {sym.id} sharing >= {min_shared} callee(s))")
    if base_size == 0 and ambig_out:
        print(
            f"  note: this symbol's only outgoing call(s) resolved "
            f"ambiguously ({ambig_out} name(s) matched 2+ candidates) "
            "— not counted, so no peers could be computed"
        )
    elif base_size == 0:
        print(
            "  note: this symbol has no outgoing calls (a leaf "
            "function) — it can't share callees with anything"
        )
    elif min_shared > 1:
        print(
            f"  note: try --min-shared {min_shared - 1} to loosen the "
            "threshold"
        )


def _run_peers(
    index: MapIndex,
    sym: Symbol,
    min_shared: int,
    as_json: bool,
    limit: int,
    budget: int | None,
) -> tuple[int, Meter | None]:
    """Execute the peers action: symbols sharing >= min_shared callees.

    A symbol with zero callees (a pure leaf function) has no peers by
    construction — a clean, expected empty result, not an error.

    Args:
        index: Loaded map index.
        sym: Resolved target symbol.
        min_shared: Minimum shared-callee count to count as a peer.
        as_json: Emit structured JSON instead of text.
        limit: Cap on text result rows.
        budget: Approximate token budget for the result rows.

    Returns:
        ``(exit_code, meter)`` — meter is ``None`` for JSON output or
        an empty result.
    """
    base = set(index.calls_out.get(sym.id, []))
    rows: list[tuple[str, list[str]]] = []
    if base:
        for other_id, callees in index.calls_out.items():
            if other_id == sym.id:
                continue
            shared = base & set(callees)
            if len(shared) >= min_shared:
                rows.append((other_id, _shared_callee_names(index, shared)))
    rows.sort(key=lambda r: (-len(r[1]), _peer_relevance_key(index, r[0])))
    coverage = _coverage_note(index)
    if as_json:
        entries = [_peer_entry(index, oid, names) for oid, names in rows]
        kept, meter = _fit_entries(entries, budget, limit)
        doc = {
            "action": "peers",
            "target": sym.id,
            "min_shared": min_shared,
            "results": kept,
            "meta": meter.as_dict(),
        }
        if coverage:
            doc["coverage_warning"] = coverage
        print(json.dumps(doc, indent=2))
        return EXIT_OK, None
    if not rows:
        ambig_out = len(index.ambiguous_out.get(sym.id, []))
        _run_peers_empty(sym, min_shared, len(base), ambig_out)
        if coverage:
            print(f"  note: {coverage}", file=sys.stderr)
        return EXIT_OK, None
    lines = [_peer_row(index, oid, names) for oid, names in rows]
    return EXIT_OK, _emit_lines(lines, budget, limit)


# ---------------------------------------------------------------------
# Throws/catches (exception/error-flow tracing — a scoped pilot per
# the design doc: Python/Java/C++/JS/TS only, Rust/Go/C permanently
# out of scope; see ``languages.LanguageSpec.throw_query``'s
# docstring).


def _caller_label(index: MapIndex, caller_id: str) -> str:
    """Human-readable label for a throws/catches ``caller`` id.

    ``caller_id`` is either a real symbol id or a module pseudo-id
    (``path::<module>``, for a top-level throw/catch) — mirrors how
    ``_related`` already splits the two shapes for callers/callees.
    """
    if caller_id.endswith(MODULE_CALLER_SUFFIX):
        path = caller_id[: -len(MODULE_CALLER_SUFFIX)]
        return f"{path} <module level>"
    sym = index.symbols_by_id.get(caller_id)
    return sym.qualname if sym is not None else caller_id


def _throws_direct(
    index: MapIndex, caller_id: str
) -> tuple[list[tuple[Symbol, list[int]]], list[ExternalCall], int, int]:
    """One caller's own resolved/external/bare/ambiguous throw data.

    Returns ``(resolved, external, bare_count, ambiguous_count)`` —
    ``resolved`` pairs a repo-defined raised-type symbol with its
    throw-site lines; a resolved type id absent from ``symbols_by_id``
    (a stale map referencing a since-deleted type) is skipped
    defensively rather than raising.
    """
    resolved: list[tuple[Symbol, list[int]]] = []
    for type_id in index.throws_out.get(caller_id, []):
        sym = index.symbols_by_id.get(type_id)
        if sym is not None:
            lines = index.throws_lines.get((caller_id, type_id), [])
            resolved.append((sym, lines))
    external = index.throws_external_out.get(caller_id, [])
    bare_count = len(index.throws_bare_out.get(caller_id, []))
    ambiguous_count = len(index.throws_ambiguous_out.get(caller_id, []))
    return resolved, external, bare_count, ambiguous_count


def _walk_throws_transitive(
    index: MapIndex, start: str, depth_cap: int
) -> tuple[
    dict[str, tuple[int, list[int]]],
    list[tuple[int, ExternalCall]],
    int,
    bool,
]:
    """BFS ``calls_out`` from ``start`` up to ``depth_cap`` hops,
    collecting every resolved/external throw reachable.

    Depth 0 is ``start`` itself. Cycle-safe (a ``seen`` set, matching
    ``walk_heritage``'s own BFS pattern).

    Returns:
        ``(resolved, external, bare_count, truncated)`` — ``resolved``
        maps a raised-type symbol id to its shallowest discovery depth
        and that depth's throw-site lines; ``truncated`` is ``True``
        when the walk hit ``depth_cap`` with unvisited callees still
        remaining (disclosed, never silently dropped).
    """
    seen = {start}
    frontier: deque[tuple[str, int]] = deque([(start, 0)])
    resolved: dict[str, tuple[int, list[int]]] = {}
    external: list[tuple[int, ExternalCall]] = []
    bare_count = 0
    truncated = False
    while frontier:
        node, depth = frontier.popleft()
        for type_id in index.throws_out.get(node, []):
            lines = index.throws_lines.get((node, type_id), [])
            if type_id not in resolved or depth < resolved[type_id][0]:
                resolved[type_id] = (depth, lines)
        external.extend(
            (depth, ext) for ext in index.throws_external_out.get(node, [])
        )
        bare_count += len(index.throws_bare_out.get(node, []))
        callees = index.calls_out.get(node, [])
        if depth >= depth_cap:
            if any(c not in seen for c in callees):
                truncated = True
            continue
        for callee in callees:
            if callee in seen:
                continue
            seen.add(callee)
            frontier.append((callee, depth + 1))
    return resolved, external, bare_count, truncated


def _throws_row(sym: Symbol, lines: list[int]) -> str:
    """One text row for a resolved throw hit."""
    site = ",".join(str(n) for n in lines) if lines else "?"
    return f"{_sym_line(sym)}  (L{site})"


def _throws_external_row(ext: ExternalCall) -> str:
    """One text row for an external (stdlib/third-party) throw hit."""
    site = ",".join(str(n) for n in ext.lines) if ext.lines else "?"
    return f"  L{site}  (external) {ext.callee}"


def _throws_gather(
    index: MapIndex, sym: Symbol, transitive: bool, depth: int
) -> tuple[
    list[tuple[Symbol, int, list[int]]],
    list[ExternalCall],
    int,
    int,
    bool,
]:
    """Compute one throws query's result set, one level or transitive.

    Split out of ``_run_throws`` purely to keep that function's
    cyclomatic complexity under the project's Ruff limit — behaviorally
    this is still the same "gather, then render" split every other
    ``_run_*`` action already uses.

    Returns:
        ``(resolved, external, bare_count, ambiguous_count,
        truncated)`` — ``resolved`` is ``(symbol, depth, lines)``
        triples (``depth`` always ``0`` for a one-level query);
        ``truncated``/non-zero ``ambiguous_count`` only ever apply to
        a transitive walk / a one-level query respectively (the design
        doc scopes ambiguous-name disclosure to the target itself, the
        same way ``_run_heritage`` already does for supertypes).
    """
    if not transitive:
        direct, external, bare_count, ambiguous_count = _throws_direct(
            index, sym.id
        )
        resolved = [(s, 0, lines) for s, lines in direct]
        return resolved, external, bare_count, ambiguous_count, False

    resolved_map, external_hits, bare_count, truncated = (
        _walk_throws_transitive(index, sym.id, depth)
    )
    resolved = [
        (index.symbols_by_id[tid], d, lines)
        for tid, (d, lines) in resolved_map.items()
        if tid in index.symbols_by_id
    ]
    resolved.sort(key=lambda r: (r[1], relevance_key(r[0], index)))
    external = [ext for _depth, ext in external_hits]
    return resolved, external, bare_count, 0, truncated


def _print_throws_json(
    index: MapIndex,
    sym: Symbol,
    resolved: list[tuple[Symbol, int, list[int]]],
    external: list[ExternalCall],
    bare_count: int,
    ambiguous_count: int,
    transitive: bool,
    depth: int,
    truncated: bool,
    budget: int | None,
    limit: int,
    coverage: str | None,
    language_supported: bool,
) -> None:
    """JSON rendering for ``_run_throws``."""
    entries = [
        {**_sym_json(index, s), "depth": d, "lines": lines}
        for s, d, lines in resolved
    ]
    entries.extend(
        {"external": True, "text": ext.callee, "lines": ext.lines}
        for ext in external
    )
    kept, meter = _fit_entries(entries, budget, limit)
    doc = {
        "action": "throws",
        "target": sym.id,
        "transitive": transitive,
        "results": kept,
        "repo_defined": len(resolved),
        "external": len(external),
        "bare_reraise": bare_count,
        "meta": meter.as_dict(),
    }
    if transitive:
        doc["depth"] = depth
        doc["truncated"] = truncated
    if ambiguous_count:
        doc["ambiguous"] = ambiguous_count
    if coverage:
        doc["coverage_warning"] = coverage
    if not language_supported:
        doc["language_supported"] = False
    print(json.dumps(doc, indent=2))


def _throws_text_lines(
    sym: Symbol,
    resolved: list[tuple[Symbol, int, list[int]]],
    external: list[ExternalCall],
    transitive: bool,
) -> tuple[str, list[str]]:
    """Build ``_run_throws``' text result prefix + data rows.

    Returns:
        ``(prefix, rows)`` — ``prefix`` holds the non-droppable summary
        line (and, under ``--transitive``, the ``[target]`` label
        line), kept separate from ``rows`` (one throw site each) so
        truncation's "N of TOTAL omitted" count reflects data rows
        only, not these header lines.
    """
    prefix_lines: list[str] = []
    if resolved or external:
        total = len(resolved) + len(external)
        prefix_lines.append(
            f"{total} throw site(s): {len(resolved)} repo-defined, "
            f"{len(external)} external"
        )
    if transitive and resolved:
        prefix_lines.append(f"{_sym_line(sym)}  [target]")
    rows: list[str] = []
    for s, d, throw_lines in resolved:
        row_prefix = "  " * d if transitive else ""
        rows.append(f"{row_prefix}{_throws_row(s, throw_lines)}")
    rows.extend(_throws_external_row(ext) for ext in external)
    return "\n".join(prefix_lines), rows


def _throws_text_notes(
    bare_count: int, ambiguous_count: int, truncated: bool, depth: int
) -> None:
    """Print ``_run_throws``' disclosure notes (stderr, unconditional)."""
    if bare_count:
        print(
            f"  note: {bare_count} re-raise site(s) omitted — type "
            "depends on the enclosing handler, not tracked",
            file=sys.stderr,
        )
    if ambiguous_count:
        print(
            f"  note: {ambiguous_count} additional raised-type name(s) "
            "resolved ambiguously — not counted here",
            file=sys.stderr,
        )
    if truncated:
        print(
            f"  note: reached the transitive depth cap ({depth}) with "
            "callees still unwalked — results may be incomplete; "
            "raise --depth to widen",
            file=sys.stderr,
        )


def _run_throws(
    index: MapIndex,
    sym: Symbol,
    transitive: bool,
    depth: int,
    as_json: bool,
    limit: int,
    budget: int | None,
) -> tuple[int, Meter | None]:
    """Execute the throws action: what calling ``sym`` can raise.

    One level (default) reads ``sym``'s own throw/raise sites (plus,
    Java only, its declared ``throws`` clause — indistinguishable in
    the data, both describe the same "error surface" question, see
    ``model.RawThrow``'s docstring). ``--transitive`` additionally
    walks ``sym``'s own call graph up to ``--depth`` hops (default
    ``DEFAULT_THROWS_DEPTH``), unioning every throw found along the
    way and disclosing when the depth cap truncated the walk.

    Args:
        index: Loaded map index.
        sym: Resolved target symbol.
        transitive: Walk the call graph instead of one level.
        depth: Hop cap for a transitive walk (ignored otherwise).
        as_json: Emit structured JSON instead of text.
        limit: Cap on text result rows.
        budget: Approximate token budget for the result rows.

    Returns:
        ``(exit_code, meter)`` — meter is ``None`` for JSON output or
        an empty result.
    """
    language_supported = languages.exception_handling_supported(sym.language)
    if language_supported:
        resolved, external, bare_count, ambiguous_count, truncated = (
            _throws_gather(index, sym, transitive, depth)
        )
    else:
        resolved, external, bare_count, ambiguous_count, truncated = (
            [],
            [],
            0,
            0,
            False,
        )
    coverage = _coverage_note(index)
    if as_json:
        _print_throws_json(
            index,
            sym,
            resolved,
            external,
            bare_count,
            ambiguous_count,
            transitive,
            depth,
            truncated,
            budget,
            limit,
            coverage,
            language_supported,
        )
        return EXIT_OK, None

    prefix, rows = _throws_text_lines(sym, resolved, external, transitive)
    _throws_text_notes(bare_count, ambiguous_count, truncated, depth)
    if not rows:
        if not language_supported:
            print(
                f"(throws not tracked for {sym.id} — {sym.language} is "
                "permanently excluded from this query; see `dekko "
                "query throws --help`)"
            )
        else:
            print(f"(no throws found for {sym.id})")
        if coverage:
            print(f"  note: {coverage}", file=sys.stderr)
        return EXIT_OK, None
    return EXIT_OK, _emit_lines(rows, budget, limit, prefix=prefix)


# JS/TS catch clauses are never type-discriminated at the syntax level
# (see ``languages.LanguageSpec.catch_query``'s docstring) — a real,
# disclosed precision gap the design doc requires stating in the
# command's own output, not just ``--help`` text, so an agent running
# this against a JS/TS-heavy repo doesn't over-trust a near-empty
# result.
_CATCHES_CAVEAT = (
    "JS/TS catch clauses are almost never type-annotated at the "
    "syntax level — a match here is either a rare typed `catch (e: "
    "Type)` (TS only) or a catch-all (which always matches regardless "
    "of type); a near-empty result on a JS/TS-heavy repo is a weak "
    "signal, not proof nothing catches this type. Also: matching is "
    "exact-name-only (v1) — a catch of a supertype of the queried "
    "type is not detected as a match."
)


def _catches_excluded_file_count(
    index: MapIndex,
) -> tuple[int, int, tuple[str, ...]]:
    """``(excluded, total, languages)`` mapped files not covered by
    throws/catches.

    ``excluded`` counts files whose recorded language (Rust/Go/C, or
    any Tier-2 generic-grammar language) never extracts throw/catch
    sites at all — see ``languages.exception_handling_supported``.
    ``total`` is every mapped file, so the caller can render a "N of
    TOTAL" disclosure. ``languages`` lists the distinct excluded
    languages actually present in this repo, sorted — the caller uses
    this instead of a static "Rust/Go/C" string so the disclosure
    names what's really excluded here (e.g. a JS/TS repo whose only
    excluded files are bash scripts should say "bash", not
    "Rust/Go/C", which would be actively misleading).
    """
    total = len(index.languages_by_path)
    excluded = 0
    excluded_langs: set[str] = set()
    for lang in index.languages_by_path.values():
        if not languages.exception_handling_supported(lang):
            excluded += 1
            excluded_langs.add(lang)
    return excluded, total, tuple(sorted(excluded_langs))


def _catch_site_matches(site: CatchSite, target: str) -> bool:
    """Whether one catch clause would handle a raised type named
    ``target`` — exact-name-or-catch-all only (the documented v1
    scope; no supertype-aware matching)."""
    return site.bare or target in site.type_names


def _catch_row(index: MapIndex, site: CatchSite) -> str:
    """One text row for a matching catch clause."""
    label = "catch-all" if site.bare else ", ".join(site.type_names)
    return (
        f"{site.path}:{site.line}  [{label}]  "
        f"{_caller_label(index, site.caller)}"
    )


def _catch_entry(site: CatchSite) -> dict:
    """One JSON entry for a matching catch clause."""
    return {
        "path": site.path,
        "line": site.line,
        "caller": site.caller,
        "type_names": site.type_names,
        "bare": site.bare,
    }


def _run_catches(
    index: MapIndex,
    target: str,
    as_json: bool,
    limit: int,
    budget: int | None,
) -> tuple[int, Meter | None]:
    """Execute the catches action: who catches an exception of type
    ``target``.

    Matches every clause across the repo by name against
    ``CatchSite.type_names`` (or a catch-all clause, which always
    matches) — a repo-wide scan, like ``uses``/``type``/``importers``,
    not a ``resolve_target`` lookup, since the common case is ``target``
    naming a stdlib/third-party type that was never extracted as a
    repo ``Symbol`` at all (see ``model.CatchSite``'s docstring).

    Args:
        index: Loaded map index.
        target: Raised type name to search for (e.g. ``ConfigError``,
            ``ValueError``).
        as_json: Emit structured JSON instead of text.
        limit: Cap on text result rows.
        budget: Approximate token budget for the result rows.

    Returns:
        ``(exit_code, meter)`` — meter is ``None`` for JSON output or
        an empty result.
    """
    hits = [s for s in index.catches if _catch_site_matches(s, target)]
    hits.sort(key=lambda s: (s.path, s.line, s.caller))
    exact_count = sum(1 for s in hits if not s.bare)
    catch_all_count = sum(1 for s in hits if s.bare)
    coverage = _coverage_note(index)
    excluded, total, excluded_langs = _catches_excluded_file_count(index)
    lang_list = "/".join(excluded_langs) if excluded_langs else "Rust/Go/C"
    # The JS/TS caveat only applies to a repo that actually has JS/TS
    # files -- printing it unconditionally (round 22 awesome-go.md
    # §3.1) is noise on a 100%-Go/C++/Python repo, where it can never
    # be relevant.
    has_jsts = any(
        lang in {"javascript", "typescript", "tsx"}
        for lang in index.languages_by_path.values()
    )
    if as_json:
        entries = [_catch_entry(s) for s in hits]
        kept, meter = _fit_entries(entries, budget, limit)
        doc = {
            "action": "catches",
            "target": target,
            "results": kept,
            "exact_matches": exact_count,
            "catch_all_matches": catch_all_count,
            "meta": meter.as_dict(),
        }
        if has_jsts:
            doc["note"] = _CATCHES_CAVEAT
        if coverage:
            doc["coverage_warning"] = coverage
        if excluded:
            doc["language_coverage"] = {
                "excluded_files": excluded,
                "total_files": total,
                "reason": (f"{lang_list} are not covered by throws/catches"),
            }
        print(json.dumps(doc, indent=2))
        return EXIT_OK, None
    header = (
        f"{len(hits)} catch clause(s) match '{target}': "
        f"{exact_count} exact, {catch_all_count} catch-all"
        if hits
        else ""
    )
    rows = [_catch_row(index, s) for s in hits]
    if has_jsts:
        print(f"  note: {_CATCHES_CAVEAT}", file=sys.stderr)
    if excluded:
        print(
            f"  note: {excluded:,} of {total:,} mapped files are in a "
            f"language ({lang_list}) this query doesn't cover; results "
            "only reflect the other "
            f"{total - excluded:,} files",
            file=sys.stderr,
        )
    if not rows:
        print(f"(no catch clauses would handle '{target}')")
        if coverage:
            print(f"  note: {coverage}", file=sys.stderr)
        return EXIT_OK, None
    return EXIT_OK, _emit_lines(rows, budget, limit, prefix=header)


# Caveat surfaced in ``env``'s own output (not just ``--help``),
# matching ``catches``'/``throws``' precedent of disclosing scope
# limits in the command's own result, not only reference docs — see
# the design doc's own "disclose the scope boundary in the same
# document a user would read to learn the command exists" discipline.
_ENV_CAVEAT = (
    "detects statically-known getenv-shaped read call sites only — "
    "not a general string-literal search, not assignment/data-flow "
    'tracking ("where does the value end up" is out of scope), and '
    "not config-file (YAML/JSON/TOML/.env) key tracing. A dynamic key "
    "(os.getenv(some_var), an f-string/template-literal key) is "
    "correctly invisible here — no attempt is made to guess it."
)


def _env_call_display(call: str, key: str) -> str:
    """Reconstruct a readable call-expression string for one env-read
    row (``os.getenv("KEY")``, ``os.environ["KEY"]``,
    ``process.env.KEY``) from its stored ``call`` shape label and
    literal ``key``.

    A second, default-value argument the original call may have had
    (``os.getenv("PORT", "8080")``) is never reconstructed here — the
    extractor only captures the key argument (see ``model.EnvRead``'s
    docstring), so there is nothing to show for it.
    """
    if call == "process.env":
        return f"process.env.{key}"
    if call.endswith("[]"):
        return f'{call[:-2]}["{key}"]'
    return f'{call}("{key}")'


def _env_caller_label(index: MapIndex, read: EnvRead) -> str:
    """Human-readable label for an env-read's enclosing definition.

    ``caller_id`` is ``None`` for a module-level read (see
    ``model.EnvRead``'s docstring) — rendered the same "<module
    level>" way every other module-level-origin fact already is
    (``_caller_label``'s throws/catches counterpart), just without a
    ``MODULE_CALLER_SUFFIX`` pseudo-id round trip: ``EnvRead`` stores
    ``None`` directly since nothing else needs to look this fact up
    by id.
    """
    if read.caller_id is None:
        return f"{read.path} <module level>"
    sym = index.symbols_by_id.get(read.caller_id)
    return sym.qualname if sym is not None else read.caller_id


def _env_row(read: EnvRead) -> str:
    """One text row for an ``env`` hit."""
    call = _env_call_display(read.call, read.key)
    return f"{read.path}:{read.line}   {call}"


def _env_entry(index: MapIndex, read: EnvRead) -> dict:
    """One JSON entry for an ``env`` hit."""
    return {
        "path": read.path,
        "line": read.line,
        "key": read.key,
        "call": read.call,
        "caller": _env_caller_label(index, read),
    }


def _run_env_not_found(index: MapIndex, needle: str) -> int:
    """Report an ``env`` target with zero matching read sites."""
    print(f"dekko: no env-var reads found for '{needle}'", file=sys.stderr)
    close = _close_names(needle, sorted(index.env_reads_by_key))
    if close:
        print("closest env vars: " + ", ".join(close), file=sys.stderr)
    coverage = _coverage_note(index)
    if coverage:
        print(f"  note: {coverage}", file=sys.stderr)
    return EXIT_NOT_FOUND


def _run_env(
    index: MapIndex,
    needle: str,
    as_json: bool,
    limit: int,
    budget: int | None,
) -> tuple[int, Meter | None]:
    """Execute the env action: every read site for one literal env-var
    name.

    Exact match only against ``index.env_reads_by_key`` — no loose/
    token matching needed, since env-var names are conventionally
    atomic ``SCREAMING_SNAKE_CASE`` tokens, not composite type-
    annotation text the way ``type``'s matching needs (see the design
    doc). Case-sensitive: ``DATABASE_URL`` and ``database_url`` are
    genuinely different keys (see ``model.EnvRead``'s docstring).

    Args:
        index: Loaded map index.
        needle: Literal env-var name to search for.
        as_json: Emit structured JSON instead of text.
        limit: Cap on text result rows.
        budget: Approximate token budget for the result rows.

    Returns:
        ``(exit_code, meter)`` — meter is ``None`` for JSON output or
        a not-found result.
    """
    reads = index.env_reads_by_key.get(needle, [])
    if not reads:
        return _run_env_not_found(index, needle), None
    reads = sorted(reads, key=lambda r: (is_test_path(r.path), r.path, r.line))
    if as_json:
        entries = [_env_entry(index, r) for r in reads]
        kept, meter = _fit_entries(entries, budget, limit)
        doc = {
            "action": "env",
            "key": needle,
            "results": kept,
            "note": _ENV_CAVEAT,
            "meta": meter.as_dict(),
        }
        coverage = _coverage_note(index)
        if coverage:
            doc["coverage_warning"] = coverage
        print(json.dumps(doc, indent=2))
        return EXIT_OK, None
    print(f"  note: {_ENV_CAVEAT}", file=sys.stderr)
    lines = [_env_row(r) for r in reads]
    return EXIT_OK, _emit_lines(lines, budget, limit)


def _env_list_row(key: str, count: int) -> str:
    """One text row for the ``env --list`` aggregate view."""
    return f"{count:>6}  {key}"


def _env_list_entry(key: str, count: int) -> dict:
    """One JSON entry for the ``env --list`` aggregate view."""
    return {"key": key, "read_sites": count}


def _run_env_list(
    index: MapIndex,
    as_json: bool,
    limit: int,
    budget: int | None,
) -> tuple[int, Meter | None]:
    """Execute ``env --list``: every distinct env-var key read
    anywhere in the repo, ranked by read-site count descending — the
    aggregate view, closer in shape to ``unused``'s flat listing than
    to a ``--by``-grouped report (no natural sub-grouping beyond
    "distinct key, sorted by count").

    Args:
        index: Loaded map index.
        as_json: Emit structured JSON instead of text.
        limit: Cap on text result rows.
        budget: Approximate token budget for the result rows.

    Returns:
        ``(exit_code, meter)`` — meter is ``None`` for JSON output or
        an empty result.
    """
    counts = sorted(
        ((key, len(reads)) for key, reads in index.env_reads_by_key.items()),
        key=lambda kv: (-kv[1], kv[0]),
    )
    coverage = _coverage_note(index)
    if not counts:
        if as_json:
            doc = {
                "action": "env",
                "list": True,
                "distinct_keys": 0,
                "results": [],
                "note": _ENV_CAVEAT,
                "meta": {},
            }
            if coverage:
                doc["coverage_warning"] = coverage
            print(json.dumps(doc, indent=2))
            return EXIT_OK, None
        print("dekko: no statically-known env-var reads found")
        if coverage:
            print(f"  note: {coverage}", file=sys.stderr)
        return EXIT_OK, None
    files = {
        r.path for reads in index.env_reads_by_key.values() for r in reads
    }
    if as_json:
        entries = [_env_list_entry(k, c) for k, c in counts]
        kept, meter = _fit_entries(entries, budget, limit)
        doc = {
            "action": "env",
            "list": True,
            "distinct_keys": len(counts),
            "files": len(files),
            "results": kept,
            "note": _ENV_CAVEAT,
            "meta": meter.as_dict(),
        }
        if coverage:
            doc["coverage_warning"] = coverage
        print(json.dumps(doc, indent=2))
        return EXIT_OK, None
    header = (
        f"dekko: {len(counts)} distinct env vars read across "
        f"{len(files)} files"
    )
    print(f"  note: {_ENV_CAVEAT}", file=sys.stderr)
    lines_out = [header] + [_env_list_row(k, c) for k, c in counts]
    return EXIT_OK, _emit_lines(lines_out, budget, limit)


def _shadow_note(index: MapIndex, target: str) -> str | None:
    """Caveat when an in-repo symbol shares the queried external name.

    An in-repo declaration sharing the target's bare name can suppress
    or corrupt receiver-qualified external-call resolution for that
    name repo-wide (bug #4/B4 — tensorflow's own ``np_array_ops.py::
    array`` shadowed ``np.array(...)``: ``find_usages("array")``
    returned one near-miss hit instead of the ~5,967 real ones, with
    no signal anything was off). This doesn't attempt to detect
    *whether* a given result was actually corrupted — that would need
    re-deriving each call site's receiver binding — it flags the
    precondition (a same-name in-repo symbol exists) unconditionally,
    the same way ``query_symbol`` already discloses "+N ambiguous call
    sites not counted" rather than staying silent about a resolver
    blind spot.

    Args:
        index: Loaded map index.
        target: The external-reference base name queried.

    Returns:
        A one-line caveat, or ``None`` when no in-repo symbol shares
        the name.
    """
    if not (
        index.symbols_by_name.get(target)
        or index.symbols_by_qualname.get(target)
    ):
        return None
    return (
        f"'{target}' is also an in-repo symbol name — this result may "
        "be incomplete if a same-named in-repo definition shadowed "
        f"some external call sites; cross-check with `query callers "
        f"{target}` (or `get_callers`)"
    )


def _run_uses_not_found(index: MapIndex, target: str) -> int:
    """Report a ``uses`` target with zero external matches."""
    internal = index.symbols_by_name.get(
        target
    ) or index.symbols_by_qualname.get(target)
    if internal:
        print(
            f"dekko: '{target}' is an internal symbol, not an "
            "external reference — 'uses'/'find_usages' only "
            "covers out-of-repo names; try `query callers "
            f"{target}` (or the `get_callers` tool) instead",
            file=sys.stderr,
        )
        return EXIT_NOT_FOUND
    print(f"dekko: no external reference matches '{target}'", file=sys.stderr)
    close = _close_names(target, list(index.externals_by_name))
    if close:
        print("closest external names: " + ", ".join(close), file=sys.stderr)
    coverage = _coverage_note(index)
    if coverage:
        print(f"  note: {coverage}", file=sys.stderr)
    return EXIT_NOT_FOUND


def _print_uses_json(
    index: MapIndex,
    target: str,
    exts: list[ExternalCall],
    shadow: str | None,
    budget: int | None,
    limit: int,
) -> None:
    """JSON rendering for a non-empty ``uses`` result."""
    entries = [
        {"caller": e.caller, "callee": e.callee, "lines": e.lines}
        for e in exts
    ]
    kept, meter = _fit_entries(entries, budget, limit)
    doc = {
        "action": "uses",
        "name": target,
        "results": kept,
        "meta": meter.as_dict(),
    }
    coverage = _coverage_note(index)
    if coverage:
        doc["coverage_warning"] = coverage
    if shadow:
        doc["shadow_warning"] = shadow
    print(json.dumps(doc, indent=2))


def _uses_rows(index: MapIndex, exts: list[ExternalCall]) -> list[str]:
    """Text rows for a non-empty ``uses`` result, one per call site."""
    rows: list[str] = []
    for ext in exts:
        path = ext.caller.split("::", 1)[0]
        if ext.caller.endswith(MODULE_CALLER_SUFFIX):
            label = "(module level)"
        else:
            s = index.symbols_by_id.get(ext.caller)
            label = signature(s) if s else ext.caller
        for line in ext.lines or [0]:
            loc = f"{path}:{line}" if line else path
            rows.append(f"{loc}  {label}  [{ext.callee}]")
    return rows


def _run_uses(
    index: MapIndex,
    target: str,
    as_json: bool,
    limit: int,
    budget: int | None,
) -> tuple[int, Meter | None]:
    """Execute the uses action: who references an external name."""
    exts = index.externals_by_name.get(target, [])
    if not exts:
        return _run_uses_not_found(index, target), None
    exts = sorted(
        exts, key=lambda e: (is_test_path(e.caller), e.caller, e.callee)
    )
    shadow = _shadow_note(index, target)
    if as_json:
        _print_uses_json(index, target, exts, shadow, budget, limit)
        return EXIT_OK, None
    if shadow:
        print(f"  note: {shadow}", file=sys.stderr)
    return EXIT_OK, _emit_lines(_uses_rows(index, exts), budget, limit)


def _source_matches(
    imp: Import, language: str, needle: str, exact: bool
) -> bool:
    """Whether an import's source text names ``needle``.

    Default (``exact=False``): a plain substring check against the raw
    stored ``Import.source``. Import source strings are already bare
    module/path text (``os.path``, ``../utils``,
    ``std::collections::HashMap``) with no generic/pointer wrapper
    syntax to see through, unlike ``type``'s identifier-token matching
    — a substring check alone correctly matches ``os.path`` against
    both ``import os.path`` and ``from os.path import join``.

    ``exact=True`` compares against ``bare_import_source(imp,
    language)`` instead of the raw ``imp.source`` — for JS/TS, the
    stored source has an arbitrary local binding name appended
    (``"react/useState"``, see ``extractor._imports_js``), which no
    developer would think to type; ``bare_import_source`` strips that
    back off so ``--exact "react"`` matches. Every other language's
    ``bare_import_source`` is a no-op (returns ``imp.source``
    unchanged), so this is a JS/TS-only behavior change. Trailing-
    slash-normalized either way so a relative source (``./utils`` vs.
    ``./utils/``) isn't a false negative.

    Args:
        imp: The matched ``Import`` entry.
        language: The importing file's language (``index.
            languages_by_path[path]``), used to pick the right
            bare-source stripping rule.
        needle: Text being searched for.
        exact: Match the literal bare source string instead of a
            substring.

    Returns:
        True when the import matches ``needle`` under the chosen mode.
    """
    if exact:
        bare = bare_import_source(imp, language)
        return bare.rstrip("/") == needle.rstrip("/")
    return needle in imp.source


def _importers_row(path: str, imp: Import, language: str) -> str:
    """One text row for an ``importers`` hit.

    Displays ``bare_import_source(imp, language)`` rather than the raw
    ``imp.source`` — for JS/TS, the stored source has an arbitrary
    local binding name appended (``"./engine/generateBones"`` for
    ``import { generateBones } from "./engine"``), which would
    otherwise print a submodule path that doesn't exist on disk (round
    22 claude-buddy.md §2.2).
    """
    bare = bare_import_source(imp, language)
    if not imp.name:
        return f"{path}  {bare}  (side-effect import)"
    return f"{path}  {bare}  (as {imp.name})"


def _importers_entry(path: str, imp: Import, language: str) -> dict:
    """One JSON entry for an ``importers`` hit.

    ``source`` is ``bare_import_source(imp, language)`` — see
    ``_importers_row`` for why.
    """
    return {
        "path": path,
        "local_name": imp.name or None,
        "source": bare_import_source(imp, language),
    }


def _run_importers_not_found(index: MapIndex, needle: str) -> int:
    """Report an ``importers`` target with zero matching imports."""
    print(f"dekko: no imports match '{needle}'", file=sys.stderr)
    sources = sorted(
        {
            imp.source
            for imports in index.imports_by_path.values()
            for imp in imports
        }
    )
    close = _close_names(needle, sources)
    if close:
        print("closest import sources: " + ", ".join(close), file=sys.stderr)
    coverage = _coverage_note(index)
    if coverage:
        print(f"  note: {coverage}", file=sys.stderr)
    return EXIT_NOT_FOUND


def _run_importers(
    index: MapIndex,
    needle: str,
    exact: bool,
    as_json: bool,
    limit: int,
    budget: int | None,
) -> tuple[int, Meter | None]:
    """Execute the importers action: files importing a matching source.

    Reconstructed at query time from ``index.imports_by_path`` rather
    than a pre-built reverse index — that dict is small (one entry per
    file, not per symbol), already fully loaded, and a full scan is
    O(files), the same "reconstruct vs. add an index field" call
    ``ambiguous.py``'s ``_raw_triples`` already made for a structurally
    identical tradeoff.

    Args:
        index: Loaded map index.
        needle: Raw import-source text (or substring) to search for.
        exact: Match the literal source string instead of a substring.
        as_json: Emit structured JSON instead of text.
        limit: Cap on text result rows.
        budget: Approximate token budget for the result rows.

    Returns:
        ``(exit_code, meter)`` — meter is ``None`` for JSON output or a
        not-found result.
    """
    rows = [
        (path, imp)
        for path, imports in index.imports_by_path.items()
        for imp in imports
        if _source_matches(
            imp, index.languages_by_path.get(path, ""), needle, exact
        )
    ]
    if not rows:
        return _run_importers_not_found(index, needle), None
    rows.sort(key=lambda r: (is_test_path(r[0]), r[0], r[1].name))
    if as_json:
        entries = [
            _importers_entry(p, imp, index.languages_by_path.get(p, ""))
            for p, imp in rows
        ]
        kept, meter = _fit_entries(entries, budget, limit)
        doc = {
            "action": "importers",
            "source": needle,
            "exact": exact,
            "results": kept,
            "meta": meter.as_dict(),
        }
        coverage = _coverage_note(index)
        if coverage:
            doc["coverage_warning"] = coverage
        print(json.dumps(doc, indent=2))
        return EXIT_OK, None
    lines = [
        _importers_row(p, imp, index.languages_by_path.get(p, ""))
        for p, imp in rows
    ]
    return EXIT_OK, _emit_lines(lines, budget, limit)


def _type_matches(type_text: str | None, needle: str, exact: bool) -> bool:
    """Whether a stored ``params[].type``/``returns`` string names ``needle``.

    Types are raw, unparsed text straight from the tree-sitter type
    annotation node (``Optional[Config]``, ``Config | None``,
    ``*Config``, ``&mut Config``, ``Vec<Config>``) — there's no type AST
    to walk here, only string matching. ``exact=False`` (the default)
    tokenizes on non-identifier characters and requires ``needle`` to
    appear as a whole token, which matches every wrapper form above
    while still rejecting ``ConfigManager``/``AppConfig``. ``exact=True``
    requires the stripped type text to equal ``needle`` verbatim.

    Args:
        type_text: The stored type string, or ``None`` when the
            parameter/return has no declared type.
        needle: Type name being searched for.
        exact: Match the literal string instead of a bare token.

    Returns:
        True when ``type_text`` names ``needle`` under the chosen mode.
    """
    if not type_text:
        return False
    if exact:
        return type_text.strip() == needle
    return needle in _IDENT_RE.findall(type_text)


def _type_usage_row(sym: Symbol, usage: str, param_name: str | None) -> str:
    """One text row for a type-usage hit."""
    tag = f"[param: {param_name}]" if usage == "param" else "[return]"
    return f"{_sym_line(sym)}  {tag}"


def _type_usage_entry(
    index: MapIndex,
    sym: Symbol,
    usage: str,
    param_name: str | None,
    raw_type: str,
) -> dict:
    """One JSON entry for a type-usage hit."""
    entry = _sym_json(index, sym)
    entry["usage"] = usage
    if param_name is not None:
        entry["param_name"] = param_name
    entry["raw_type"] = raw_type
    return entry


def _run_type_not_found(index: MapIndex, needle: str) -> int:
    """Report a ``type`` target with zero matching functions/methods."""
    print(f"dekko: no results for type '{needle}'", file=sys.stderr)
    type_names = [
        s.name for s in index.symbols_by_id.values() if s.kind in TYPE_KINDS
    ]
    close = _close_names(needle, type_names)
    if close:
        print("closest type names: " + ", ".join(close), file=sys.stderr)
    coverage = _coverage_note(index)
    if coverage:
        print(f"  note: {coverage}", file=sys.stderr)
    return EXIT_NOT_FOUND


def type_usage_rows(
    index: MapIndex, needle: str, exact: bool = False
) -> list[tuple[Symbol, str, str | None, str]]:
    """Every function/method row using ``needle`` as a param/return type.

    The reusable core behind ``_run_type_usage`` (``dekko query type``/
    ``find_type_usages``) — factored out so a second call site
    (``workset``'s ``--type-impact``) can reuse the same matching logic
    without duplicating it. Walks every ``function``/``method``
    symbol's ``returns`` and ``params[].type`` directly — a pure read
    over data already on disk in ``map.json``, no resolver involvement.
    Only ``function``/``method`` symbols ever carry non-empty
    ``params``/``returns`` (see ``extractor._collect_definitions``), so
    this cannot answer "what struct fields are typed X" — that's a
    real extraction-pipeline gap, not a matching-strategy shortcoming.

    Args:
        index: Loaded map index.
        needle: Type name to search for.
        exact: Match the literal stored type text instead of a bare
            identifier token inside wrapper syntax.

    Returns:
        ``(symbol, usage, param_name, raw_type)`` rows — ``usage`` is
        ``"param"`` or ``"return"``, ``param_name`` is ``None`` for a
        return-type row — sorted by relevance (most central/production
        code first). A symbol appears once per matching param/return,
        so it can appear more than once (e.g. a function that both
        takes and returns the same type).
    """
    rows: list[tuple[Symbol, str, str | None, str]] = []
    for sym in index.symbols_by_id.values():
        if sym.kind not in ("function", "method"):
            continue
        if _type_matches(sym.returns, needle, exact):
            rows.append((sym, "return", None, sym.returns))
        rows.extend(
            (sym, "param", p.name, p.type)
            for p in sym.params
            if _type_matches(p.type, needle, exact)
        )
    rows.sort(key=lambda r: relevance_key(r[0], index))
    return rows


def type_usage_name_index(index: MapIndex) -> frozenset[str]:
    """Every identifier token used as a param/return type, repo-wide.

    ``unused.py``'s ``--kinds types`` needs an ``exact=False``
    "is this type used as a param/return type anywhere" answer for
    *every* type symbol in the repo, not just one needle. Calling
    ``type_usage_rows`` once per type name is O(types * symbols) —
    measured at ~370s on spring-boot's ~14k type symbols against its
    ~69k total symbols, unacceptable for a single CLI invocation. This
    inverts the loop instead: one O(symbols) pass tokenizing every
    function/method's ``returns``/``params[].type`` text the same way
    ``_type_matches(..., exact=False)`` does, building the set once so
    each type symbol's membership check is O(1) after. The same
    "reconstruct once, not per-query" discipline ``ambiguous.py``'s own
    ``_raw_triples`` already established for a structurally analogous
    cost concern.

    Args:
        index: Loaded map index.

    Returns:
        Every identifier token found in any function/method's declared
        return type or parameter type text. A type name in this set
        has at least one ``exact=False`` type-usage match somewhere in
        the repo (per ``type_usage_rows(index, name, exact=False)``);
        a type name absent from it has none.
    """
    names: set[str] = set()
    for sym in index.symbols_by_id.values():
        if sym.kind not in ("function", "method"):
            continue
        if sym.returns:
            names.update(_IDENT_RE.findall(sym.returns))
        for p in sym.params:
            if p.type:
                names.update(_IDENT_RE.findall(p.type))
    return frozenset(names)


def _run_type_usage(
    index: MapIndex,
    needle: str,
    exact: bool,
    as_json: bool,
    limit: int,
    budget: int | None,
) -> tuple[int, Meter | None]:
    """Execute the type action: functions/methods taking/returning needle.

    Args:
        index: Loaded map index.
        needle: Type name to search for.
        exact: Match the literal stored type text instead of a bare
            identifier token inside wrapper syntax.
        as_json: Emit structured JSON instead of text.
        limit: Cap on text result rows.
        budget: Approximate token budget for the result rows.

    Returns:
        ``(exit_code, meter)`` — meter is ``None`` for JSON output or a
        not-found result.
    """
    rows = type_usage_rows(index, needle, exact)
    if not rows:
        return _run_type_not_found(index, needle), None
    if as_json:
        entries = [
            _type_usage_entry(index, sym, usage, pname, raw)
            for sym, usage, pname, raw in rows
        ]
        kept, meter = _fit_entries(entries, budget, limit)
        doc = {
            "action": "type",
            "name": needle,
            "exact": exact,
            "results": kept,
            "meta": meter.as_dict(),
        }
        coverage = _coverage_note(index)
        if coverage:
            doc["coverage_warning"] = coverage
        print(json.dumps(doc, indent=2))
        return EXIT_OK, None
    lines = [
        _type_usage_row(sym, usage, pname) for sym, usage, pname, _ in rows
    ]
    return EXIT_OK, _emit_lines(lines, budget, limit)


def _heritage_direction(action: str) -> str:
    """``"out"`` for ``supertypes`` (walk what a type points at),
    ``"in"`` for ``subtypes`` (walk what points at a type) — mirrors
    ``calls_out``/``calls_in``'s own directional split for
    callers/callees."""
    return "out" if action == "supertypes" else "in"


def _heritage_adjacency(
    index: MapIndex, direction: str
) -> dict[str, list[str]]:
    """The right adjacency table for a heritage walk direction."""
    return index.heritage_out if direction == "out" else index.heritage_in


def _heritage_relation_between(
    index: MapIndex, direction: str, node: str, other: str
) -> str:
    """The relation of one heritage edge, defaulting to ``"extends"``.

    ``index.heritage_relation`` is always keyed ``(subtype, supertype)``
    regardless of which direction a walk is traveling — this flips the
    lookup key for ``direction == "in"`` so callers never need to know
    that detail. The default only matters for a map written before
    doc version 6 (no ``heritage_relation`` entries at all), where
    ``"extends"`` is the least-surprising fallback (every Phase 1
    language's plain-class case).
    """
    key = (node, other) if direction == "out" else (other, node)
    return index.heritage_relation.get(key, "extends")


def _one_hop_heritage(
    index: MapIndex, start: str, direction: str, relation: str | None
) -> list[tuple[Symbol, str, int]]:
    """Direct heritage neighbors of ``start``, each tagged depth 1."""
    adjacency = _heritage_adjacency(index, direction)
    out: list[tuple[Symbol, str, int]] = []
    for nid in adjacency.get(start, []):
        rel = _heritage_relation_between(index, direction, start, nid)
        if relation and rel != relation:
            continue
        sym = index.symbols_by_id.get(nid)
        if sym is not None:
            out.append((sym, rel, 1))
    out.sort(key=lambda item: relevance_key(item[0], index))
    return out


def walk_heritage(
    index: MapIndex, start: str, direction: str, relation: str | None
) -> list[tuple[Symbol, str, int]]:
    """BFS collect every ancestor/descendant of ``start``, depth-tagged.

    Public (no leading underscore) since ``workset``'s ``--type-impact``
    reuses this directly as a second call site, not just
    ``_run_heritage`` (``dekko query supertypes``/``subtypes``).
    Collects every ancestor (``direction="out"``) or descendant
    (``direction="in"``) of ``start``, each tagged with its hop depth
    and the relation of the edge that discovered it. Cycle-safe (a
    ``seen`` set, matching ``trace.py``'s own BFS pattern) even though
    a real heritage graph should never cycle — a resolver
    misattribution is the only way one could, and this must not hang
    if it happens. Diamond inheritance (a symbol reachable through two
    different paths) is deduplicated by ``seen``: it appears once, at
    its first-discovered (shallowest) depth, never twice.

    Args:
        index: Loaded map index.
        start: Symbol id to walk from.
        direction: ``"out"`` (supertypes) or ``"in"`` (subtypes).
        relation: Restrict the walk to edges of this relation only, or
            ``None`` for every relation.

    Returns:
        ``(symbol, relation, depth)`` triples in BFS (shallowest-first)
        order — depth-grouped, the order the text renderer indents by.
    """
    adjacency = _heritage_adjacency(index, direction)
    seen = {start}
    frontier: deque[tuple[str, int]] = deque([(start, 0)])
    out: list[tuple[Symbol, str, int]] = []
    while frontier:
        node, depth = frontier.popleft()
        for nid in adjacency.get(node, []):
            rel = _heritage_relation_between(index, direction, node, nid)
            if relation and rel != relation:
                continue
            if nid in seen or nid not in index.symbols_by_id:
                continue
            seen.add(nid)
            out.append((index.symbols_by_id[nid], rel, depth + 1))
            frontier.append((nid, depth + 1))
    return out


def _heritage_row(sym: Symbol, relation: str, depth: int) -> str:
    """One text row for a heritage hit, indented by hop depth."""
    indent = "  " * (depth - 1)
    return f"{indent}{_sym_line(sym)}  [{relation}]"


def _heritage_entry(
    index: MapIndex, sym: Symbol, relation: str, depth: int
) -> dict:
    """One JSON entry for a heritage hit."""
    entry = _sym_json(index, sym)
    entry["relation"] = relation
    entry["depth"] = depth
    return entry


def _heritage_external_label(index: MapIndex, sym: Symbol, name: str) -> str:
    """``"external"`` or ``"unresolved"`` for a heritage clause dekko
    couldn't resolve to a repo symbol.

    Distinguishes two very different situations the resolver's
    ``heritage_external_out`` bucket otherwise conflates under one
    ``(external)`` label (round-18 claude-code finding): a genuinely
    out-of-repo base type (an npm/stdlib/framework class) versus an
    in-repo name the extractor simply never captured as a
    heritage-eligible symbol — the concrete case being a TS
    ``type X = { ... }`` object-type alias used with ``implements``,
    which parses fine but isn't extracted as any kind of ``Symbol`` at
    all (``query symbol`` on it returns "no symbol matches"). Labeling
    that ``(external)`` actively misleads: an agent reading it would
    reasonably conclude the base is a framework/stdlib type, when it's
    first-party code one ``query symbol`` lookup away.

    This is a presentation-only distinction — no new resolution is
    attempted, and the edge is still not walkable either way. Two
    narrow, JS/TS-specific signals are checked (the only language
    family where this extraction gap is currently known to apply):

    1. A same-file type-alias declaration with this bare name exists
       (``index.type_aliases_by_path`` — round-19 claude-code finding:
       ``ShellCommandImpl implements ShellCommand`` where
       ``ShellCommand`` is a same-file ``type X = {...}``. The
       original round-18 fix only covered the cross-file case below,
       since a same-file alias needs no import statement and so never
       had a candidate for that loop to check).
    2. The clause's target name matches a same-named import in the
       subtype's own file, and that import's source looks like a
       relative path into the repo (starts with ``.`` or ``/``, as
       opposed to a bare package specifier like ``"react"``) — the
       original round-18 signal, for the cross-file case.

    Either signal is enough to know the name is at least local.

    Args:
        index: Loaded map index.
        sym: The subtype symbol whose heritage clause is unresolved.
        name: The heritage clause's raw target text (``ext.callee``).

    Returns:
        ``"unresolved"`` when a same-file type alias or a same-named
        relative import exists for this name, ``"external"``
        otherwise.
    """
    bare = name.split("<", 1)[0].strip().rsplit(".", 1)[-1]
    if bare in index.type_aliases_by_path.get(sym.path, frozenset()):
        return "unresolved"
    for imp in index.imports_by_path.get(sym.path, []):
        if imp.name == bare and imp.source.startswith((".", "/")):
            return "unresolved"
    return "external"


def _run_heritage_wrong_kind(sym: Symbol) -> int:
    """Report a resolved target that isn't a type-kind symbol."""
    print(
        f"dekko: '{sym.id}' is a {sym.kind}, not a type; "
        "supertypes/subtypes only apply to class/interface/enum/"
        "struct/record/trait symbols",
        file=sys.stderr,
    )
    return EXIT_NOT_FOUND


def _print_heritage_json(
    index: MapIndex,
    action: str,
    sym: Symbol,
    hits: list[tuple[Symbol, str, int]],
    externals: list[ExternalCall],
    transitive: bool,
    relation: str | None,
    budget: int | None,
    limit: int,
    coverage: str | None,
    ambig_in: int,
    ambig_out: int,
) -> None:
    """JSON rendering for ``_run_heritage`` (supertypes/subtypes)."""
    entries = [_heritage_entry(index, s, rel, depth) for s, rel, depth in hits]
    entries.extend(
        {
            "external": True,
            "text": ext.callee,
            "lines": ext.lines,
            "unresolved_local": (
                _heritage_external_label(index, sym, ext.callee)
                == "unresolved"
            ),
        }
        for ext in externals
    )
    kept, meter = _fit_entries(entries, budget, limit)
    doc = {
        "action": action,
        "target": sym.id,
        "transitive": transitive,
        "results": kept,
        "meta": meter.as_dict(),
    }
    if relation:
        doc["relation"] = relation
    if coverage:
        doc["coverage_warning"] = coverage
    if ambig_in:
        doc["ambiguous_in"] = ambig_in
    if ambig_out:
        doc["ambiguous_out"] = ambig_out
    print(json.dumps(doc, indent=2))


def _run_heritage(
    index: MapIndex,
    action: str,
    sym: Symbol,
    transitive: bool,
    relation: str | None,
    as_json: bool,
    limit: int,
    budget: int | None,
) -> tuple[int, Meter | None]:
    """Execute the supertypes/subtypes action for a resolved type symbol.

    ``supertypes`` walks ``heritage_out`` (what ``sym`` itself declares
    heritage toward); ``subtypes`` walks ``heritage_in`` (what declares
    heritage toward ``sym``) — the same inbound/outbound split
    ``callers``/``callees`` already use for the call graph.
    ``--transitive``/``relation="..."`` select a full BFS ancestor/
    descendant walk (``walk_heritage``) versus one hop
    (``_one_hop_heritage``); a relation filter applies at every hop.

    External heritage (``heritage_external_out``, ``supertypes`` only —
    a subtype relates outward to real or external supertypes, but
    nothing ever points *in* from an id-less external symbol) and
    ambiguous-heritage counts (``heritage_ambiguous_out`` for
    ``supertypes``, ``heritage_ambiguous_in`` for ``subtypes``) are
    disclosed for ``sym`` itself only — not recursively for
    intermediate nodes discovered by a transitive walk, since an
    external supertype has no symbol id to continue walking from and
    keeping the disclosure scoped to the query's own target mirrors
    how ``_run_relation`` already scopes ``ambig_in``/``ambig_out`` to
    the target alone rather than every hop of a (non-existent, for
    calls) multi-hop traversal.
    """
    direction = _heritage_direction(action)
    hits = (
        walk_heritage(index, sym.id, direction, relation)
        if transitive
        else _one_hop_heritage(index, sym.id, direction, relation)
    )
    externals = (
        index.heritage_external_out.get(sym.id, [])
        if action == "supertypes"
        else []
    )
    ambig_out = (
        len(index.heritage_ambiguous_out.get(sym.id, []))
        if action == "supertypes"
        else 0
    )
    ambig_in = (
        len(index.heritage_ambiguous_in.get(sym.id, []))
        if action == "subtypes"
        else 0
    )
    coverage = _coverage_note(index)
    if as_json:
        _print_heritage_json(
            index,
            action,
            sym,
            hits,
            externals,
            transitive,
            relation,
            budget,
            limit,
            coverage,
            ambig_in,
            ambig_out,
        )
        return EXIT_OK, None
    lines: list[str] = []
    if transitive and hits:
        lines.append(f"{_sym_line(sym)}  [target]")
    lines += [_heritage_row(s, rel, depth) for s, rel, depth in hits]
    for ext in externals:
        label = _heritage_external_label(index, sym, ext.callee)
        for line in ext.lines or [sym.start_line]:
            lines.append(f"  {sym.path}:{line}  ({label}) {ext.callee}")
    if ambig_out:
        print(
            f"  note: {ambig_out} additional supertype name(s) "
            "resolved ambiguously — not counted here",
            file=sys.stderr,
        )
    if ambig_in:
        print(
            f"  note: {ambig_in} additional subtype(s) named this "
            "type ambiguously — not counted here",
            file=sys.stderr,
        )
    if not lines:
        print(f"(no {action} of {sym.id})")
        if coverage:
            print(f"  note: {coverage}", file=sys.stderr)
        return EXIT_OK, None
    return EXIT_OK, _emit_lines(lines, budget, limit)


_TYPE_ZERO_FAN_NOTE = (
    "call/reference edges only — a type used solely as a parameter, "
    "field, or return-type annotation reports 0/0 here even when it's "
    "heavily used; this is not evidence the type is unused"
)


def _run_symbol(
    index: MapIndex, sym: Symbol, as_json: bool, notes: bool
) -> tuple[int, Meter | None]:
    """Execute the symbol card action."""
    fan_in = len(index.calls_in.get(sym.id, []))
    fan_out = len(index.calls_out.get(sym.id, []))
    ambig_in = len(index.ambiguous_in.get(sym.id, []))
    referenced_by = len(index.referenced_in.get(sym.id, []))
    sym_notes = index.notes.get(sym.id, []) if notes else []
    # A struct/class/interface/... only ever gets call/reference edges
    # from being constructed or passed by value — used purely as a
    # parameter/field/return-type annotation, it always reads 0/0,
    # which is easy to misread as "unused" (bug #11/B11).
    type_zero_fan = (
        sym.kind in TYPE_KINDS
        and fan_in == 0
        and fan_out == 0
        and not referenced_by
    )
    if as_json:
        doc = _sym_json(index, sym)
        doc.update(
            {
                "language": sym.language,
                "end_line": sym.end_line,
                "fan_in": fan_in,
                "fan_out": fan_out,
            }
        )
        if ambig_in:
            doc["ambiguous_in"] = ambig_in
        if referenced_by:
            doc["referenced_by"] = referenced_by
        if type_zero_fan:
            doc["fan_note"] = _TYPE_ZERO_FAN_NOTE
        if notes:
            doc["notes"] = index.notes.get(sym.id, [])
        print(json.dumps(doc, indent=2))
        return EXIT_OK, None
    print(signature(sym))
    print(f"  kind: {sym.kind} ({sym.language})")
    print(f"  at: {sym.path}:{sym.start_line}-{sym.end_line}")
    fan_line = f"  fan-in: {fan_in}, fan-out: {fan_out}"
    if ambig_in:
        fan_line += f" (+{ambig_in} ambiguous call sites not counted)"
    print(fan_line)
    if referenced_by:
        # fan-in alone can read as "definitely unused" for a callback
        # wired up by reference and never itself called (bug #2b) —
        # this line is what stops that misread.
        print(f"  referenced-by: {referenced_by} (not called)")
    if type_zero_fan:
        print(f"  note: {_TYPE_ZERO_FAN_NOTE}")
    for text in sym_notes:
        print(f"  note: {text}")
    return EXIT_OK, None


def _run_file(
    index: MapIndex,
    target: str,
    as_json: bool,
    limit: int,
    budget: int | None,
) -> tuple[int, Meter | None]:
    """Execute the file action: list a file's symbols."""
    matches = paths_matching(index, target)
    if not matches:
        print(f"dekko: no mapped file matches '{target}'", file=sys.stderr)
        coverage = _coverage_note(index)
        if coverage:
            print(f"  note: {coverage}", file=sys.stderr)
        return EXIT_NOT_FOUND, None
    if len(matches) > 1:
        print(
            f"dekko: '{target}' is ambiguous; candidates:",
            file=sys.stderr,
        )
        for p in matches:
            print(f"  {p}", file=sys.stderr)
        return EXIT_AMBIGUOUS, None

    path = matches[0]
    symbols = index.symbols_by_path[path]
    if as_json:
        entries = [_sym_json(index, s) for s in symbols]
        kept, meter = _fit_entries(entries, budget, limit)
        doc = {
            "path": path,
            "language": index.languages_by_path.get(path, ""),
            "symbols": kept,
            "meta": meter.as_dict(),
        }
        print(json.dumps(doc, indent=2))
        return EXIT_OK, None
    return EXIT_OK, _emit_lines([_sym_line(s) for s in symbols], budget, limit)


# ``cohesion``'s weak-signal disclosure (design doc: "the note: line is
# load-bearing, not decorative"). Always printed, in both text and
# JSON output, never dropped by budget/limit capping — this is what
# keeps a connectivity view from being misread as real "which
# functions belong together" clustering, which this action does not
# implement (see ``symbol-cohesion-clustering-design.md``).
_COHESION_NOTE = (
    "note: this groups symbols that are mutually reachable, not "
    "symbols that are tightly coupled vs. loosely coupled — a file "
    "that's one connected component (the common case) gets no useful "
    'split suggestion from this view. Real "which functions belong '
    'together" clustering is not implemented.'
)


def _intra_file_edges(index: MapIndex, path: str) -> list[tuple[str, str]]:
    """Resolved call/reference edges where both endpoints are defined
    in ``path``.

    Restricts ``calls_out``/``referenced_out`` (already resolved,
    already fully loaded — the entire data cost of this feature) to
    edges wholly inside one file. Module-level pseudo-caller ids (see
    ``MODULE_CALLER_SUFFIX``) appear as keys in ``calls_out`` too, but
    are never real ``Symbol`` ids, so they're never in ``in_file`` and
    drop out of the ``src not in in_file`` check automatically — no
    special-casing needed here, unlike ``peers``'s handling of the
    same id shape.

    Args:
        index: Loaded map index.
        path: Repo-relative path of the file being analyzed.

    Returns:
        ``(src, dst)`` id pairs, one per resolved intra-file edge,
        self-references excluded. A pair appearing in both
        ``calls_out`` and ``referenced_out`` is kept twice — these are
        two distinct edge types (a call vs. a by-value reference), not
        one edge double-counted.
    """
    in_file = {s.id for s in index.symbols_by_path.get(path, [])}
    edges: list[tuple[str, str]] = []
    for table in (index.calls_out, index.referenced_out):
        for src, dsts in table.items():
            if src not in in_file:
                continue
            edges.extend(
                (src, dst) for dst in dsts if dst in in_file and dst != src
            )
    return edges


def connected_components(
    ids: list[str], edges: list[tuple[str, str]]
) -> list[set[str]]:
    """Union-Find over an intra-file edge list. O(E * alpha(V)).

    Every id in ``ids`` gets its own component even with zero edges —
    the result always partitions the full id set, not just the ids
    that happen to appear in ``edges``, so a symbol with no intra-file
    edges still shows up as its own size-1 component rather than being
    silently dropped.

    Args:
        ids: Every symbol id to partition (typically a file's full
            symbol set, in definition order).
        edges: ``(src, dst)`` pairs to union together.

    Returns:
        Every connected component as a set of member ids.
    """
    parent = {i: i for i in ids}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in edges:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    groups: dict[str, set[str]] = {}
    for i in ids:
        groups.setdefault(find(i), set()).add(i)
    return list(groups.values())


def _cohesion_groups(
    ids: list[str], edges: list[tuple[str, str]]
) -> tuple[list[list[str]], list[str]]:
    """Split a file's connected components into clusters and isolates.

    A component of size 1 is, by construction, a symbol with zero
    intra-file edges (any edge would have unioned it with another
    symbol) — so no separate "has no edges" check is needed to
    classify it as isolated.

    Args:
        ids: Every symbol id in the file, in definition order.
        edges: Intra-file ``(src, dst)`` edges, from
            ``_intra_file_edges``.

    Returns:
        ``(clusters, isolated)``. ``clusters`` is every component with
        2+ members (each a list of member ids in definition order),
        sorted largest-first, ties broken by the first member's
        definition order. ``isolated`` is every 1-member component's
        id, in definition order.
    """
    order = {sid: i for i, sid in enumerate(ids)}
    components = connected_components(ids, edges)
    clusters = sorted(
        (sorted(c, key=order.__getitem__) for c in components if len(c) > 1),
        key=lambda c: (-len(c), order[c[0]]),
    )
    isolated = sorted(
        (next(iter(c)) for c in components if len(c) == 1),
        key=order.__getitem__,
    )
    return clusters, isolated


def _cohesion_json(
    path: str,
    symbol_count: int,
    edge_count: int,
    clusters: list[list[str]],
    isolated: list[str],
    names_by_id: dict[str, str],
) -> dict:
    """Build the ``cohesion`` JSON document."""
    return {
        "action": "cohesion",
        "path": path,
        "symbol_count": symbol_count,
        "edge_count": edge_count,
        "weak_signal": True,
        "components": [
            {"size": len(c), "symbols": [names_by_id[i] for i in c]}
            for c in clusters
        ],
        "isolated": [names_by_id[i] for i in isolated],
        "note": _COHESION_NOTE,
    }


def _run_cohesion(
    index: MapIndex,
    target: str,
    as_json: bool,
    limit: int,
    budget: int | None,
) -> tuple[int, Meter | None]:
    """Execute the cohesion action: intra-file connected-components.

    A deliberately weak signal, not real clustering — see
    ``_COHESION_NOTE`` and the design doc. Groups a file's symbols by
    mutual reachability over intra-file calls/references only; a file
    that's fully connected (the common case) yields one big cluster
    and no useful split suggestion.

    Args:
        index: Loaded map index.
        target: File path (matched like the ``file`` action — exact
            repo-relative path, or any trailing path suffix).
        as_json: Emit structured JSON instead of text.
        limit: Cap on text result rows (one row per cluster, plus one
            for the isolated group).
        budget: Approximate token budget for the result rows; the
            header and the weak-signal note are never dropped.

    Returns:
        ``(exit_code, meter)`` — meter is ``None`` for JSON output or
        a not-found/ambiguous result.
    """
    matches = paths_matching(index, target)
    if not matches:
        print(f"dekko: no mapped file matches '{target}'", file=sys.stderr)
        coverage = _coverage_note(index)
        if coverage:
            print(f"  note: {coverage}", file=sys.stderr)
        return EXIT_NOT_FOUND, None
    if len(matches) > 1:
        print(
            f"dekko: '{target}' is ambiguous; candidates:",
            file=sys.stderr,
        )
        for p in matches:
            print(f"  {p}", file=sys.stderr)
        return EXIT_AMBIGUOUS, None

    path = matches[0]
    symbols = index.symbols_by_path[path]
    ids = [s.id for s in symbols]
    edges = _intra_file_edges(index, path)
    clusters, isolated = _cohesion_groups(ids, edges)
    names_by_id = {s.id: s.qualname for s in symbols}

    if as_json:
        doc = _cohesion_json(
            path, len(ids), len(edges), clusters, isolated, names_by_id
        )
        print(json.dumps(doc, indent=2))
        return EXIT_OK, None

    header = (
        f"dekko: {len(ids)} symbols, {len(edges)} intra-file edges "
        "(weak signal — connectivity only, not clustering)"
    )
    rows = [
        f"  connected component {n} ({len(c)} symbols): "
        + ", ".join(names_by_id[i] for i in c)
        for n, c in enumerate(clusters, start=1)
    ]
    if isolated:
        rows.append(
            f"  isolated ({len(isolated)} symbols, no intra-file "
            "edges): " + ", ".join(names_by_id[i] for i in isolated)
        )
    prefix = header + "\n" + _COHESION_NOTE
    kept, meter = fit_to_budget(rows, budget, limit, prefix=prefix)
    print(header)
    for row in kept:
        print(row)
    print(_COHESION_NOTE)
    return EXIT_OK, meter


def _dispatch_scan(
    index: MapIndex,
    action: str,
    target: str,
    as_json: bool,
    limit: int,
    budget: int | None,
    exact: bool,
    env_list: bool,
) -> tuple[int, Meter | None] | None:
    """Route the whole-repo-scan actions that never resolve a symbol
    target (``file``/``uses``/``type``/``importers``/``catches``/
    ``env``/``cohesion``).

    Split out of ``_dispatch`` purely to keep that function's
    cyclomatic complexity under the project's Ruff limit. Returns
    ``None`` when ``action`` is none of these, telling ``_dispatch``
    to fall through to ``resolve_target``-based routing instead.
    """
    if action == "file":
        return _run_file(index, target, as_json, limit, budget)
    if action == "uses":
        return _run_uses(index, target, as_json, limit, budget)
    if action == "type":
        return _run_type_usage(index, target, exact, as_json, limit, budget)
    if action == "importers":
        return _run_importers(index, target, exact, as_json, limit, budget)
    if action == "catches":
        return _run_catches(index, target, as_json, limit, budget)
    if action == "cohesion":
        return _run_cohesion(index, target, as_json, limit, budget)
    if action == "env":
        if env_list:
            return _run_env_list(index, as_json, limit, budget)
        return _run_env(index, target, as_json, limit, budget)
    return None


def _dispatch(
    index: MapIndex,
    action: str,
    target: str,
    as_json: bool,
    limit: int,
    budget: int | None,
    sites: bool,
    notes: bool,
    exact: bool,
    transitive: bool,
    relation: str | None,
    min_shared: int,
    depth: int,
    env_list: bool,
) -> tuple[int, Meter | None]:
    """Route one query action to its executor."""
    scanned = _dispatch_scan(
        index, action, target, as_json, limit, budget, exact, env_list
    )
    if scanned is not None:
        return scanned

    sym, candidates = resolve_target(index, target)
    if sym is None:
        return report_unresolved(target, candidates, index), None
    if action == "symbol":
        return _run_symbol(index, sym, as_json, notes)
    if action in ("supertypes", "subtypes"):
        if sym.kind not in TYPE_KINDS:
            return _run_heritage_wrong_kind(sym), None
        return _run_heritage(
            index, action, sym, transitive, relation, as_json, limit, budget
        )
    if action == "peers":
        return _run_peers(index, sym, min_shared, as_json, limit, budget)
    if action == "throws":
        return _run_throws(
            index, sym, transitive, depth, as_json, limit, budget
        )
    return _run_relation(index, action, sym, as_json, limit, budget, sites)


def run(
    index: MapIndex,
    action: str,
    target: str,
    as_json: bool,
    limit: int,
    sites: bool = False,
    notes: bool = True,
    budget: int | None = None,
    exact: bool = False,
    transitive: bool = False,
    relation: str | None = None,
    min_shared: int = DEFAULT_MIN_SHARED,
    depth: int = DEFAULT_THROWS_DEPTH,
    env_list: bool = False,
) -> int:
    """Execute one query action against a loaded index.

    Args:
        index: Loaded map index.
        action: One of ``ACTIONS``.
        target: Symbol or file target string; for ``uses``, the base
            identifier of an external reference (``run``, ``Path``);
            for ``type``, a type/class/struct/interface name; for
            ``catches``, a raised type name (``ConfigError``,
            ``ValueError``); for ``env``, a literal env-var name
            (``DATABASE_URL``) — ignored (may be an empty string) when
            ``env_list`` is set, since ``env --list`` scans the whole
            repo rather than looking up one key; for ``cohesion``, a
            file path, matched the same way as the ``file`` action.
        as_json: Emit structured JSON instead of text.
        limit: Cap on text result rows.
        sites: For callers/callees, print one row per call site
            (``path:line`` of the call expression) instead of one per
            related definition.
        notes: Show a symbol's notes on its card (``symbol`` action).
        budget: Approximate token budget for the result rows, or
            ``None``. Lowest-relevance rows are dropped first. For
            ``callers``/``callees``/``uses``/``type``/``supertypes``/
            ``subtypes``/``throws``/``catches``/``cohesion``, ``None``
            falls back to ``DEFAULT_RELATION_BUDGET`` rather than
            going unbounded — a high-fan-in symbol's full row list is
            otherwise capped
            only by ``limit``'s row count, which can still render
            thousands of tokens (the CLI and MCP paths previously
            diverged here; both now share this one fallback).
        exact: For ``type``, match the stored type text exactly instead
            of a bare identifier token inside wrapper syntax (e.g.
            ``Optional[Config]``).
        transitive: For ``supertypes``/``subtypes``, walk the full
            ancestor/descendant DAG instead of one hop; for ``throws``,
            walk the call graph up to ``depth`` hops instead of just
            ``target``'s own body.
        relation: For ``supertypes``/``subtypes``, restrict results to
            one heritage relation (``"extends"``/``"implements"``/
            ``"impl"``/``"embeds"``) — see ``HERITAGE_RELATIONS``.
        min_shared: For ``peers``, minimum shared-callee count to
            count as a peer (default: ``DEFAULT_MIN_SHARED``).
        depth: For ``throws --transitive``, the call-graph walk's hop
            cap (default: ``DEFAULT_THROWS_DEPTH``); ignored otherwise.
        env_list: For ``env``, scan the whole repo for every distinct
            env-var key read anywhere (``env --list``) instead of
            looking up one ``target`` key. Ignored for every other
            action.

    Returns:
        Process exit code.
    """
    effective_budget = budget
    if budget is None and action in _BUDGETED_ACTIONS:
        effective_budget = DEFAULT_RELATION_BUDGET
    buf = io.StringIO()
    with redirect_stdout(buf):
        code, meter = _dispatch(
            index,
            action,
            target,
            as_json,
            limit,
            effective_budget,
            sites,
            notes,
            exact,
            transitive,
            relation,
            min_shared,
            depth,
            env_list,
        )
    text = buf.getvalue()
    sys.stdout.write(text)
    if code == EXIT_OK and not as_json and text.strip():
        print(meter.footer() if meter is not None else token_footer(text))
    return code
