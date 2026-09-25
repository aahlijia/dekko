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

``--kinds`` (``"callables"`` by default, same scanned population as
before) also accepts ``"types"`` (scan restricted to classes/
interfaces/enums/structs/records/traits) and ``"all"`` (every symbol
kind is scanned, same as ``"callables"``) — see ``find_unused``.
Heritage/type-usage evidence for any type-kind symbol is credited
under every ``--kinds`` value, not just ``"types"``/``"all"``; only
the *scanned population* differs by kind.

``--suspect`` (opt-in, off by default) cross-references excluded symbols
against ``dekko ambiguous``'s collision list: a symbol kept off this
report only by inbound call-graph fan-in is a suspect when its bare name
is also one `dekko ambiguous` independently proved collision-prone (2+
repo-defined candidates, unresolved) somewhere else in the repo — see
``find_suspects``. This does not catch every misattribution (a name
colliding with exactly one non-repo builtin never appears in
``ambiguous`` either), only the subset that also collides 2+ ways
somewhere else in the repo.

A mirror-image caveat is always on (no flag needed): a symbol *is*
reported unused, but some call site elsewhere in the repo may reach it
through dynamic dispatch -- a `this.method()`/`self.method()` call
through an abstract base, or `tool.prompt()` on an interface- or
trait-typed value, when 2+ same-named implementations exist and the
resolver can't attribute the call to any single one of them. See
``find_dispatch_candidates``. Every such row is marked in the main
listing, and ``--dispatch`` (opt-in) additionally lists them with the
check command to run.
"""

import fnmatch
import json
import re
from dataclasses import dataclass
from typing import NamedTuple

from dekko.analysis import ambiguous, query
from dekko.classify import is_test_path
from dekko.core.languages import SPEC_BY_NAME
from dekko.core.resolver import is_guarded_method_name
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
# gaps are found), applied here to trait names rather than method
# names.
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
    ``impl <StdTrait> for X`` block per type. It trades for a known
    narrow false-negative (a same-named inherent method sitting
    alongside a trait impl).
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
    if _consumer_is_external(sym, index):
        return True
    if _matches_globs(sym.path, root_globs):
        return True
    if sym.test or is_test_path(sym.path):
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


def _consumer_is_external(sym: Symbol, index: MapIndex) -> bool:
    """Whether ``sym`` is an object-literal member handed to external code.

    A member of a literal that is a direct argument of a call to a
    binding imported from outside the repo (``createReconciler({
    hideInstance() {} })`` with ``createReconciler`` imported from
    ``react-reconciler``) is called by that package, never by this
    repo: an entry point in the same sense ``decorated`` and
    ``exported`` are, not dead code. The test is the import, not the
    external table: a receiver call the resolver could not attribute
    (``deps.callModel({...})``, ``Promise.resolve({...})``) also lands
    in the external table, and the literal it carries is consumed by
    in-repo code the map just couldn't follow. Those members stay
    flagged, where the property-read evidence can still mark them.
    """
    consumer = sym.literal_consumer
    if not consumer:
        return False
    head = consumer.split(".", 1)[0]
    sources = {
        imp.name: imp.source for imp in index.imports_by_path.get(sym.path, ())
    }
    source = sources.get(head)
    if source is None:
        return False
    # An import's ``source`` carries the imported member too
    # (``react-reconciler/createReconciler`` for a default import,
    # ``pkg.mod.name`` in Python); the module graph records the bare
    # module it failed to place (``react-reconciler``).
    return any(
        source == module or source.startswith((module + "/", module + "."))
        for module in index.module_external.get(sym.path, ())
    )


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
    code just because nothing *calls* it directly.
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


def _used_keys(index: MapIndex) -> set[tuple[str, str]]:
    """``(path, qualname)`` keys kept alive by any evidence source.

    Both callables evidence (``calls_in``/``referenced_in``) and type
    evidence (heritage/type-usage) are always included, regardless of
    ``--kinds``. ``--kinds`` only ever narrows ``find_unused``'s
    scanned *population* (which symbol kinds are even considered); it
    never narrows which evidence counts as "used" for whatever
    population is being scanned. Previously, type evidence
    (``_used_keys_types``) was gated behind ``kinds in ("types",
    "all")``, so a type-kind symbol used only in type position (never
    called or subclassed) was false-positive-flagged as unused under
    the default ``"callables"`` kind. Since ``_used_keys_types`` only ever
    adds evidence, never removes candidates, always including it can
    only shrink ``find_unused``'s result, never grow it, for any
    ``kinds`` value.

    Args:
        index: Loaded map index.

    Returns:
        The union of every used key implied by any evidence source.
    """
    return _used_keys_callables(index) | _used_keys_types(index)


STATUS_FLAGGED = "flagged"
STATUS_USED = "used"
STATUS_ROOT = "root"
STATUS_CALL_BLIND = "call-blind-language"


class UnusedStatus(NamedTuple):
    """Whether ``dekko unused`` lists a symbol, and if not, why.

    Attributes:
        flagged: True when ``find_unused`` would return the symbol.
        reason: ``STATUS_FLAGGED``, or the first rule that spares it:
            ``STATUS_CALL_BLIND`` (its language has no extracted calls
            at all, so there is no verdict), ``STATUS_USED`` (inbound
            call, reference, heritage or type-usage evidence), or
            ``STATUS_ROOT`` (a plausible entry point).
    """

    flagged: bool
    reason: str


@dataclass(frozen=True)
class _Evidence:
    """The repo-wide lookups every per-symbol verdict reads.

    Built once per ``find_unused`` sweep (or per ``unused_status``
    call) so the verdict itself stays a cheap, pure function of one
    symbol.

    Attributes:
        reexports: Names re-exported from a package entry point.
        used: ``(path, qualname)`` keys kept alive by any evidence.
        container_index: ``(path, qualname)`` to container type symbol.
        blind: Languages with symbols but not one extracted call.
    """

    reexports: set[str]
    used: set[tuple[str, str]]
    container_index: dict[tuple[str, str], Symbol]
    blind: set[str]


def _gather_evidence(index: MapIndex) -> _Evidence:
    """Build the lookups ``_status_of`` needs, once."""
    return _Evidence(
        reexports=reexported_names(index),
        used=_used_keys(index),
        container_index=_container_type_index(index),
        blind=languages_without_calls(index),
    )


def _status_of(
    sym: Symbol,
    evidence: _Evidence,
    root_globs: tuple[str, ...],
    index: MapIndex,
) -> str:
    """The one place that decides whether a symbol is unused.

    ``find_unused`` and ``unused_status`` both answer from here. Round
    32: ``sanity --unused`` kept its own private notion of "unused"
    (no ``calls_in``/``referenced_in``), which is not this question,
    and told agents that ``SpringApplication.run`` had been flagged
    dead. Sharing the function is the fix; a second copy of these
    rules is how the two drift apart again.

    The three sparing rules are independent, so their order changes
    no verdict, only which reason is reported when several hold.
    ``used`` outranks ``root`` because most symbols are exported, and
    "it has 157 callers" is the more useful thing to say.
    """
    if sym.language in evidence.blind:
        return STATUS_CALL_BLIND
    if (sym.path, sym.qualname) in evidence.used:
        return STATUS_USED
    if _is_root(
        sym, evidence.reexports, root_globs, index, evidence.container_index
    ):
        return STATUS_ROOT

    return STATUS_FLAGGED


def unused_status(
    index: MapIndex,
    sym: Symbol,
    root_globs: tuple[str, ...] = (),
) -> UnusedStatus:
    """Whether ``dekko unused`` would list ``sym``, by the same rules.

    Args:
        index: Loaded map index.
        sym: The symbol to check.
        root_globs: Extra path globs whose symbols are always roots
            (``dekko unused --roots``).

    Returns:
        The verdict and the reason behind it.
    """
    reason = _status_of(sym, _gather_evidence(index), root_globs, index)
    return UnusedStatus(flagged=reason == STATUS_FLAGGED, reason=reason)


def find_unused(
    index: MapIndex,
    root_globs: tuple[str, ...],
    kinds: str = "callables",
) -> list[Symbol]:
    """Return symbols with no inbound use that are not roots.

    Args:
        index: Loaded map index.
        root_globs: Extra path globs whose symbols are always roots.
        kinds: ``"callables"`` (default — every symbol kind is
            scanned; call/reference evidence always applies, and any
            type-kind symbol encountered is also credited with
            heritage/type-usage evidence, same as under
            ``"types"``/``"all"``), ``"types"`` (scan is restricted to
            ``TYPE_KINDS`` symbols), or ``"all"`` (every symbol kind is
            scanned, same population as ``"callables"``). Evidence
            sources no longer vary by ``kinds`` — only the scanned
            population does.

    Returns:
        Unused symbols sorted by path then line.
    """
    evidence = _gather_evidence(index)
    found = [
        sym
        for sym in index.symbols_by_id.values()
        if (kinds != "types" or sym.kind in TYPE_KINDS)
        and _status_of(sym, evidence, root_globs, index) == STATUS_FLAGGED
    ]
    return sorted(found, key=lambda s: (s.path, s.start_line))


def languages_without_calls(index: MapIndex) -> set[str]:
    """Languages with symbols in the map but not one extracted call.

    "No inbound calls" is only evidence of dead code in a language
    whose calls dekko can see. A Tier-2 (generic-grammar) language is
    parsed by node-type heuristics, and when its call node doesn't
    match them, every function in it has fan-in 0 by construction.
    Tensorflow hit exactly that: bash calls
    are ``command`` nodes, none were collected, and ``unused`` listed
    ``tfrun()`` -- 27 real call sites -- with no caveat. Bash itself
    is fixed at the extractor, but ~55 other generic grammars sit
    behind the same heuristic, so ``unused`` refuses to judge a
    language it has zero call evidence for, and says so
    (``_blind_language_caveat``).

    A caller counts whatever became of its call (resolved, ambiguous,
    or external): any of the three proves extraction saw calls there.

    Tier-1 languages are never reported: each has a dedicated,
    tested call query, so a Tier-1 file with no calls really has none,
    and its fan-in-0 symbols are exactly what ``unused`` is for. The
    blind spot is a property of the heuristic extractor alone.
    """
    lang_of_path = {s.path: s.language for s in index.symbols_by_id.values()}
    generic = set(lang_of_path.values()) - set(SPEC_BY_NAME)
    if not generic:
        return set()
    callers = set(index.calls_out) | set(index.ambiguous_out)
    for calls in index.externals_by_name.values():
        callers.update(call.caller for call in calls)
    seeing = {lang_of_path.get(c.split("::", 1)[0]) for c in callers}
    return generic - seeing


def _blind_language_caveat(index: MapIndex, kinds: str) -> str | None:
    """Disclose the symbols ``find_unused`` declined to judge, or ``None``."""
    blind = languages_without_calls(index)
    if not blind:
        return None
    counts: dict[str, int] = {}
    for sym in index.symbols_by_id.values():
        if sym.language in blind and (
            kinds != "types" or sym.kind in TYPE_KINDS
        ):
            counts[sym.language] = counts.get(sym.language, 0) + 1
    if not counts:
        return None
    mix = ", ".join(f"{lang} {n}" for lang, n in sorted(counts.items()))
    return (
        f"note: {sum(counts.values())} symbol(s) not evaluated ({mix}) -- "
        "dekko extracted no calls from any file in that language here, "
        'so "no callers" would not be evidence of dead code. Check '
        "those with grep."
    )


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
    used = _used_keys(index)
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


EVIDENCE_AMBIGUOUS = "ambiguous"
EVIDENCE_GUARDED_NAME = "guarded-name"
EVIDENCE_PROPERTY_READ = "property-read"


def _has_property_read(sym: Symbol, index: MapIndex) -> bool:
    """Whether a property read somewhere in the repo might reach ``sym``.

    A getter or an object-literal handler is used by being *read*
    (``cmd.isHidden``, ``matchingCommand?.immediate``), a shape no
    call edge covers and one the map records as a read site, never an
    edge (see ``model.RawRead``). The read names a property, not a
    definition, so it counts when the name is genuinely shared (2+
    repo callables or variables define it, the same bar the
    guarded-name rule sets) or when ``sym`` itself is an object-literal
    member, the shape reads reach. A lone free function whose name
    happens to be read off some unrelated object is ordinary unused
    code.
    """
    if not index.reads_by_name.get(sym.name):
        return False
    if sym.in_literal:
        return True
    definitions = [
        s
        for s in index.symbols_by_name.get(sym.name, ())
        if s.kind not in TYPE_KINDS
    ]

    return len(definitions) >= 2


def _has_guarded_receiver_call(sym: Symbol, index: MapIndex) -> bool:
    """Whether a receiver call the resolver sent external might reach ``sym``.

    The resolver's noise guard sends every receiver call to a guarded
    method name (``tool.description()``, ``node.parse()``) external,
    however many repo symbols define it, so those calls never reach
    ``ambiguous_in``. They are still the same evidence: when 2+ repo
    symbols share the name and a receiver call uses it, any of them
    might be the target. A lone definition is ordinary unused code.
    """
    if not is_guarded_method_name(sym.name):
        return False
    if len(index.symbols_by_name.get(sym.name, ())) < 2:
        return False
    return any(
        ext.callee.rsplit(".", 1)[-1] == sym.name and "." in ext.callee
        for ext in index.externals_by_name.get(sym.name, ())
    )


def _dispatch_evidence(sym: Symbol, index: MapIndex) -> str | None:
    """Why a flagged symbol might be a dispatch target, or ``None``.

    ``EVIDENCE_AMBIGUOUS`` when its own id is a candidate of an
    unresolved call, the stronger signal; ``EVIDENCE_GUARDED_NAME``
    when only a receiver call the noise guard sent external uses its
    name (see ``_has_guarded_receiver_call``); ``EVIDENCE_PROPERTY_READ``
    when only a property read uses it (see ``_has_property_read``).
    """
    if index.ambiguous_in.get(sym.id):
        return EVIDENCE_AMBIGUOUS
    if _has_guarded_receiver_call(sym, index):
        return EVIDENCE_GUARDED_NAME
    if _has_property_read(sym, index):
        return EVIDENCE_PROPERTY_READ
    return None


def _dispatch_evidence_by_id(
    found: list[Symbol], index: MapIndex
) -> dict[str, str]:
    """Candidate id to its evidence, for the candidates among ``found``."""
    out: dict[str, str] = {}
    for sym in found:
        evidence = _dispatch_evidence(sym, index)
        if evidence is not None:
            out[sym.id] = evidence
    return out


def find_dispatch_candidates(
    index: MapIndex,
    root_globs: tuple[str, ...],
    kinds: str = "callables",
) -> list[Symbol]:
    """Symbols `find_unused` flagged that dynamic dispatch might reach.

    A symbol is a dispatch candidate when it would be in-scope for
    `find_unused`'s kind filter, it WAS reported unused (unlike
    `find_suspects`, which only checks excluded symbols), and either:

    - its own id appears as a candidate in `index.ambiguous_in`: some
      call site named this symbol's bare name, matched 2+ same-named
      repo-defined candidates including this one, and could not be
      resolved to any single target. That is the shape of a
      `this.method()`/`self.method()` call through an abstract base
      that never defines the method, and of a receiver call through an
      interface- or trait-typed value (`tool.prompt()` where every tool
      object defines `prompt`); or
    - its name is one the resolver's noise guard never resolves on a
      receiver call, 2+ repo symbols share it, and some receiver call
      uses it (see `_dispatch_evidence`); or
    - its name is read as a property somewhere (`cmd.isHidden`, a
      getter or object-literal handler used without a call) and
      either 2+ repo symbols share it or it is itself an
      object-literal member (see `_has_property_read`).

    Args:
        index: Loaded map index.
        root_globs: Same set `find_unused` was called with.
        kinds: Same `--kinds` scoping `find_unused` uses.

    Returns:
        Dispatch-candidate symbols sorted by path then line -- a
        subset of `find_unused`'s own result, never a disjoint set.
    """
    found = find_unused(index, root_globs, kinds)
    evidence = _dispatch_evidence_by_id(found, index)
    return [s for s in found if s.id in evidence]


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
# list.
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


def _section_caps(
    section_cap: int, limit: int | None, budget: int | None
) -> tuple[int, int | None]:
    """``(limit, budget)`` for a supplemental section.

    Each section has its own flat default cap so it can't steal budget
    from the main list. An explicit ``--limit`` (``limit`` is ``None``
    when none was given) replaces that cap in either direction, so the
    footer's "raise --limit" advice is true: a 733-row section capped
    at 20 that ``--limit 1400`` couldn't lift read as complete.
    ``--budget`` applies to the section on its own (the same number,
    independently, the way ``sanity`` applies it per bucket), and
    ``fit_to_budget``'s footer says what was dropped.
    """
    return (section_cap if limit is None else limit, budget)


def _print_section(
    header: str,
    rows: list[str],
    section_cap: int,
    limit: int | None,
    budget: int | None,
) -> None:
    """Print a supplemental section: header, capped rows, footer."""
    print()
    print(header)
    cap, section_budget = _section_caps(section_cap, limit, budget)
    kept, meter = fit_to_budget(rows, section_budget, cap)
    for row in kept:
        print(row)
    if meter.omitted:
        print(f"  {meter.footer()}")


def _print_suspects_text(
    suspects: list[Symbol], limit: int | None = None, budget: int | None = None
) -> None:
    """Print the ``--suspect`` section after the main unused listing.

    A separate, independent section from the main list — printed even
    when ``find_unused`` reported nothing, since a symbol can be
    "suspiciously alive" regardless of how many other symbols are
    genuinely dead.
    """
    header = (
        f"suspects: {len(suspects)} excluded symbols share a name with "
        "1+ ambiguous call site(s) elsewhere in the repo -- their inbound "
        "fan-in may be misattributed, not genuine. Run `dekko ambiguous "
        "--name <name>` on each to check."
    )
    rows = [_suspect_row_text(s) for s in suspects]
    _print_section(header, rows, _SUSPECT_LIMIT, limit, budget)


# Independent, flat row cap for the `--dispatch` section -- same
# rationale as `_SUSPECT_LIMIT`: kept out of the primary list's
# `--limit`/`--budget` so this section never silently steals budget
# from the main unused list.
_DISPATCH_LIMIT = 20


def _dispatch_check_command(sym: Symbol) -> str:
    """The ``dekko sanity --unused`` hint for one dispatch candidate.

    Uses the full ``path:qualname:line`` target form, not the bare
    ``qualname`` — an overloaded target (2+ symbols sharing the same
    ``(path, qualname)``) needs the trailing ``:line`` to disambiguate,
    matching the exact hint ``resolve_target``'s own ambiguous-target
    error message already tells the user to append (the row already
    carries ``sym.start_line``, so there's no reason to make the
    copy-pasted command hit that error at all).
    """
    return f"dekko sanity --unused {sym.path}:{sym.qualname}:{sym.start_line}"


def _dispatch_json(sym: Symbol, evidence: str | None) -> dict:
    """Structured rendering of one dispatch candidate.

    Unused fields, which evidence made it a candidate, and the check
    command to run before trusting the "unused" verdict for this
    symbol.
    """
    doc = _sym_json(sym)
    doc["evidence"] = evidence
    doc["check_command"] = _dispatch_check_command(sym)
    return doc


def _dispatch_row_text(sym: Symbol, evidence: str | None = None) -> str:
    """One dispatch candidate's listing row, text form."""
    reason = (
        "possible property-read target (getter/handler read, not called)"
        if evidence == EVIDENCE_PROPERTY_READ
        else "possible polymorphic-dispatch target"
    )
    return (
        f"  {sym.path}:{sym.start_line}  {signature(sym)}  [{sym.kind}]"
        f"  -- {reason} ({_dispatch_check_command(sym)})"
    )


