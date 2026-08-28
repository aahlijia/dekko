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

``--suspect`` (opt-in, off by default) cross-references excluded
symbols against ``dekko ambiguous``'s collision list: a symbol kept
off this report only by inbound call-graph fan-in is a suspect when
its bare name is also one `dekko ambiguous` independently proved
collision-prone (2+ repo-defined candidates, unresolved) somewhere
else in the repo — see ``find_suspects`` and round-23 design doc
``.features/plans/round23/21-unused-ambiguous-crossref.md``. This
does not catch every misattribution (a name colliding with exactly
one non-repo builtin never appears in ``ambiguous`` either), only the
subset that also collides 2+ ways somewhere else in the repo.

A mirror-image caveat is always on (no flag needed): a symbol *is*
reported unused, but its own id is one of the unresolved candidates
of some ambiguous call site elsewhere in the repo -- the shape a
`this.method()`/`self.method()` polymorphic-dispatch call through an
abstract base produces when 2+ concrete overrides exist and the
resolver can't attribute the base's call to any single one of them.
See ``find_dispatch_candidates`` and round-24 design doc
``.features/plans/round24/04-unused-dispatch-shaped-candidate-flag.md``.
``--dispatch`` (opt-in) additionally lists which flagged symbols these
are.
"""

import fnmatch
import json
import re

from dekko.analysis import ambiguous, query
from dekko.classify import is_test_path
from dekko.render.mapfile import MapIndex
from dekko.core.model import TYPE_KINDS, Symbol
from dekko.textutil import fit_to_budget, signature

EXIT_NONE = 0
EXIT_FOUND = 1
KINDS_CHOICES = ("callables", "types", "all")

# Rust std-library traits whose implementation implies implicit,
# trait-dispatched calls to a type's method -- invisible to a static
# call-expression walk (``Display::fmt`` via ``{}``/``.to_string()``,
# ``From::from`` via ``.into()``/``?``, ``Iterator::next`` via
# ``for``, operator overloads via ``+``/``==``/indexing, etc.). A
# curated allowlist, same maintenance model as
# ``resolver._RUST_STD_METHOD_NAMES`` (extended opportunistically as
# future rounds find gaps), applied here to trait names rather than
# method names. See round-23 design doc
# ``03-rust-trait-dispatch-unused-false-positive.md``.
_RUST_STD_TRAIT_NAMES = frozenset(
    {
        "Display",
        "Debug",
        "From",
        "TryFrom",
        "Into",
        "Default",
        "Clone",
        "Copy",
        "PartialEq",
        "Eq",
        "PartialOrd",
        "Ord",
        "Hash",
        "Drop",
        "Iterator",
        "IntoIterator",
        "Deref",
        "DerefMut",
        "Index",
        "IndexMut",
        "Add",
        "Sub",
        "Mul",
        "Div",
        "Neg",
        "Not",
        "AsRef",
        "AsMut",
        "Borrow",
        "ToString",
        "Send",
        "Sync",
    }
)


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


_TRAIT_PATH_SPLIT_RE = re.compile(r"::|\.|->")


def _trait_base_name(text: str) -> str:
    """Extract a heritage clause's bare trait name.

    Rust ``impl`` blocks are commonly written with a module-qualified
    trait path (``impl fmt::Display for X`` after ``use std::fmt``)
    and/or generic arguments (``impl From<String> for X``);
    ``heritage_external_out``'s ``ExternalCall.callee`` carries the
    clause exactly as written (``"fmt::Display"``, ``"From<String>"``),
    so this strips both -- mirrors ``extractor._split_callee_text``'s
    cut-at-``<``-then-split-on-path-separators shape (kept as a small
    local copy rather than importing that module-private helper across
    a package boundary).
    """
    cleaned = re.split(r"[(<]", text, maxsplit=1)[0]
    parts = [
        p.strip() for p in _TRAIT_PATH_SPLIT_RE.split(cleaned) if p.strip()
    ]
    return parts[-1] if parts else cleaned.strip()


def _container_type_index(index: MapIndex) -> dict[tuple[str, str], Symbol]:
    """``(path, qualname) -> Symbol`` for every ``TYPE_KINDS`` symbol.

    Built once per ``find_unused`` run (not per symbol) so the Rust
    trait-dispatch root check below can look up a method's enclosing
    type in O(1) rather than scanning the map per method.
    """
    return {
        (sym.path, sym.qualname): sym
        for sym in index.symbols_by_id.values()
        if sym.kind in TYPE_KINDS
    }


def _implements_std_trait(
    sym: Symbol,
    index: MapIndex,
    container_index: dict[tuple[str, str], Symbol],
) -> bool:
    """Whether a Rust method's enclosing type implements a std trait.

    Reads ``index.heritage_external_out`` (already-resolved "this type
    implements external trait T" evidence) rather than tracking which
    specific ``impl`` block a method came from -- a type-level
    approximation, exact for the common case of one relevant
    ``impl <StdTrait> for X`` block per type. See round-23 design doc
    ``03-rust-trait-dispatch-unused-false-positive.md`` for the
    known narrow false-negative this trades for (a same-named
    inherent method sitting alongside a trait impl).
    """
    parts = sym.qualname.split(".")
    if len(parts) < 2:
        return False
    container = container_index.get((sym.path, ".".join(parts[:-1])))
    if container is None:
        return False
    externals = index.heritage_external_out.get(container.id, [])
    return any(
        _trait_base_name(ext.callee) in _RUST_STD_TRAIT_NAMES
        for ext in externals
    )


def _is_root(
    sym: Symbol,
    reexports: set[str],
    root_globs: tuple[str, ...],
    index: MapIndex,
    container_index: dict[tuple[str, str], Symbol],
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
    if sym.name in reexports:
        return True
    if sym.language == "rust" and sym.kind == "method":
        return _implements_std_trait(sym, index, container_index)
    return False


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
    container_index = _container_type_index(index)
    found = [
        sym
        for sym in index.symbols_by_id.values()
        if (kinds != "types" or sym.kind in TYPE_KINDS)
        and (sym.path, sym.qualname) not in used
        and not _is_root(sym, reexports, root_globs, index, container_index)
    ]
    return sorted(found, key=lambda s: (s.path, s.start_line))


def _has_direct_fan_in(sym: Symbol, index: MapIndex) -> bool:
    """Whether ``sym``'s own id carries direct inbound call/reference evidence.

    Distinct from ``_mark_used``'s container-marking: a class excluded
    from ``find_unused`` only because one of its methods was called
    does not have direct fan-in on the class's own id, even though the
    class itself counts as "used" for ``find_unused``'s purposes.
    ``find_suspects`` cares specifically about the former case — a
    symbol whose *own* id is a resolved call/reference target, which is
    exactly the evidence a single-candidate resolver misattribution
    would fabricate.
    """
    return bool(index.calls_in.get(sym.id)) or bool(
        index.referenced_in.get(sym.id)
    )


def find_suspects(
    index: MapIndex,
    root_globs: tuple[str, ...],
    kinds: str = "callables",
) -> list[Symbol]:
    """Symbols excluded from `find_unused` whose name is a proven collider.

    A symbol is a suspect when: it would be in-scope for `find_unused`'s
    kind filter, it was NOT reported unused, it is not a root (root
    exclusion is unrelated to call-graph trust), it has at least one
    *direct* calls_in/referenced_in entry for its own id (the fan-in
    that specifically kept it off the unused list, as opposed to being
    marked used only because a child method of it was called), and its
    bare `name` is in `ambiguous.collision_names(index)`.

    Args:
        index: Loaded map index.
        root_globs: Extra path globs whose symbols are always roots —
            same set `find_unused` was called with, so root exclusion
            agrees between the two passes.
        kinds: Same `--kinds` scoping `find_unused` uses.

    Returns:
        Suspect symbols sorted by path then line.
    """
    reexports = reexported_names(index)
    used = _used_keys(index, kinds)
    container_index = _container_type_index(index)
    collision_names = ambiguous.collision_names(index)
    found = [
        sym
        for sym in index.symbols_by_id.values()
        if (kinds != "types" or sym.kind in TYPE_KINDS)
        and (sym.path, sym.qualname) in used
        and not _is_root(sym, reexports, root_globs, index, container_index)
        and sym.name in collision_names
        and _has_direct_fan_in(sym, index)
    ]
    return sorted(found, key=lambda s: (s.path, s.start_line))


def find_dispatch_candidates(
    index: MapIndex,
    root_globs: tuple[str, ...],
    kinds: str = "callables",
) -> list[Symbol]:
    """Symbols `find_unused` flagged whose own id is an unresolved
    ambiguous-call candidate elsewhere in the repo.

    A symbol is a dispatch candidate when: it would be in-scope for
    `find_unused`'s kind filter, it WAS reported unused (unlike
    `find_suspects`, which only checks excluded symbols), and its own
    id appears as a candidate in `index.ambiguous_in` -- i.e. some
    call site elsewhere in the repo named this symbol's bare name,
    matched 2+ same-named repo-defined candidates including this one,
    and could not be resolved to any single target. This is exactly
    the shape a `this.method()`/`self.method()` polymorphic-dispatch
    call through an abstract base produces when the base class itself
    never defines the method: every concrete override is a same-named
    candidate, none can be picked over the others, and the base
    class's call to it never becomes a resolved edge for any of them.

    Args:
        index: Loaded map index.
        root_globs: Same set `find_unused` was called with.
        kinds: Same `--kinds` scoping `find_unused` uses.

    Returns:
        Dispatch-candidate symbols sorted by path then line -- a
        subset of `find_unused`'s own result, never a disjoint set.
    """
    found = find_unused(index, root_globs, kinds)
    return sorted(
        (s for s in found if index.ambiguous_in.get(s.id)),
        key=lambda s: (s.path, s.start_line),
    )


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


# Independent, flat row cap for the `--suspect` section — deliberately
# not routed through the primary list's `--limit`/`--budget` so the
# suspects section never silently steals budget from the main unused
# list (round-23 design doc `21-unused-ambiguous-crossref.md`).
_SUSPECT_LIMIT = 20


def _suspect_json(sym: Symbol) -> dict:
    """Structured rendering of one suspect: unused fields + collision info."""
    doc = _sym_json(sym)
    doc["collides_with"] = sym.name
    doc["check_command"] = f"dekko ambiguous --name {sym.name}"
    return doc


def _suspect_row_text(sym: Symbol) -> str:
    """One suspect's listing row, text form."""
    return (
        f"  {sym.path}:{sym.start_line}  {signature(sym)}  [{sym.kind}]"
        f"  -- name '{sym.name}' also collides ambiguously "
        f"(dekko ambiguous --name {sym.name})"
    )


