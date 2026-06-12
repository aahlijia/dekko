"""Find unused symbols in the codebase."""

import fnmatch
import json

from .mapfile import MapIndex
from .model import Symbol


def is_root(sym: Symbol, roots_globs: list[str]) -> bool:
    """Determine if a symbol is a 'root' (expected to have no callers)."""
    if sym.exported:
        return True
    if sym.decorated:
        return True
    
    # main functions
    if sym.name in ("main", "__main__"):
        return True

    # tests
    if "test" in sym.path.lower():
        return True
    if sym.name.startswith("test_") or sym.name.endswith("_test"):
        return True

    # globs
    for g in roots_globs:
        if fnmatch.fnmatch(sym.id, g) or fnmatch.fnmatch(sym.path, g) or fnmatch.fnmatch(sym.name, g):
            return True

    return False


def run(index: MapIndex, roots_globs: list[str], as_json: bool = False) -> int:
    """Execute the unused command.

    Args:
        index: The loaded map index.
        roots_globs: List of glob patterns for root paths/symbols.
        as_json: Emit structured JSON instead of text.
    """
    unused: list[Symbol] = []
    for sym in index.symbols_by_id.values():
        if len(index.calls_in.get(sym.id, [])) == 0:
            if not is_root(sym, roots_globs):
                unused.append(sym)

    # Sort deterministically
    unused.sort(key=lambda s: (s.path, s.start_line, s.name))

    if as_json:
        out = [
            {
                "id": s.id,
                "name": s.name,
                "path": s.path,
                "line": s.start_line,
                "language": s.language,
                "kind": s.kind,
            }
            for s in unused
        ]
        print(json.dumps(out, indent=2))
        return 0

    if not unused:
        print("No unused symbols found.")
        return 0

    print(f"Found {len(unused)} unused symbols:")
    for sym in unused:
        print(f"{sym.id}  ({sym.path}:{sym.start_line})")
    return 0
