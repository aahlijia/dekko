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
import sys
from contextlib import redirect_stdout

from .classify import is_test_path, relevance_key
from .mapfile import MapIndex, format_unsupported
from .model import TYPE_KINDS, ExternalCall, Symbol
from .textutil import Meter, fit_to_budget, signature, token_footer
from .resolver import MODULE_CALLER_SUFFIX

EXIT_OK = 0
EXIT_NOT_FOUND = 3
EXIT_AMBIGUOUS = 4

ACTIONS = ("callers", "callees", "symbol", "file", "uses")

# Default token cap for relation-shaped actions (callers/callees/uses)
# when the caller passes no budget. Without this, a high-fan-in
# symbol's full caller/callee list (or an external name's every
# reference) renders unbounded text capped only by --limit's row
# count — the 2026-07-31 eval measured ~3,524 tokens on a 469-caller
# symbol with no budget passed, over 4x the advertised default.
# Callers can always pass a larger budget explicitly.
DEFAULT_RELATION_BUDGET = 800

_BUDGETED_ACTIONS = ("callers", "callees", "uses")


def paths_matching(index: MapIndex, path: str) -> list[str]:
    """File paths equal to ``path`` or ending in ``/path``."""
    if path in index.symbols_by_path:
        return [path]
    suffix = "/" + path
    return sorted(p for p in index.symbols_by_path if p.endswith(suffix))


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


def _emit_lines(lines: list[str], budget: int | None, limit: int) -> Meter:
    """Print rows trimmed to the caps and return the cost meter."""
    kept, meter = fit_to_budget(lines, budget, limit)
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
        elif low in cand or cand in low:
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


def _suggest_symbols(index: MapIndex, target: str) -> list[Symbol]:
    """Symbols worth offering for a target that resolved to nothing.

    Matches the qualname part of the target (and its last segment)
    against the name index, so a wrong or stale path qualifier still
    finds the right symbol. Production code ranks before test code.
    """
    qual = target.rpartition(":")[2]
    seen: dict[str, Symbol] = {}
    for needle in dict.fromkeys((qual, qual.rpartition(".")[2])):
        for name in _close_names(needle, list(index.symbols_by_name)):
            for sym in index.symbols_by_name[name]:
                seen.setdefault(sym.id, sym)
    ranked = sorted(
        seen.values(),
        key=lambda s: (is_test_path(s.path), s.path, s.qualname),
    )
    return ranked[:_MAX_SUGGESTIONS]


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
    ranked = sorted(
        candidates, key=lambda s: (is_test_path(s.path), s.path, s.qualname)
    )
    for sym in ranked[:_MAX_AMBIGUOUS_CANDIDATES]:
        print(
            f"  {sym.path}:{sym.start_line}  {signature(sym)}", file=sys.stderr
        )
    if len(ranked) > _MAX_AMBIGUOUS_CANDIDATES:
        more = len(ranked) - _MAX_AMBIGUOUS_CANDIDATES
        sample = ranked[0]
        print(
            f"  … +{more} more (qualify with `{sample.path}:{target}` "
            "to narrow)",
            file=sys.stderr,
        )
    if len({(s.path, s.qualname) for s in candidates}) == 1:
        # Every candidate shares (path, qualname) — an overload set a
        # plain `file.py:qualname` qualifier can never narrow, since
        # that's exactly the key they collide on. The line-number
        # qualifier (round-08 §2.5) is the only escape hatch; point at
        # it directly with a real candidate's own line as an example.
        sample = ranked[0]
        print(
            "  … path+qualname alone can't disambiguate these (same "
            "file, same name) — append `:LINE` from a row above, e.g. "
            f"`{sample.path}:{sample.qualname}:{sample.start_line}`",
            file=sys.stderr,
        )
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
    """Rows for a module-level pseudo-caller."""
    if sites:
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


def _dispatch(
    index: MapIndex,
    action: str,
    target: str,
    as_json: bool,
    limit: int,
    budget: int | None,
    sites: bool,
    notes: bool,
) -> tuple[int, Meter | None]:
    """Route one query action to its executor."""
    if action == "file":
        return _run_file(index, target, as_json, limit, budget)
    if action == "uses":
        return _run_uses(index, target, as_json, limit, budget)

    sym, candidates = resolve_target(index, target)
    if sym is None:
        return report_unresolved(target, candidates, index), None
    if action == "symbol":
        return _run_symbol(index, sym, as_json, notes)
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
) -> int:
    """Execute one query action against a loaded index.

    Args:
        index: Loaded map index.
        action: One of ``ACTIONS``.
        target: Symbol or file target string; for ``uses``, the base
            identifier of an external reference (``run``, ``Path``).
        as_json: Emit structured JSON instead of text.
        limit: Cap on text result rows.
        sites: For callers/callees, print one row per call site
            (``path:line`` of the call expression) instead of one per
            related definition.
        notes: Show a symbol's notes on its card (``symbol`` action).
        budget: Approximate token budget for the result rows, or
            ``None``. Lowest-relevance rows are dropped first. For
            ``callers``/``callees``/``uses``, ``None`` falls back to
            ``DEFAULT_RELATION_BUDGET`` rather than going unbounded —
            a high-fan-in symbol's full row list is otherwise capped
            only by ``limit``'s row count, which can still render
            thousands of tokens (the CLI and MCP paths previously
            diverged here; both now share this one fallback).

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
        )
    text = buf.getvalue()
    sys.stdout.write(text)
    if code == EXIT_OK and not as_json and text.strip():
        print(meter.footer() if meter is not None else token_footer(text))
    return code