def _print_suspects_text(suspects: list[Symbol]) -> None:
    """Print the ``--suspect`` section after the main unused listing.

    A separate, independent section from the main list — printed even
    when ``find_unused`` reported nothing, since a symbol can be
    "suspiciously alive" regardless of how many other symbols are
    genuinely dead.
    """
    print()
    header = (
        f"suspects: {len(suspects)} excluded symbols share a name with "
        "1+ ambiguous call site(s) elsewhere in the repo -- their inbound "
        "fan-in may be misattributed, not genuine. Run `dekko ambiguous "
        "--name <name>` on each to check."
    )
    print(header)
    for sym in suspects[:_SUSPECT_LIMIT]:
        print(_suspect_row_text(sym))


# Independent, flat row cap for the `--dispatch` section -- same
# rationale as `_SUSPECT_LIMIT`: kept out of the primary list's
# `--limit`/`--budget` so this section never silently steals budget
# from the main unused list.
_DISPATCH_LIMIT = 20


def _dispatch_json(sym: Symbol) -> dict:
    """Structured rendering of one dispatch candidate.

    Unused fields plus the check command to run before trusting the
    "unused" verdict for this symbol.
    """
    doc = _sym_json(sym)
    doc["check_command"] = f"dekko sanity --unused {sym.qualname}"
    return doc


