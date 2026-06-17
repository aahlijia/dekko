"""Find symbols with no inbound calls that look like dead code.

A symbol is reported when nothing in the repo calls it (no resolved
callers and no module-level call sites) *and* it is not a plausible
entry point. Roots are excluded conservatively to avoid false
positives: ``main``, test files, decorated/annotated symbols, the
language's public surface (Rust ``pub``, Go capitals, Java ``public``,
JS/TS ``export``), Python dunders and ``__init__.py`` re-exports, and
any path matched by ``--roots``.

Because detection is call-graph based, a class used only via subclassing
or type annotations, or a symbol reached through dynamic dispatch, can
still surface — treat the output as a lead, not a verdict.
"""

import fnmatch
import json

from .classify import is_test_path
from .mapfile import MapIndex
from .model import Symbol
from .textutil import fit_to_budget, signature

EXIT_NONE = 0
EXIT_FOUND = 1


def _matches_globs(path: str, globs: tuple[str, ...]) -> bool:
    """Whether a path (or its basename) matches any user root glob."""
    base = path.rsplit("/", 1)[-1]
    return any(
        fnmatch.fnmatch(path, g) or fnmatch.fnmatch(base, g) for g in globs
    )


def _is_dunder(name: str) -> bool:
    """Whether a name is a Python dunder, e.g. ``__init__``."""
    return name.startswith("__") and name.endswith("__")


def reexported_names(index: MapIndex) -> set[str]:
    """Names imported into any ``__init__.py`` (package re-exports)."""
    names: set[str] = set()
    for path, imports in index.imports_by_path.items():
        if path == "__init__.py" or path.endswith("/__init__.py"):
            names.update(imp.name for imp in imports)
    return names


def _is_root(
    sym: Symbol, reexports: set[str], root_globs: tuple[str, ...]
) -> bool:
    """Whether a symbol is a plausible entry point (not dead code)."""
    if sym.name == "main":
        return True
    if sym.decorated or sym.exported:
        return True
    if _matches_globs(sym.path, root_globs):
        return True
    if is_test_path(sym.path):
        return True
    if sym.language == "go" and sym.name[:1].isupper():
        return True
    if _is_dunder(sym.name):
        return True
    return sym.name in reexports


def _used_keys(index: MapIndex) -> set[tuple[str, str]]:
    """``(path, qualname)`` keys that any inbound edge keeps alive.

    A called symbol marks itself *and* every enclosing container (so a
    class counts as used when one of its methods is called).
    """
    used: set[tuple[str, str]] = set()
    for sym_id, callers in index.calls_in.items():
        if not callers:
            continue
        sym = index.symbols_by_id.get(sym_id)
        if sym is None:
            continue
        parts = sym.qualname.split(".")
        for end in range(1, len(parts) + 1):
            used.add((sym.path, ".".join(parts[:end])))
    return used


def find_unused(index: MapIndex, root_globs: tuple[str, ...]) -> list[Symbol]:
    """Return symbols with no inbound use that are not roots.

    Args:
        index: Loaded map index.
        root_globs: Extra path globs whose symbols are always roots.

    Returns:
        Unused symbols sorted by path then line.
    """
    reexports = reexported_names(index)
    used = _used_keys(index)
    found = [
        sym
        for sym in index.symbols_by_id.values()
        if (sym.path, sym.qualname) not in used
        and not _is_root(sym, reexports, root_globs)
    ]
    return sorted(found, key=lambda s: (s.path, s.start_line))


def _sym_json(sym: Symbol) -> dict:
    """Structured rendering of one unused symbol."""
    return {
        "id": sym.id,
        "kind": sym.kind,
        "path": sym.path,
        "line": sym.start_line,
        "language": sym.language,
        "signature": signature(sym),
    }


def run(
    index: MapIndex,
    root_globs: tuple[str, ...],
    as_json: bool,
    limit: int,
    budget: int | None = None,
) -> int:
    """Report unused symbols as text or JSON.

    Args:
        index: Loaded map index.
        root_globs: Extra path globs to treat as roots.
        as_json: Emit structured JSON instead of text.
        limit: Cap on result rows.
        budget: Approximate token budget for the rows, or ``None``.

    Returns:
        ``0`` when none are found, ``1`` when some are.
    """
    found = find_unused(index, root_globs)
    if as_json:
        entries = [_sym_json(s) for s in found]
        serialized = [json.dumps(e) for e in entries]
        kept_ser, meter = fit_to_budget(serialized, budget, limit)
        doc = {"results": entries[: len(kept_ser)], "meta": meter.as_dict()}
        print(json.dumps(doc, indent=2))
        return EXIT_FOUND if found else EXIT_NONE

    if not found:
        print("dekko: no unused symbols")
        return EXIT_NONE

    header = f"dekko: {len(found)} unused symbols"
    rows = [
        f"  {s.path}:{s.start_line}  {signature(s)}  [{s.kind}]" for s in found
    ]
    kept, meter = fit_to_budget(rows, budget, limit, prefix=header)
    print(header)
    for row in kept:
        print(row)
    print(meter.footer())
    return EXIT_FOUND
