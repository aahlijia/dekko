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

``--kinds`` (``"callables"`` by default, matching the above unchanged)
also accepts ``"types"`` (scan restricted to classes/interfaces/enums/
structs/records/traits, additionally weighing heritage and type-usage
evidence) and ``"all"`` (both, unioned) — see ``find_unused``.
"""

import fnmatch
import json

from dekko.analysis import query
from dekko.classify import is_test_path
from dekko.render.mapfile import MapIndex
from dekko.core.model import TYPE_KINDS, Symbol
from dekko.textutil import fit_to_budget, signature

EXIT_NONE = 0
EXIT_FOUND = 1
KINDS_CHOICES = ("callables", "types", "all")


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


def _mark_used(used: set[tuple[str, str]], sym: Symbol) -> None:
    """Mark ``sym`` and every enclosing container as used.

    A symbol keyed one level deep (``Config.load``) also marks its
    container (``Config``) — a class counts as used when one of its
    methods is called/referenced/type-used, not just when the class
    itself is.
    """
    parts = sym.qualname.split(".")
    for end in range(1, len(parts) + 1):
        used.add((sym.path, ".".join(parts[:end])))


def _used_keys_callables(index: MapIndex) -> set[tuple[str, str]]:
    """``(path, qualname)`` keys that any inbound call/reference keeps alive.

    Unchanged from before ``--kinds`` existed: a called symbol marks
    itself *and* every enclosing container (so a class counts as used
    when one of its methods is called). A symbol referenced as a
    value — a callback wired up by name and never itself called (see
    ``model.RawRef``) — counts as used the same way: it is not dead
    code just because nothing *calls* it directly (bug #2b /
    Performance #3's false positives).
    """
    used: set[tuple[str, str]] = set()
    for table in (index.calls_in, index.referenced_in):
        for sym_id, callers in table.items():
            if not callers:
                continue
            sym = index.symbols_by_id.get(sym_id)
            if sym is None:
                continue
            _mark_used(used, sym)
    return used


def _used_keys_types(index: MapIndex) -> set[tuple[str, str]]:
    """``(path, qualname)`` keys ``TYPE_KINDS`` symbols keep alive.

    Heritage (implemented/extended, ``heritage_in``) and type-usage
    (used as a parameter/return type, ``query.type_usage_name_index``)
    evidence — the two signals a type-definition can be "used" by that
    a called function's ``calls_in``/``referenced_in`` entry can't
    capture, since types usually aren't called or referenced as a bare
    value (you construct instances, extend them, or annotate a
    parameter/return with them).
    """
    used: set[tuple[str, str]] = set()
    usage_names = query.type_usage_name_index(index)
    for sym in index.symbols_by_id.values():
        if sym.kind not in TYPE_KINDS:
            continue
        has_subtypes = bool(index.heritage_in.get(sym.id))
        has_usage = sym.name in usage_names
        if has_subtypes or has_usage:
            _mark_used(used, sym)
    return used


def _used_keys(index: MapIndex, kinds: str) -> set[tuple[str, str]]:
    """``(path, qualname)`` keys kept alive, scoped by ``--kinds``.

    Callables evidence (``calls_in``/``referenced_in``) is always
    included, regardless of ``kinds`` — a deliberate divergence from
    the design doc's original sketch (which gated it behind
    ``kinds in ("callables", "all")``, i.e. excluded it entirely for
    ``kinds="types"``). Excluding it turned out to false-positive on
    the common case of a type that's only ever *constructed*
    (``Config()``) and never subclassed or type-annotated elsewhere:
    construction already resolves a call edge (usually to ``__init__``
    or the bare type symbol) that ``_used_keys_callables``'s
    container-marking already treats as "used" today, so dropping that
    evidence for ``--kinds types`` would flag plainly-alive types as
    dead. Always including it costs nothing extra for ``kinds="all"``
    (identical either way) and only strictly reduces false positives
    for ``kinds="types"``.

    Args:
        index: Loaded map index.
        kinds: One of ``KINDS_CHOICES`` — which additional (beyond
            callables, always included) evidence to consult.

    Returns:
        The union of used keys implied by ``kinds``.
    """
    used = _used_keys_callables(index)
    if kinds in ("types", "all"):
        used |= _used_keys_types(index)
    return used


def find_unused(
    index: MapIndex,
    root_globs: tuple[str, ...],
    kinds: str = "callables",
) -> list[Symbol]:
    """Return symbols with no inbound use that are not roots.

    Args:
        index: Loaded map index.
        root_globs: Extra path globs whose symbols are always roots.
        kinds: ``"callables"`` (default, today's unchanged behavior —
            every symbol kind is scanned, using only
            calls_in/referenced_in evidence), ``"types"`` (scan is
            restricted to ``TYPE_KINDS`` symbols, using heritage +
            type-usage evidence in addition to calls_in/referenced_in),
            or ``"all"`` (every symbol kind is scanned, using every
            evidence source).

    Returns:
        Unused symbols sorted by path then line.
    """
    reexports = reexported_names(index)
    used = _used_keys(index, kinds)
    found = [
        sym
        for sym in index.symbols_by_id.values()
        if (kinds != "types" or sym.kind in TYPE_KINDS)
        and (sym.path, sym.qualname) not in used
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


def _kind_totals(found: list[Symbol]) -> dict[str, int]:
    """Count ``found`` by broad category: ``types`` vs. ``callables``.

    ``callables`` here is the catch-all "everything else" bucket
    (functions, methods, and the rare unused module-level ``variable``
    symbol) — mirroring ``--kinds``' own two-way split rather than
    introducing a third category for the uncommon ``variable`` case.
    """
    types_n = sum(1 for s in found if s.kind in TYPE_KINDS)
    return {"callables": len(found) - types_n, "types": types_n}


def run(
    index: MapIndex,
    root_globs: tuple[str, ...],
    as_json: bool,
    limit: int,
    budget: int | None = None,
    kinds: str = "callables",
) -> int:
    """Report unused symbols as text or JSON.

    Args:
        index: Loaded map index.
        root_globs: Extra path globs to treat as roots.
        as_json: Emit structured JSON instead of text.
        limit: Cap on result rows.
        budget: Approximate token budget for the rows, or ``None``.
        kinds: ``"callables"`` (default), ``"types"``, or ``"all"`` —
            see ``find_unused``.

    Returns:
        ``0`` when none are found, ``1`` when some are.
    """
    found = find_unused(index, root_globs, kinds)
    if as_json:
        entries = [_sym_json(s) for s in found]
        serialized = [json.dumps(e) for e in entries]
        kept_ser, meter = fit_to_budget(serialized, budget, limit)
        doc = {
            "results": entries[: len(kept_ser)],
            "meta": meter.as_dict(),
            "kind_totals": _kind_totals(found),
        }
        print(json.dumps(doc, indent=2))
        return EXIT_FOUND if found else EXIT_NONE

    if not found:
        print("dekko: no unused symbols")
        return EXIT_NONE

    if kinds == "all":
        totals = _kind_totals(found)
        header = (
            f"dekko: {len(found)} unused symbols "
            f"({totals['callables']} callables, {totals['types']} types)"
        )
    else:
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