def _dispatch_row_text(sym: Symbol) -> str:
    """One dispatch candidate's listing row, text form."""
    return (
        f"  {sym.path}:{sym.start_line}  {signature(sym)}  [{sym.kind}]"
        f"  -- possible polymorphic-dispatch target "
        f"(dekko sanity --unused {sym.qualname})"
    )


def _print_dispatch_text(dispatch_candidates: list[Symbol]) -> None:
    """Print the ``--dispatch`` section after the main unused listing.

    A separate, independent section from the main list and from
    ``--suspect``'s own section — printed even when ``find_unused``
    reported nothing, matching ``_print_suspects_text``'s shape.
    """
    print()
    header = (
        f"dispatch candidates: {len(dispatch_candidates)} of these "
        "unused-flagged symbols are unresolved-ambiguous-call "
        "candidates elsewhere in the repo -- may be reached via "
        "this.method()/self.method() polymorphic dispatch the "
        "resolver can't attribute. Run `dekko sanity --unused <name>` "
        "on each before deleting."
    )
    print(header)
    for sym in dispatch_candidates[:_DISPATCH_LIMIT]:
        print(_dispatch_row_text(sym))


def _dispatch_caveat(dispatch_candidates: list[Symbol]) -> str | None:
    """Advisory caveat, or ``None``, gated on a nonzero dispatch count.

    Always-on (no flag needed), mirroring ``_c_abi_caveat``'s
    structure: since ``find_dispatch_candidates`` only needs one extra
    ``dict.get()`` per already-computed ``found`` row, this doesn't
    need ``--suspect``'s opt-in gating, which exists for that
    feature's costlier per-name lookup across a large collision-name
    set. See round-24 design doc
    ``.features/plans/round24/04-unused-dispatch-shaped-candidate-flag.md``.
    """
    n = len(dispatch_candidates)
    if n == 0:
        return None
    return (
        f"note: {n} of these are unresolved-ambiguous-call candidates "
        "elsewhere in the repo -- may be reached via this.method()/"
        "self.method() polymorphic dispatch the resolver can't "
        "attribute. Run `dekko sanity --unused <name>` before "
        "deleting any of them (see --dispatch for which ones)."
    )


