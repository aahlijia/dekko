"""Query the loaded map index: callers, callees, symbols, files.

Targets use the agreed syntax: bare ``name``, ``Class.method``,
``file.py:name``, or ``file.py:Class.method``. File qualifiers match
on the full repo-relative path or any trailing path suffix.
"""

import io
import json
import sys
from contextlib import redirect_stdout

from .classify import is_test_path, relevance_key
from .mapfile import MapIndex
from .model import Symbol
from .textutil import Meter, fit_to_budget, signature, token_footer
from .resolver import MODULE_CALLER_SUFFIX

EXIT_OK = 0
EXIT_NOT_FOUND = 3
EXIT_AMBIGUOUS = 4

ACTIONS = ("callers", "callees", "symbol", "file", "uses")


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

    Args:
        index: Loaded map index.
        target: Bare name, qualname, or ``path:qualname`` form.

    Returns:
        ``(match, candidates)``: a unique match (or ``None``) plus all
        candidates considered. No candidates means not found; several
        with no match means ambiguous.
    """
    if ":" in target:
        path_part, _, qual = target.rpartition(":")
        candidates = [
            s
            for p in paths_matching(index, path_part)
            for s in index.symbols_by_path[p]
            if s.qualname == qual or s.name == qual
        ]
    else:
        candidates = list(
            index.symbols_by_qualname.get(target)
            or index.symbols_by_name.get(target)
            or []
        )
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


def report_unresolved(target: str, candidates: list[Symbol]) -> int:
    """Explain a failed resolution and return the exit code.

    Ambiguous candidates are listed production code first, test code
    last (presentation only — resolution itself is unchanged).
    """
    if not candidates:
        print(f"dekko: no symbol matches '{target}'", file=sys.stderr)
        return EXIT_NOT_FOUND
    print(f"dekko: '{target}' is ambiguous; candidates:", file=sys.stderr)
    ranked = sorted(
        candidates, key=lambda s: (is_test_path(s.path), s.path, s.qualname)
    )
    for sym in ranked:
        print(f"  {sym.path}:{sym.qualname}", file=sys.stderr)
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
    if as_json:
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
        print(json.dumps(doc, indent=2))
        return EXIT_OK, None
    lines: list[str] = []
    for s in symbols:
        lines += _site_rows(index, action, sym, s) if sites else [_sym_line(s)]
    for path in modules:
        lines += _module_rows(index, action, sym, path, sites)
    if not lines:
        print(f"(no {action} of {sym.id})")
        return EXIT_OK, None
    return EXIT_OK, _emit_lines(lines, budget, limit)


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
        print(
            f"dekko: no external reference matches '{target}'",
            file=sys.stderr,
        )
        return EXIT_NOT_FOUND, None
    exts = sorted(
        exts, key=lambda e: (is_test_path(e.caller), e.caller, e.callee)
    )
    if as_json:
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
        print(json.dumps(doc, indent=2))
        return EXIT_OK, None
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
    return EXIT_OK, _emit_lines(rows, budget, limit)


def _run_symbol(
    index: MapIndex, sym: Symbol, as_json: bool, notes: bool
) -> tuple[int, Meter | None]:
    """Execute the symbol card action."""
    fan_in = len(index.calls_in.get(sym.id, []))
    fan_out = len(index.calls_out.get(sym.id, []))
    sym_notes = index.notes.get(sym.id, []) if notes else []
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
        if notes:
            doc["notes"] = index.notes.get(sym.id, [])
        print(json.dumps(doc, indent=2))
        return EXIT_OK, None
    print(signature(sym))
    print(f"  kind: {sym.kind} ({sym.language})")
    print(f"  at: {sym.path}:{sym.start_line}-{sym.end_line}")
    print(f"  fan-in: {fan_in}, fan-out: {fan_out}")
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
        return report_unresolved(target, candidates), None
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
            ``None``. Lowest-relevance rows are dropped first.

    Returns:
        Process exit code.
    """
    buf = io.StringIO()
    with redirect_stdout(buf):
        code, meter = _dispatch(
            index, action, target, as_json, limit, budget, sites, notes
        )
    text = buf.getvalue()
    sys.stdout.write(text)
    if code == EXIT_OK and not as_json and text.strip():
        print(meter.footer() if meter is not None else token_footer(text))
    return code