# The per-row marker on a dispatch candidate in the main listing.
_DISPATCH_MARKER = "  [dispatch?]"


def _print_dispatch_text(
    dispatch_candidates: list[Symbol],
    limit: int | None = None,
    budget: int | None = None,
    evidence_by_id: dict[str, str] | None = None,
) -> None:
    """Print the ``--dispatch`` section after the main unused listing.

    A separate, independent section from the main list and from
    ``--suspect``'s own section — printed even when ``find_unused``
    reported nothing, matching ``_print_suspects_text``'s shape.
    """
    header = (
        f"dispatch candidates: {len(dispatch_candidates)} of these "
        "unused-flagged symbols share a name with an unresolved call "
        "or a property read elsewhere in the repo -- may be reached "
        "via polymorphic dispatch the resolver can't attribute "
        "(this.method()/self.method(), or a receiver call through an "
        "interface/trait-typed value like tool.prompt()) or via a "
        "getter/handler read as a property (cmd.isHidden). Run `dekko "
        "sanity --unused <name>` on each before deleting."
    )
    evidence = evidence_by_id or {}
    rows = [
        _dispatch_row_text(s, evidence.get(s.id)) for s in dispatch_candidates
    ]
    _print_section(header, rows, _DISPATCH_LIMIT, limit, budget)