_C_ABI_CAVEAT = (
    'note: exported/extern "C" symbols may be consumed outside this '
    "repo's call graph — treat top hits on a public C API skeptically"
)


def _c_abi_caveat(found: list[Symbol]) -> str | None:
    """Advisory caveat, or ``None``, gated on ``found`` containing C/C++.

    ``unused``'s "no inbound calls" model is a static call-graph
    analysis -- it cannot see cross-binary/ABI consumers (Go/Swift/pip
    bindings calling through a compiled ``.so``) by construction, no
    matter how good in-repo resolution gets. Gated on the *results*,
    not just "this repo contains some C/C++ files somewhere" -- a
    Python-heavy repo with one incidental ``.c`` file that produces
    zero unused hits stays silent, while a repo where C/C++ symbols
    make up the noisy tail gets the caveat exactly when it's relevant.
    Layer 1 of round-23 design doc
    ``.features/plans/round23/22-unused-extern-c-caveat.md`` -- purely
    advisory text, no change to which symbols are reported.
    """
    if any(sym.language in ("c", "cpp") for sym in found):
        return _C_ABI_CAVEAT
    return None


def _kind_totals(found: list[Symbol]) -> dict[str, int]:
    """Count ``found`` by broad category: ``types`` vs. ``callables``.

    ``callables`` here is the catch-all "everything else" bucket
    (functions, methods, and the rare unused module-level ``variable``
    symbol) — mirroring ``--kinds``' own two-way split rather than
    introducing a third category for the uncommon ``variable`` case.
    """
    types_n = sum(1 for s in found if s.kind in TYPE_KINDS)
    return {"callables": len(found) - types_n, "types": types_n}


