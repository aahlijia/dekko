"""Query the loaded map index: callers, callees, symbols, files.

Targets use the agreed syntax: bare ``name``, ``Class.method``,
``file.py:name``, or ``file.py:Class.method``. File qualifiers match
on the full repo-relative path or any trailing path suffix.
"""

import json
import sys

from .mapfile import MapIndex
from .model import Symbol
from .render_md import signature
from .resolver import MODULE_CALLER_SUFFIX

EXIT_OK = 0
EXIT_NOT_FOUND = 3
EXIT_AMBIGUOUS = 4

ACTIONS = ("callers", "callees", "symbol", "file")


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


def _print_capped(lines: list[str], limit: int) -> None:
    """Print lines up to a cap, noting how many were omitted."""
    for line in lines[:limit]:
        print(line)
    if len(lines) > limit:
        print(f"... and {len(lines) - limit} more (raise --limit)")


def report_unresolved(target: str, candidates: list[Symbol]) -> int:
    """Explain a failed resolution and return the exit code."""
    if not candidates:
        print(f"dekko: no symbol matches '{target}'", file=sys.stderr)
        return EXIT_NOT_FOUND
    print(f"dekko: '{target}' is ambiguous; candidates:", file=sys.stderr)
    for sym in candidates:
        print(f"  {sym.path}:{sym.qualname}", file=sys.stderr)
    return EXIT_AMBIGUOUS


def _run_relation(
    index: MapIndex, action: str, sym: Symbol, as_json: bool, limit: int
) -> int:
    """Execute callers/callees for a resolved symbol."""
    symbols, modules = _related(index, sym, action)
    if as_json:
        doc = {
            "action": action,
            "target": sym.id,
            "results": [_sym_json(index, s) for s in symbols],
            "module_level": modules,
        }
        print(json.dumps(doc, indent=2))
        return EXIT_OK
    lines = [_sym_line(s) for s in symbols]
    lines += [f"{path}  (module level)" for path in modules]
    if not lines:
        print(f"(no {action} of {sym.id})")
        return EXIT_OK
    _print_capped(lines, limit)
    return EXIT_OK


def _run_symbol(index: MapIndex, sym: Symbol, as_json: bool) -> int:
    """Execute the symbol card action."""
    fan_in = len(index.calls_in.get(sym.id, []))
    fan_out = len(index.calls_out.get(sym.id, []))
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
        print(json.dumps(doc, indent=2))
        return EXIT_OK
    print(signature(sym))
    print(f"  kind: {sym.kind} ({sym.language})")
    print(f"  at: {sym.path}:{sym.start_line}-{sym.end_line}")
    print(f"  fan-in: {fan_in}, fan-out: {fan_out}")
    return EXIT_OK


def _run_file(index: MapIndex, target: str, as_json: bool, limit: int) -> int:
    """Execute the file action: list a file's symbols."""
    matches = paths_matching(index, target)
    if not matches:
        print(f"dekko: no mapped file matches '{target}'", file=sys.stderr)
        return EXIT_NOT_FOUND
    if len(matches) > 1:
        print(
            f"dekko: '{target}' is ambiguous; candidates:",
            file=sys.stderr,
        )
        for p in matches:
            print(f"  {p}", file=sys.stderr)
        return EXIT_AMBIGUOUS

    path = matches[0]
    symbols = index.symbols_by_path[path]
    if as_json:
        doc = {
            "path": path,
            "language": index.languages_by_path.get(path, ""),
            "symbols": [_sym_json(index, s) for s in symbols],
        }
        print(json.dumps(doc, indent=2))
        return EXIT_OK
    _print_capped([_sym_line(s) for s in symbols], limit)
    return EXIT_OK


def run(
    index: MapIndex, action: str, target: str, as_json: bool, limit: int
) -> int:
    """Execute one query action against a loaded index.

    Args:
        index: Loaded map index.
        action: One of ``ACTIONS``.
        target: Symbol or file target string.
        as_json: Emit structured JSON instead of text.
        limit: Cap on text result lines.

    Returns:
        Process exit code.
    """
    if action == "file":
        return _run_file(index, target, as_json, limit)

    sym, candidates = resolve_target(index, target)
    if sym is None:
        return report_unresolved(target, candidates)
    if action == "symbol":
        return _run_symbol(index, sym, as_json)
    return _run_relation(index, action, sym, as_json, limit)