def _dispatch_caveat(dispatch_candidates: list[Symbol]) -> str | None:
    """Advisory caveat, or ``None``, gated on a nonzero dispatch count.

    Always-on (no flag needed), mirroring ``_c_abi_caveat``'s
    structure: since ``find_dispatch_candidates`` only needs one extra
    ``dict.get()`` per already-computed ``found`` row, this doesn't
    need ``--suspect``'s opt-in gating, which exists for that
    feature's costlier per-name lookup across a large collision-name
    set.
    """
    n = len(dispatch_candidates)
    if n == 0:
        return None
    return (
        f"note: {n} of these are unresolved-ambiguous-call or "
        "property-read candidates elsewhere in the repo -- may be "
        "reached via polymorphic dispatch the resolver can't attribute "
        "(this.method()/self.method(), or a receiver call through an "
        "interface/trait-typed value like tool.prompt()) or via a "
        "getter/handler read as a property (cmd.isHidden). They are "
        "marked [dispatch?]; run `dekko sanity --unused <name>` before "
        "deleting any of them (--dispatch lists them with the command)."
    )


# Above this share of dispatch candidates, "run unused, trust the list"
# is wrong more often than right, and the trailing caveat is too easy
# to miss under a long listing. A small floor keeps a 3-row listing
# with 2 candidates from shouting.
_DISPATCH_MAJORITY_RATIO = 0.5
_DISPATCH_MAJORITY_MIN = 20