def _build_json_doc(
    found: list[Symbol],
    suspects: list[Symbol],
    dispatch_candidates: list[Symbol],
    c_abi_caveat: str | None,
    dispatch_caveat: str | None,
    suspect: bool,
    dispatch: bool,
    budget: int | None,
    limit: int,
) -> dict:
    """Build ``run``'s ``--json`` document, factored out to keep
    ``run`` itself under the module's cyclomatic-complexity cap.
    """
    entries = [_sym_json(s) for s in found]
    serialized = [json.dumps(e) for e in entries]
    kept_ser, meter = fit_to_budget(serialized, budget, limit)
    doc = {
        "results": entries[: len(kept_ser)],
        "meta": meter.as_dict(),
        "kind_totals": _kind_totals(found),
        "caveats": [c_abi_caveat] if c_abi_caveat else [],
        "dispatch_caveat": dispatch_caveat,
    }
    if suspect:
        doc["suspects"] = [_suspect_json(s) for s in suspects[:_SUSPECT_LIMIT]]
    if dispatch:
        doc["dispatch_candidates"] = [
            _dispatch_json(s) for s in dispatch_candidates[:_DISPATCH_LIMIT]
        ]
    return doc


def _print_text(
    found: list[Symbol],
    kinds: str,
    budget: int | None,
    limit: int,
    c_abi_caveat: str | None,
    dispatch_caveat: str | None,
) -> None:
    """Print ``run``'s text-mode listing, footer, and caveats.

    Factored out of ``run`` to keep it under the module's cyclomatic-
    complexity cap; prints nothing beyond the "no unused symbols" line
    when ``found`` is empty, matching ``run``'s prior inline behavior.
    """
    if not found:
        print("dekko: no unused symbols")
        return

    if kinds == "all":
        totals = _kind_totals(found)
        header = (
            f"dekko: {len(found)} unused symbols "
            f"({totals['callables']} callables, {totals['types']} "
            "types)"
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
    if c_abi_caveat:
        print(c_abi_caveat)
    if dispatch_caveat:
        print(dispatch_caveat)


def run(
    index: MapIndex,
    root_globs: tuple[str, ...],
    as_json: bool,
    limit: int,
    budget: int | None = None,
    kinds: str = "callables",
    suspect: bool = False,
    dispatch: bool = False,
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
        suspect: When ``True``, also run ``find_suspects`` and append
            its result as a ``"suspects"`` section (text) or key
            (JSON). Off by default — costs nothing extra when unset
            and never changes the existing output shape.
        dispatch: When ``True``, also run ``find_dispatch_candidates``
            and append its result as a ``"dispatch_candidates"``
            section (text) or key (JSON). Off by default; the
            always-on caveat below is unaffected by this flag.

    Note:
        When ``found`` contains at least one C or C++ symbol, an
        advisory caveat is printed (text, after the footer line) or
        added to ``doc["caveats"]`` (JSON, ``[]`` otherwise) — a static
        call graph cannot see cross-binary/ABI consumers of exported
        C symbols. See ``_c_abi_caveat``.

        When one or more flagged symbols are dispatch candidates (see
        ``find_dispatch_candidates``), an always-on advisory caveat is
        printed (text) or added to ``doc["dispatch_caveat"]`` (JSON,
        ``None`` otherwise) regardless of ``dispatch``. See
        ``_dispatch_caveat``.

    Returns:
        ``0`` when none are found, ``1`` when some are. Reflects only
        ``find_unused``'s result — the suspects/dispatch section/key
        never changes the exit code.
    """
    found = find_unused(index, root_globs, kinds)
    suspects = find_suspects(index, root_globs, kinds) if suspect else []
    dispatch_candidates = find_dispatch_candidates(index, root_globs, kinds)
    c_abi_caveat = _c_abi_caveat(found)
    dispatch_caveat = _dispatch_caveat(dispatch_candidates)

    if as_json:
        doc = _build_json_doc(
            found,
            suspects,
            dispatch_candidates,
            c_abi_caveat,
            dispatch_caveat,
            suspect,
            dispatch,
            budget,
            limit,
        )
        print(json.dumps(doc, indent=2))
        return EXIT_FOUND if found else EXIT_NONE

    _print_text(found, kinds, budget, limit, c_abi_caveat, dispatch_caveat)

    if suspect:
        _print_suspects_text(suspects)
    if dispatch:
        _print_dispatch_text(dispatch_candidates)

    return EXIT_FOUND if found else EXIT_NONE