def _dispatch_majority_warning(n_dispatch: int, n_found: int) -> str | None:
    """Leading warning when most flagged symbols are dispatch candidates.

    On spring-boot, 2,323 of 3,281 flagged symbols (70.8%)
    were also polymorphic-dispatch candidates the resolver can't
    attribute through interface-typed call sites. ``_dispatch_caveat``
    fired correctly, but as the *last* line under thousands of rows,
    on exactly the repo shape (interface-heavy Java/Spring) where the
    list is mostly not dead code. Printed above the listing instead,
    so it is read before the rows are.
    """
    if n_dispatch < _DISPATCH_MAJORITY_MIN or n_found == 0:
        return None
    ratio = n_dispatch / n_found
    if ratio < _DISPATCH_MAJORITY_RATIO:
        return None
    return (
        f"warning: {n_dispatch} of {n_found} ({ratio:.0%}) flagged "
        "symbols are polymorphic-dispatch candidates -- on this repo "
        "most of this list is likely NOT dead code (interface/"
        "trait-typed call sites the resolver can't attribute). Treat "
        "it as leads, not a delete list; verify with `dekko sanity "
        "--unused <name>`."
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
    Purely advisory text, no change to which symbols are reported.
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
    limit: int | None,
    evidence_by_id: dict[str, str] | None = None,
) -> dict:
    """Build ``run``'s ``--json`` document, factored out to keep
    ``run`` itself under the module's cyclomatic-complexity cap.

    ``limit`` is ``None`` when the caller gave none: the main list
    then takes its default and each section its own (see
    ``_section_caps``). A result row that is a dispatch candidate
    carries ``"dispatch_candidate": true``; the key is absent on every
    other row, so thousands of rows don't each grow by a false.
    ``evidence_by_id`` (see ``_dispatch_evidence``) labels each
    ``--dispatch`` row.
    """
    evidence_by_id = evidence_by_id or {}
    candidate_ids = {s.id for s in dispatch_candidates}
    entries = [_sym_json(s) for s in found]
    for entry in entries:
        if entry["id"] in candidate_ids:
            entry["dispatch_candidate"] = True
    serialized = [json.dumps(e) for e in entries]
    main_limit = query.DEFAULT_LIMIT if limit is None else limit
    kept_ser, meter = fit_to_budget(serialized, budget, main_limit)
    doc = {
        "results": entries[: len(kept_ser)],
        "meta": meter.as_dict(),
        "kind_totals": _kind_totals(found),
        "caveats": [c_abi_caveat] if c_abi_caveat else [],
        "dispatch_caveat": dispatch_caveat,
        "dispatch_majority_warning": _dispatch_majority_warning(
            len(dispatch_candidates), len(found)
        ),
    }
    if suspect:
        cap, _ = _section_caps(_SUSPECT_LIMIT, limit, budget)
        doc["suspects"] = [_suspect_json(s) for s in suspects[:cap]]
        doc["suspects_meta"] = {
            "returned": len(doc["suspects"]),
            "total": len(suspects),
        }
    if dispatch:
        cap, _ = _section_caps(_DISPATCH_LIMIT, limit, budget)
        doc["dispatch_candidates"] = [
            _dispatch_json(s, evidence_by_id.get(s.id))
            for s in dispatch_candidates[:cap]
        ]
        doc["dispatch_meta"] = {
            "returned": len(doc["dispatch_candidates"]),
            "total": len(dispatch_candidates),
        }
    return doc


def _print_text(
    found: list[Symbol],
    kinds: str,
    budget: int | None,
    limit: int,
    c_abi_caveat: str | None,
    dispatch_caveat: str | None,
    majority_warning: str | None = None,
    dispatch_ids: frozenset[str] = frozenset(),
) -> None:
    """Print ``run``'s text-mode listing, footer, and caveats.

    ``majority_warning`` (see ``_dispatch_majority_warning``) is the
    one caveat printed *above* the rows rather than below them. A row
    whose id is in ``dispatch_ids`` ends with ``[dispatch?]``.

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
        f"  {s.path}:{s.start_line}  {signature(s)}  [{s.kind}]"
        + (_DISPATCH_MARKER if s.id in dispatch_ids else "")
        for s in found
    ]
    kept, meter = fit_to_budget(rows, budget, limit, prefix=header)
    print(header)
    if majority_warning:
        print(majority_warning)
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
    limit: int | None = None,
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
        limit: Cap on result rows, or ``None`` when the caller gave
            none: the main list then shows ``query.DEFAULT_LIMIT`` rows
            and each supplemental section its own default. An explicit
            value caps the main list and every section alike.
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
        ``None`` otherwise) regardless of ``dispatch``, and each such
        row is marked. See ``_dispatch_caveat``.

    Returns:
        ``0`` when none are found, ``1`` when some are. Reflects only
        ``find_unused``'s result — the suspects/dispatch section/key
        never changes the exit code.
    """
    found = find_unused(index, root_globs, kinds)
    suspects = find_suspects(index, root_globs, kinds) if suspect else []
    evidence_by_id = _dispatch_evidence_by_id(found, index)
    dispatch_candidates = [s for s in found if s.id in evidence_by_id]
    c_abi_caveat = _c_abi_caveat(found)
    dispatch_caveat = _dispatch_caveat(dispatch_candidates)
    blind_caveat = _blind_language_caveat(index, kinds)

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
            evidence_by_id,
        )
        if blind_caveat:
            doc["caveats"].append(blind_caveat)
        print(json.dumps(doc, indent=2))
        return EXIT_FOUND if found else EXIT_NONE

    _print_text(
        found,
        kinds,
        budget,
        query.DEFAULT_LIMIT if limit is None else limit,
        c_abi_caveat,
        dispatch_caveat,
        _dispatch_majority_warning(len(dispatch_candidates), len(found)),
        frozenset(evidence_by_id),
    )
    # Printed here, not inside _print_text: it matters most on the
    # "no unused symbols" early-return path, where a clean-looking
    # result would otherwise hide that a whole language went unjudged.
    if blind_caveat:
        print(blind_caveat)

    if suspect:
        _print_suspects_text(suspects, limit, budget)
    if dispatch:
        _print_dispatch_text(
            dispatch_candidates, limit, budget, evidence_by_id
        )

    return EXIT_FOUND if found else EXIT_NONE
