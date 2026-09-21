"""Best-effort static call resolution: raw calls → graph edges.

Resolution order for each call: same class/container → explicit type
receiver → typed parameter → same file → imported names → unique
repo-wide name match → class/own-constructor pair collapse → lone
non-method candidate for a bare call. Anything still unclear is
reported as ambiguous rather than guessed; names with no in-repo
candidates are external. Ambiguous calls contribute to no
candidate's ``calls_in``/fan-in — they are never guessed into an edge
— so a symbol's fan-in can undercount actual usage when its name
collides with another definition; see ``mapfile.MapIndex.ambiguous_in``
(who tried to call a given candidate ambiguously) and
``mapfile.MapIndex.ambiguous_out`` (what a given caller called
ambiguously) for how many call sites were dropped on each side.

An explicit ``Type::method()``/``Type.staticMethod()`` receiver (the
type's own bare name, not a variable of that type) is stronger
evidence than either the self/this or typed-parameter steps, but
neither of those fires for it — a call like ``BufferDiff::new(...)``
written inside ``BufferDiff``'s own file (so there's no import to key
off) used to fall through to the generic same-file/fast-path ladder
and land ambiguous whenever the repo defined more than one same-named
method elsewhere, silently dropping the edge (zed's ``BufferDiff.new``
read zero callers despite 13 real call sites — round-09 §2.1 part A).
``_receiver_type_match`` closes this by checking, before the typed-
parameter step, whether the receiver's first segment is itself the
bare name of an in-repo type (``model.TYPE_KINDS``) and, if so,
whether it uniquely narrows the same-named candidates by qualname.

A call through one of the *calling function's own declared
parameters* (``controller.initTask(...)`` where the caller declares
``controller: Controller``) is resolved against that parameter's
declared type before falling back to the (purely coincidental)
same-file step — see ``_typed_param_match``. A call/construction that
resolves to a class-shaped symbol also credits that class's own
explicit constructor method (JS/TS ``constructor``, Python
``__init__``, Java's same-named ``constructor_declaration``) when one
was extracted, via ``_constructor_of`` — without this, ``new
ClassName(...)`` construction was invisible to the constructor
method's fan-in even though it resolved fine to the class itself (or,
for Java specifically, fell into ``ambiguous`` entirely, since a Java
constructor's own bare name is the class name — see
``_construction_pick``). These three gaps were bug #2's undercounted-
caller family (cline's ``get_callers("Controller.initTask")`` finding
2 of 9 real callers; cline/spring-boot's ``Controller.constructor``/
``AutoConfigurations.of`` reading fan-in 0 despite real call sites).

Bare-identifier *references* (a callback passed by value rather than
invoked — see ``model.RawRef``) go through the same candidate ladder
via ``resolve_refs()``, but land in a wholly separate ``referenced``/
``referenced_in``/``referenced_out`` table, never merged with
``edges``/``calls_in``/``calls_out``. Unlike calls, an unresolved
reference is simply dropped rather than recorded as ambiguous — there
is no "ambiguous references" concept, mirroring how an unresolved call
already falls through to ``external`` with nothing further tracked.

The "unique repo-wide name match" step (``_pick_candidate``'s
``len(candidates) == 1`` fast path) is skipped in favor of ambiguous
when the call looks like a built-in/global rather than a genuine
repo-symbol reference — see ``_is_noise_call``. Without this guard, a
repo that happens to define exactly one symbol sharing a name with a
language built-in or ambient global (``trim``, ``expect``,
``describe``, a TS ``declare global`` augmentation of ``String``, ...)
had every unrelated built-in/global call site silently credited to
that one symbol's fan-in, since nothing else in the ladder ever
disambiguates a bare-name call with no receiver or an untyped
receiver. Confirmed live against cline: a same-file-only helper
literally named ``trim`` (true fan-in 8) was reported at fan-in 1,404,
almost entirely misattributed ``String.prototype.trim()`` calls — see
``test-repos/reports/investigation-1.2-resolver-fanin.md``.

The "imported names" step (``_import_match``) ordinarily keys on a
*local binding* name (``from x import y`` / ``import {y} from 'x'``),
which C/C++'s whole-file ``#include`` has no equivalent of — so for
those languages it falls back to checking every ``#include`` in the
caller's file against every candidate's file instead (see
``_WHOLE_FILE_IMPORT_LANGUAGES``), the same ``_module_matches`` check
``affected.py``'s ``_import_hits`` already uses for its diff-import
evidence tier. Without this, a same-named free function defined in two
different files was unresolvable for C/C++ regardless of which header
the caller actually included — see
``test-repos/reports/investigation-1.5-cpp-gtest-affected.md``.

The final fallback, ``_bare_call_non_method_match``, uses ``Symbol.kind``
itself as a disambiguator: a syntactically bare (receiverless) call
can never invoke a *method* in any language dekko parses — reaching
one always requires some receiver/qualifier at the call site
(``recv.Method()``, ``obj.method()``, ``Type::method()``). Dropping
method-kind candidates from an otherwise-ambiguous set and checking
whether exactly one non-method candidate remains turns a real
same-name collision into a correct resolution without guessing.
Round-12 master report §3.2: awesome-go's bare, same-package
``Generate(tt.input)`` (``pkg/slug``'s free function) misresolved as
ambiguous against an unrelated method with a completely different
receiver/arity, ``(g *IDGenerator) Generate(...)`` in ``pkg/markdown``
— causing ``dekko affected``/``workset`` to report zero impacted
tests for a change a same-package unit test directly covered.
"""

import fnmatch
import gc
import hashlib
import json
import multiprocessing
import os
import posixpath
import re
import sys
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ProcessPoolExecutor
from concurrent.futures import TimeoutError as PoolTimeoutError
from concurrent.futures.process import BrokenProcessPool
from dataclasses import asdict, dataclass, field, fields, replace
from multiprocessing.context import BaseContext
from pathlib import Path, PurePosixPath
from typing import TypeVar

from dekko.classify import is_test_path
from dekko.core import languages, walker
from dekko.core.model import (
    TYPE_KINDS,
    CallGraph,
    CatchSite,
    Edge,
    ExternalCall,
    FileMap,
    HeritageEdge,
    Import,
    ModuleEdge,
    ModuleGraph,
    Param,
    RawCall,
    RawHeritage,
    RawRef,
    Symbol,
    ThrowEdge,
)

_SELF_RECEIVERS = {"self", "this", "Self", "cls"}
_PATH_SPLIT = re.compile(r"::|\.|/")
_INDEX_STEMS = {"__init__", "mod", "lib", "index"}
# Languages whose imports are whole-*file* (``#include``), not
# per-symbol bindings (``from x import y`` / ``import {y} from 'x'``)
# — ``_import_match``'s ordinary name-keyed hint lookups can never
# fire for these, since neither the call's own name nor its receiver
# is ever the local name of an import (there is no such thing). See
# ``_import_match``'s whole-file fallback and
# ``test-repos/reports/investigation-1.5-cpp-gtest-affected.md``.
_WHOLE_FILE_IMPORT_LANGUAGES = frozenset({"c", "cpp"})
# Language groupings that genuinely interoperate within one codebase --
# a C header routinely declares something only a C++ ``.cc``/``.cpp``
# implements or calls, and a JS/TS/TSX toolchain routinely shares
# same-named exports across those three extensions. Mirrors the exact
# same pairing/triple ``_WHOLE_FILE_IMPORT_LANGUAGES`` and
# ``_IMPORT_RESOLVERS`` (below) already encode for import resolution --
# see ``_language_filtered``'s docstring for why this is the boundary
# a same-bare-name candidate is allowed to cross. Every language with
# no declared family here (python, rust, go, java, ...) has no such
# precedent anywhere in the resolver and defaults to a same-language-
# only singleton family in ``_language_filtered``.
_LANGUAGE_FAMILIES: dict[str, frozenset[str]] = {
    "c": frozenset({"c", "cpp"}),
    "cpp": frozenset({"c", "cpp"}),
    "javascript": frozenset({"javascript", "typescript", "tsx"}),
    "typescript": frozenset({"javascript", "typescript", "tsx"}),
    "tsx": frozenset({"javascript", "typescript", "tsx"}),
}
# Every raw-usage shape the shared candidate ladder resolves — a call,
# a bare-value reference, and a heritage clause all expose the same
# ``name``/``receiver`` fields and only differ in what table the
# result lands in. ``RawHeritage`` has no ``caller_id`` (it has
# ``subtype_id`` instead — a heritage clause never has an enclosing
# function the way a call/ref can), so every ladder step that reads
# ``caller_id`` is a caller-side concern (``_resolve_call``/
# ``_resolve_ref``/``_resolve_one_heritage``), never something
# ``_pick_candidate`` itself touches.
_Referable = RawCall | RawRef | RawHeritage

MODULE_CALLER_SUFFIX = "::<module>"

# Below this many raw calls/refs across the whole repo, resolution is
# fast enough single-threaded that a process pool's own startup +
# index-pickling overhead isn't worth paying — parallelization only
# pays off once the per-file/per-call loop itself is the bottleneck.
# See round 11's tensorflow finding (~857K raw calls, single-threaded
# resolve()/resolve_refs() dominating wall-clock even on an 11-core
# machine): this threshold is deliberately well below that scale so
# medium repos see a win too, while trivial ones (most test fixtures)
# stay sequential.
#
# Round 30 (.features/fixes/round30/03-resolve-pool-memory-overhead.md):
# this alone is not a sufficient gate. It answers "is there enough work
# to parallelize at all" but says nothing about how many workers that
# work justifies, and each worker costs a private, unpickled copy of the
# whole repo index under ``spawn``. spring-boot (285,609 calls, far past
# this threshold) measured a *net loss* at 11 workers: 6.82s pooled vs.
# 4.91s sequential. ``_pool_workers`` below applies the two additional
# limits that fix that; this constant survives as the floor below which
# no pool is ever built.
_RESOLVE_PARALLEL_MIN_ITEMS = 5_000

# Minimum items (raw calls/refs/throws/catches) a resolve worker must
# have to be worth its own copy of the repo index.
#
# Calibrated by measured sweep on spring-boot (round 30, 11-core/18 GB;
# `_resolve_all`, same work, varying worker count):
#
#     workers   wall    speedup   items/worker
#           1   4.91s     1.00x        285,609
#           2   4.59s     1.07x        142,804
#           3   3.99s     1.23x         95,203   <- optimum
#           4   4.29s     1.14x         71,402
#           6   6.04s     0.81x         47,601   <- net loss
#           8   6.45s     0.76x         35,701
#          11   6.82s     0.72x         25,964
#
# The curve peaks near 95K items/worker and goes net-negative below
# ~50K. 75_000 puts spring-boot at its measured optimum (3 workers) and
# leaves tensorflow (1,395,061 calls) unconstrained by this limit — its
# worker count is governed by the memory cap instead, which is the
# intended division of labor between the two.
_RESOLVE_MIN_ITEMS_PER_WORKER = 75_000

# NOTE (round 30): a RAM-based worker cap was built here and then
# REMOVED after measurement refuted it. The theory was sound-looking --
# each worker holds a private ~2.5 GB copy of the indices under
# ``spawn``, so tensorflow at 11 workers attempts ~27.5 GB of live
# objects on an 18 GB machine and measured only 35% parallel efficiency.
# The predicted fix (fewer workers to stay out of swap) does not
# materialize. Sweeping tensorflow in both orders, to control for host
# drift:
#
#     workers   order 11->6->4   order 4->6->11
#           4          240.04s          268.45s
#           6          199.59s          252.48s
#          11          152.93s          250.66s
#
# Fewer workers never won. macOS's memory compression evidently absorbs
# ~1.5x oversubscription far better than a swap model predicts, so
# capping only gave up real parallelism. Do not reintroduce a proactive
# memory cap without measurement on a genuinely RAM-starved machine
# (untested here: an 18 GB host cannot simulate an 8 GB one). The
# pathological case already has a reactive guard --
# ``run_pooled_with_retry`` retries a ``BrokenProcessPool`` at
# ``_POOL_RETRY_WORKERS``.

# How many chunks to build per worker when parallelizing a resolution
# pass (round 17 scaling investigation:
# .features/plans/round17/round17-resolve-all-scaling-plan.md). Static
# one-chunk-per-worker partitioning left workers that finished early
# with nothing else to pick up -- measured at 2.2x-3.9x speedup on 8
# workers instead of the ~8-10x a compute-bound, evenly-splittable
# workload should get close to. Building ``workers *
# _RESOLVE_CHUNK_OVERSUBSCRIPTION`` chunks and submitting them all to
# the pool's own task queue (instead of exactly ``workers`` chunks,
# one per worker) lets an idle worker pull the next chunk as soon as
# it finishes, the same dynamic-rebalancing pattern
# ``repo_ops._extract_misses``'s ``pool.map(..., chunksize=1)`` already
# gets ~10x from. 4 was chosen as a middle point between finer-grained
# balancing (higher multiplier) and per-task dispatch overhead (lower
# multiplier) without a full empirical sweep on this repo's own CI
# hardware -- see the design doc's "Tune the oversubscription
# multiplier empirically" step for the sweep that would refine this.
_RESOLVE_CHUNK_OVERSUBSCRIPTION = 4

# Worker count for the one bounded retry a broken process pool gets
# (round 17: an MCP server's auto-regen requesting os.cpu_count()
# workers while a sibling `dekko map --jobs 0` does the same on the
# same machine can starve worker-process startup enough to raise
# BrokenProcessPool). Reduced-but-nonzero, not straight to sequential
# -- see ``run_pooled_with_retry``'s docstring for why.
_POOL_RETRY_WORKERS = 2

# Delay before the one bounded retry fires (round 23 §15: a
# ``BrokenProcessPool`` right after `uv tool install --reinstall`
# resolving `/…/bin/dekko`'s `FileNotFoundError` -- the reinstall's
# shim delete-then-relink is a brief filesystem race, not just CPU
# contention; firing the retry immediately gives it a real chance of
# landing in the same still-unsettled window and failing identically,
# which is consistent with the retry itself visibly failing in that
# report despite this function's existing bounded-retry mechanism).
# Cheap and harmless regardless of the exact transient cause -- CPU
# contention (round 17's original motivating case) also benefits from
# not immediately retrying into the same conditions. 1.5s is an
# estimate (the report's own "30s later, worked cleanly" data point
# suggests the window closes well under 30s), not empirically tuned;
# revisit if a tighter repro becomes available.
_POOL_RETRY_DELAY_S = 1.5

# Per-future result-retrieval bound (round 21 Track A: cline's
# ``dekko map --jobs 0`` hung 6+ minutes at 0% CPU across every
# worker, later revealed via a manual kill to be a worker that
# resolved a completely different Python interpreter than its parent
# process and never came up at all). A single chunk/file's real
# extraction or resolution work is seconds at most even on a
# tensorflow-scale repo -- this is deliberately generous (an order of
# magnitude beyond that) so it only ever fires on a genuinely wedged
# worker, never a merely slow one, while still turning an indefinite
# silent hang into a bounded, actionable error. Applied per future
# (each call/chunk gets its own fresh budget), never as a shared
# deadline across a whole batch -- see each call site's own
# ``.result(timeout=POOL_RESULT_TIMEOUT_S)`` usage.
POOL_RESULT_TIMEOUT_S = 600

_PoolResultT = TypeVar("_PoolResultT")

# Symbol fields the resolution ladder can never read, so a change to one
# of them cannot change any other file's resolution. Everything else is
# compared when deciding whether cached resolution is still valid (see
# ``symbol_projection``) -- the comparison is driven off
# ``dataclasses.fields(Symbol)`` minus this set, never a hand-kept
# inclusion list, so a field added to ``Symbol`` later is compared by
# default. The failure direction is then "invalidate more than strictly
# necessary", never "reuse something stale".
#
# ``start_line``/``end_line`` are the load-bearing exclusions: editing a
# function body shifts the line numbers of every symbol below it, and if
# those counted as changes the cache would never hit on the exact edit
# shape it exists to serve. They are safe to exclude because symbol ids
# are line-independent (``extractor.py`` builds ``relpath::Qualname``),
# so no cached edge can be invalidated by a line shift alone.
_RESOLUTION_BLIND_SYMBOL_FIELDS = frozenset({"start_line", "end_line", "doc"})

_PROJECTED_SYMBOL_FIELDS: tuple[str, ...] = ()


def _projected_symbol_fields() -> tuple[str, ...]:
    """Symbol field names compared by ``symbol_projection``, cached."""
    global _PROJECTED_SYMBOL_FIELDS
    if not _PROJECTED_SYMBOL_FIELDS:
        _PROJECTED_SYMBOL_FIELDS = tuple(
            f.name
            for f in fields(Symbol)
            if f.name not in _RESOLUTION_BLIND_SYMBOL_FIELDS
        )
    return _PROJECTED_SYMBOL_FIELDS


def symbol_projection(symbols: list[Symbol] | list[dict]) -> str:
    """Canonical form of the parts of ``symbols`` resolution can read.

    Two symbol lists with the same projection are interchangeable as far
    as every *other* file's resolution is concerned, so cached
    resolution for unchanged files stays valid across an edit that leaves
    the projection intact.

    Accepts ``Symbol`` objects (the freshly extracted side) or the plain
    dicts the extraction cache stores (the previous side), and normalizes
    both through JSON so a ``Param`` dataclass and its cached dict form
    compare equal.

    Args:
        symbols: Symbols of one file, in extraction order. Order is
            significant and preserved: the ``#N`` suffix the extractor
            appends to same-qualname collisions is assigned in
            extraction order, so reordering two colliding symbols swaps
            which id means which symbol while leaving the *set* of ids
            identical.

    Returns:
        A stable string; compare with ``==``.
    """
    names = _projected_symbol_fields()
    rows = []
    for sym in symbols:
        d = asdict(sym) if isinstance(sym, Symbol) else sym
        rows.append({name: d.get(name) for name in names})
    return json.dumps(rows, sort_keys=True, separators=(",", ":"))


_RESOLVE_FINGERPRINT = ""


def resolve_fingerprint() -> str:
    """Hash of this module's source, for invalidating cached resolution.

    The extraction cache's existing ``spec_hash``
    (``languages.spec_fingerprint``) covers what the *extractor* pulls
    out of a file, and says nothing about the resolution ladder. Without
    a separate key, editing ``_pick_candidate`` and re-running would
    silently reuse edges resolved by the old ladder -- a footgun aimed
    squarely at whoever is working on the resolver.

    Hashing the whole module over-invalidates (a comment edit costs one
    full resolve) and that is the right trade: the alternative, a
    hand-bumped constant, relies on every future contributor remembering.

    Returns:
        A stable hex digest, or ``""`` when the source can't be read (a
        frozen/zipped install, where the released version string in the
        cache document is the operative key anyway).
    """
    global _RESOLVE_FINGERPRINT
    if not _RESOLVE_FINGERPRINT:
        try:
            source = Path(__file__).read_bytes()
        except OSError:
            return ""
        _RESOLVE_FINGERPRINT = hashlib.sha256(source).hexdigest()
    return _RESOLVE_FINGERPRINT


@dataclass(frozen=True)
class _NameGroup:
    """One bare symbol name's resolution-relevant state in one file.

    Built by ``_group_by_name`` and compared by ``name_delta`` -- see
    that function's docstring for why grouping (rather than comparing
    the file's symbol list as a whole) is what makes a *name-scoped*
    reuse gate provable.

    Attributes:
        rows: One canonical JSON row per symbol sharing this name
            (``symbol_projection``'s per-symbol form, not the whole
            file's). Compared by ``==``/``!=`` only -- a **set**, not a
            sequence, because within one name unordered comparison is
            correct here even though whole-file ``symbol_projection``
            must stay order-sensitive (round 30 risk #2): two entries
            that swap their ``#N`` collision suffix produce two
            genuinely different row strings (the suffix lives in
            ``id``), so the set still changes when a swap changes what
            the ids mean, and stays equal when it doesn't.
        kinds: Every ``Symbol.kind`` seen under this name.
        containers: For ``kind == "method"`` entries, the bare name of
            the qualname's enclosing type (``Cls.method`` -> ``Cls``).
            Feeds the constructor-collapse rule in ``name_delta``.
    """

    rows: frozenset[str]
    kinds: frozenset[str]
    containers: frozenset[str]


_EMPTY_NAME_GROUP = _NameGroup(
    rows=frozenset(), kinds=frozenset(), containers=frozenset()
)


def _group_by_name(
    symbols: list[Symbol] | list[dict],
) -> dict[str, _NameGroup]:
    """Bucket a file's resolution-relevant symbol projections by name.

    Args:
        symbols: A file's symbols, in either form ``symbol_projection``
            accepts.

    Returns:
        Bare name -> ``_NameGroup``. A name absent from the input has
        no entry (callers use ``dict.get(name, _EMPTY_NAME_GROUP)``).
    """
    field_names = _projected_symbol_fields()
    rows: dict[str, set[str]] = {}
    kinds: dict[str, set[str]] = {}
    containers: dict[str, set[str]] = {}
    for sym in symbols:
        d = asdict(sym) if isinstance(sym, Symbol) else sym
        name = str(d.get("name", ""))
        row = {f: d.get(f) for f in field_names}
        rows.setdefault(name, set()).add(
            json.dumps(row, sort_keys=True, separators=(",", ":"))
        )
        kind = str(d.get("kind", ""))
        kinds.setdefault(name, set()).add(kind)
        qualname = str(d.get("qualname", ""))
        if kind == "method" and "." in qualname:
            container = qualname.rsplit(".", 1)[0].rsplit(".", 1)[-1]
            containers.setdefault(name, set()).add(container)
    return {
        name: _NameGroup(
            rows=frozenset(rows.get(name, ())),
            kinds=frozenset(kinds.get(name, ())),
            containers=frozenset(containers.get(name, ())),
        )
        for name in rows.keys() | kinds.keys()
    }


@dataclass(frozen=True)
class NameDelta:
    """A dirty file's symbol-name delta, for the incremental resolve gate.

    ``storage.resolvecache.build_reuse``'s v1 gate falls back to a full
    repo-wide resolve whenever a dirty file's symbol set changed at
    all. This is v2: identify exactly which bare *names* changed
    meaning, so an unchanged file's cached resolution can be trusted
    unless it actually depends on one of them. See
    ``.features/fixes/round30/01-incremental-resolution.md``'s "v2"
    section and ``test-repos/reports/31-tokentest-7repo-post04355/
    FIX-PLAN-remaining.md`` WP-B for the dependency-class analysis this
    implements.

    Attributes:
        blocks_reuse: True when a type-kind symbol (``model.TYPE_KINDS``)
            is among the changed names. Every one of ``_pick_candidate``'s
            type-aware steps (``_receiver_type_match``,
            ``_rust_type_path_receiver``, ``_owned_by_receiver_type``'s
            trait check, ``_typed_param_match``) reads the *whole*
            repo-wide index for a type-kind name, not just this file's
            own calls -- so a type addition/removal/change can affect
            resolution anywhere a same-named receiver or parameter type
            is written, which no per-file name scan can bound. When
            this is set, ``changed``/``newly_defined`` are incomplete
            and must not be used; the caller falls back to a full
            resolve instead.
        changed: Bare names whose grouped entries (``_NameGroup.rows``)
            differ between the old and new symbol list -- added,
            removed, or substantively changed (candidate count, kind,
            qualname, params, ...; never a pure line-number shift, see
            ``_projected_symbol_fields``). Includes the constructor-
            collapse extension: when a changed name is constructor-
            shaped (``_CONSTRUCTOR_NAMES``, e.g. Python's ``__init__``
            or JS/TS's ``constructor``), its enclosing type's own bare
            name is added too. Without this, adding an ``__init__`` to
            an existing class would go undetected by any cached
            caller's *own* name-scan: ``_constructor_of`` looks up the
            new method by a name (``__init__``) that never appears in
            an already-cached ``MyClass()`` edge, whose callee id ends
            in ``MyClass``, not ``__init__``. Adding the class's own
            name closes that gap without needing to know which files
            call the class directly -- the ordinary name-scan finds
            them once the class's name is itself in the delta.
        newly_defined: The subset of ``changed`` with *no* entries in
            the old projection at all -- names the repo did not define
            before this edit. Only a genuinely new name can flip an
            import-alias miss (``_alias_candidates``, which recovers a
            name via ``alias_original_name`` and retries the index
            lookup under it) from empty to non-empty; a name that
            already existed already had whatever candidates it was
            going to have, so it can't newly enable that recovery.
    """

    blocks_reuse: bool
    changed: frozenset[str]
    newly_defined: frozenset[str]


def name_delta(
    old_symbols: list[Symbol] | list[dict],
    new_symbols: list[Symbol] | list[dict],
) -> NameDelta:
    """Compute which symbol names changed meaning between two versions.

    Args:
        old_symbols: The file's previously cached symbols.
        new_symbols: The file's freshly extracted symbols.

    Returns:
        A ``NameDelta``. See its docstring for what each field means
        and why the constructor-collapse extension is folded into
        ``changed`` here rather than left to the caller.
    """
    old = _group_by_name(old_symbols)
    new = _group_by_name(new_symbols)
    changed = {
        name
        for name in old.keys() | new.keys()
        if old.get(name, _EMPTY_NAME_GROUP).rows
        != new.get(name, _EMPTY_NAME_GROUP).rows
    }
    blocks_reuse = any(
        old.get(name, _EMPTY_NAME_GROUP).kinds & TYPE_KINDS
        or new.get(name, _EMPTY_NAME_GROUP).kinds & TYPE_KINDS
        for name in changed
    )
    newly_defined = {
        name for name in changed if not old.get(name, _EMPTY_NAME_GROUP).rows
    }
    ctor_containers: set[str] = set()
    for name in changed & _CONSTRUCTOR_NAME_SET:
        ctor_containers |= old.get(name, _EMPTY_NAME_GROUP).containers
        ctor_containers |= new.get(name, _EMPTY_NAME_GROUP).containers

    return NameDelta(
        blocks_reuse=blocks_reuse,
        changed=frozenset(changed | ctor_containers),
        newly_defined=frozenset(newly_defined),
    )


def resolved_id_name(symbol_id: str) -> str:
    """Bare ``Symbol.name`` a resolved symbol id's qualname ends in.

    Ids are ``relpath::Qualname`` (``extractor._make_symbol``), with an
    optional ``#N`` collision suffix appended to the *whole* id, never
    to the qualname itself. Splitting on the first ``"::"`` therefore
    isolates the qualname cleanly -- a relpath can contain ``.`` and
    ``/`` but never ``::``, and a qualname (dot-joined containers) never
    contains ``::`` either (``_make_symbol`` converts any ``::`` in a
    captured name into dot-joined containers before building the id).

    Used by ``storage.resolvecache``'s name-delta gate to ask "does
    this cached edge/ambiguous candidate depend on name N" without
    storing the name redundantly alongside every id.

    Args:
        symbol_id: A real, resolved ``Symbol.id`` -- never a module
            pseudo-caller id (``MODULE_CALLER_SUFFIX``), which this
            function never receives in practice since only genuine
            *callees*/*candidates* are ever looked up this way.

    Returns:
        The bare name, with any ``#N`` suffix stripped.
    """
    _, _, qualname = symbol_id.partition("::")
    tail = qualname.rsplit(".", 1)[-1]
    return tail.partition("#")[0]


def alias_original_name(source: str) -> str:
    """The pre-alias bare name ``_alias_candidates`` recovers from an
    import's ``source``.

    Factored out of ``_alias_candidates`` so
    ``storage.resolvecache``'s name-delta gate can replicate exactly
    what that function would look up for a given import, rather than
    risking the two derivations drifting apart -- see
    ``NameDelta.newly_defined``.

    Args:
        source: An ``Import.source`` string.

    Returns:
        Its last path-like segment, or ``""`` for an empty source.
    """
    return _PATH_SPLIT.split(source)[-1] if source else ""


@dataclass(frozen=True)
class ResolveReuse:
    """Per-file ``_resolve_all`` output that may be reused verbatim.

    Built by ``storage.resolvecache.build_reuse`` only when the global
    resolution inputs are provably identical to the cached run, so that
    an unchanged file's resolution is identical by construction rather
    than by heuristic. See
    ``.features/fixes/round30/01-incremental-resolution.md``.

    Attributes:
        cached: ``path -> {"edges": [...], "ambiguous": [...],
            "external": [...]}``, as written by
            ``partition_resolution``.
        dirty: Paths whose cached entry must *not* be used, because the
            file changed (or was never resolved). Every path in the
            repo is either in ``dirty`` or has a usable ``cached``
            entry; nothing may fall between the two, or its edges would
            silently vanish from the map.
    """

    cached: dict[str, dict]
    dirty: frozenset[str]


def _caller_path(caller_id: str) -> str:
    """Repo-relative path owning ``caller_id``.

    Symbol ids are ``relpath::Qualname`` (``extractor.py``), and module
    pseudo-ids are ``relpath::<module>``, so the path is everything
    before the first separator.
    """
    return caller_id.split("::", 1)[0]


def partition_resolution(
    files: list[FileMap], graph: "CallGraph"
) -> dict[str, dict]:
    """Split a resolved call graph into per-owning-file entries.

    Sound because a call's ``caller_id`` always belongs to the file the
    call was extracted from -- the same invariant ``_resolve_all``'s
    chunking already relies on, so no two files can produce a colliding
    edge/ambiguous/external key.

    Args:
        files: Every mapped file, so a file that resolved to nothing
            still gets an (empty) entry. Without that, such a file would
            look "never resolved" to ``build_reuse`` and be re-resolved
            on every run forever.
        graph: The resolved graph to split.

    Returns:
        ``path -> {"edges", "ambiguous", "external"}``, JSON-ready.
    """
    out: dict[str, dict] = {
        fm.path: {"edges": [], "ambiguous": [], "external": []} for fm in files
    }
    for edge in graph.edges:
        entry = out.get(_caller_path(edge.caller))
        if entry is not None:
            entry["edges"].append([edge.caller, edge.callee, edge.lines])
    for caller, name, cands in graph.ambiguous:
        entry = out.get(_caller_path(caller))
        if entry is not None:
            entry["ambiguous"].append([caller, name, cands])
    for ext in graph.external:
        entry = out.get(_caller_path(ext.caller))
        if entry is not None:
            entry["external"].append([ext.caller, ext.callee, ext.lines])
    return out


def _merge_reused(
    reuse: ResolveReuse,
    edges: dict[tuple[str, str], set[int]],
    ambiguous: dict[tuple[str, str], list[str]],
    external: dict[tuple[str, str], set[int]],
) -> None:
    """Fold every non-dirty cached entry into the fresh accumulators."""
    for path, entry in reuse.cached.items():
        if path in reuse.dirty:
            continue
        for caller, callee, lines in entry.get("edges", ()):
            edges.setdefault((caller, callee), set()).update(lines)
        for caller, name, cands in entry.get("ambiguous", ()):
            ambiguous.setdefault((caller, name), list(cands))
        for caller, target, lines in entry.get("external", ()):
            external.setdefault((caller, target), set()).update(lines)


def _pool_workers(workers: int, items: int) -> int:
    """How many resolve workers this workload justifies.

    Takes the strictest of two limits: the caller's own request, and the
    work available (``_RESOLVE_MIN_ITEMS_PER_WORKER`` per worker, since
    each worker costs a private copy of the whole repo index under
    ``spawn``). Returns 1 to mean "run sequentially, build no pool at
    all" -- a pool of one worker is strictly worse than the in-process
    path, since it pays the full index transfer for zero parallelism.

    A third limit, a RAM-based cap, was implemented here and removed
    after measurement refuted it -- see the note above
    ``_RESOLVE_CHUNK_OVERSUBSCRIPTION`` for the sweep data and why not to
    reintroduce it blind.

    Round 30: before this existed, each pass gated only on
    ``workers > 1 and items >= _RESOLVE_PARALLEL_MIN_ITEMS``, which let
    spring-boot run 11 workers on work that justified 3 and measured a
    net loss against sequential. See
    ``.features/fixes/round30/03-resolve-pool-memory-overhead.md``.

    Args:
        workers: Worker count the caller asked for.
        items: Units of work in this pass (raw calls, refs, throws, or
            catch clauses, depending on the pass).

    Returns:
        Worker count to build the pool with, or 1 for sequential.
    """
    if workers <= 1 or items < _RESOLVE_PARALLEL_MIN_ITEMS:
        return 1
    justified = items // _RESOLVE_MIN_ITEMS_PER_WORKER
    chosen = min(workers, justified)
    return chosen if chosen >= 2 else 1


# Process-wide cached verdict of ``_choose_pool_mp_context`` -- see
# ``_pool_mp_context`` for why the decision is made exactly once.
_pool_ctx_cache: BaseContext | None = None


def _pool_mp_context() -> BaseContext:
    """The (cached) start method for dekko's process pools.

    Decided once per process, at the first pool build, and reused for
    every later one. The cache is not an optimization -- it is what
    makes the thread gate sound: ``ProcessPoolExecutor.shutdown(
    wait=False)`` (every call site's teardown, deliberately, since
    round 22) can leave the executor's manager/feeder threads alive
    for a moment after a pool finishes, so a naive per-pool check
    would see the *extraction* pool's harmless ghost threads and
    silently downgrade every *resolve* pass to ``spawn`` in the exact
    single-threaded CLI path fork exists for. At first-pool time the
    check is honest: the CLI/MCP parent has one thread, and the daemon
    has already started its status thread before any request can
    build a pool, so each process caches the verdict that is correct
    for its whole lifetime.

    Returns:
        The multiprocessing context every pool build should pass as
        ``mp_context=``.
    """
    global _pool_ctx_cache
    if _pool_ctx_cache is None:
        _pool_ctx_cache = _choose_pool_mp_context()

    return _pool_ctx_cache


def _choose_pool_mp_context() -> BaseContext:
    """Start method for dekko's process pools: ``fork`` when provably safe.

    ``fork`` gives workers copy-on-write access to the parent's memory:
    the resolution indices are not pickled, transferred, or duplicated
    per worker the way they are under ``spawn`` (round 30 measured
    ~205 MB pickled per worker expanding to ~2.5 GB live on a
    tensorflow-scale repo -- see ``.features/fixes/round30/
    03b-fork-and-single-pool-designs.md``). Linux got this for free as
    the platform default through Python 3.13; choosing the context
    explicitly both extends it to macOS and pins it on Linux before
    3.14's ``forkserver`` default flip silently takes it away
    (``forkserver`` re-pickles initargs per worker, so it has
    ``spawn``'s transfer cost -- it buys nothing here).

    ``fork`` is only safe from a single-threaded parent, so the gate is
    a runtime thread-count check at pool-build time -- the daemon (its
    status thread is always running while a request executes) can never
    pass it, with no plumbing to forget. Windows has no ``fork`` at
    all. ``DEKKO_POOL_START_METHOD`` is the escape hatch, consulted
    only when the safety gates would allow ``fork``: ``spawn`` opts a
    problem host back out, ``fork`` is an explicit default. A
    first-attempt failure under ``fork`` is retried under ``spawn`` by
    ``run_pooled_with_retry``, so a host where ``fork`` misbehaves
    degrades to exactly the pre-round-30 behavior at the cost of one
    wasted attempt.

    Returns:
        The freshly chosen context. Callers go through
        ``_pool_mp_context``, which caches the first verdict for the
        life of the process -- including this function's env-var read.
    """
    if sys.platform == "win32":
        return multiprocessing.get_context("spawn")
    if threading.active_count() > 1:
        return multiprocessing.get_context("spawn")
    forced = os.environ.get("DEKKO_POOL_START_METHOD")
    if forced in ("spawn", "fork"):
        return multiprocessing.get_context(forced)

    return multiprocessing.get_context("fork")


class PoolStalledError(RuntimeError):
    """A process-pool future made no progress within its timeout.

    Raised by ``run_pooled_with_retry`` when a worker never returns a
    result within ``POOL_RESULT_TIMEOUT_S`` -- almost always a wedged
    worker spawn (round 21 Track A), not a legitimately slow
    computation. Deliberately distinct from ``BrokenProcessPool``
    (which the pool itself raises on an outright crash): a stalled
    worker that never starts or never finishes doesn't necessarily
    crash the pool at all, so nothing else would ever surface this.
    """


def _pool_retry_note(what: str, retry_workers: int, forked: bool) -> None:
    """Print the process-pool-retry disclosure note to stderr.

    Mirrors round 15's ``_maybe_warn_sequential`` pattern (a one-line
    ``note:`` on stderr before a slower fallback path runs) so a
    caller that ends up waiting longer for a reduced-parallelism retry
    isn't left in the dark about why. When the first attempt ran under
    ``fork``, the retry also switches start method to ``spawn`` (see
    ``run_pooled_with_retry``), and the note says so -- the disclosure
    should name the actual mechanism change, not just the worker count.
    """
    plural = "" if retry_workers == 1 else "s"
    switched = ", switching fork -> spawn" if forked else ""
    print(
        f"note: process pool failed during {what} (likely CPU "
        "contention from another concurrent dekko process on this "
        f"machine) -- retrying with reduced parallelism "
        f"({retry_workers} worker{plural}{switched})",
        file=sys.stderr,
    )


def run_pooled_with_retry(
    run: Callable[[int, BaseContext], _PoolResultT],
    workers: int,
    what: str,
) -> _PoolResultT:
    """Run a process-pool step, retrying once at reduced parallelism if
    the pool itself breaks.

    Round 30 (c): also chooses the pool's start method. The first
    attempt runs under ``_pool_mp_context()`` (``fork`` from a provably
    single-threaded POSIX parent, ``spawn`` otherwise); the retry
    always runs under ``spawn``. A first-attempt failure under ``fork``
    is at least as likely to be fork-specific (a macOS Objective-C
    ``+initialize`` abort in a worker is delivered as exactly
    ``BrokenProcessPool``) as it is the round-17 contention case, and
    retrying under ``spawn`` covers both causes at once -- ``fork`` is
    strictly opportunistic, and its worst case is one wasted attempt
    followed by the previously-shipped behavior.

    ``BrokenProcessPool`` most often means sibling multiprocessing
    contention on the host machine starved worker-process startup
    (round 17), not that process pools are fundamentally broken here
    -- a bounded retry at a small but nonzero worker count is far more
    likely to survive the same transient contention than either
    repeating at full parallelism (same risk) or falling straight to
    fully-sequential (round 15 measured 5+ minutes cold on a
    tensorflow-scale repo; silently downgrading an in-flight MCP call
    to that is its own trap).

    Not a retry loop: exactly one bounded second attempt at
    ``_POOL_RETRY_WORKERS`` (or fewer, if ``workers`` was already
    smaller), after a fixed ``_POOL_RETRY_DELAY_S`` backoff. If that
    attempt also raises ``BrokenProcessPool``, it propagates unchanged
    -- a genuinely wedged or resource-exhausted machine should surface
    a clear error, not hang retrying indefinitely. The delay exists
    because an immediate retry can land in the exact same transient
    window that caused the first failure (round 23 §15: observed right
    after ``uv tool install --reinstall``, where the retry itself also
    failed -- see ``_POOL_RETRY_DELAY_S``'s comment).

    Before every attempt, pins ``multiprocessing``'s spawn executable
    to this process's own ``sys.executable`` (round 21 Track A: cline
    reproduced a spawned worker resolving a completely different
    Python interpreter -- the system Anaconda install -- than its own
    parent's ``uv tool``-managed venv, under host CPU contention,
    producing a 6+ minute silent hang). Explicit pinning is cheap,
    always correct (a worker should always run under the exact
    interpreter its own parent is running under), and closes off that
    failure mode regardless of whichever PATH/resolution mechanism
    let it happen. Each call site's own ``run`` closure is separately
    responsible for bounding its own ``future.result()``/``.result()``
    retrieval with ``POOL_RESULT_TIMEOUT_S`` -- a
    :class:`PoolTimeoutError` escaping ``run`` is re-raised here as
    :class:`PoolStalledError` with an actionable message, turning what
    would otherwise be an indefinite silent hang into a bounded,
    diagnosable error.

    Args:
        run: Builds a fresh pool at the given worker count and
            multiprocessing context (pass it as ``mp_context=``) and
            returns the merged result. Called once, or twice on a
            first-attempt ``BrokenProcessPool``; must be safe to call
            again with no partial state left visible to the caller
            (every call site in this module is a closure over locals
            only, so this holds here).
        workers: The worker count to attempt first.
        what: Short label for the disclosure note (e.g. ``"call
            resolution"``).

    Returns:
        Whatever ``run`` returns.

    Raises:
        BrokenProcessPool: If the retry attempt also fails.
        PoolStalledError: If a worker made no progress within
            ``POOL_RESULT_TIMEOUT_S``.
    """
    multiprocessing.set_executable(sys.executable)
    ctx = _pool_mp_context()
    try:
        try:
            return run(workers, ctx)
        except BrokenProcessPool:
            retry_workers = min(workers, _POOL_RETRY_WORKERS)
            forked = ctx.get_start_method() == "fork"
            _pool_retry_note(what, retry_workers, forked)
            time.sleep(_POOL_RETRY_DELAY_S)
            multiprocessing.set_executable(sys.executable)
            return run(retry_workers, multiprocessing.get_context("spawn"))
    except PoolTimeoutError as exc:
        raise PoolStalledError(
            f"process pool made no progress during {what} within "
            f"{POOL_RESULT_TIMEOUT_S}s -- a worker likely failed to "
            "start or stalled (e.g. under heavy CPU contention from "
            "another concurrent dekko process on this machine). "
            "Retry with --jobs 1, or after system load has "
            "subsided."
        ) from exc


def _run_pool_bounded(
    pool: ProcessPoolExecutor, futures: list["Future[_PoolResultT]"]
) -> list[_PoolResultT]:
    """Collect every future's result, each bounded by
    ``POOL_RESULT_TIMEOUT_S`` -- and, on a timeout, tear the pool down
    without waiting on wedged workers, instead of letting the call
    site's ``with ProcessPoolExecutor(...) as pool:`` block on
    ``ProcessPoolExecutor.__exit__``'s default ``shutdown(wait=True)``.

    Round 22 claude-code.md finding: every pool call site used to be a
    bare ``with ProcessPoolExecutor(...) as pool:`` around a
    ``future.result(timeout=POOL_RESULT_TIMEOUT_S)`` loop. When that
    ``.result()`` call raised ``PoolTimeoutError``, the exception
    unwound out through ``__exit__``, which unconditionally calls
    ``shutdown(wait=True)`` -- and ``wait=True`` blocks until every
    worker process the pool ever launched actually terminates and is
    joined. A genuinely wedged worker (spawned but never running --
    exactly the failure ``POOL_RESULT_TIMEOUT_S`` exists to bound)
    never self-terminates, so the pool hung indefinitely *after* the
    600s bound was already hit, silently, with zero output -- the
    documented timeout never actually protected anyone. This function
    is called with the pool built and owned by the caller (no ``with``
    statement involved), so a timeout here can shut the pool down
    without waiting and forcibly kill any still-wedged worker before
    re-raising, rather than blocking on them.

    Args:
        pool: An already-constructed, not-yet-``with``-entered pool
            (the caller owns its lifecycle via ``try``/``finally``).
        futures: Futures already submitted to ``pool``.

    Returns:
        Each future's result, in submission order.

    Raises:
        PoolTimeoutError: If any future doesn't resolve within
            ``POOL_RESULT_TIMEOUT_S`` -- after tearing the pool down.
    """
    try:
        return [f.result(timeout=POOL_RESULT_TIMEOUT_S) for f in futures]
    except PoolTimeoutError:
        # ``_processes`` is a private ``concurrent.futures.process``
        # attribute (dict of pid -> multiprocessing.Process) -- there
        # is no public API to force-kill an already-launched-but-
        # wedged worker; ``shutdown()`` alone only stops the pool from
        # accepting new work, it doesn't touch a worker that never
        # started running. A future CPython release could rename this
        # attribute, in which case this becomes a no-op (the
        # ``AttributeError`` is deliberately not caught here so that
        # shows up loudly rather than silently reverting to the old
        # hang) -- see the accompanying test that exercises this exact
        # branch against a real ``ProcessPoolExecutor``. Snapshotted
        # *before* ``shutdown()`` -- ``shutdown()`` clears
        # ``self._processes`` to ``None`` as part of its own teardown
        # regardless of ``wait``, so reading it after would always see
        # ``None``.
        procs = list(pool._processes.values())
        pool.shutdown(wait=False, cancel_futures=True)
        for proc in procs:
            if proc.is_alive():
                proc.kill()
        raise


# Worker-process-local copies of the shared, read-only indices every
# resolution pass needs. Populated once per worker process (not once
# per submitted chunk) by ``_init_resolve_worker``, so that
# oversubscribing a pool to many more chunks than workers (round 17,
# see ``_RESOLVE_CHUNK_OVERSUBSCRIPTION``) doesn't also multiply how
# many times these (potentially large -- tens of MB on a big repo)
# structures get pickled across process boundaries. ``None`` outside a
# pool worker process; every pass that reads these asserts non-``None``
# first as a guard against a worker function accidentally being called
# without the initializer having run.
_worker_index: dict[str, list[Symbol]] | None = None
_worker_by_name_path: dict[tuple[str, str], list[Symbol]] | None = None
_worker_imports_by_file: dict[str, dict[str, Import]] | None = None
_worker_repo_stems: set[str] | None = None
_worker_symbols_by_id: dict[str, Symbol] | None = None


def _init_resolve_worker(
    index: dict[str, list[Symbol]],
    by_name_path: dict[tuple[str, str], list[Symbol]],
    imports_by_file: dict[str, dict[str, Import]],
    repo_stems: set[str] | None,
    symbols_by_id: dict[str, Symbol] | None,
) -> None:
    """Stash the shared read-only indices in this worker process once.

    Passed as ``ProcessPoolExecutor(initializer=...)``: runs exactly
    once per worker process, before that worker picks up its first
    submitted chunk -- not once per chunk, the way passing these same
    arguments through every ``submit()`` call (today's one-chunk-per-
    worker shape) does. A plain module-level function (not a closure)
    so it stays picklable as an ``initializer=`` target under
    ``spawn``.

    ``repo_stems``/``symbols_by_id`` are ``None`` for pools that don't
    need them (``resolve_refs``/``resolve_throws``/``resolve_catches``
    all resolve against a subset of the five indices ``_resolve_all``
    needs) -- each pass's own worker wrapper only reads the globals it
    actually uses.

    Args:
        index: Name -> candidate symbols, repo-wide.
        by_name_path: ``(name, path)`` -> candidate symbols.
        imports_by_file: Per-file import bindings.
        repo_stems: Every file's repo-relative stem, or ``None`` if
            this pool's task doesn't need it.
        symbols_by_id: Symbol id -> ``Symbol``, or ``None`` if this
            pool's task doesn't need it.
    """
    global _worker_index, _worker_by_name_path, _worker_imports_by_file
    global _worker_repo_stems, _worker_symbols_by_id
    _worker_index = index
    _worker_by_name_path = by_name_path
    _worker_imports_by_file = imports_by_file
    _worker_repo_stems = repo_stems
    _worker_symbols_by_id = symbols_by_id


def resolve(
    files: list[FileMap],
    workers: int = 1,
    root: Path | None = None,
    reuse: ResolveReuse | None = None,
) -> CallGraph:
    """Resolve every raw call across the repo into a call graph.

    Args:
        files: Per-file extraction results.
        workers: Worker count for parallel call-graph resolution
            (1 = sequential, the default — every caller except
            ``cli.py``'s ``run_map`` leaves this at 1, matching prior
            behavior exactly). See ``_resolve_all`` for how chunking
            and the parallelization threshold work.
        root: Repository root, used only to discover and parse
            JS/TS project config: ``tsconfig.json``/``jsconfig.json``
            path aliases for import resolution (see
            ``resolve_imports``) and the workspace package table (see
            ``load_workspace_packages``) the call, reference and
            heritage passes use to recognize a workspace-package
            import as in-repo. ``None``
            (the default) skips that discovery entirely — every caller
            that doesn't pass a real root sees byte-identical behavior
            to before this parameter existed.
        reuse: Cached per-file call resolution to reuse for unchanged
            files, from ``storage.resolvecache.build_reuse``. ``None``
            (the default, and every caller except ``run_map``) resolves
            the whole repo exactly as before. Only the *call* pass is
            reusable; refs/heritage/imports/throws/catches are always
            recomputed in full — they are a small share of the cost and
            caching them would drag in path-set and tsconfig
            invalidation questions the call pass doesn't have. See
            ``.features/fixes/round30/01-incremental-resolution.md``.

    Returns:
        The resolved ``CallGraph`` with bidirectional adjacency.
    """
    index = _build_index(files)
    by_name_path = _build_name_path_index(files)
    # One discovery pass feeds both the symbol-level passes (name ->
    # directory) and the module graph (entry-point fields).
    manifests = _load_workspace_manifests(root) if root is not None else {}
    workspace_pkgs = {n: m.package_dir for n, m in manifests.items()}
    imports_by_file = _imports_by_file(files, workspace_pkgs)
    symbols_by_id = {sym.id: sym for fm in files for sym in fm.symbols}
    repo_stems = {_repo_stem(PurePosixPath(fm.path)) for fm in files}

    # Round 30 (c): under fork-context pools, CPython refcounting
    # dirties copy-on-write pages on mere *reads*, so each worker
    # progressively re-privatizes index pages as it touches them.
    # gc.freeze() moves everything currently alive (the FileMaps and
    # the five indices built above) into the permanent generation so
    # at least the GC's own per-object header writes stop forcing
    # copies. Refcount writes have no stdlib mitigation; this is the
    # cheap half. Skipped entirely unless a fork pool is actually
    # possible, so spawn-context behavior is byte-for-byte unchanged.
    fork_pools = (
        workers > 1 and _pool_mp_context().get_start_method() == "fork"
    )
    if fork_pools:
        gc.freeze()
    try:
        # Only the changed files need re-resolving, but they resolve
        # against the *whole* repo's indices built above -- narrowing
        # the file list without narrowing the indices is what makes
        # this sound.
        to_resolve = (
            files
            if reuse is None
            else [fm for fm in files if fm.path in reuse.dirty]
        )
        if reuse is not None and not to_resolve:
            edges, ambiguous, external = {}, {}, {}
        else:
            edges, ambiguous, external = _resolve_all(
                to_resolve,
                index,
                by_name_path,
                imports_by_file,
                repo_stems,
                symbols_by_id,
                workers,
            )
        if reuse is not None:
            _merge_reused(reuse, edges, ambiguous, external)

        graph = CallGraph(
            edges=[
                Edge(caller=c, callee=e, lines=sorted(lines))
                for (c, e), lines in sorted(edges.items())
            ],
            ambiguous=[
                (caller, name, cands)
                for (caller, name), cands in sorted(ambiguous.items())
            ],
            external=[
                ExternalCall(caller=c, callee=t, lines=sorted(lines))
                for (c, t), lines in sorted(external.items())
            ],
        )
        _build_adjacency(graph)
        graph.referenced, graph.referenced_in, graph.referenced_out = (
            resolve_refs(files, workers, workspace_pkgs)
        )
        (
            graph.heritage,
            graph.heritage_out,
            graph.heritage_in,
            graph.heritage_ambiguous,
            graph.heritage_external,
            graph.heritage_synthetic_tiebreak_count,
            graph.heritage_unplaced_subtype_count,
        ) = resolve_heritage(files, workspace_pkgs)
        graph.modules = resolve_imports(
            files, root=root, workspace_manifests=manifests
        )
        (
            graph.throws,
            graph.throws_out,
            graph.throws_ambiguous,
            graph.throws_external,
            graph.throws_bare,
        ) = resolve_throws(files, workers)
        graph.catches = resolve_catches(files, workers)
        # No resolution pass needed — a literal env-var key is already
        # the fully-resolved fact (see model.EnvRead's docstring), so
        # this is a plain flatten across files, not a call into a
        # dedicated resolve_env_reads() the way every other section
        # above is.
        graph.env_reads = [r for fm in files for r in fm.env_reads]
    finally:
        if fork_pools:
            gc.unfreeze()

    return graph


def _resolve_files_chunk(
    files: list[FileMap],
    index: dict[str, list[Symbol]],
    by_name_path: dict[tuple[str, str], list[Symbol]],
    imports_by_file: dict[str, dict[str, Import]],
    repo_stems: set[str],
    symbols_by_id: dict[str, Symbol],
) -> tuple[
    dict[tuple[str, str], set[int]],
    dict[tuple[str, str], list[str]],
    dict[tuple[str, str], set[int]],
]:
    """Resolve every call in ``files`` into fresh, local accumulators.

    A pure function of its arguments — reads the shared, already-built
    indices but never mutates anything outside its own local
    ``edges``/``ambiguous``/``external`` dicts — so it can run
    standalone inside a worker process with no cross-worker locking.
    Module-level (not a closure) so ``ProcessPoolExecutor`` can pickle
    it; also the sequential (``workers <= 1``) code path, called
    directly with the full file list.
    """
    edges: dict[tuple[str, str], set[int]] = {}
    ambiguous: dict[tuple[str, str], list[str]] = {}
    external: dict[tuple[str, str], set[int]] = {}
    for fm in files:
        file_imports = imports_by_file.get(fm.path, {})
        raw_imports = (
            fm.imports if fm.language in _WHOLE_FILE_IMPORT_LANGUAGES else None
        )
        for call in fm.calls:
            _resolve_call(
                call,
                index=index,
                by_name_path=by_name_path,
                file_imports=file_imports,
                repo_stems=repo_stems,
                symbols_by_id=symbols_by_id,
                edges=edges,
                ambiguous=ambiguous,
                external=external,
                raw_imports=raw_imports,
            )
    return edges, ambiguous, external


def _resolve_files_chunk_worker(
    files: list[FileMap],
) -> tuple[
    dict[tuple[str, str], set[int]],
    dict[tuple[str, str], list[str]],
    dict[tuple[str, str], set[int]],
]:
    """Per-task pool entry point for oversubscribed call resolution.

    Thin wrapper around ``_resolve_files_chunk``: reads the shared
    indices ``_init_resolve_worker`` already stashed in this worker
    process's globals instead of receiving them as arguments, so each
    ``submit()``'s pickled payload is just this chunk's own ``files``
    slice -- keeping per-task dispatch cost small even with many more
    chunks than workers (``_RESOLVE_CHUNK_OVERSUBSCRIPTION``).
    """
    assert _worker_index is not None  # initializer always runs first
    assert _worker_repo_stems is not None
    assert _worker_symbols_by_id is not None
    return _resolve_files_chunk(
        files,
        _worker_index,
        _worker_by_name_path,
        _worker_imports_by_file,
        _worker_repo_stems,
        _worker_symbols_by_id,
    )


def _chunk_files(files: list[FileMap], n: int) -> list[list[FileMap]]:
    """Split ``files`` into up to ``n`` contiguous, roughly-even chunks."""
    if n <= 1 or len(files) < 2:
        return [files]
    n = min(n, len(files))
    chunk_size = -(-len(files) // n)  # ceil division
    return [
        files[i : i + chunk_size] for i in range(0, len(files), chunk_size)
    ]


def _resolve_all(
    files: list[FileMap],
    index: dict[str, list[Symbol]],
    by_name_path: dict[tuple[str, str], list[Symbol]],
    imports_by_file: dict[str, dict[str, Import]],
    repo_stems: set[str],
    symbols_by_id: dict[str, Symbol],
    workers: int,
) -> tuple[
    dict[tuple[str, str], set[int]],
    dict[tuple[str, str], list[str]],
    dict[tuple[str, str], set[int]],
]:
    """Resolve every file's calls, across a process pool when it pays off.

    ``_pool_workers`` decides how many workers this run justifies, from
    the caller's request, the raw-call count, and available RAM. When it
    returns 1, this is exactly the old single-process loop (via
    ``_resolve_files_chunk`` called once on the full file list) — same
    result, same cost, no pool startup overhead paid for nothing. That
    now happens on repos well above ``_RESOLVE_PARALLEL_MIN_ITEMS``
    whose work doesn't justify a second worker's private index copy,
    which is deliberate: round 30 measured spring-boot (285,609 calls)
    losing to sequential at 11 workers. Otherwise ``files`` is split
    into up to ``_pool_workers``-many x oversubscription
    chunks; each chunk resolves independently against the same
    shared, read-only indices (safe: a call's ``caller_id`` always
    belongs to the file it was extracted from, so no two chunks ever
    produce a colliding edge/ambiguous/external key), and results are
    merged. The final ``edges``/``ambiguous``/``external`` dicts are
    then sorted in ``resolve()`` exactly as before, so output order
    (and therefore ``map.json``) is independent of how many workers
    ran or which one finished first — a parallel run must be
    byte-identical to a sequential one.

    A ``BrokenProcessPool`` on the parallel path (round 17: sibling
    multiprocessing contention on the host machine) gets one bounded
    retry at reduced parallelism via ``run_pooled_with_retry`` before
    propagating — see that function's docstring.
    """
    total_calls = sum(len(fm.calls) for fm in files)
    pool_workers = _pool_workers(workers, total_calls)
    if pool_workers <= 1:
        return _resolve_files_chunk(
            files,
            index,
            by_name_path,
            imports_by_file,
            repo_stems,
            symbols_by_id,
        )

    def _run(
        w: int,
        ctx: BaseContext,
    ) -> tuple[
        dict[tuple[str, str], set[int]],
        dict[tuple[str, str], list[str]],
        dict[tuple[str, str], set[int]],
    ]:
        chunks = _chunk_files(files, w * _RESOLVE_CHUNK_OVERSUBSCRIPTION)
        if len(chunks) < 2:
            return _resolve_files_chunk(
                files,
                index,
                by_name_path,
                imports_by_file,
                repo_stems,
                symbols_by_id,
            )

        edges: dict[tuple[str, str], set[int]] = {}
        ambiguous: dict[tuple[str, str], list[str]] = {}
        external: dict[tuple[str, str], set[int]] = {}
        pool = ProcessPoolExecutor(
            max_workers=w,
            mp_context=ctx,
            initializer=_init_resolve_worker,
            initargs=(
                index,
                by_name_path,
                imports_by_file,
                repo_stems,
                symbols_by_id,
            ),
        )
        try:
            futures = [
                pool.submit(_resolve_files_chunk_worker, chunk)
                for chunk in chunks
            ]
            for (
                chunk_edges,
                chunk_ambiguous,
                chunk_external,
            ) in _run_pool_bounded(pool, futures):
                for key, lines in chunk_edges.items():
                    edges.setdefault(key, set()).update(lines)
                for key, cands in chunk_ambiguous.items():
                    ambiguous.setdefault(key, cands)
                for key, lines in chunk_external.items():
                    external.setdefault(key, set()).update(lines)
        finally:
            pool.shutdown(wait=False)
        return edges, ambiguous, external

    return run_pooled_with_retry(_run, pool_workers, "call resolution")


def _resolve_refs_chunk(
    files: list[FileMap],
    index: dict[str, list[Symbol]],
    by_name_path: dict[tuple[str, str], list[Symbol]],
    imports_by_file: dict[str, dict[str, Import]],
    symbols_by_id: dict[str, Symbol],
    repo_stems: set[str],
) -> dict[tuple[str, str], set[int]]:
    """Resolve every reference in ``files`` into a fresh, local ``edges``
    dict — the reference-resolution analog of ``_resolve_files_chunk``,
    same pure-function/worker-safety shape."""
    edges: dict[tuple[str, str], set[int]] = {}
    for fm in files:
        file_imports = imports_by_file.get(fm.path, {})
        file_exports = any(sym.exported for sym in fm.symbols)
        for ref in fm.refs:
            _resolve_ref(
                ref,
                index=index,
                by_name_path=by_name_path,
                file_imports=file_imports,
                symbols_by_id=symbols_by_id,
                edges=edges,
                repo_stems=repo_stems,
                file_exports=file_exports,
            )
    return edges


def _resolve_refs_chunk_worker(
    files: list[FileMap],
) -> dict[tuple[str, str], set[int]]:
    """Per-task pool entry point for oversubscribed reference
    resolution -- reads the shared indices ``_init_resolve_worker``
    stashed in this worker process's globals, the reference-resolution
    analog of ``_resolve_files_chunk_worker``."""
    assert _worker_index is not None  # initializer always runs first
    assert _worker_symbols_by_id is not None
    assert _worker_repo_stems is not None
    return _resolve_refs_chunk(
        files,
        _worker_index,
        _worker_by_name_path,
        _worker_imports_by_file,
        _worker_symbols_by_id,
        _worker_repo_stems,
    )


def resolve_refs(
    files: list[FileMap],
    workers: int = 1,
    workspace_pkgs: dict[str, str] | None = None,
) -> tuple[list[Edge], dict[str, list[str]], dict[str, list[str]]]:
    """Resolve every raw value reference across the repo.

    Mirrors ``resolve()``'s resolution ladder, but for ``RawRef``s
    (bare identifiers used as values — see ``model.RawRef``) instead
    of ``RawCall``s. Kept as a distinct pass with its own return
    shape/tables rather than folding into ``edges``/``calls_in``/
    ``calls_out`` — see the module docstring for why. A
    ``BrokenProcessPool`` on the parallel path gets one bounded retry
    at reduced parallelism via ``run_pooled_with_retry`` before
    propagating.

    Args:
        files: Per-file extraction results.
        workers: Worker count for parallel resolution (1 = sequential,
            the default). See ``resolve``'s own ``workers`` parameter
            and ``_resolve_all``'s docstring for the parallelization
            shape this mirrors.
        workspace_pkgs: JS/TS workspace package name → directory (see
            ``load_workspace_packages``), or ``None``. Lets a
            workspace-package import narrow a colliding name to the
            package it was imported from.

    Returns:
        ``(edges, referenced_in, referenced_out)``, the same shape
        ``resolve()`` builds for calls, but for references.
    """
    index = _build_index(files)
    by_name_path = _build_name_path_index(files)
    imports_by_file = _imports_by_file(files, workspace_pkgs)
    symbols_by_id = {sym.id: sym for fm in files for sym in fm.symbols}
    repo_stems = {_repo_stem(PurePosixPath(fm.path)) for fm in files}

    total_refs = sum(len(fm.refs) for fm in files)
    pool_workers = _pool_workers(workers, total_refs)
    use_pool = pool_workers > 1

    def _run(w: int, ctx: BaseContext) -> dict[tuple[str, str], set[int]]:
        chunks = (
            _chunk_files(files, w * _RESOLVE_CHUNK_OVERSUBSCRIPTION)
            if use_pool
            else [files]
        )
        if len(chunks) < 2:
            return _resolve_refs_chunk(
                files,
                index,
                by_name_path,
                imports_by_file,
                symbols_by_id,
                repo_stems,
            )

        edges: dict[tuple[str, str], set[int]] = {}
        pool = ProcessPoolExecutor(
            max_workers=w,
            mp_context=ctx,
            initializer=_init_resolve_worker,
            initargs=(
                index,
                by_name_path,
                imports_by_file,
                repo_stems,
                symbols_by_id,
            ),
        )
        try:
            futures = [
                pool.submit(_resolve_refs_chunk_worker, chunk)
                for chunk in chunks
            ]
            for result in _run_pool_bounded(pool, futures):
                for key, lines in result.items():
                    edges.setdefault(key, set()).update(lines)
        finally:
            pool.shutdown(wait=False)
        return edges

    edges = run_pooled_with_retry(_run, pool_workers, "reference resolution")

    edge_list = [
        Edge(caller=c, callee=e, lines=sorted(lines))
        for (c, e), lines in sorted(edges.items())
    ]
    referenced_in: dict[str, list[str]] = {}
    referenced_out: dict[str, list[str]] = {}
    for edge in edge_list:
        referenced_out.setdefault(edge.caller, []).append(edge.callee)
        referenced_in.setdefault(edge.callee, []).append(edge.caller)
    for table in (referenced_in, referenced_out):
        for key in table:
            table[key] = sorted(set(table[key]))
    return edge_list, referenced_in, referenced_out


def _resolve_ref(
    ref: RawRef,
    index: dict[str, list[Symbol]],
    by_name_path: dict[tuple[str, str], list[Symbol]],
    file_imports: dict[str, Import],
    symbols_by_id: dict[str, Symbol],
    edges: dict[tuple[str, str], set[int]],
    repo_stems: set[str],
    file_exports: bool = False,
) -> None:
    """Resolve one reference; ambiguous/unmatched refs are dropped.

    Unlike ``_resolve_call``, there is no ``ambiguous``/``external``
    bucket for references — an unresolved reference (no candidates,
    or more than one with no disambiguating signal) simply contributes
    no edge, mirroring how a call with no in-repo candidates already
    falls through to ``external`` with nothing further tracked.
    """
    caller_id = ref.caller_id or f"{ref.path}{MODULE_CALLER_SUFFIX}"
    candidates = index.get(ref.name, [])
    if ref.bound is not None:
        # The identifier names a parameter or a local (round 32 Track
        # 5b). Not the candidate pre-filter ``_pick_candidate`` warns
        # about: nothing is being narrowed, the reference itself is
        # impossible, so dropping it can only ever remove an edge.
        fixture = _fixture_param_target(ref, candidates)
        if fixture is not None and fixture.id != caller_id:
            edges.setdefault((caller_id, fixture.id), set()).add(ref.line)
        return
    if not candidates:
        alias = _alias_candidates(ref, file_imports, index)
        if len(alias) == 1 and alias[0].id != caller_id:
            edges.setdefault((caller_id, alias[0].id), set()).add(ref.line)
        return
    same_file = by_name_path.get((ref.name, ref.path), [])
    target = _pick_candidate(
        ref,
        candidates,
        same_file,
        file_imports,
        symbols_by_id.get(ref.caller_id or ""),
        by_name_path,
        index,
    )
    if (
        target is not None
        and target.id != caller_id
        and _ref_target_visible(
            ref, target, file_imports, repo_stems, file_exports
        )
    ):
        edges.setdefault((caller_id, target.id), set()).add(ref.line)


_CONFTEST = "conftest.py"


def _fixture_param_target(
    ref: RawRef, candidates: list[Symbol]
) -> Symbol | None:
    """The pytest fixture a bound parameter stands for, if any.

    The one bound reference that still earns an edge. ``def
    test_x(short_root): run(short_root)`` shadows the ``short_root``
    fixture function lexically, and *is* that fixture semantically:
    pytest injects by parameter name. 102 of the 119 shadowed
    reference sites in dekko's own repo are this shape, and without
    the edge a fixture that is passed along but never called in the
    test is invisible to ``affected`` and ``query uses``.

    Same file first, then the ``conftest.py`` in the nearest ancestor
    directory, which is the only cross-file name Python sees without
    an import (so this path deliberately skips
    ``_ref_target_visible``, whose Python rule cut 47 such edges in
    0.43.69). ``decorated`` means *any* decorator, not
    ``@pytest.fixture`` specifically; inside a test file, against a
    parameter of the same name, that is close enough. Two candidates
    at the same distance: no edge.
    """
    if ref.bound != "param" or not is_test_path(ref.path):
        return None
    fixtures = [
        c
        for c in candidates
        if c.language == "python" and c.kind == "function" and c.decorated
    ]
    same_file = [c for c in fixtures if c.path == ref.path]
    if same_file:
        return same_file[0] if len(same_file) == 1 else None
    here = PurePosixPath(ref.path).parent
    for directory in (here, *here.parents):
        found = [c for c in fixtures if c.path == str(directory / _CONFTEST)]
        if found:
            return found[0] if len(found) == 1 else None

    return None


# Languages whose references are bare *value* identifiers, the only
# kind a local binding can shadow. Java's references are syntactic
# ``Type::method`` and Go's are type identifiers: a wrong edge there is
# an ordinary name collision, not this bug, and is left to the ladder.
_REF_VISIBILITY_LANGUAGES = frozenset(
    {"python", "javascript", "typescript", "tsx"}
)
_JS_FAMILY = _LANGUAGE_FAMILIES["javascript"]


def _ref_target_visible(
    ref: RawRef,
    target: Symbol,
    file_imports: dict[str, Import],
    repo_stems: set[str],
    file_exports: bool = False,
) -> bool:
    """Whether the file holding ``ref`` could name ``target`` at all.

    Round 32 Track 5. The ladder ``_resolve_ref`` shares with calls
    ends in name-only rungs (sole candidate, last resort). For a call
    that is a fair guess: ``count(x)`` on a local is rare. For a bare
    value identifier it is not: ``const count = ...; if (count >= 3)``
    is every other line, and each one became a reference edge to
    whichever unrelated ``count`` the repo happened to define once.
    Measured: 35% of claude-code's reference edges and 57% of cline's
    joined files that cannot see each other (``error``, ``c``,
    ``value``, ``sessionId``). ``unused`` spared dead code because of
    them and ``query uses`` listed them.

    In these languages a cross-file name has to be brought into scope,
    so an edge to another file needs an import binding that name, and
    that import has to point into this repo. A *veto on the result*,
    not a pre-filter on the candidates, for the reason
    ``_pick_candidate`` gives: narrowing a list can turn
    "honestly ambiguous" into "confidently guessed"; a veto can only
    ever remove an edge.

    Errs toward keeping the edge wherever the import table can't be
    trusted to be complete:

    - A JS-family file with no recorded import **and no exported
      symbol** (``file_exports``). That is a script: it shares one
      global scope with every other script, which is also exactly how
      TypeScript treats a file with neither ``import`` nor ``export``.
      A file that exports something is a module and shares nothing, so
      a free ``process`` or ``performance`` in it is the runtime
      global, not ``cronScheduler.ts::process`` (Track 5b: 33 such
      sites on claude-code, 49 on cline).
    - A target declared in a ``.d.ts``: ambient types are global.

    Not this function's job: a local that shadows a *same-file* or
    an *imported* symbol (claude-code ``utils/ide.ts`` imports
    ``errorMessage`` and rebinds it in a catch block), or any local in
    a zero-import file. Those never get here since Track 5b: the
    extractor tags them (``RawRef.bound``) and ``_resolve_ref`` drops
    them before the ladder runs.
    """
    if target.path == ref.path or target.language not in (
        _REF_VISIBILITY_LANGUAGES
    ):
        return True
    imp = file_imports.get(ref.name)
    if imp is not None:
        # Imported, but from where? ``import fs from "node:fs"`` then
        # ``fs.mkdtempSync(..)`` is the external ``fs``, whatever some
        # test file's ``const fs = ...`` happens to be called. Calls
        # have had this guard since ``_shadowed_by_external_import``;
        # references never did, and capturing ``x.prop`` reads made it
        # matter (cline: ``fs``, ``os``, vitest's ``expect``).
        return _import_is_in_repo(imp, repo_stems)
    if target.language in _JS_FAMILY:
        is_script = not file_imports and not file_exports
        return is_script or target.path.endswith(".d.ts")

    return False


def resolve_heritage(
    files: list[FileMap],
    workspace_pkgs: dict[str, str] | None = None,
) -> tuple[
    list[HeritageEdge],
    dict[str, list[str]],
    dict[str, list[str]],
    list[tuple[str, str, list[str]]],
    list[ExternalCall],
    int,
    int,
]:
    """Resolve every heritage clause across the repo into a heritage graph.

    Reuses the exact same candidate ladder ``resolve()`` runs for
    calls (``_pick_candidate``, via ``_resolve_one_heritage``) after
    pre-filtering candidates to ``TYPE_KINDS`` — a base-class name
    resolving to a same-named function would be a bug, not an edge.
    Lands each clause in one of the same three buckets ``resolve()``
    uses for calls (resolved/ambiguous/external), the shape
    ``CallGraph.heritage``/``heritage_ambiguous``/``heritage_external``
    need — unlike ``resolve_refs()``, which only ever produces resolved
    edges (a heritage clause naming an out-of-repo framework base class,
    e.g. Python's ``class MyModel(BaseModel):`` from pydantic, is a
    common, expected case worth surfacing, not one to silently drop).

    ``caller=None`` is passed to ``_pick_candidate`` throughout: a
    heritage clause has no enclosing function body the way a call or
    reference does, so the self/this-container and typed-parameter
    ladder steps (which both require a non-``None`` caller) are inert
    here and simply no-op, letting the remaining steps (receiver-type,
    same-file, import hints, the noise guard, and the fallbacks) run
    unmodified.

    Also builds ``crate_roots`` (``_rust_crate_roots_index_all``) once,
    repo-wide, and threads it through every ``_resolve_one_heritage``
    call so ``_import_match``'s Rust crate-aware fallback step (see
    ``_rust_crate_hint_matches``) can resolve a heritage clause naming
    a crate-root re-exported trait/type against a same-named
    repo-wide collision — round 22 zed.md §3.2 (``impl Render for
    Editor`` previously fell through to ``heritage_ambiguous`` because
    ``Render``'s own declaring file, ``element.rs``, is never a
    segment of its ``use gpui::Render;`` import source). Round 23
    (``.features/plans/round23/
    09-subtypes-ambiguous-resolution-rate.md``) added two things: Fix
    A, a ``call.receiver``-as-crate-hint step in ``_import_match`` for
    the fully-qualified ``impl gpui::Render for X`` spelling, which has
    no ``use`` binding for either ``Render`` or ``gpui`` to build a
    hint from otherwise (see ``_rust_receiver_crate_match``); and Fix
    B, switching this from the single-root ``_rust_crate_roots_index``
    to the collision-aware ``_rust_crate_roots_index_all`` (one crate
    name can map to multiple root directories, e.g. a real crate plus
    a same-named test-fixture directory) after live measurement
    against zed showed the single-root version was a genuine 50/50
    coin flip between "every affected clause resolves correctly" and
    "every affected clause silently resolves to the wrong crate's
    same-named symbol" -- see ``_rust_crate_hint_matches``'s docstring
    and the design doc's "Implemented" note for the numbers.

    Args:
        files: Per-file extraction results.
        workspace_pkgs: JS/TS workspace package name → directory (see
            ``load_workspace_packages``), or ``None``. Without it, a
            clause whose base is imported by package name (``import
            type { ApiHandler } from "@cline/llms"``) is misfiled as
            external -- round 31 cline.md §4.1 Bug A.

    Returns:
        ``(heritage_edges, heritage_out, heritage_in,
        heritage_ambiguous, heritage_external,
        synthetic_tiebreak_count, unplaced_subtype_count)`` — the
        first five are the same shapes ``resolve()`` assigns onto
        ``CallGraph.heritage``/``heritage_out``/``heritage_in``/
        ``heritage_ambiguous``/``heritage_external``. Built as a plain
        tuple return (mirroring ``resolve_refs()``'s own return shape)
        rather than a ``CallGraph`` method, since ``resolve()`` just
        assigns the pieces onto the graph it already built, exactly as
        it already does for ``resolve_refs()``'s result.
        ``synthetic_tiebreak_count`` (round 24, ``.features/plans/
        round24/03-heritage-crate-decoy-tiebreak.md``) is how many of
        the resolved edges above were resolved via
        ``_prefer_non_synthetic_crate_match`` rather than an
        unambiguous structural match — a convention-based guess about
        which of two same-named crates is "the real one," surfaced to
        ``CallGraph.heritage_synthetic_tiebreak_count`` so ``query
        subtypes``/``supertypes`` can disclose it rather than blending
        it silently into every other, more certain resolution.
        ``unplaced_subtype_count`` (round 31 A3) is how many clauses
        with an empty ``subtype_id`` (see ``RawHeritage.subtype_name``)
        were dropped because ``_resolve_heritage_subtype_id`` found
        zero or 2+ same-crate candidates for the written type name —
        surfaced the same way, so a later round can see how many were
        genuinely unplaceable rather than the count silently vanishing
        into "clause never happened."
    """
    index = _build_index(files)
    by_name_path = _build_name_path_index(files)
    imports_by_file = _imports_by_file(files, workspace_pkgs)
    repo_stems = {_repo_stem(PurePosixPath(fm.path)) for fm in files}
    crate_roots = _rust_crate_roots_index_all(
        frozenset(fm.path for fm in files)
    )
    tiebreak_hits = [0]
    unplaced_subtype_count = 0

    edges: dict[tuple[str, str], set[int]] = {}
    relations: dict[tuple[str, str], str] = {}
    ambiguous: dict[tuple[str, str], list[str]] = {}
    external: dict[tuple[str, str], set[int]] = {}
    for fm in files:
        file_imports = imports_by_file.get(fm.path, {})
        raw_imports = (
            fm.imports if fm.language in _WHOLE_FILE_IMPORT_LANGUAGES else None
        )
        for h in fm.heritage:
            if not h.subtype_id:
                subtype_id = _resolve_heritage_subtype_id(h, index)
                if subtype_id is None:
                    unplaced_subtype_count += 1
                    continue
                h = replace(h, subtype_id=subtype_id)
            _resolve_one_heritage(
                h,
                index,
                by_name_path,
                file_imports,
                repo_stems,
                edges,
                relations,
                ambiguous,
                external,
                raw_imports=raw_imports,
                crate_roots=crate_roots,
                tiebreak_hits=tiebreak_hits,
            )

    heritage_edges = [
        HeritageEdge(
            subtype=s,
            supertype=t,
            relation=relations[(s, t)],
            lines=sorted(lns),
        )
        for (s, t), lns in sorted(edges.items())
    ]
    heritage_out: dict[str, list[str]] = {}
    heritage_in: dict[str, list[str]] = {}
    for edge in heritage_edges:
        heritage_out.setdefault(edge.subtype, []).append(edge.supertype)
        heritage_in.setdefault(edge.supertype, []).append(edge.subtype)
    for table in (heritage_out, heritage_in):
        for key in table:
            table[key] = sorted(set(table[key]))
    heritage_ambiguous = [
        (subtype, name, cands)
        for (subtype, name), cands in sorted(ambiguous.items())
    ]
    heritage_external = [
        ExternalCall(caller=s, callee=t, lines=sorted(lns))
        for (s, t), lns in sorted(external.items())
    ]
    return (
        heritage_edges,
        heritage_out,
        heritage_in,
        heritage_ambiguous,
        heritage_external,
        tiebreak_hits[0],
        unplaced_subtype_count,
    )


def _resolve_heritage_subtype_id(
    h: RawHeritage, index: dict[str, list[Symbol]]
) -> str | None:
    """Resolve a cross-file Rust ``impl`` clause's own subject symbol.

    Round 31 zed coverage pass F2/A3: ``extractor._heritage_rust_impl``
    emits a clause with ``subtype_id=""`` and ``subtype_name`` set
    when the implementing type isn't defined in the same file as the
    ``impl`` block — an ordinary Rust layout
    (``crates/search/src/text_finder/render.rs: impl Render for
    TextFinder``, the ``struct TextFinder`` itself living in
    ``text_finder.rs``), not a rare one.

    Narrowed to the clause's own crate (``_rust_crate_dir``), matching
    this resolver's existing Rust crate-scoping convention elsewhere
    in this module (``_owned_by_receiver_type``, the crate-decoy
    tiebreaks): an unqualified struct name repeats across zed's own
    crates often enough (several same-named ``Editor``, ``View``, ...)
    that ignoring crate boundaries here would trade one guess for
    another, not remove the guess.

    Args:
        h: A heritage clause with an empty ``subtype_id`` and a
            non-empty ``subtype_name``.
        index: Bare symbol name to every symbol sharing it.

    Returns:
        The unique matching symbol's id, or ``None`` when zero or 2+
        same-crate ``TYPE_KINDS`` symbols share ``h.subtype_name`` —
        the caller counts this rather than guessing (see
        ``resolve_heritage``'s ``unplaced_subtype_count``).
    """
    own_crate = _rust_crate_dir(h.path)
    # A ``type`` alias is never the placement (round 31 F6b started
    # indexing them). ``subtype_name`` is the bare last segment, so
    # zed's ``impl<T> TideResultExt for tide::Result<T>`` would land on
    # collab's own unrelated ``pub type Result<T, E = Error>``, the
    # crate's only ``Result``. An alias names someone else's type by
    # definition; the impl belongs to that type, not to the alias.
    matches = [
        sym
        for sym in index.get(h.subtype_name, [])
        if sym.kind in TYPE_KINDS
        and sym.kind != "type_alias"
        and _rust_crate_dir(sym.path) == own_crate
    ]
    return matches[0].id if len(matches) == 1 else None


def _narrow_impl_candidates_to_traits(
    candidates: list[Symbol],
) -> list[Symbol]:
    """Narrow ``impl Trait for Type`` candidates to trait-kind only.

    Round 31 zed coverage pass F8: heritage candidates were filtered
    only to ``TYPE_KINDS`` (every type kind), but a Rust ``impl X for
    Y`` clause's ``X`` can only ever name a *trait* — a same-named
    struct/enum/other type is never a legal candidate (``impl
    <struct> for Y`` doesn't compile). 66 of zed's 86
    ``heritage_ambiguous`` entries had exactly one trait candidate
    alongside an unrelated same-named struct (``Component`` the trait
    vs. ``extension_api::Component`` the struct, line 364) — the
    struct half of every one of those pairs was pure noise dragging a
    resolvable clause into "ambiguous."

    Same shape as ``query._sole_type_candidate`` (round 31 P3.4),
    applied here at resolve time instead of at query time: only
    narrows when doing so leaves at least one candidate (rule 0.3 in
    the round 31 fix design — "no evidence is not negative evidence").
    A clause whose name matches *no* trait at all keeps its full,
    unnarrowed candidate list — it may still resolve some other way
    (same-file, import hint) that this kind filter alone can't rule
    out with certainty, and an empty result here is not the "no
    candidate can possibly be the target" proof the ``Type::name``
    owner rule gets to make.

    Args:
        candidates: Already ``TYPE_KINDS``-filtered same-named
            symbols (either the repo-wide index lookup or the
            same-file lookup — both call sites need this identically).

    Returns:
        Only the ``trait``-kind entries of ``candidates``, or, when
        none are traits, ``candidates`` minus any Rust ``type_alias``.
    """
    traits = [c for c in candidates if c.kind == "trait"]
    if traits:
        return traits

    # No trait by that name. The "keep everything" allowance above is
    # for kinds that *might* be the target; a Rust ``type`` alias never
    # is (``impl Alias for Y`` doesn't compile, trait aliases being
    # unstable). Round 31 F6b started indexing those aliases, and
    # without this veto zed's ``impl ActionHandler for
    # A11yActionHandler`` (accesskit's trait) resolved to ``ui``'s
    # unrelated ``type ActionHandler = Box<dyn Fn(..)>``, its only
    # same-named in-repo symbol. Path-gated since TS reuses the
    # ``impl`` relation, and ``implements`` of a type alias is legal.
    return [
        c
        for c in candidates
        if not (c.kind == "type_alias" and c.path.endswith(".rs"))
    ]


def _resolve_one_heritage(
    h: RawHeritage,
    index: dict[str, list[Symbol]],
    by_name_path: dict[tuple[str, str], list[Symbol]],
    file_imports: dict[str, Import],
    repo_stems: set[str],
    edges: dict[tuple[str, str], set[int]],
    relations: dict[tuple[str, str], str],
    ambiguous: dict[tuple[str, str], list[str]],
    external: dict[tuple[str, str], set[int]],
    raw_imports: list[Import] | None = None,
    crate_roots: dict[str, list[str]] | None = None,
    tiebreak_hits: list[int] | None = None,
) -> None:
    """Resolve one heritage clause; mirrors ``_resolve_call``'s shape.

    Candidates are pre-filtered to ``TYPE_KINDS`` at every step (the
    bare-name index lookup, the same-file lookup, and the alias-import
    recovery) before ``_pick_candidate``'s ladder runs.

    ``raw_imports``, when given, is the declaring file's full
    (undeduped) import list for whole-file-include languages (C/C++)
    -- mirrors ``_resolve_files_chunk``'s call-resolution path. Without
    it, ``_pick_candidate``'s ``_import_match`` step has no way to
    disambiguate a same-named C/C++ heritage base via "which one does
    this file's own ``#include`` list actually pull in," which is the
    *only* signal available for that language pair (round 22
    tensorflow.md §5: ``resolve_heritage`` never built or threaded this
    at all, unlike ``_resolve_files_chunk``, losing 828 of ~829 real
    ``OpKernel`` subtype edges to ``ambiguous``).

    ``crate_roots``, when given, is the repo-wide Rust crate-name to
    every matching crate-root-directory index
    (``_rust_crate_roots_index_all``, built once by
    ``resolve_heritage()``), passed straight through to
    ``_pick_candidate``'s own ``crate_roots`` parameter -- see that
    docstring and ``_rust_crate_hint_matches`` for what it fixes
    (round 22 zed.md §3.2: a crate-root re-exported Rust trait
    colliding with a same-named type elsewhere in the repo).

    ``tiebreak_hits``, when given, is passed straight through to
    ``_pick_candidate``'s own parameter of the same name (round 24
    heritage crate-decoy tiebreak) so ``resolve_heritage()`` can count
    how many resolved edges in this repo rest on that convention-based
    guess.
    """
    if _receiver_is_external(h, file_imports, repo_stems):
        external.setdefault((h.subtype_id, h.text), set()).add(h.line)
        return

    candidates = [c for c in index.get(h.name, []) if c.kind in TYPE_KINDS]
    if h.relation == "impl":
        candidates = _narrow_impl_candidates_to_traits(candidates)
    if not candidates:
        alias = [
            c
            for c in _alias_candidates(h, file_imports, index)
            if c.kind in TYPE_KINDS
        ]
        if len(alias) == 1:
            _add_heritage_edge(h, alias[0].id, edges, relations)
            return
        if len(alias) > 1:
            _record_ambiguous(h.subtype_id, h.name, alias, ambiguous)
            return
        external.setdefault((h.subtype_id, h.text), set()).add(h.line)
        return

    same_file = [
        c
        for c in by_name_path.get((h.name, h.path), [])
        if c.kind in TYPE_KINDS
    ]
    if h.relation == "impl":
        same_file = _narrow_impl_candidates_to_traits(same_file)
    target = _pick_candidate(
        h,
        candidates,
        same_file,
        file_imports,
        None,
        by_name_path,
        index,
        repo_stems,
        raw_imports,
        crate_roots,
        tiebreak_hits,
    )
    if target is _NOISE:
        external.setdefault((h.subtype_id, h.text), set()).add(h.line)
        return
    if target is not None:
        _add_heritage_edge(h, target.id, edges, relations)
        return
    decoy_free = _hintless_decoy_tiebreak(h, candidates, tiebreak_hits)
    if decoy_free is not None:
        _add_heritage_edge(h, decoy_free.id, edges, relations)
        return
    _record_ambiguous(h.subtype_id, h.name, candidates, ambiguous)


def _hintless_decoy_tiebreak(
    h: RawHeritage,
    candidates: list[Symbol],
    tiebreak_hits: list[int] | None,
) -> Symbol | None:
    """Last-resort Rust fixture-decoy tiebreak for a clause with no hint.

    Round 24's ``_prefer_non_synthetic_crate_match`` only ever ran
    inside the crate-hint steps, i.e. when the file names the crate
    (``use gpui::Render;`` / ``impl gpui::Render for X``). Round 31
    zed.md measured what that leaves behind on its own motivating
    example: 174 of 358 ``impl Render for`` clauses resolved, 184
    ambiguous -- and **every one of the 184 had the identical two
    candidates**, the real ``crates/gpui/src/element.rs::Render`` and
    the ``tooling/lints/test_fixture/gpui`` stand-in. Those files reach
    ``Render`` through a glob (``use ui::prelude::*;``, 165 of them) or
    a re-exporting crate (``use ui::Render;``, 19), so there is no
    crate name to build a hint from, and there never will be short of
    tracing glob re-exports.

    The same convention answers it without a hint: real code does not
    implement a test fixture's stand-in trait. Reuses the round-24
    function whole, so its guarantees carry over unchanged -- a clause
    written *inside* the fixture crate resolves to the fixture's own
    trait (the structural self-crate check, tried first), exactly one
    non-synthetic survivor is required, and every edge resolved this
    way is counted in ``tiebreak_hits`` and disclosed by ``query
    subtypes``/``supertypes`` as resting on a convention rather than a
    structural match. Two real crates defining the same trait name
    stay ambiguous.

    Rust only: the crate-directory notion the tiebreak reasons about
    (``_rust_crate_dir``) is Rust-shaped.
    """
    if not h.path.endswith(".rs"):
        return None
    if _looks_like_synthetic_crate_root(_rust_crate_dir(h.path)):
        # The clause itself lives in a fixture/vendor tree. "Real code
        # doesn't implement a fixture's trait" says nothing about
        # *fixture* code: zed's ``test_fixture/render_consumer`` crate
        # depends on the sibling ``test_fixture/gpui`` stand-in, not on
        # the real gpui, and a different crate is not something the
        # self-crate check can see. Live-testing caught 8 such edges
        # pointed at the real trait. Stay ambiguous.
        return None
    same_language = _language_filtered(h, candidates)
    if len(same_language) < 2:
        return None
    if _nearer_to_a_decoy(h.path, same_language):
        return None
    return _prefer_non_synthetic_crate_match(
        same_language, h.path, tiebreak_hits
    )


def _nearer_to_a_decoy(path: str, candidates: list[Symbol]) -> bool:
    """Whether ``path`` sits closer to a fixture candidate than a real one.

    The hintless tiebreak's premise is "real code doesn't implement a
    fixture's trait". Code that lives *beside* a fixture is the
    exception, and a path marker can't see it: zed's dylint UI tests
    (``tooling/lints/ui/*.rs``) carry no ``test_fixture`` segment, yet
    ``tooling/lints/src/lib.rs`` compiles them with
    ``--extern=gpui=<fixture rlib>``, so their ``use gpui::*;`` is the
    stand-in. Round 31's zed coverage pass caught 7 such edges this
    tiebreak had newly pointed at the real trait (they were honestly
    ambiguous before it existed). The build flag is unknowable
    statically; shared directory depth is a usable proxy.
    ``tooling/lints/ui/x.rs`` shares ``tooling/lints`` with the decoy
    and nothing with ``crates/gpui``, while a real consumer
    (``crates/editor/...``) shares ``crates`` with the real crate and
    nothing with the decoy. Ties go to resolving.
    """

    def shared(other: str) -> int:
        depth = 0
        for a, b in zip(path.split("/")[:-1], other.split("/")[:-1]):
            if a != b:
                break
            depth += 1
        return depth

    decoys = [
        shared(c.path)
        for c in candidates
        if _looks_like_synthetic_crate_root(_rust_crate_dir(c.path))
    ]
    real = [
        shared(c.path)
        for c in candidates
        if not _looks_like_synthetic_crate_root(_rust_crate_dir(c.path))
    ]
    if not decoys or not real:
        return False
    return max(decoys) > max(real)


def _add_heritage_edge(
    h: RawHeritage,
    target_id: str,
    edges: dict[tuple[str, str], set[int]],
    relations: dict[tuple[str, str], str],
) -> None:
    """Record one resolved heritage edge, skipping a self-loop.

    A type can never be its own supertype (the extractor never emits
    that shape), but this mirrors ``_add_edge``'s self-recursion guard
    for defense in depth.
    """
    if target_id == h.subtype_id:
        return
    key = (h.subtype_id, target_id)
    edges.setdefault(key, set()).add(h.line)
    relations.setdefault(key, h.relation)


# ---------------------------------------------------------------------
# Throws/catches (exception/error-flow tracing)
#
# A deliberately lighter-weight resolution than ``resolve_heritage()``'s
# full ``_pick_candidate`` ladder: the overwhelmingly common case is a
# raised/caught type that was never extracted as a repo ``Symbol`` at
# all (``ValueError``, ``IOException``, ``std::runtime_error``), so
# ``_resolve_type_name`` only tries the cheap, high-confidence steps
# (unique repo-wide name, same-file, import hint) before giving up as
# "external" — a design choice, not a shortcut (see the design doc's
# "Resolution" section).


def _resolve_type_name(
    name: str,
    path: str,
    index: dict[str, list[Symbol]],
    by_name_path: dict[tuple[str, str], list[Symbol]],
    file_imports: dict[str, Import],
) -> tuple[Symbol | None, list[Symbol]]:
    """Resolve a raised/caught type name to a repo ``TYPE_KINDS`` symbol.

    Args:
        name: Bare raised/caught type name, as written.
        path: File the raise/throw or catch clause appears in.
        index: Bare name → symbols (see ``_build_index``).
        by_name_path: ``(name, path)`` → same-file symbols (see
            ``_build_name_path_index``).
        file_imports: This file's local name → import record.

    Returns:
        ``(resolved, candidates)`` — ``resolved`` is the unique
        ``TYPE_KINDS``-filtered match, or ``None`` when the name is
        external (``candidates`` empty — the common case) or
        genuinely ambiguous (``candidates`` has 2+ entries, a real
        same-name-in-two-files collision).
    """
    candidates = [c for c in index.get(name, []) if c.kind in TYPE_KINDS]
    if not candidates:
        return None, []
    if len(candidates) == 1:
        return candidates[0], candidates
    same_file = [
        c for c in by_name_path.get((name, path), []) if c.kind in TYPE_KINDS
    ]
    if len(same_file) == 1:
        return same_file[0], candidates
    imp = file_imports.get(name)
    if imp is not None:
        hinted = [c for c in candidates if _module_matches(imp.source, c.path)]
        if len(hinted) == 1:
            return hinted[0], candidates
    return None, candidates


def _resolve_throws_chunk(
    files: list[FileMap],
    index: dict[str, list[Symbol]],
    by_name_path: dict[tuple[str, str], list[Symbol]],
    imports_by_file: dict[str, dict[str, Import]],
) -> tuple[
    dict[tuple[str, str], set[int]],
    dict[tuple[str, str], list[str]],
    dict[tuple[str, str], set[int]],
    list[tuple[str, str, int]],
]:
    """Resolve every raise/throw site in ``files`` into fresh, local
    accumulators — the throw-resolution analog of
    ``_resolve_files_chunk``, same pure-function/worker-safety shape.
    """
    edges: dict[tuple[str, str], set[int]] = {}
    ambiguous: dict[tuple[str, str], list[str]] = {}
    external: dict[tuple[str, str], set[int]] = {}
    bare: list[tuple[str, str, int]] = []

    for fm in files:
        file_imports = imports_by_file.get(fm.path, {})
        for t in fm.throws:
            caller_id = t.caller_id or f"{t.path}{MODULE_CALLER_SUFFIX}"
            if t.name is None:
                bare.append((caller_id, t.path, t.line))
                continue
            target, candidates = _resolve_type_name(
                t.name, t.path, index, by_name_path, file_imports
            )
            if target is not None:
                if target.id != caller_id:
                    edges.setdefault((caller_id, target.id), set()).add(t.line)
                continue
            if len(candidates) >= 2:
                _record_ambiguous(caller_id, t.name, candidates, ambiguous)
                continue
            external.setdefault((caller_id, t.text or t.name), set()).add(
                t.line
            )

    return edges, ambiguous, external, bare


def _resolve_throws_chunk_worker(
    files: list[FileMap],
) -> tuple[
    dict[tuple[str, str], set[int]],
    dict[tuple[str, str], list[str]],
    dict[tuple[str, str], set[int]],
    list[tuple[str, str, int]],
]:
    """Per-task pool entry point for oversubscribed throw resolution --
    reads the shared indices ``_init_resolve_worker`` stashed in this
    worker process's globals, the throw-resolution analog of
    ``_resolve_files_chunk_worker``."""
    assert _worker_index is not None  # initializer always runs first
    return _resolve_throws_chunk(
        files, _worker_index, _worker_by_name_path, _worker_imports_by_file
    )


def resolve_throws(
    files: list[FileMap], workers: int = 1
) -> tuple[
    list[ThrowEdge],
    dict[str, list[str]],
    list[tuple[str, str, list[str]]],
    list[ExternalCall],
    list[tuple[str, str, int]],
]:
    """Resolve every raise/throw site across the repo.

    Args:
        files: Per-file extraction results.
        workers: Worker count for parallel resolution (1 = sequential,
            the default). Mirrors ``resolve_refs``'s own ``workers``
            parameter and ``_resolve_all``'s chunking/threshold shape.
            A ``BrokenProcessPool`` on the parallel path gets one
            bounded retry at reduced parallelism via
            ``run_pooled_with_retry`` before propagating.

    Returns:
        ``(throws, throws_out, throws_ambiguous, throws_external,
        throws_bare)`` — the shapes ``resolve()`` assigns onto
        ``CallGraph.throws``/``throws_out``/``throws_ambiguous``/
        ``throws_external``/``throws_bare``.
    """
    index = _build_index(files)
    by_name_path = _build_name_path_index(files)
    imports_by_file = _imports_by_file(files)

    total_throws = sum(len(fm.throws) for fm in files)
    pool_workers = _pool_workers(workers, total_throws)
    use_pool = pool_workers > 1

    def _run(
        w: int,
        ctx: BaseContext,
    ) -> tuple[
        dict[tuple[str, str], set[int]],
        dict[tuple[str, str], list[str]],
        dict[tuple[str, str], set[int]],
        list[tuple[str, str, int]],
    ]:
        chunks = (
            _chunk_files(files, w * _RESOLVE_CHUNK_OVERSUBSCRIPTION)
            if use_pool
            else [files]
        )
        if len(chunks) < 2:
            return _resolve_throws_chunk(
                files, index, by_name_path, imports_by_file
            )

        edges: dict[tuple[str, str], set[int]] = {}
        ambiguous: dict[tuple[str, str], list[str]] = {}
        external: dict[tuple[str, str], set[int]] = {}
        bare: list[tuple[str, str, int]] = []
        pool = ProcessPoolExecutor(
            max_workers=w,
            mp_context=ctx,
            initializer=_init_resolve_worker,
            initargs=(index, by_name_path, imports_by_file, None, None),
        )
        try:
            futures = [
                pool.submit(_resolve_throws_chunk_worker, chunk)
                for chunk in chunks
            ]
            for c_edges, c_ambiguous, c_external, c_bare in _run_pool_bounded(
                pool, futures
            ):
                for key, lines in c_edges.items():
                    edges.setdefault(key, set()).update(lines)
                for key, cands in c_ambiguous.items():
                    ambiguous.setdefault(key, cands)
                for key, lines in c_external.items():
                    external.setdefault(key, set()).update(lines)
                bare.extend(c_bare)
        finally:
            pool.shutdown(wait=False)
        return edges, ambiguous, external, bare

    edges, ambiguous, external, bare = run_pooled_with_retry(
        _run, pool_workers, "throw resolution"
    )

    throw_edges = [
        ThrowEdge(caller=c, type=ty, lines=sorted(lns))
        for (c, ty), lns in sorted(edges.items())
    ]
    throws_out: dict[str, list[str]] = {}
    for edge in throw_edges:
        throws_out.setdefault(edge.caller, []).append(edge.type)
    for key in throws_out:
        throws_out[key] = sorted(set(throws_out[key]))
    throws_ambiguous = [
        (caller, name, cands)
        for (caller, name), cands in sorted(ambiguous.items())
    ]
    throws_external = [
        ExternalCall(caller=c, callee=t, lines=sorted(lns))
        for (c, t), lns in sorted(external.items())
    ]
    return (
        throw_edges,
        throws_out,
        throws_ambiguous,
        throws_external,
        sorted(bare),
    )


def _resolve_catches_chunk(
    files: list[FileMap],
    index: dict[str, list[Symbol]],
    by_name_path: dict[tuple[str, str], list[Symbol]],
    imports_by_file: dict[str, dict[str, Import]],
) -> list[CatchSite]:
    """Resolve every except/catch clause in ``files`` into a fresh,
    local list — the catch-resolution analog of ``_resolve_refs_chunk``,
    same pure-function/worker-safety shape."""
    sites: list[CatchSite] = []
    for fm in files:
        file_imports = imports_by_file.get(fm.path, {})
        for c in fm.catches:
            caller_id = c.caller_id or f"{c.path}{MODULE_CALLER_SUFFIX}"
            repo_types: dict[str, str] = {}
            for name in c.types:
                target, _candidates = _resolve_type_name(
                    name, c.path, index, by_name_path, file_imports
                )
                if target is not None:
                    repo_types[name] = target.id
            sites.append(
                CatchSite(
                    caller=caller_id,
                    path=c.path,
                    type_names=list(c.types),
                    repo_types=repo_types,
                    bare=c.bare,
                    line=c.line,
                )
            )
    return sites


def _resolve_catches_chunk_worker(files: list[FileMap]) -> list[CatchSite]:
    """Per-task pool entry point for oversubscribed catch resolution --
    reads the shared indices ``_init_resolve_worker`` stashed in this
    worker process's globals, the catch-resolution analog of
    ``_resolve_files_chunk_worker``."""
    assert _worker_index is not None  # initializer always runs first
    return _resolve_catches_chunk(
        files, _worker_index, _worker_by_name_path, _worker_imports_by_file
    )


def resolve_catches(files: list[FileMap], workers: int = 1) -> list[CatchSite]:
    """Resolve every except/catch clause across the repo.

    Unlike ``resolve_throws()``, this produces no separate ambiguous/
    external bucket — a ``dekko query catches Y`` request matches by
    name against ``CatchSite.type_names`` directly (see the design
    doc's "mostly a name-index lookup" note and ``CatchSite``'s own
    docstring), so per-clause resolution only matters for
    ``repo_types``' summary-disclosure role, not for query
    correctness.

    Args:
        files: Per-file extraction results.
        workers: Worker count for parallel resolution (1 = sequential,
            the default). Mirrors ``resolve_throws``'s own ``workers``
            parameter and ``_resolve_all``'s chunking/threshold shape.
            A ``BrokenProcessPool`` on the parallel path gets one
            bounded retry at reduced parallelism via
            ``run_pooled_with_retry`` before propagating.

    Returns:
        Every clause across the repo as a ``CatchSite``, sorted by
        ``(path, line, caller)``.
    """
    index = _build_index(files)
    by_name_path = _build_name_path_index(files)
    imports_by_file = _imports_by_file(files)

    total_catches = sum(len(fm.catches) for fm in files)
    pool_workers = _pool_workers(workers, total_catches)
    use_pool = pool_workers > 1

    def _run(w: int, ctx: BaseContext) -> list[CatchSite]:
        chunks = (
            _chunk_files(files, w * _RESOLVE_CHUNK_OVERSUBSCRIPTION)
            if use_pool
            else [files]
        )
        if len(chunks) < 2:
            return _resolve_catches_chunk(
                files, index, by_name_path, imports_by_file
            )

        sites: list[CatchSite] = []
        pool = ProcessPoolExecutor(
            max_workers=w,
            mp_context=ctx,
            initializer=_init_resolve_worker,
            initargs=(index, by_name_path, imports_by_file, None, None),
        )
        try:
            futures = [
                pool.submit(_resolve_catches_chunk_worker, chunk)
                for chunk in chunks
            ]
            for chunk_sites in _run_pool_bounded(pool, futures):
                sites.extend(chunk_sites)
        finally:
            pool.shutdown(wait=False)
        return sites

    sites = run_pooled_with_retry(_run, pool_workers, "catch resolution")
    sites.sort(key=lambda s: (s.path, s.line, s.caller))
    return sites


def _resolve_call(
    call: RawCall,
    index: dict[str, list[Symbol]],
    by_name_path: dict[tuple[str, str], list[Symbol]],
    file_imports: dict[str, Import],
    repo_stems: set[str],
    symbols_by_id: dict[str, Symbol],
    edges: dict[tuple[str, str], set[int]],
    ambiguous: dict[tuple[str, str], list[str]],
    external: dict[tuple[str, str], set[int]],
    raw_imports: list[Import] | None = None,
) -> None:
    """Resolve one call and record it in the right bucket."""
    caller_id = call.caller_id or f"{call.path}{MODULE_CALLER_SUFFIX}"
    if _receiver_is_external(call, file_imports, repo_stems):
        external.setdefault((caller_id, call.text), set()).add(call.line)
        return

    candidates = index.get(call.name, [])
    if not candidates:
        alias = _alias_candidates(call, file_imports, index)
        if len(alias) == 1:
            _add_call_and_constructor(
                caller_id, alias[0], call.line, by_name_path, edges
            )
            return
        if len(alias) > 1:
            _record_ambiguous(caller_id, call.name, alias, ambiguous)
            return
        external.setdefault((caller_id, call.text), set()).add(call.line)
        return

    same_file = by_name_path.get((call.name, call.path), [])
    target = _pick_candidate(
        call,
        candidates,
        same_file,
        file_imports,
        symbols_by_id.get(call.caller_id or ""),
        by_name_path,
        index,
        repo_stems,
        raw_imports,
    )
    if target is _NOISE:
        external.setdefault((caller_id, call.text), set()).add(call.line)
        return
    if target is not None:
        _add_call_and_constructor(
            caller_id, target, call.line, by_name_path, edges
        )
        return
    _record_ambiguous(caller_id, call.name, candidates, ambiguous)


def _add_edge(
    caller_id: str,
    target_id: str,
    line: int,
    edges: dict[tuple[str, str], set[int]],
) -> None:
    """Record one resolved call-site line, skipping self-recursion noise."""
    if target_id != caller_id:
        edges.setdefault((caller_id, target_id), set()).add(line)


def _add_call_and_constructor(
    caller_id: str,
    target: Symbol,
    line: int,
    by_name_path: dict[tuple[str, str], list[Symbol]],
    edges: dict[tuple[str, str], set[int]],
) -> None:
    """Record the resolved call edge, plus a constructor edge if any.

    See ``_constructor_of`` (module docstring, bug #2): a call or
    construction that resolves to a class-shaped symbol also counts
    toward that class's own explicit constructor method's fan-in, when
    the language extracted one as its own symbol.
    """
    _add_edge(caller_id, target.id, line, edges)
    ctor = _constructor_of(target, by_name_path)
    if ctor is not None:
        _add_edge(caller_id, ctor.id, line, edges)


def _record_ambiguous(
    caller_id: str,
    name: str,
    candidates: list[Symbol],
    ambiguous: dict[tuple[str, str], list[str]],
) -> None:
    """Record a call/alias name with 2+ same-name candidates.

    Args:
        caller_id: Id of the calling symbol (or a module pseudo-id).
        name: The bare callee name (as written at the call site).
        candidates: The 2+ same-name symbols that could not be
            disambiguated.
        ambiguous: The graph's ambiguous-call accumulator, keyed on
            ``(caller_id, name)``, mutated in place.
    """
    # Candidate lists are presentation data: production code first,
    # test/fixture symbols last.
    ranked = sorted(candidates, key=lambda c: (is_test_path(c.path), c.id))
    ambiguous.setdefault((caller_id, name), [c.id for c in ranked])


def _language_filtered(
    call: _Referable, candidates: list[Symbol]
) -> list[Symbol]:
    """Drop candidates whose language can never be the real target.

    A Python class is never the target of a C++ call site, and vice
    versa — no step later in ``_pick_candidate``'s ladder ever compares
    a candidate's language against the call/heritage site's own, so a
    same-bare-name symbol in a completely unrelated language could
    otherwise win a later, weaker heuristic (round 21 tensorflow.md
    §5: ``errors::InvalidArgumentError`` calls resolving to a same-
    named, unrelated Python class purely because it was the sole
    non-method candidate left once ``_bare_call_non_method_match``
    ran). ``call.path``'s registry language (Tier-1 only — every
    symbol candidate comes from Tier-1 extraction, so a Tier-2/
    unrecognized call-site path has nothing meaningful to compare
    against) is the source of truth for the call site's own language.

    Two-stage narrowing, not a single same-language check: same-
    language candidates win outright when any exist. Otherwise, the
    result narrows to ``_LANGUAGE_FAMILIES``' same-*family* candidates
    — the legitimate cross-language case (a C header declaring
    something only a C++ ``.cc`` implements/calls; a JS/TS/TSX
    toolchain sharing exports across those three extensions) that
    dekko already treats as one interoperating unit elsewhere in this
    module (``_WHOLE_FILE_IMPORT_LANGUAGES``, ``_IMPORT_RESOLVERS``).

    This **can legitimately return an empty list** — round 21's
    residual tensorflow finding (`.features/fixes/resolver-vendored-
    exclusion-false-match.md`): when the real C++ target is itself
    excluded from the index (e.g. it lives under a vendored/excluded
    directory like ``third_party/``) and the only remaining bare-name
    candidate is a same-named symbol in a wholly unrelated language
    family (e.g. Python), that candidate must never win by default —
    there is no legitimate precedent anywhere in the resolver for
    Python answering a C++ call the way C headers answer C++ calls.
    Removing candidates that can never legitimately be the right
    answer must never turn a resolvable call into an unresolvable one
    *when a same-family candidate exists*; a call that can only
    "resolve" via a definitively unrelated language family is
    intentionally downgraded to unresolved/ambiguous (via
    ``_pick_candidate``'s caller falling through to
    ``_record_ambiguous`` with the original, unfiltered candidate
    list) rather than answered wrong. A language with no declared
    family (python, rust, go, java, ...) behaves exactly as before:
    same-language-or-nothing, since its family is itself alone.
    """
    spec = languages.spec_for_path(call.path)
    if spec is None:
        return candidates

    same_language = [c for c in candidates if c.language == spec.name]
    if same_language:
        return same_language

    family = _LANGUAGE_FAMILIES.get(spec.name, frozenset({spec.name}))
    return [c for c in candidates if c.language in family]


# Structural layer 2 (`.features/plans/round25/
# 06-structural-layer2-arity-resolution.md`): the single-candidate
# rung's "nothing else worked, but there's only one name in the whole
# repo, guess it" fast path is itself only as trustworthy as its one
# remaining piece of evidence -- the name. This adds a second,
# structural check next to that name-based guess: does the call
# site's own written argument count even fit the sole candidate's
# declared parameters? A mismatch (spring-boot's `isTrue()` -- 0
# written args -- against a repo-defined 1-required-arg `isTrue`)
# means the "only same name in the repo" candidate can't actually be
# the real target, regardless of how the name-based layer-1 denylist
# (`_is_noise_call`, above) does or doesn't already cover that name.

# The only two Tier-1 languages whose parser puts an implicit receiver
# (Python `self`/`cls`, Rust `self_parameter`) inside a symbol's own
# declared `params` list at all -- every other language either has no
# receiver concept in its param list (Go's receiver is a separate
# definition-query field, never part of `params`) or never implicitly
# supplies one at the call site. A receiver-qualified call's written
# argument count never includes the receiver itself (`obj.method(a)`
# writes one argument, not two), so this leading param must be
# stripped before comparing against `call.arg_count` -- see
# `_candidate_arity`.
_IMPLICIT_RECEIVER_LANGUAGES = frozenset({"python", "rust"})
_PYTHON_RECEIVER_PARAM_NAMES = frozenset({"self", "cls"})

# Python's bare `*`/`/` keyword-only/positional-only separators
# (`def f(a, *, b): ...` / `def f(a, /, b): ...`) surface as ordinary
# `Param` entries from `extractor._params_python` (kept for signature-
# display fidelity), but name no actual parameter and must not count
# toward a candidate's arity range -- excluded by name, since neither
# is a syntactically valid Python parameter name a real parameter
# could collide with.
_ARITY_SYNTAX_MARKER_NAMES = frozenset({"*", "/"})


def _is_receiver_param(param: Param, language: str) -> bool:
    """Whether a candidate's leading declared param is an implicit
    self/cls-shaped receiver for ``language``, not a real argument.

    Args:
        param: The candidate's first declared parameter.
        language: The candidate symbol's ``Symbol.language``.

    Returns:
        True when this param is the language's own implicit-receiver
        shape (Python ``self``/``cls``, Rust ``self_parameter`` in any
        of its ``self``/``&self``/``&mut self``/``mut self`` textual
        forms) and should be excluded from an arity computation for a
        receiver-qualified call.
    """
    if language == "python":
        return param.name in _PYTHON_RECEIVER_PARAM_NAMES
    if language == "rust":
        # The last word, not a strip-the-prefix: a lifetimed receiver
        # (``&'a self``, ``&'a mut self``) used to fall through as an
        # ordinary parameter, which put the method's arity one too
        # high and made ``_sole_candidate_match`` reject a correct
        # lone target (round 32, live-testing on zed:
        # ``syntax_map.layers(&buffer)`` against ``fn layers<'a>(&'a
        # self, buffer: ..)``). Rust reserves ``self`` for the
        # receiver, so a parameter whose name ends in it is one.
        return param.name.lstrip("&").split()[-1:] == ["self"]
    return False


def _param_arity(params: list[Param]) -> tuple[int, int | None]:
    """A declared parameter list's (min, max) plausible argument count.

    Args:
        params: A candidate's declared parameters (already stripped of
            any implicit receiver -- see ``_candidate_arity``).

    Returns:
        ``(min_count, max_count)``. ``max_count`` is ``None`` when any
        parameter is variadic (no upper bound on written arguments).
        A parameter with ``has_default=True`` lowers the minimum
        without affecting the maximum; a plain required parameter
        raises both. Python's bare ``*``/``/`` syntax-marker params
        are excluded entirely (see ``_ARITY_SYNTAX_MARKER_NAMES``).
    """
    relevant = [p for p in params if p.name not in _ARITY_SYNTAX_MARKER_NAMES]
    min_count = sum(
        1 for p in relevant if not p.has_default and not p.variadic
    )
    if any(p.variadic for p in relevant):
        return min_count, None
    return min_count, len(relevant)


def _candidate_arity(
    candidate: Symbol, call: _Referable
) -> tuple[int, int | None]:
    """``candidate``'s (min, max) arity range for ``call``.

    Strips the candidate's own leading receiver-shaped param (Python
    ``self``/``cls``, Rust ``self_parameter``) first whenever ``call``
    is receiver-qualified and the candidate's language is one of the
    two whose parser puts an implicit receiver in the declared param
    list at all -- see ``_IMPLICIT_RECEIVER_LANGUAGES``.

    Args:
        candidate: The sole remaining candidate symbol.
        call: The raw call/reference being resolved.

    Returns:
        The same ``(min_count, max_count)`` shape as ``_param_arity``.
    """
    params = candidate.params
    if (
        getattr(call, "receiver", None)
        and candidate.language in _IMPLICIT_RECEIVER_LANGUAGES
        and params
        and _is_receiver_param(params[0], candidate.language)
    ):
        params = params[1:]
    return _param_arity(params)


def _arity_plausible(candidate: Symbol, call: _Referable) -> bool:
    """Whether ``call``'s written argument count fits ``candidate``.

    The safe default is ``True`` (never suppress) whenever
    ``call.arg_count`` carries no signal -- either because ``call`` is
    a ``RawHeritage`` (no such attribute at all, via ``getattr``'s
    default), a Tier-2/generic-grammar call (``arg_count`` stays
    ``None`` by construction -- see ``RawCall.arg_count``), or a
    Tier-1 args-capture miss. This mirrors this module's "report
    ambiguous rather than guess" philosophy: an uncomputable or
    missing arity signal must never itself become a new false-positive
    suppression.

    Args:
        candidate: The sole remaining candidate symbol.
        call: The raw call/reference/heritage clause being resolved.

    Returns:
        True when ``call.arg_count`` is unavailable, or falls within
        ``candidate``'s computed ``(min, max)`` arity range; False on
        a confirmed mismatch.
    """
    arg_count = getattr(call, "arg_count", None)
    if arg_count is None:
        return True
    min_count, max_count = _candidate_arity(candidate, call)
    if arg_count < min_count:
        return False
    return max_count is None or arg_count <= max_count


class _Noise:
    """Sentinel: ``_pick_candidate`` determined this call is noise
    (``_is_noise_call`` fired), not a genuine multi-candidate collision
    -- distinguishes the two ``None``-shaped outcomes so the caller can
    bucket correctly (round 22 cline.md §3.1: a noise-suppressed call
    with exactly one real candidate used to be recorded as ambiguous,
    identically to a real 2+-candidate collision, since both paths
    returned bare ``None``)."""


_NOISE = _Noise()


def _pick_candidate_ladder(
    call: _Referable,
    candidates: list[Symbol],
    same_file: list[Symbol],
    file_imports: dict[str, Import],
    caller: Symbol | None,
    by_name_path: dict[tuple[str, str], list[Symbol]],
    index: dict[str, list[Symbol]],
    repo_stems: set[str] | None = None,
    raw_imports: list[Import] | None = None,
    crate_roots: dict[str, list[str]] | None = None,
    tiebreak_hits: list[int] | None = None,
) -> Symbol | _Noise | None:
    """Apply the resolution ladder; ``None`` means ambiguous.

    Shared by ``_resolve_call`` and ``_resolve_ref`` — ``call`` is
    either a ``RawCall`` or a ``RawRef``; both expose the same
    ``name``/``receiver`` fields this ladder reads.

    ``same_file`` is the pre-bucketed list of like-named symbols in the
    calling file, so the same-file and container steps avoid rescanning
    every repo-wide candidate for a common name.

    ``index`` is the full bare-name → symbols index, used only by
    ``_receiver_type_match`` to check whether a call's receiver names
    an in-repo type rather than a variable.

    ``repo_stems`` gates the built-in/global-name noise check (see
    ``_is_noise_call``) — ``_resolve_call`` and ``_resolve_one_heritage``
    both pass it (the latter's own candidates are pre-filtered to
    ``TYPE_KINDS``, so the check rarely fires there in practice, but it
    is reachable); ``_resolve_ref`` never does, so ``_resolve_ref``'s
    reference resolution (a separate table from ``calls_in``/fan-in,
    not affected by the bug this check targets) is left byte-for-byte
    unchanged.

    When the noise guard fires, this returns the ``_NOISE`` sentinel
    rather than ``None`` — distinct from "genuinely ambiguous, 2+ real
    candidates with no disambiguating signal" (round 22 cline.md §3.1:
    a noise-suppressed call with exactly *one* real candidate was
    previously indistinguishable from a real ambiguous collision, since
    both returned bare ``None``, so every noise-suppressed call got
    recorded via ``_record_ambiguous`` — contradicting that function's
    own "2+ candidates" docstring and inflating the ambiguous count on
    common built-in method names like ``trim``). Callers that pass a
    non-``None`` ``repo_stems`` must check for ``_NOISE`` before
    treating a non-``None`` return as a resolved ``Symbol``.

    ``raw_imports``, when given, is the calling file's full (undeduped)
    import list — passed only for whole-file-include languages
    (C/C++), and used by ``_import_match``'s fallback step. See
    ``_WHOLE_FILE_IMPORT_LANGUAGES``.

    ``crate_roots``, when given, is the repo-wide Rust crate-name to
    every matching crate-root-directory index
    (``_rust_crate_roots_index_all``), used by ``_import_match``'s
    crate-aware fallback step (see ``_rust_crate_hint_matches``) to
    resolve a crate-root re-exported Rust trait/type against a
    same-named repo-wide collision that its own file-stem can't reach.
    Currently only threaded in by ``resolve_heritage()`` (round 22
    zed.md §3.2, the ``query subtypes``/heritage-resolution path);
    ``resolve()``'s call/ref resolution path leaves this ``None``.

    ``tiebreak_hits``, when given, is a mutable single-element counter
    passed straight through to ``_import_match`` (round 24, ``.features/
    plans/round24/03-heritage-crate-decoy-tiebreak.md``) -- incremented
    whenever a crate-root collision is broken by preferring the one
    candidate whose crate root doesn't look like a test-fixture/vendor
    stand-in. Same threading scope as ``crate_roots``: only
    ``resolve_heritage()`` ever passes a live counter.

    Before any of the above runs, ``candidates`` is narrowed to those
    matching the call site's own language, or failing that its
    language *family* (see ``_language_filtered`` and
    ``_LANGUAGE_FAMILIES``) — a same-bare-name candidate in a language
    that can never legitimately be the target is removed before it
    gets a chance to win one of the later, weaker heuristics (round 21
    tensorflow.md §5). This narrowing can leave ``candidates`` empty
    (no same-language *or* same-family candidate exists), in which
    case every remaining ladder step below is a no-op over an empty
    list and this function returns ``None`` — the caller
    (``_resolve_call``/``_resolve_ref``/``_resolve_one_heritage``)
    then records the call as ambiguous against the original,
    unfiltered candidate list, rather than silently resolving through
    a candidate in a definitively unrelated language family (see
    `.features/fixes/resolver-vendored-exclusion-false-match.md`).
    ``same_file`` needs no equivalent filtering: every symbol in it is
    already, by construction, in the same file (and therefore the
    same language) as the call site.
    """
    candidates = _language_filtered(call, candidates)
    candidates, same_file, shape_narrowed = _rust_shape_narrowed_candidates(
        call, candidates, same_file, index, file_imports, repo_stems
    )
    if shape_narrowed and not candidates:
        return _NOISE if repo_stems is not None else None

    structural = _structural_match(
        call, candidates, same_file, caller, index, file_imports
    )
    if structural is not None:
        return structural

    if len(same_file) == 1:
        only = same_file[0]
        if caller is None or only.id != caller.id:
            return only
        # same_file's sole match is the caller's own symbol -- a
        # coincidental bare-name collision between the call and its
        # own enclosing symbol, not a genuine same-file target (a
        # real self/this-qualified recursive call is already handled
        # earlier, by _container_match). Fall through instead of
        # "resolving" a call to itself and having _add_edge's self-
        # recursion filter silently discard it -- give the later
        # ladder steps (import hints, in particular) a chance to find
        # the real target.

    hinted = _import_match(
        call, candidates, file_imports, raw_imports, crate_roots, tiebreak_hits
    )
    if hinted is not None:
        return hinted

    if repo_stems is not None and _is_noise_call(
        call, file_imports, repo_stems
    ):
        return _NOISE

    if len(candidates) == 1:
        return _sole_candidate_match(
            call, candidates[0], by_name_path, repo_stems is not None
        )

    return _last_resort_match(call, candidates, by_name_path)


def _pick_candidate(
    call: _Referable,
    candidates: list[Symbol],
    same_file: list[Symbol],
    file_imports: dict[str, Import],
    caller: Symbol | None,
    by_name_path: dict[tuple[str, str], list[Symbol]],
    index: dict[str, list[Symbol]],
    repo_stems: set[str] | None = None,
    raw_imports: list[Import] | None = None,
    crate_roots: dict[str, list[str]] | None = None,
    tiebreak_hits: list[int] | None = None,
) -> Symbol | _Noise | None:
    """Run the candidate ladder, then veto a structurally impossible pick.

    See ``_pick_candidate_ladder`` for the ladder itself and every
    parameter. The one rule applied here: a Rust dot-call
    (``recv.name(..)``) can never reach a free function (round 31 zed
    coverage pass F11: ``.px(..)`` landing on ``fn px``, ``x.clone()``
    on a test module's ``fn clone``).

    It is a *veto on the result*, deliberately not a filter on the
    candidates going in. The first implementation pre-filtered, and
    integration review measured what that did on zed: removing a free
    ``fn or`` left ``EnvVar.or`` as the lone survivor, so 137
    ``Option::or`` calls (``stdout.or(stderr)``) newly resolved to it;
    removing a same-file free ``fn focus_handle`` left one same-file
    method, so ``cx.focus_handle()`` newly took it. Narrowing a
    candidate list turns "honestly ambiguous" into "confidently
    guessed" whenever it happens to leave one. A veto can only ever
    remove an edge.
    """
    picked = _pick_candidate_ladder(
        call,
        candidates,
        same_file,
        file_imports,
        caller,
        by_name_path,
        index,
        repo_stems,
        raw_imports,
        crate_roots,
        tiebreak_hits,
    )
    if (
        isinstance(picked, Symbol)
        and _rust_is_dot_call(call)
        and not _drop_free_functions([picked])
    ):
        return _NOISE if repo_stems is not None else None

    return picked


def _sole_candidate_match(
    call: _Referable,
    only: Symbol,
    by_name_path: dict[tuple[str, str], list[Symbol]],
    noise_aware: bool,
) -> "Symbol | _Noise | None":
    """Resolve, or reject, the single remaining candidate for a call.

    Structural layer 2: when the sole candidate's declared arity
    doesn't fit the call site's written argument count, it is dropped
    rather than returned anyway. ``_last_resort_match`` is then run
    over an *empty* list, not skipped: ``_bare_call_non_method_match``
    would otherwise re-derive this same single candidate for a bare,
    non-method call (its own "exactly one non-method candidate left"
    check is a no-op when there was only ever one), silently undoing
    this guard for exactly the bare-call shape it exists to cover.

    A rejected sole candidate is **not** ambiguous: a collision needs
    two live candidates, and this call has none. Round 31 cline.md
    §4.2: 542 of cline's 6,271 "ambiguous" call entries had exactly one
    candidate, all of this shape -- ``arr.at(-1)`` (1 arg) against the
    repo's only ``at``, a local ``at(r, c)``; ``Buffer.byteLength(s,
    "utf8")`` against a one-parameter ``byteLength(value)``. Filing
    them as ambiguous made ``query symbol at`` print "+86 additional
    call site(s) resolved ambiguously" about calls that provably
    cannot be its own, and showed up in ``dekko ambiguous --by name``
    as the self-contradictory "avg 1.0 candidates". Same defect class
    and same remedy as the round 22 ``_NOISE`` split: no plausible
    repo target means external.

    Args:
        call: The raw call/reference/heritage clause being resolved.
        only: The single language-filtered candidate.
        by_name_path: ``(name, path)`` → same-file symbols.
        noise_aware: Whether the caller handles ``_NOISE`` (it passed
            a non-``None`` ``repo_stems`` to ``_pick_candidate``).
            ``_resolve_ref`` doesn't, and has no external bucket to
            feed, so it keeps the plain ``None``.
    """
    if _arity_plausible(only, call):
        return only
    if noise_aware:
        return _NOISE
    return _last_resort_match(call, [], by_name_path)


def _last_resort_match(
    call: _Referable,
    candidates: list[Symbol],
    by_name_path: dict[tuple[str, str], list[Symbol]],
) -> Symbol | None:
    """The final two ``_pick_candidate`` ladder steps for 2+ candidates.

    Split out from ``_pick_candidate`` itself purely to keep that
    function's cyclomatic complexity under the project's Ruff limit —
    behaviorally this is still just the next two rungs of the same
    ladder, tried in order: the class/own-constructor pair collapse
    (``_construction_pick``, only ever applicable to exactly 2
    candidates), then the bare-call/non-method fallback
    (``_bare_call_non_method_match``, which works for any candidate
    count).
    """
    if len(candidates) == 2:
        pair = _construction_pick(candidates, by_name_path)
        if pair is not None:
            return pair

    return _bare_call_non_method_match(call, candidates)


def _bare_call_non_method_match(
    call: _Referable, candidates: list[Symbol]
) -> Symbol | None:
    """Prefer a lone non-method candidate for a receiverless call.

    A syntactically bare call/reference (``call.receiver`` falsy) can
    never invoke a *method* — every language dekko parses requires
    some receiver/qualifier at the call site to reach a symbol with a
    receiver (Go's ``recv.Method()``, Python/JS/TS's ``obj.method()``,
    Rust/C++'s ``Type::method()``). When a bare name collides with an
    unrelated method elsewhere in the repo, narrowing to non-method
    candidates can turn a real name collision into a correct
    single-candidate resolution. Round-12 master report §3.2:
    awesome-go's bare, same-package ``Generate(tt.input)`` (a call to
    ``pkg/slug``'s free function ``Generate``) misresolved as
    ambiguous against an unrelated method with a completely different
    receiver/arity, ``(g *IDGenerator) Generate(...)`` in
    ``pkg/markdown`` — this is trivially distinguishable, since a bare
    call can never mean the method.

    Only used as a last resort, after every earlier ladder step
    (receiver-aware matches, same-file, import hints, the noise
    guard, and the single-/pair-candidate fast paths) has already had
    its shot — a candidate list that already resolved via one of
    those never reaches this check. Returns ``None`` (deferring to
    the ambiguous fallback) unless dropping method-kind candidates
    narrows the list to exactly one; 2+ remaining non-method
    candidates are still a genuine, unresolved collision.
    """
    if call.receiver:
        return None
    non_methods = [c for c in candidates if c.kind != "method"]
    if len(non_methods) == 1:
        return non_methods[0]
    return None


# Well-known JS/TS global constructor/type/cast names (``String(x)``,
# ``Array.isArray``-shaped bare calls) and ambient test-framework
# globals (vitest/jest/mocha, commonly injected without an explicit
# import via ``globals: true``) — a same-named free function/shim a
# repo happens to define is essentially never what a *bare* call to
# one of these names means. See
# ``test-repos/reports/investigation-1.2-resolver-fanin.md``: cline's
# ``interface String`` (a TS ``declare global`` augmentation, not a
# real definition) was credited with 548 calls that were actually
# ``String(...)`` casts; its ``expect``/``describe`` hotspots were an
# unrelated local shim/helper credited with calls actually aimed at
# vitest's globals.
_AMBIENT_GLOBAL_NAMES = frozenset(
    {
        "String", "Number", "Boolean", "Array", "Object", "Symbol",
        "Promise", "Map", "Set", "Date", "RegExp", "Error", "JSON",
        "Math",
        "expect", "describe", "it", "test", "beforeEach", "afterEach",
        "beforeAll", "afterAll",
    }
)  # fmt: skip

# Well-known String/Array/Object prototype method names. When a
# *receiver-qualified* call reaches this point in the ladder, every
# receiver-aware disambiguation step (self/this, typed parameter,
# same-file, import hint) has already had its shot and failed — the
# only remaining "evidence" for the single-candidate fast path is
# "this name happens to be otherwise unique in the repo," which is
# false for these names precisely because they are called constantly
# on ordinary local variables (``opts.config.trim()``) that are never
# provably typed as the repo's own like-named class. See the same
# investigation report: cline's ``trim`` (fan-in 1,404, true fan-in 8)
# was almost entirely misattributed ``String.prototype.trim()`` calls.
# ``get``/``resolve``/``create`` added round 22 (cline.md §3.1): confirmed
# leaking through with inflated ``avg_candidates`` in cline's own report
# (``get`` averaged 32.0 candidates -- almost certainly
# ``Map.get()``/``Promise.resolve()``/``Object.create()`` noise, not real
# repo-symbol collisions). ``has``/``now`` added round 23
# (cline.md §2.1): a closure-local ``const now = () => Date.now()``
# was credited with every ``Date.now()``/``performance.now()`` call in
# the repo (404 misattributed sites), and ``Map.prototype.has``/
# ``Set.prototype.has``/``Reflect.has`` calls through untyped
# receivers inflated an unrelated repo-defined ``has`` to 436
# misattributed sites vs. 0 credible. ``on``/``once``/``off``/
# ``emit``/``addListener``/``removeListener`` added round 25
# (cline.md Finding 2): a debug-harness ``CdpClient.on`` (fan-in 95
# reported, resolver "fully confident") silently absorbed an unrelated
# plain Node.js ``EventEmitter``/stream ``.on("data", handler)`` call
# elsewhere in the repo -- the same false-positive shape as ``has``/
# ``now`` above, just for Node's core ``EventEmitter`` idiom instead of
# ``Map``/``Date``. ``addEventListener``/``removeEventListener``/
# ``dispatchEvent`` added alongside for the browser/DOM
# ``EventTarget`` interface, the same idiom family, common in VS Code
# extension code (both cline and claude-code are).
_BUILTIN_METHOD_NAMES = frozenset(
    {
        "trim", "trimStart", "trimEnd", "toString", "valueOf",
        "toLowerCase", "toUpperCase", "includes", "indexOf", "slice",
        "splice", "concat", "join", "push", "pop", "shift", "unshift",
        "forEach", "map", "filter", "reduce", "startsWith", "endsWith",
        "padStart", "padEnd", "repeat", "charAt", "substring",
        "replace", "replaceAll", "split", "flat", "hasOwnProperty",
        "get", "resolve", "create", "has", "now",
        "on", "once", "off", "emit", "addListener", "removeListener",
        "addEventListener", "removeEventListener", "dispatchEvent",
    }
)  # fmt: skip

# Chain-call method names from popular fluent/builder-pattern
# libraries (Zod's schema builder, Commander.js's CLI builder, and the
# like) — ``z.string().describe("...")``, ``program.description("...")``.
# Same shape of false-positive as ``_BUILTIN_METHOD_NAMES``: a
# receiver-qualified call whose receiver isn't provably typed as an
# in-repo class, so the only "evidence" for the single-candidate fast
# path is name uniqueness — which fails whenever a repo also happens
# to define its own like-named method/function. Confirmed live against
# cline twice: ``describe`` (a Zod ``.describe()`` schema call) still
# read fan-in 60 after ``_BUILTIN_METHOD_NAMES`` alone, because
# ``describe`` isn't a String/Array/Object prototype method — see
# ``test-repos/reports/investigation-1.2-resolver-fanin.md``'s
# "residual gap" note; and ``description`` (a Commander.js
# ``.description()`` builder call on a local ``Command``/``program``
# instance) read fan-in 14, all credited to an unrelated top-level
# ``const description = ...`` binding in a separate script — see
# ``test-repos/reports/11-tokentest-7repo-postdaemonfix/cline.md``
# (master report finding #5). Originally named
# ``_SCHEMA_BUILDER_METHOD_NAMES`` for the Zod-only case; renamed once
# a second, unrelated fluent-builder library hit the exact same
# false-positive shape, since "schema builder" no longer describes the
# whole set. Extend here whenever another fluent/chain-builder
# collision turns up — this is the third occurrence of the same
# pattern class, not a new one.
_CHAIN_BUILDER_METHOD_NAMES = frozenset(
    {
        # Zod (and similar schema/validation builders).
        "describe",
        # Commander.js's fluent CLI-builder API.
        "description", "option", "action", "version", "alias",
        "arguments", "usage", "command", "parse", "hook", "addCommand",
        "helpOption", "allowUnknownOption", "showHelpAfterError",
    }
)  # fmt: skip

# Well-known Rust std/prelude trait method names (``Iterator``,
# ``Option``, ``Result``, ``Clone``, ``ToString``, ...). Same
# false-positive shape as ``_BUILTIN_METHOD_NAMES``, just for Rust
# instead of JS/TS: a receiver-qualified call whose receiver isn't
# provably typed as an in-repo class reaches this guard, and these
# names are called constantly on ordinary local values/std types
# rather than an in-repo type sharing the name. Confirmed live against
# zed: ``Editor.new_internal``'s bare ``.then()`` attributed to an
# unrelated ``PathContextCondition.then`` (a CI-tool crate) and
# ``.iter_mut()`` to ``AtlasTextureList.iter_mut`` (``gpui``) — see
# round-09 §2.1 part B (``test-repos/reports/09-tokentest-7repo-postfix/
# zed.md`` §3). Not gated by language, matching how
# ``_BUILTIN_METHOD_NAMES`` (JS/TS-flavored) already isn't — these
# names are unlikely method names to collide with in other languages.
# Round 31 zed coverage pass F9: the iterator/Option/Result adaptor
# vocabulary was missing from this set entirely -- ``.flatten()``
# (zed's *only* ``fn flatten``, in ``text.rs``) absorbed 454 unrelated
# std-iterator callers (``x.iter().flatten()``-shaped, per
# ``query callers``/grep cross-check; 566 total ``.flatten()`` call
# sites repo-wide). Each addition below was checked against the
# current zed index first, per the design's own instruction: 12 of
# the 22 candidate names (``flat_map``, ``filter_map``, ``skip``,
# ``zip``, ``rev``, ``enumerate``, ``peekable``, ``nth``, ``copied``,
# ``ok_or``, ``ok_or_else``, ``as_deref``) have *zero* repo-defined
# methods with that name, so adding them is a pure no-op risk-wise
# (this guard only ever suppresses a call from reaching a real
# in-repo candidate, and there is none to suppress). ``flatten`` and
# ``chain`` each have exactly one repo-defined method; ``chain``'s
# (``gpui_wgpu/src/cosmic_text_system.rs``) takes no ``self``, so a
# dot-call could never legitimately reach it anyway. The rest
# (``any``, ``all``, ``find``, ``position``, ``last``, ``count``,
# ``sum``, ``cloned``) have 3-27 repo-defined candidates each — this
# guard only ever suppresses the *no-structural-evidence* single-/
# pair-candidate fast path (see this function's own docstring), which
# never applies past 2 candidates, so for these the guard only
# reclassifies an already-unresolvable call from ambiguous to
# external, never turns a real resolution into a miss.
_RUST_STD_METHOD_NAMES = frozenset(
    {
        "then", "then_some", "iter", "iter_mut", "into_iter", "map",
        "map_err", "and_then", "or_else", "unwrap", "unwrap_or",
        "unwrap_or_else", "unwrap_or_default", "expect", "clone",
        "into", "as_ref", "as_mut", "as_str", "as_slice", "to_string",
        "to_owned", "to_vec", "borrow", "borrow_mut", "lock", "read",
        "write", "collect", "filter", "for_each", "fold", "is_some",
        "is_none", "is_ok", "is_err", "ok", "err", "take", "replace",
        "flatten", "flat_map", "filter_map", "skip", "zip", "chain",
        "rev", "enumerate", "peekable", "any", "all", "find",
        "position", "last", "nth", "count", "sum", "cloned", "copied",
        "ok_or", "ok_or_else", "as_deref",
        # Channel receivers (std mpsc, smol, futures, flume, tokio all
        # spell it the same). Round 32: zed defines exactly one
        # ``try_recv`` (gpui's ``PriorityQueueState``), and once the
        # lifetimed-``self`` arity fix stopped rejecting it by
        # accident, 66 ``rx.try_recv()`` calls on ordinary channels
        # took it as their sole candidate. Its one real caller is in
        # the same file and resolves on that rung, before this guard.
        # ``try_send`` has no repo definition: a no-op today.
        "try_recv", "try_send",
    }
)  # fmt: skip

# AssertJ/JUnit/Hamcrest fluent-assertion chain terminals
# (``assertThat(x).isTrue()``, ``assertThat(list).hasSize(3)``). Same
# false-positive shape as the three sets above, just for Java's
# dominant assertion-library idiom: a receiver-qualified call whose
# receiver is an untyped ``assertThat(...)`` chain result reaches this
# guard, and these names are called constantly across test suites
# rather than naming an in-repo type sharing the name. Confirmed live
# against spring-boot round 23 (§2.1): a single real
# ``ResolvedDockerHost.isTrue`` caller had its fan-in inflated to 1,103
# by unrelated AssertJ ``.isTrue()`` chain calls (~1,100x inflation).
_JAVA_ASSERTION_METHOD_NAMES = frozenset(
    {
        "isTrue", "isFalse", "isEqualTo", "isNotEqualTo", "isNotNull",
        "isNull", "isEmpty", "isNotEmpty", "isPresent", "isAbsent",
        "hasSize", "contains", "containsExactly", "doesNotContain",
        "isInstanceOf", "isSameAs", "isNotSameAs",
    }
)  # fmt: skip

# Generic builder-pattern terminal method name, shared across
# Java/Kotlin/Go/Rust builder idioms alike (``SomeBuilder.build()``)
# -- not JS-flavored like ``_CHAIN_BUILDER_METHOD_NAMES`` above, which
# only covers Zod/Commander's specific fluent APIs. Same false-positive
# shape: a receiver-qualified call whose receiver isn't provably typed
# as the repo's own like-named builder reaches this guard purely on
# "otherwise unique repo-wide" name evidence. Confirmed live against
# spring-boot round 23 (§2.2): a repo-defined ``Builder.build`` read 43
# real callers plus 1,131 additional ambiguous-but-uncounted sites from
# unrelated builder types across the codebase.
#
# Deliberately narrower than the design doc's original proposal, which
# also suggested ``of``/``from``/``with``: dropped after finding real
# collisions during implementation, not merely speculative risk --
# this repo's own resolver test fixtures already use a same-file
# ``build`` method as an incidental placeholder name in two unrelated
# ladder-step tests, and ``from`` in particular is Rust's own trait
# convention name (``impl From<X> for Y { fn from(x: X) -> Y }`) --
# virtually every Rust repo defines many legitimate, resolvable
# same-named ``from`` methods, so denylisting it repo-wide (this guard
# is not language-gated) would trade a modest inflation fix for a much
# larger true-resolution loss on a language dekko already invests
# heavily in getting right. See the "Implemented" note in
# ``.features/plans/round23/
# 01-resolver-single-candidate-false-confidence.md`` for the full
# reasoning; ``of``/``with`` were dropped alongside ``from`` for
# consistency (no repro evidence backs them individually either) with
# nothing lost, since the confirmed spring-boot repro is specifically
# ``Builder.build()``.
_BUILDER_METHOD_NAMES = frozenset({"build"})

# Node's core module names -- a bare (non-relative) JS/TS import source
# exactly matching one of these is never a same-named local file,
# regardless of stem collision (round 22 claude-buddy.md §2.1: `import
# { join } from "path"` was matching a repo's own `server/path.ts`
# purely on stem equality, inflating `affected`/`workset`'s impacted-
# test count with false positives across three consecutive rounds).
# Deliberately short: only names common enough to plausibly collide
# with a real repo module name are worth hard-coding here; extend as
# new collisions turn up, same maintenance model as
# ``_BUILTIN_METHOD_NAMES``.
_NODE_BUILTIN_MODULE_NAMES = frozenset(
    {
        "path", "fs", "os", "util", "events", "stream", "crypto",
        "http", "https", "net", "url", "querystring", "buffer",
        "child_process", "assert", "zlib", "readline", "dns", "tls",
        "cluster", "timers", "string_decoder", "vm", "module",
        "constants", "worker_threads", "perf_hooks",
    }
)  # fmt: skip

_JS_TS_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")

# Well-known ambient/global receiver objects -- the receiver-side
# counterpart to ``_AMBIENT_GLOBAL_NAMES`` above, which only covers
# *receiverless* bare calls. None of the five method-name denylists
# above check the call's *receiver*, only its method name, so a
# receiver-qualified call to a well-known global object (``console``,
# ``process``, ``window``, ...) whose method name isn't itself in one
# of those sets still reaches the bare-name ambiguous/single-candidate
# fallback. Confirmed live against claude-buddy round 27 (finding M1):
# ``console.warn("...")`` in ``cli/validate-species.ts`` was reported
# ambiguous against 7 unrelated same-named free-function ``warn()``
# definitions, since ``warn`` is in none of the five method-name sets.
# A receiver-object match is a stronger, name-independent signal than
# any single method name -- every method on ``console`` is noise, not
# just ``warn`` -- so this is checked before the method-name sets, not
# folded into them.
_AMBIENT_GLOBAL_RECEIVERS = frozenset(
    {
        "console", "process", "window", "document", "global",
        "globalThis", "localStorage", "sessionStorage", "navigator",
    }
)  # fmt: skip


def _is_noise_call(
    call: _Referable,
    file_imports: dict[str, Import],
    repo_stems: set[str],
) -> bool:
    """Whether this call is likely a built-in/global, not a repo symbol.

    Checked right before the single-/pair-candidate fast paths in
    ``_pick_candidate`` — after every receiver-aware disambiguation
    step has already failed to find real evidence, this rejects the
    "it happens to be the only same-named repo symbol" guess for
    names strongly associated with language built-ins or ambient
    globals, in favor of leaving the call ambiguous/unresolved rather
    than silently inflating an unrelated symbol's fan-in.

    Args:
        call: The raw call or reference being resolved.
        file_imports: Local name to import record for the calling
            file.
        repo_stems: Every repo file's matching stem (see
            ``_repo_stem``), used to tell an external import from a
            same-repo one.

    Returns:
        True when the call should be treated as noise rather than
        resolved via the single-candidate fast path.
    """
    if _shadowed_by_external_import(call, file_imports, repo_stems):
        return True
    if not call.receiver:
        return call.name in _AMBIENT_GLOBAL_NAMES
    # An *exact* self/this receiver (no further chain) is the shape
    # ``_self_container`` already resolves when the class defines a
    # like-named method; a multi-segment chain rooted at self/this
    # (``this.options.authToken.trim()``) is a property/value access,
    # not a sibling-method call, and must not be exempted here — the
    # `receiver` text is the raw expression before the final call, so
    # only an exact match is the single-token shape `_self_container`
    # itself checks.
    if call.receiver in _SELF_RECEIVERS:
        return False
    first = _PATH_SPLIT.split(call.receiver)[0]
    if first in _AMBIENT_GLOBAL_RECEIVERS:
        return True
    return (
        call.name in _BUILTIN_METHOD_NAMES
        or call.name in _CHAIN_BUILDER_METHOD_NAMES
        or call.name in _RUST_STD_METHOD_NAMES
        or call.name in _JAVA_ASSERTION_METHOD_NAMES
        or call.name in _BUILDER_METHOD_NAMES
    )


def _shadowed_by_external_import(
    call: _Referable,
    file_imports: dict[str, Import],
    repo_stems: set[str],
) -> bool:
    """Whether a bare (no-receiver) call's own name is a local import.

    ``expect(...)`` in a file that does ``import { expect } from
    "vitest"`` always means the imported ``expect`` — JS/TS lexical
    scoping means an import binding shadows every other same-named
    thing in that file, including an unrelated repo symbol the
    bare-name index happens to find. Restricted to receiver-less calls
    since an import only binds the identifier itself, not an arbitrary
    method name reached through some other receiver.

    Args:
        call: The raw call or reference being resolved.
        file_imports: Local name to import record for the calling
            file.
        repo_stems: Every repo file's matching stem, used to tell an
            external import from a same-repo one.

    Returns:
        True when ``call.name`` is imported in this file from a
        source matching no file in the repo.
    """
    if call.receiver:
        return False
    imp = file_imports.get(call.name)
    if imp is None:
        return False
    return not _import_is_in_repo(imp, repo_stems)


# Wrapper type names whose own instance methods pass straight through
# to their inner type ``T`` (Rust ``Box<T>``/``Rc<T>``/``Arc<T>``/
# ``Ref<T>``/``RefMut<T>``, Python ``Optional[T]``). Every entry here
# is *tried itself first* (see ``_typed_param_token_candidates``), then
# the search continues one layer in if that doesn't match — it isn't
# a claim that the wrapper is *never* the real receiver, only that
# dekko can't always see when a call is written against the wrapper's
# *contents* instead (a closure/rebind/destructure local dekko has no
# way to attribute a declared type to). A genuine collection type
# (``Vec``, ``List``, ``Array``, ``Promise``, ``HashMap``,
# ``BTreeMap``, ...) stays *opaque*: the method belongs to the
# collection itself, and there's no equivalent "same-name rebind to
# the element type" idiom to protect. Round 31 zed coverage pass F7:
# ``active_rows: &BTreeMap<DisplayRow, u8>`` then
# ``active_rows.get(..)`` used to try *every* remaining identifier —
# including ``DisplayRow``, a generic argument, never the receiver's
# own type — and land on an unrelated crate's ``DisplayRow.get``.
# ``_typed_param_token_candidates`` stops descending as soon as it
# hits the first opaque wrapper, so a collection's own generic
# arguments are never tried; a reference/mutability qualifier token
# (``&mut Foo``, ``mut Foo``) is skipped outright, never itself tried
# or counted as a wrapper.
#
# Every entry past ``Optional`` here is a deliberate deviation from
# the design doc, which named ``Entity<T>`` (and, by the same shape,
# would have named ``Option``/``Result``/``Mutex``/etc.) as *opaque*
# examples. Live-measuring the design's literal choice against zed
# (not just its own worked examples) first showed -3888/+156 edges,
# two orders of magnitude past the "9 zed edges" this item's own
# accept criterion expects, with ``ambiguous`` jumping +3207 --
# treating ``Entity`` as fully opaque broke it. Each addition below is
# read against source, not guessed:
#
# - ``Entity``/``WeakEntity``: gpui's shared-mutable-cell handle
#   (semantically ``Rc<RefCell<T>>``). Dominant real shape:
#   ``entity.update(cx, |inner, cx| inner.method())`` -- the closure
#   parameter is named the same as the outer ``Entity<Buffer>``
#   parameter, and the method call inside is real, on ``Buffer``
#   (verified: ``crates/action_log/src/action_log.rs::
#   ActionLog.reject_edits_in_ranges``).
# - ``Mutex``: the identical shape via ``let x = x.lock();`` instead
#   of a closure (verified:
#   ``crates/agent/src/db.rs::ThreadsDatabase.save_thread_sync``,
#   ``connection: &Arc<Mutex<Connection>>`` then
#   ``let connection = connection.lock(); connection.exec_bound(..)``).
# - ``Option``/``Result``: Rust's own irrefutable-destructure idiom,
#   ``let Some(x) = x else { .. };``/``let Ok(x) = x else { .. };``,
#   rebinds the *same name* to the unwrapped inner value -- as
#   pervasive in real Rust as the ``Entity``/``Mutex`` shapes above
#   (verified: ``crates/language/src/language_settings.rs::
#   LanguageSettings.resolve``, ``buffer: Option<&'a Buffer>`` then
#   ``let Some(buffer) = buffer else { .. }; buffer.file()``). Unlike
#   a plain collection, there is no way to call a method through an
#   un-destructured ``Option``/``Result`` at all, so this doesn't
#   weaken the F7 collection-argument guard the way it might first
#   appear to.
#
# All of the above still have real methods of their own
# (``Entity::clone``/``downgrade``, ``Mutex::lock``/``try_lock``), so
# none of them are purely transparent either -- confirmed by
# re-measuring after the Entity-only fix (still -3708/+158, newly-LOST
# dominated by ``Entity.clone`` targets). Every entry is tried, outer
# type first (see ``_typed_param_token_candidates``), and whichever
# one actually has a matching candidate wins. ``Option``/``Result``
# themselves never do (no in-repo ``TYPE_KINDS`` symbol names them,
# since they're foreign) -- ``_typed_param_match``'s own in-repo-type
# gate for a parameterized token is what keeps a coincidental foreign-
# type-local-impl match (zed's own ``impl Into<SelectionEffects> for
# Option<Autoscroll>``) from absorbing an unrelated ``Option<X>``'s
# ``.into()`` call; that gate is unaffected by adding them here.
_TRANSPARENT_TYPE_WRAPPERS = frozenset(
    {
        "Box", "Rc", "Arc", "Ref", "RefMut", "Optional",
        "Entity", "WeakEntity", "Mutex", "Option", "Result",
    }
)  # fmt: skip
_TYPE_QUALIFIER_WORDS = frozenset({"mut", "const", "ref", "readonly"})
_TYPE_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


_OBJECT_TYPE_FIELD = re.compile(r"([A-Za-z_$][\w$]*)\??\s*:\s*([A-Z][\w$]*)")


def _object_type_field_tokens(
    type_text: str, receiver: str
) -> list[tuple[str, bool]] | None:
    """Receiver-type tokens for a TS/JS inline object-type parameter.

    ``input: { bot: Chat; client: HubSessionClient; ... }`` is not a
    wrapper around one type: each field carries its own, and the call
    site says which one it means (``input.client.getSchedule(..)``).
    The outermost-token rule in ``_typed_param_token_candidates`` was
    written for ``Wrapper<T>`` shapes; applied here it stopped at
    ``Chat``, the first field's type, and integration review found it
    had dropped 9 correct ``HubSessionClient.*`` edges on cline.

    Args:
        type_text: The parameter's declared type, as written.
        receiver: The call's receiver text (``input.client``).

    Returns:
        ``None`` when ``type_text`` isn't an inline object type, so the
        caller falls back to the ordinary token chain. Otherwise the
        single field type the receiver's second segment names, or an
        empty list when it names none (a call made directly on the
        object literal, or on a field this type doesn't declare).
    """
    if not type_text.lstrip().startswith("{"):
        return None
    segments = _PATH_SPLIT.split(receiver)
    if len(segments) < 2:
        return []
    fields = dict(_OBJECT_TYPE_FIELD.findall(type_text))
    field_type = fields.get(segments[1])
    return [(field_type, False)] if field_type else []


def _typed_param_token_candidates(
    type_text: str,
) -> list[tuple[str, bool]]:
    """Ordered type-name tokens to try as the declared type's own
    receiver type, outermost first.

    Every transparent wrapper (``_TRANSPARENT_TYPE_WRAPPERS``) is
    itself tried — it can have real methods of its own — and then the
    search continues one layer in, since it *also* commonly wraps a
    type whose methods are reached some other way dekko can't see
    (Rust's ``Box``/``Rc``/``Arc``/``Ref``/``RefMut`` deref coercion;
    gpui's ``Entity<T>``/``Mutex<T>``-family shadowing idioms, see
    ``_TRANSPARENT_TYPE_WRAPPERS``'s own comment). The chain stops
    the moment it reaches an *opaque* wrapper or a plain type name —
    that token is tried, but the search never descends into ITS own
    generic arguments (round 31 zed coverage pass F7: a collection's
    key/value type parameters are never the receiver).

    A lowercase-leading token is skipped outright — never tried, never
    counted as a stop — rather than treated as the type itself. Every
    language dekko indexes names a defined type (class/struct/
    interface/...) in PascalCase by convention, so a lowercase segment
    is always a module/namespace qualifier (Rust ``watch::Receiver``,
    ``std::collections::BTreeMap``), never the type. Without this, a
    scoped path's own leading module segment (``watch`` in
    ``watch::Receiver<()>``) would itself become the one token tried
    — the real type, ``Receiver``, right behind it, never reached
    (live-testing on zed: ``needs_refresh: watch::Receiver<()>`` then
    ``needs_refresh.changed()`` regressed exactly this way while this
    fix was in progress).

    Args:
        type_text: A declared parameter's type, as written.

    Returns:
        ``(token, is_parameterized)`` pairs to try, in order — empty
        when ``type_text`` has no identifier tokens at all.
        ``is_parameterized`` is True when the token is immediately
        followed by ``<`` (it takes its own type arguments) — read by
        ``_typed_param_match`` to decide whether the token needs an
        in-repo-type check before being tried (see that function's own
        docstring for why a parameterized token needs it and a bare
        one doesn't).
    """
    tokens: list[tuple[str, bool]] = []
    for match in _TYPE_TOKEN_RE.finditer(type_text):
        token = match.group()
        if token in _TYPE_QUALIFIER_WORDS:
            continue
        if not token[:1].isupper():
            continue
        is_parameterized = type_text[match.end() : match.end() + 1] == "<"
        tokens.append((token, is_parameterized))
        if token not in _TRANSPARENT_TYPE_WRAPPERS:
            break
    return tokens


def _receiver_type_match(
    call: _Referable,
    candidates: list[Symbol],
    index: dict[str, list[Symbol]],
) -> Symbol | None:
    """Resolve a call through an explicit ``Type::method()`` receiver.

    ``BufferDiff::new(...)``/``BufferDiff.new(...)`` where the receiver
    is the type's own bare name (not a variable of that type) is
    stronger evidence than either the self/this or typed-parameter
    steps: it names the exact type explicitly, rather than requiring
    it be inferred from ``self``/``this`` or a declared parameter's
    annotation. Neither of those steps fires for this shape, so without
    this check the call falls into the generic same-file/import/fast-
    path ladder — which silently drops it as ambiguous whenever the
    repo defines the method name more than once elsewhere (zed's
    ``BufferDiff.new``, called from inside ``BufferDiff``'s own file,
    read zero callers despite 13 real call sites — round-09 §2.1
    part A).

    Args:
        call: The raw call or reference being resolved.
        candidates: Every same-named symbol repo-wide (already looked
            up by the caller).
        index: Bare symbol name to every symbol sharing it, used to
            check whether the receiver's first segment names an
            in-repo type (``model.TYPE_KINDS``) rather than a variable.

    Returns:
        The uniquely-matching method, or ``None`` when the receiver
        doesn't name a known in-repo type, or the type doesn't narrow
        ``candidates`` to exactly one.
    """
    if not call.receiver:
        return None
    first = _PATH_SPLIT.split(call.receiver)[0]
    same_name = index.get(first, [])
    if not any(sym.kind in TYPE_KINDS for sym in same_name):
        return None
    target_qual = f"{first}.{call.name}"
    matched = [c for c in candidates if c.qualname == target_qual]
    if len(matched) == 1:
        return matched[0]
    return None


def _structural_match(
    call: _Referable,
    candidates: list[Symbol],
    same_file: list[Symbol],
    caller: Symbol | None,
    index: dict[str, list[Symbol]],
    file_imports: dict[str, Import] | None = None,
) -> Symbol | None:
    """The ladder's three structural steps, strongest first.

    Self/this container, explicit ``Type::method`` receiver, then a
    typed parameter of the caller. Grouped only to keep
    ``_pick_candidate`` under the complexity ceiling; order and
    behavior are exactly what they were inline.
    """
    container_match = _container_match(call, caller, same_file)
    if container_match is not None:
        return container_match
    receiver_type = _receiver_type_match(call, candidates, index)
    if receiver_type is not None:
        return receiver_type
    return _typed_param_match(call, candidates, caller, index, file_imports)


def _rust_shape_narrowed_candidates(
    call: _Referable,
    candidates: list[Symbol],
    same_file: list[Symbol],
    index: dict[str, list[Symbol]],
    file_imports: dict[str, Import] | None = None,
    repo_stems: set[str] | None = None,
) -> tuple[list[Symbol], list[Symbol], bool]:
    """Narrow candidates by Rust call shape, before the rest of
    ``_pick_candidate``'s ladder runs.

    Three shapes each rule out an entire class of candidate: a
    ``Type::name`` path can only reach that type's own members
    (``_owned_by_receiver_type``), the same path rooted at a type the
    repo doesn't define can reach nothing at all
    (``_rust_unknown_type_path``, round 31 F6b), and a ``recv.name``
    dot-call can never reach a free function
    (``_drop_free_functions``, round 31 F11). Split out of
    ``_pick_candidate`` purely to keep that function's cyclomatic
    complexity under the project's Ruff limit
    (round 31 rule 0.5) — mirrors ``_structural_match``'s own reason
    for existing. ``_rust_type_path_receiver``/``_rust_is_dot_call``
    test the same call text for opposite join characters (``::`` vs
    ``.``), so the two shapes are mutually exclusive by construction
    and at most one narrowing ever applies (the two ``::`` rules are
    exclusive too: one needs an in-repo type, the other needs none).

    Args:
        call: The raw call or reference being resolved.
        candidates: Every same-named, language-filtered symbol
            repo-wide.
        same_file: Same-named symbols in the calling file.
        index: Bare symbol name to every symbol sharing it.
        file_imports: The calling file's import bindings by local
            name, for the unknown-type rule's in-repo ``use`` check.
        repo_stems: Every repo file's matching stem, same purpose.

    Returns:
        ``(candidates, same_file, narrowed)`` — the (possibly)
        narrowed lists, and whether either shape actually applied.
        ``narrowed`` tells the caller whether an empty ``candidates``
        here means "this call structurally cannot reach any repo
        symbol" (worth a ``_NOISE``/external verdict) as opposed to
        merely having started out empty for an unrelated reason.
    """
    if _rust_type_path_receiver(call, index):
        return (
            _owned_by_receiver_type(call, candidates, index),
            _owned_by_receiver_type(call, same_file, index),
            True,
        )
    if _rust_unknown_type_path(
        call, candidates, index, file_imports, repo_stems
    ):
        # Rooted at a type the repo doesn't define (round 31 F6b):
        # nothing here can be the target.
        return [], [], True
    if _rust_is_dot_call(call) and not _drop_free_functions(candidates):
        # Every candidate is a free function: nothing a dot-call could
        # mean. Anything less than that is left alone here on purpose,
        # see ``_pick_candidate``'s veto.
        return [], [], True
    return candidates, same_file, False


def _rust_is_dot_call(call: _Referable) -> bool:
    """Whether a Rust call joins its receiver and name with ``.``,
    not ``::``.

    Round 31 zed coverage pass F11: a Rust *method* call
    (``recv.name(..)``) can never reach a free (module-level)
    function — the language simply has no syntax for it, unlike
    Python/JS/TS's ``module.func()``, a legitimate dot-call on a
    namespace object (so this check is Rust-only, gated the same way
    ``_rust_type_path_receiver`` gates itself). ``.px(..)`` on a
    ``Styled`` trait method resolving to the unrelated free function
    ``fn px(...)`` (``crates/gpui/src/geometry.rs``) was 28 of zed's
    dekko-only ``sanity`` rows for that name alone.

    Uses ``call.text`` (via ``getattr``, since ``RawRef`` carries no
    ``text`` field at all — round 31 rule 0.6; its ``receiver`` field
    exists but is always ``None``, which already short-circuits this
    function before ``text`` is ever read) rather than re-deriving the
    join character: for a genuine method call built by
    ``extractor._callee_parts``, ``receiver`` is the *full* qualifier
    expression's text (not just its first segment the way
    ``call.receiver`` is read elsewhere in this ladder), so ``text``
    ends in exactly ``.<name>`` for a dot-call and ``::<name>`` for a
    scoped path — never both.

    Args:
        call: The raw call or reference being resolved.

    Returns:
        True when ``call`` is a Rust call whose text ends in
        ``.<name>``.
    """
    if not call.receiver or not call.path.endswith(".rs"):
        return False
    text = getattr(call, "text", "") or ""
    return text.endswith(f".{call.name}")


def _drop_free_functions(symbols: list[Symbol]) -> list[Symbol]:
    """Remove free (containerless) function candidates.

    A free function's ``qualname`` equals its bare ``name`` (no
    ``.`` — see ``Symbol.qualname``'s own docstring); a method or
    associated function always has a container prefix. Used by the
    round 31 F11 dot-call guard in ``_pick_candidate``: only
    ``kind == "function"`` is dropped, never a variable/type/other
    kind, and only when it's genuinely containerless.

    Args:
        symbols: Candidates to filter.

    Returns:
        ``symbols`` with every free-function entry removed.
    """
    return [
        s
        for s in symbols
        if not (s.kind == "function" and "." not in s.qualname)
    ]


def _rust_type_path_receiver(
    call: _Referable, index: dict[str, list[Symbol]]
) -> str | None:
    """The in-repo type a Rust ``Type::name(...)`` path is rooted at.

    Returns the receiver's last path segment when the call is a Rust
    ``::`` path whose receiver ends in the name of an in-repo type
    (``Point::new``, ``gpui::Point::new``), else ``None``. ``Self`` and
    lowercase module paths (``module::func``) never qualify.
    """
    last = _rust_type_path_last_segment(call)
    if last is None:
        return None
    # A ``type_alias`` alone doesn't qualify (round 31 F6b): an alias's
    # members live under the type it aliases, so ``Alias::new()`` has
    # no ``Alias.new`` to find and the owner rule would veto the real
    # ``Real.new``. Leave those to the ordinary ladder.
    if not any(
        sym.kind in TYPE_KINDS and sym.kind != "type_alias"
        for sym in index.get(last, [])
    ):
        return None

    return last


def _rust_type_path_last_segment(call: _Referable) -> str | None:
    """The type-shaped last receiver segment of a Rust ``::`` path.

    ``gpui::Point::<f32>::new`` gives ``Point``. Purely syntactic, no
    index lookup: ``None`` for a non-Rust call, a dot-call, ``Self``,
    and a lowercase module path (``module::func``).
    """
    receiver = getattr(call, "receiver", None)
    if not receiver or not call.path.endswith(".rs"):
        return None
    if f"{receiver}::" not in (getattr(call, "text", "") or ""):
        return None
    last = receiver.rsplit("::", 1)[-1].split("<", 1)[0].strip()
    if not last[:1].isupper() or last == "Self":
        return None

    return last


def _rust_unknown_type_path(
    call: _Referable,
    candidates: list[Symbol],
    index: dict[str, list[Symbol]],
    file_imports: dict[str, Import] | None,
    repo_stems: set[str] | None,
) -> bool:
    """Whether a Rust ``Type::name`` path is rooted at a type the repo
    doesn't define at all.

    Round 31 zed coverage pass F6b: ``Default::default()``,
    ``Vec::new()``, ``Box::new()`` and a macro-generated
    ``StyleRefinement::default()`` name a std, third-party, or
    macro-minted type. No repo symbol can be the target, yet the
    generic ladder took whatever ``new``/``default`` sat in the
    caller's file (1,076 wrong edges on zed, 884 of them ``Vec``/
    ``Box``/``Default``/``String``). ``_owned_by_receiver_type``
    couldn't veto them because its gate needs an in-repo type to be
    the owner.

    True only when every way the repo could know the name comes up
    empty:

    - no in-repo symbol of *any* kind carries it. This is what needed
      Rust ``type`` aliases indexed first: ``type Alias = Real;`` makes
      ``Alias::new()`` a real in-repo call, and without the alias
      symbol this rule would send it external.
    - no candidate is a member of it. A macro-generated struct with a
      handwritten ``impl Foo { fn new() }`` has no ``Foo`` symbol but
      does have ``Foo.new``.
    - the calling file doesn't ``use`` it from inside the repo. A
      rename matches no symbol by design, and it needn't be written
      in this file: ``pub use text::Buffer as TextBuffer;`` in one
      crate, then ``use language::TextBuffer;`` here (live-testing on
      zed: a same-file-only ``as`` check lost 2 real
      ``TextBuffer::new_normalized`` edges).
    - it isn't an associated-type path. ``T::ProtoRequest::stop()``
      and ``Self::Output::new()`` name a type only the trait solver
      knows; the ladder's trait-method guess was right on zed (3 of
      3), so they stay with the ladder.

    Names of one or two characters are skipped: those are generic
    parameters (``T::default()``, ``Tx::new()``), which the ladder
    already treats as unknowable.

    Args:
        call: The raw call or reference being resolved.
        candidates: Every same-named, language-filtered symbol
            repo-wide.
        index: Bare symbol name to every symbol sharing it.
        file_imports: The calling file's import bindings by local name.
        repo_stems: Every repo file's matching stem, to tell an in-repo
            ``use`` from an external one. ``None`` (a caller that
            can't take a ``_NOISE`` verdict) counts every ``use`` as
            in-repo.

    Returns:
        True when no repo candidate can be this path's target.
    """
    last = _rust_type_path_last_segment(call)
    if last is None or len(last) <= 2 or not candidates:
        return False
    if index.get(last) or _rust_is_associated_type_path(call):
        return False
    if any(_container_name(cand) == last for cand in candidates):
        return False

    binding = (file_imports or {}).get(last)
    if binding is None:
        return True

    return repo_stems is not None and not _import_is_in_repo(
        binding, repo_stems
    )


def _rust_is_associated_type_path(call: _Referable) -> bool:
    """Whether a Rust path reaches its type through another type.

    ``T::ProtoRequest::name`` and ``Self::Output::name``: a segment
    before the last one is itself type-shaped (capitalized, or
    ``Self``), where a plain module path (``std::collections::
    HashMap::name``) is lowercase all the way to the type.
    """
    receiver = (getattr(call, "receiver", None) or "").strip()
    if receiver.startswith("<"):
        # ``<Cmd as LspCommand>::ProtoRequest::name``: the qualified
        # form of the same thing (live-testing on zed, 1 real edge).
        return True

    qualifiers = receiver.split("<", 1)[0].split("::")[:-1]
    return any(seg.strip()[:1].isupper() for seg in qualifiers)


def _container_name(sym: Symbol) -> str | None:
    """Bare name of the type or trait ``sym`` is a member of, if any."""
    if "." not in sym.qualname:
        return None

    return sym.qualname.rsplit(".", 1)[0].rsplit(".", 1)[-1]


def _owned_by_receiver_type(
    call: _Referable,
    candidates: list[Symbol],
    index: dict[str, list[Symbol]],
) -> list[Symbol]:
    """Keep only candidates a ``Type::name`` path could actually mean.

    ``_receiver_type_match`` uses an explicit type receiver as positive
    evidence only: exactly one ``Type.name`` wins, anything else falls
    through to the generic ladder *with the full candidate list*. For
    ``Point::default()`` where ``Default`` is derived (no ``Point.
    default`` symbol exists), that ladder then picked whatever
    ``default`` was nearest: ``ScrollHandle.default``, because it is
    the only ``default`` in the same file. Round 31 zed coverage pass
    F6 counted 35 such wrong edges that 0.43.61 exposed by no longer
    short-circuiting ``crate::``-imported receivers to ``external``
    (they were skipped before, for the wrong reason). The rule was
    always missing; that release just stopped hiding it.

    A Rust ``Type::name`` path can only name a member of ``Type``, or
    a default method of a trait ``Type`` implements. So a candidate
    survives when its container is ``Type`` itself, or is a trait. A
    free function, an unrelated struct (``Enum::Variant(..)`` landing
    on a same-named struct), or another type's method cannot be the
    target. Nothing left means no plausible repo target.
    """
    owner = _rust_type_path_receiver(call, index)
    if owner is None:
        return candidates
    own: list[Symbol] = []
    via_trait: list[Symbol] = []
    for cand in candidates:
        if "." not in cand.qualname:
            continue
        container_name = cand.qualname.rsplit(".", 1)[0].rsplit(".", 1)[-1]
        if container_name == owner:
            own.append(cand)
        elif any(sym.kind == "trait" for sym in index.get(container_name, [])):
            via_trait.append(cand)
    # The type's own members outrank another trait's defaults: the
    # trait allowance exists for ``Type::method()`` where ``Type``
    # itself declares no such member, not as a rival to one it does.
    # Live-testing on zed: ``RangeExt::overlaps(&a, &b)`` drifted to an
    # unrelated ``AnchorRangeExt.overlaps`` because a UFCS call's extra
    # explicit ``self`` argument made that one's arity fit better.
    kept = own or via_trait
    # One type routinely has two same-named members: an inherent
    # ``fn zero() -> Self`` and a trait impl's ``fn zero(_cx: ())``.
    # The written argument count tells them apart, and it has to be
    # applied *here*: narrowing ``same_file`` to the owner can leave
    # the wrong one as a lone same-file match, which the ladder takes
    # without an arity check (live-testing on zed: 7 ``Point::zero()``
    # calls moved from ``point.rs``'s inherent fn to the ``Dimension``
    # impl sitting in the caller's own file).
    plausible = [c for c in kept if _arity_plausible(c, call)]
    return plausible or kept


def _typed_param_match(
    call: _Referable,
    candidates: list[Symbol],
    caller: Symbol | None,
    index: dict[str, list[Symbol]],
    file_imports: dict[str, Import] | None = None,
) -> Symbol | None:
    """Resolve a call through one of the caller's own typed parameters.

    ``controller.initTask(...)``, where the calling function declares
    a parameter ``controller: Controller`` — cline's headline finding
    for bug #2: the container (``self``/``this``) and same-file steps
    never look at a receiver that is neither, so a call through an
    explicitly-typed parameter of a *different* name than its class
    either fell through to a coincidental same-file match or, when no
    same-file candidate existed, straight to ``ambiguous`` whenever
    another same-named method existed anywhere else in the repo.

    Tries each of the declared type's own ordered candidate tokens
    (``_typed_param_token_candidates``), outermost first, and returns
    the first one with a unique match — round 31 zed coverage pass F7
    (see ``_TRANSPARENT_TYPE_WRAPPERS``'s comment): trying every
    remaining identifier in the type string without regard to nesting,
    including a generic *argument* buried inside an opaque wrapper,
    used to land a call on an unrelated type entirely.

    A *parameterized* token (one taking its own type arguments, e.g.
    ``Option<...>``) is tried only when it also names an in-repo
    ``TYPE_KINDS`` symbol. Without this, a token that happens to share
    a name with a well-known foreign/std generic container (``Option``,
    ``Result``) could still find a *method* candidate with that exact
    qualname — not because the repo defines such a type, but because
    Rust allows a local trait impl on a foreign generic instantiated
    with a local type argument (zed's own ``impl
    Into<SelectionEffects> for Option<Autoscroll>``, extracted with
    container name ``Option`` since dekko's impl-block naming strips
    the generic argument the same way a call's argument list already
    is). That real symbol — genuinely the *only* thing in the whole
    repo named ``Option.into`` — then silently absorbed every
    unrelated ``.into()`` call whose declared parameter type merely
    mentioned ``Option<...>`` anywhere, caught live-measuring this fix
    against zed (-3153/+56 before this gate, mostly ``Option``/
    ``Result``-shaped false positives). A *non*-parameterized token
    (``WPARAM``, a plain foreign FFI type with no generic slot) gets
    no such gate: it can never collide across differently-instantiated
    uses the way a generic container can, so a local extension-trait
    impl on it (zed's own ``impl HiLoWord for WPARAM``, gpui_windows)
    is exactly as reliable a signal as it always was — gating it the
    same way regressed real, correct matches (live-measuring: WPARAM/
    LPARAM Windows FFI extension methods).

    Args:
        call: The raw call or reference being resolved.
        candidates: Every same-named symbol repo-wide (already looked
            up by the caller).
        caller: The enclosing symbol, or ``None`` for module-level
            calls — which have no declared parameters to check.
        index: Bare symbol name to every symbol sharing it — used both
            to gate a parameterized candidate token above and by the
            Rust same-name-in-2+-crates tiebreak below.

    Returns:
        The uniquely-matching method, or ``None`` when the receiver
        isn't one of ``caller``'s declared, typed parameters, or no
        candidate token narrows ``candidates`` to exactly one (after
        the in-repo-type gate, for a parameterized token).
    """
    if caller is None or not call.receiver:
        return None
    first = _PATH_SPLIT.split(call.receiver)[0]
    param_type = next(
        (p.type for p in caller.params if p.name == first and p.type),
        None,
    )
    if not param_type:
        return None
    tokens = _object_type_field_tokens(param_type, call.receiver)
    if tokens is None:
        tokens = _typed_param_token_candidates(param_type)
    for token, is_parameterized in tokens:
        # A Rust ``type`` alias doesn't open the gate (round 31 F6b
        # started indexing them): ``type Result<T> = std::result::
        # Result<T, Error>;`` is the foreign generic container this
        # gate exists to keep out, under a local name.
        if is_parameterized and not any(
            sym.kind in TYPE_KINDS
            and not (sym.kind == "type_alias" and sym.path.endswith(".rs"))
            for sym in index.get(token, [])
        ):
            continue
        target_qual = f"{token}.{call.name}"
        matched = [c for c in candidates if c.qualname == target_qual]
        if len(matched) != 1:
            continue
        only = matched[0]
        if call.path.endswith(".rs") and _rust_typed_match_looks_cross_crate(
            call, only, token, index, file_imports, param_type
        ):
            continue
        return only
    return None


def _rust_typed_match_looks_cross_crate(
    call: _Referable,
    only: Symbol,
    outer: str,
    index: dict[str, list[Symbol]],
    file_imports: dict[str, Import] | None = None,
    type_text: str = "",
) -> bool:
    """Whether ``_typed_param_match``'s sole candidate is provably the
    *wrong* crate's same-named type.

    Round 31 zed coverage pass F7, "second half": a declared type's
    outer identifier can genuinely name a *different* type in two
    crates (zed defines its own ``Entity<T>`` in both ``gpui`` and
    ``workspace``). Bare qualname equality (``"Entity.focus"``) can't
    tell which one a candidate method belongs to, so a method that
    exists on the *other* crate's same-named type but not the
    caller's own can still look like a unique match —
    ``view.read(cx).focus_handle(cx).focus(cx)`` inside ``gpui``
    (which cannot depend on ``workspace``) resolved to
    ``workspace::Entity.focus``.

    Only fires — and only *can* fire — when there's positive evidence
    of the wrong pick: the outer identifier names a type in 2+ crates
    *and* one of them is the caller's own crate *and* the sole
    candidate isn't in that crate. Any one of those missing (no
    same-name collision at all, or the caller's own crate doesn't
    define this type either) leaves the match untouched — round 31's
    rule 0.3, "no evidence is not negative evidence": this function
    can only disprove a match, never merely fail to confirm one, so a
    declared type dekko can't independently place (common: many
    typed-parameter matches target a type only ever seen through this
    one parameter annotation) is never penalized for that alone.

    Args:
        call: The raw call or reference being resolved.
        only: ``_typed_param_match``'s sole qualname-matching
            candidate.
        outer: The declared type's outermost identifier.
        index: Bare symbol name to every symbol sharing it.

    Returns:
        True only when the caller's own crate defines a same-named
        type and ``only`` isn't in it.
    """
    imp = (file_imports or {}).get(outer)
    if imp is not None and not imp.source.startswith(_RUST_IN_CRATE_PREFIXES):
        # The file says where this type comes from, and it isn't here.
        # zed's ``language`` crate defines its own ``BufferSnapshot``
        # *and* ``syntax_map.rs`` does ``use text::{BufferSnapshot, ..}``:
        # "the caller's own crate defines one too" is then no evidence
        # against ``text::BufferSnapshot.remote_id``. Integration review
        # of this guard found 37 correct edges it had disproved.
        return False

    if re.search(
        rf"\b(?!crate\b|self\b|super\b)[a-z_]\w*::{outer}\b", type_text
    ):
        # ``text: &text::BufferSnapshot`` names the foreign crate in the
        # annotation itself.
        return False

    outer_types = [s for s in index.get(outer, []) if s.kind in TYPE_KINDS]
    if len(outer_types) < 2:
        return False
    own_crate = _rust_crate_dir(call.path)
    own = [t for t in outer_types if _rust_crate_dir(t.path) == own_crate]
    if not own or _rust_crate_dir(only.path) == own_crate:
        return False
    # "The caller's crate defines one too" only disproves the match
    # when that one is actually *in scope here*: imported via
    # ``crate::``/``super::``/``self::``, or defined in this very file.
    # zed's ``markdown`` crate has a private ``struct Context<'a>`` in
    # ``html/html_minifier.rs``; ``markdown.rs`` never imports it and
    # gets ``gpui::Context`` through a prelude glob, yet the unscoped
    # version of this check disproved every ``cx.observe_global(..)``
    # edge in the file (integration review, round 31).
    #
    # A ``pub`` own-crate type is the opposite case: it reaches other
    # modules through ``pub use binding::*;``-style globs dekko can't
    # trace, so it counts as in scope without an import. ``gpui``'s
    # ``keymap.rs`` gets ``gpui::KeyBinding`` exactly that way, and
    # without this its ``binding.name()`` lands on ``ui``'s unrelated
    # ``KeyBinding.name``, a crate gpui cannot depend on.
    return (
        imp is not None
        or any(t.path == call.path for t in own)
        or any(t.exported for t in own)
    )


# Constructor method names this resolver recognizes for a class-shaped
# symbol, checked in order against ``(name, cls.path)`` — JS/TS/TSX's
# fixed ``constructor``, Python's fixed ``__init__``, and last, the
# class's own bare name (Java's ``constructor_declaration`` has no
# distinct keyword: its extracted ``name`` field is the class name).
_CONSTRUCTOR_NAMES = ("constructor", "__init__")
# Set form for ``name_delta``'s membership test — the class's-own-name
# case above needs no separate handling there, since that name is
# already the changed name itself (see ``NameDelta.changed``).
_CONSTRUCTOR_NAME_SET = frozenset(_CONSTRUCTOR_NAMES)


def _constructor_of(
    cls: Symbol, by_name_path: dict[tuple[str, str], list[Symbol]]
) -> Symbol | None:
    """The class's own explicit constructor method, if extracted.

    ``new ClassName(...)``/bare ``ClassName(...)`` construction always
    resolves to the class symbol itself, which under-counts a class's
    real "how is this constructed" fan-in whenever the language also
    extracts an explicit constructor as its own method symbol —
    cline's ``Controller.constructor`` read fan-in 0 despite a real
    ``new Controller(...)`` call site (bug #2). When one exists,
    ``_add_call_and_constructor`` adds a second edge to it alongside
    the class-level edge, so both "who constructs this class" and
    "who calls the constructor body" are counted.

    Args:
        cls: A resolved symbol, checked only when it is class-shaped
            (``model.TYPE_KINDS``) — a plain function/method target
            returns ``None`` immediately.
        by_name_path: ``(bare name, file path)`` → same-file symbols.

    Returns:
        The constructor method symbol, or ``None`` when ``cls`` isn't
        a type-kind symbol or has no matching constructor definition.
    """
    if cls.kind not in TYPE_KINDS:
        return None
    for name in (*_CONSTRUCTOR_NAMES, cls.name):
        for sym in by_name_path.get((name, cls.path), []):
            qual = f"{cls.qualname}.{name}"
            if sym.kind == "method" and sym.qualname == qual:
                return sym
    return None


def _construction_pick(
    candidates: list[Symbol],
    by_name_path: dict[tuple[str, str], list[Symbol]],
) -> Symbol | None:
    """Collapse a same-name {class, own-constructor} pair to the class.

    Java's ``constructor_declaration`` shares its bare name with its
    own class (no distinct keyword the way JS/TS's ``constructor`` or
    Python's ``__init__`` is), so ``new Foo(...)`` finds two same-
    named candidates — the class ``Foo`` and its constructor method
    ``Foo.Foo`` — and used to be recorded as unresolvably ambiguous,
    undercounting fan-in for *both* (bug #2). This isn't a real
    ambiguity: the two symbols are one class and its own constructor,
    so the class wins as the primary target (matching JS/TS/Python's
    convention elsewhere in this ladder) — ``_add_call_and_constructor``
    then finds and adds the constructor edge alongside it automatically.

    Args:
        candidates: Exactly two same-named symbols (the caller only
            invokes this when ``len(candidates) == 2``).
        by_name_path: ``(bare name, file path)`` → same-file symbols.

    Returns:
        The class symbol when ``candidates`` is one class and its own
        constructor method, else ``None`` (a real ambiguity).
    """
    a, b = candidates
    for cls, ctor in ((a, b), (b, a)):
        found = _constructor_of(cls, by_name_path)
        if found is not None and found.id == ctor.id:
            return cls
    return None


def _self_container(call: _Referable, caller: Symbol | None) -> str | None:
    """Container qualname when the call goes through self/this."""
    if caller is None or call.receiver is None:
        return None
    first = _PATH_SPLIT.split(call.receiver)[0]
    if first not in _SELF_RECEIVERS:
        return None
    if "." not in caller.qualname:
        return None
    return caller.qualname.rsplit(".", 1)[0]


def _container_match(
    call: _Referable, caller: Symbol | None, same_file: list[Symbol]
) -> Symbol | None:
    """Resolve a self/this call against the caller's own container.

    Split out of ``_pick_candidate`` (rather than inlined) to keep its
    branch count down as the ladder has grown more steps; behavior is
    unchanged from when this was inline.

    Args:
        call: The raw call or reference being resolved.
        caller: The enclosing symbol, or ``None`` for module-level
            calls.
        same_file: Like-named symbols in the calling file.

    Returns:
        The uniquely-matching same-file, same-container method, or
        ``None`` when the receiver isn't self/this or the container
        doesn't narrow to exactly one candidate.
    """
    container = _self_container(call, caller)
    if container is None:
        return None
    target_qual = f"{container}.{call.name}"
    same = [c for c in same_file if c.qualname == target_qual]
    if len(same) == 1:
        return same[0]
    return None


def _import_match(
    call: _Referable,
    candidates: list[Symbol],
    file_imports: dict[str, Import],
    raw_imports: list[Import] | None = None,
    crate_roots: dict[str, list[str]] | None = None,
    tiebreak_hits: list[int] | None = None,
) -> Symbol | None:
    """Match candidates against import hints for this file.

    ``raw_imports`` (whole-file-include languages only — see
    ``_WHOLE_FILE_IMPORT_LANGUAGES``) is tried as a fallback when the
    per-name hints above find nothing: a C/C++ ``#include`` binds no
    single symbol name the way ``from x import y``/``import {y} from
    'x'`` do, so ``file_imports.get(call.name)`` (keyed by a *local
    binding* name) structurally can never hit for these languages,
    regardless of what the call's own name or receiver is. Instead,
    check every ``#include`` in the file against every candidate's
    file — the same ``_module_matches`` check ``affected.py``'s
    ``_import_hits`` already does for its diff-import evidence tier —
    and resolve when exactly one candidate's file is actually included
    here. Verified against a fixture reproducing tensorflow's
    ``rewrite_utils.cc``/``rewrite_utils_test.cc`` gtest pair (same
    file paths, same symbol names, same header) — see
    ``test-repos/reports/investigation-1.5-cpp-gtest-affected.md`` and
    ``tests/test_resolver.py::test_cpp_call_disambiguated_via_whole_file_include``.

    ``crate_roots`` (Rust only, see ``_rust_crate_roots_index_all``) is
    tried, per hint, right after that hint's own ``_module_matches``
    attempt comes up empty: a Rust trait/type re-exported at its
    crate root (``pub use submodule::*;``) is imported elsewhere by
    crate-qualified path (``use gpui::Render;``), which
    ``_module_matches``'s file-stem check can never match against the
    symbol's *actual* declaring file — round 22 zed.md §3.2. See
    ``_rust_crate_hint_matches`` for the matching rule.

    Round 23 Fix A (``.features/plans/round23/
    09-subtypes-ambiguous-resolution-rate.md``): when the ``hints``
    loop above finds nothing at all -- because neither the callee name
    nor the receiver's leading segment has a local ``use`` binding to
    build a hint from in the first place -- a Rust heritage clause's
    own ``call.receiver`` is tried directly as a bare crate-name hint.
    This covers Rust's fully-qualified impl spelling
    (``impl gpui::Render for X``, no ``use gpui::Render;`` anywhere in
    the file), which previously produced an empty ``hints`` list and
    never even reached ``_rust_crate_hint_matches`` -- the crate-root
    fallback existed but had nothing to loop over.

    ``tiebreak_hits`` (round 24, ``.features/plans/round24/
    03-heritage-crate-decoy-tiebreak.md``) is an optional mutable
    single-element counter, incremented whenever
    ``_prefer_non_synthetic_crate_match`` fires inside either of the
    two crate-hint steps below -- lets ``resolve_heritage()`` disclose
    how many resolved edges rest on that convention-based tiebreak
    rather than a structural match.
    """
    # Workspace narrowing runs first on purpose: for a package-shaped
    # specifier, ``_module_matches``'s stem test below can only ever
    # hit by coincidence (``@cline/shared/Logger`` "matching" an
    # unrelated ``Logger.ts`` in another package), so the package
    # directory is the stronger evidence and gets first say.
    candidates, narrowed = _workspace_narrowed(call, candidates, file_imports)
    if narrowed and len(candidates) == 1:
        return candidates[0]
    candidates, narrowed = _rust_own_crate_narrowed(
        call, candidates, file_imports
    )
    if narrowed and len(candidates) == 1:
        return candidates[0]

    hints: list[str] = []
    imp = file_imports.get(call.name)
    if imp is not None:
        hints.append(imp.source)
    if call.receiver:
        first = _PATH_SPLIT.split(call.receiver)[0]
        rec_imp = file_imports.get(first)
        if rec_imp is not None:
            hints.append(rec_imp.source)
    hinted = _hint_match(
        hints, candidates, crate_roots, call.path, tiebreak_hits
    )
    if hinted is not None:
        return hinted
    receiver_hint = _rust_receiver_crate_match(
        call, candidates, crate_roots, tiebreak_hits
    )
    if receiver_hint is not None:
        return receiver_hint
    if raw_imports:
        matched = [
            c
            for c in candidates
            if any(_module_matches(i.source, c.path) for i in raw_imports)
        ]
        if len(matched) == 1:
            return matched[0]
    return None


def _hint_match(
    hints: list[str],
    candidates: list[Symbol],
    crate_roots: dict[str, list[str]] | None,
    caller_path: str,
    tiebreak_hits: list[int] | None = None,
) -> Symbol | None:
    """Try each ``file_imports``-derived hint against ``candidates``.

    Split out of ``_import_match`` purely to keep that function's
    cyclomatic complexity under the project's Ruff limit -- the
    per-hint ``_module_matches``-then-``_rust_crate_hint_matches``
    loop that function has run unmodified since before round 23.

    Round 24 (``.features/plans/round24/
    03-heritage-crate-decoy-tiebreak.md``): when a hint matches 2+
    crate roots, ``_prefer_non_synthetic_crate_match`` gets one more
    try before this hint gives up -- see that function's own
    docstring for why this can only ever resolve, never misresolve,
    relative to the unmodified ``len(crate_matched) == 1`` check above
    it. ``caller_path`` (``call.path``) is threaded through purely so
    that function can apply its self-crate guard.
    """
    for hint in hints:
        matched = [c for c in candidates if _module_matches(hint, c.path)]
        if len(matched) == 1:
            return matched[0]
        if crate_roots:
            crate_matched = [
                c
                for c in candidates
                if _rust_crate_hint_matches(hint, c.path, crate_roots)
            ]
            if len(crate_matched) == 1:
                return crate_matched[0]
            tiebroken = _prefer_non_synthetic_crate_match(
                crate_matched, caller_path, tiebreak_hits
            )
            if tiebroken is not None:
                return tiebroken
    return None


def _rust_receiver_crate_match(
    call: _Referable,
    candidates: list[Symbol],
    crate_roots: dict[str, list[str]] | None,
    tiebreak_hits: list[int] | None = None,
) -> Symbol | None:
    """Round 23 Fix A: try a heritage clause's bare receiver as a
    crate-name hint directly, split out of ``_import_match`` purely to
    keep that function's cyclomatic complexity under the project's
    Ruff limit.

    Rust's fully-qualified impl spelling (``impl gpui::Render for X``)
    binds no ``use`` for either ``Render`` or ``gpui``, so
    ``_import_match``'s ordinary ``hints`` list (built only from
    ``file_imports`` lookups) stays empty and its crate-aware loop
    never runs. ``call.receiver`` (``"gpui"`` here) is itself a
    directly usable crate-name hint in that case -- reuses
    ``_rust_crate_hint_matches`` unchanged, just fed a hint sourced
    from the call site rather than a ``file_imports`` entry.

    Round 24: the same ``_prefer_non_synthetic_crate_match`` fallback
    ``_hint_match`` gained also applies here, since this is the other
    call site that can produce a 2+-candidate ``crate_matched`` list.
    """
    if not crate_roots or not call.receiver:
        return None
    first = _PATH_SPLIT.split(call.receiver)[0]
    crate_matched = [
        c
        for c in candidates
        if _rust_crate_hint_matches(first, c.path, crate_roots)
    ]
    if len(crate_matched) == 1:
        return crate_matched[0]
    return _prefer_non_synthetic_crate_match(
        crate_matched, call.path, tiebreak_hits
    )


def _alias_candidates(
    call: _Referable,
    file_imports: dict[str, Import],
    index: dict[str, list[Symbol]],
) -> list[Symbol]:
    """Recover candidates when a call/ref uses a local import alias.

    ``import { real as alias } from "..."`` binds a local name to a
    definition the index knows only by its *declared* name, so a
    direct ``index.get(call.name, [])`` lookup on the alias always
    misses. This recovers the pre-alias name from the import's
    ``source`` — its last path-like segment, matching how
    ``_imports_js``/``_imports_python``/``_rust_use_leaf`` all build
    that string — and retries the lookup under that name, filtered to
    symbols whose file plausibly matches the import.

    Args:
        call: The raw call or reference whose bare name missed the
            direct index lookup.
        file_imports: Local name to import record for the calling
            file.
        index: Bare symbol name to every symbol with that name.

    Returns:
        Repo-wide candidates for the recovered original name whose
        file matches the import's source. Empty when ``call.name``
        isn't a known local import, or no original-name candidate's
        file matches — e.g. an alias for a genuinely external
        package, which must keep resolving to ``external``.
    """
    imp = file_imports.get(call.name)
    if imp is None:
        return []
    original = alias_original_name(imp.source)
    return [
        c
        for c in index.get(original, [])
        if _module_matches(imp.source, c.path)
    ]


# Rust std/core/alloc namespace roots -- a fully-qualified inline path
# (``impl std::fmt::Display for X``, ``core::mem::swap(...)``) binds no
# ``use std;`` anywhere in the file, since Rust doesn't require one to
# use a fully-qualified path. ``_receiver_is_external``'s ordinary
# check (first segment has a local ``use``-bound import) always misses
# this shape, so a same-bare-name in-repo collision (e.g. an in-repo
# ``enum Display``) silently wins instead of the receiver being
# recognized as external. Confirmed live against zed round 27 (finding
# M3): ``impl std::fmt::Display for SharedUri`` resolved to an
# unrelated in-repo ``Display`` enum rather than ``(external)``, while
# the sibling ``impl std::fmt::Debug for SharedUri`` on the same line
# happened to show ``(external)`` correctly only because it has no
# in-repo collision to begin with (a different code path, not this
# check).
#
# Round 27 finding M3's original fix re-split ``call.receiver`` looking
# for more than one segment -- but ``call.receiver`` is *already*
# flattened to a single bare token (``"std"``) by the time it reaches
# this function (``_heritage_rust_impl`` -> ``_heritage_name_parts`` ->
# ``_split_callee_text`` in extractor.py keeps only the first and last
# path segments, discarding everything between), so that check could
# structurally never fire (round-27 post-fix verification, finding
# POST-3). Fixed here by testing ``call.text`` instead -- the one field
# that still carries the receiver's genuine, unflattened structure --
# split on the literal ``::`` token specifically (not the general
# ``_PATH_SPLIT``, which also matches ``.`` and would misclassify a
# same-named local variable like ``std.run()`` as a multi-segment
# path). Gated to Rust call/heritage sites specifically (via
# ``languages.spec_for_path``, the same idiom ``_language_filtered``
# already uses) since this text-based check, unlike the original
# receiver-based one, is no longer provably collision-free across every
# language once it reads unflattened text.
_RUST_STD_NAMESPACE_ROOTS = frozenset({"std", "core", "alloc"})


def _rust_std_namespace_root_path(call: RawCall | RawHeritage) -> bool:
    """Whether ``call``/``h`` is a Rust site whose full text is a
    ``std``/``core``/``alloc``-rooted, multi-segment ``::`` path.

    Args:
        call: The raw call or heritage clause being resolved.

    Returns:
        True only for a Rust-language call/heritage site whose full
        original text (``call.text``, unflattened by extraction) has
        two or more ``::``-separated segments with a well-known
        namespace root first.
    """
    spec = languages.spec_for_path(call.path)
    if spec is None or spec.name != "rust":
        return False
    cleaned = re.split(r"[(<]", call.text, maxsplit=1)[0]
    segments = cleaned.split("::")
    return len(segments) > 1 and segments[0].strip() in (
        _RUST_STD_NAMESPACE_ROOTS
    )


def _receiver_is_external(
    call: RawCall | RawHeritage,
    file_imports: dict[str, Import],
    repo_stems: set[str],
) -> bool:
    """Check whether a call/heritage clause's receiver is a non-repo import.

    Runs before the bare-name index lookup so a call like
    ``subprocess.run(...)`` (or a heritage clause like
    ``class MyModel(pydantic.BaseModel):``) is recorded as external
    even when the repo happens to define its own same-named symbol
    elsewhere — the bare-name ladder never gets a chance to misresolve
    or strand it in the ``ambiguous`` bucket. Shared by
    ``_resolve_call`` and ``_resolve_one_heritage`` (not ``_resolve_ref``,
    which has no ``external`` bucket to feed) — only ``call.receiver``
    (and, for the Rust std-namespace check, ``call.text``/``call.path``)
    is read, neither of which ``RawRef`` exposes.

    Args:
        call: The raw call or heritage clause being resolved.
        file_imports: Local name to import record for the calling
            file.
        repo_stems: Every repo file's matching stem (see
            ``_repo_stem``), used to test whether an import's source
            plausibly names a module in this repo.

    Returns:
        True when the receiver resolves to an import whose source
        matches no file in the repo, or when the call/heritage site is
        a Rust ``std``/``core``/``alloc``-rooted multi-segment ``::``
        path (see ``_rust_std_namespace_root_path``) regardless of any
        local ``use`` binding. False when there is no receiver, the
        receiver isn't an import (local variable, ``self``, ...), or
        the import does plausibly point into the repo.
    """
    if not call.receiver:
        return False
    if _rust_std_namespace_root_path(call):
        return True
    first = _PATH_SPLIT.split(call.receiver)[0]
    imp = file_imports.get(first)
    if imp is None:
        return False
    return not _import_is_in_repo(imp, repo_stems)


def _import_segments(source: str) -> set[str]:
    """Split an import source string into path-like segments.

    Args:
        source: Import source string (``pkg.mod.name``, ``a::b::c``).

    Returns:
        The non-empty segments, excluding relative-import markers.
    """
    return {
        s
        for s in _PATH_SPLIT.split(source)
        if s and s not in ("crate", "super", "self")
    }


def _dotted_components(source: str) -> set[str]:
    """``/``-components of an import source that could name a dotted file.

    ``_import_segments`` splits on ``.`` as well as ``/`` (right for
    ``pkg.mod.name``, wrong for ``./catalog.generated-access``), so a
    file whose *stem* contains a dot needs its own comparison set. Each
    dotted component contributes itself (the extensionless JS/TS
    spelling) and itself minus one suffix (``./user.service.js``, the
    ESM spelling of ``user.service.ts``; ``gen/foo.pb.h`` for a C++
    include). A leading-dot component (``.``, ``..``) is a relative
    marker, never a file.

    Args:
        source: Import source string.

    Returns:
        Candidate stems, possibly empty. Only ever compared against
        dotted repo stems.
    """
    out: set[str] = set()
    for part in source.split("/"):
        if "." not in part or part.startswith("."):
            continue
        out.add(part)
        out.add(part.rsplit(".", 1)[0])
    return out


def _repo_stem(path: PurePosixPath) -> str:
    """Compute the stem used to match a file against import sources.

    Args:
        path: Repo-relative path of a file.

    Returns:
        The file's stem, or its parent directory's name for index
        files (``__init__.py``, ``mod.rs``, ...) and for every Go
        file (see below).

    Go's importable unit is the *package*, declared per-directory --
    every ``.go`` file in ``pkg/slug/`` belongs to package ``slug``
    regardless of its own filename (``generator.go``, ``helpers.go``,
    ...). Unlike Python/JS/Rust/C++, where "the file's own stem is the
    importable unit" holds, a Go file's individual stem must never be
    the matching unit -- round-13 master report §1: a qualified
    cross-package call (``slug.Generate(...)`` importing
    ``.../pkg/slug``) against ``pkg/slug/generator.go`` used to compare
    the import source against ``"generator"`` (the file's own stem,
    absent from the import path's segments) instead of ``"slug"`` (the
    package/directory name, present in it), so the call fell through
    to ``external`` instead of resolving. This mirrors the existing
    ``_INDEX_STEMS`` directory-name fallback but applies
    unconditionally to every ``.go`` file, not just index files.
    """
    if path.suffix == ".go" and path.parent.name:
        return path.parent.name
    stem = path.stem
    if stem in _INDEX_STEMS and path.parent.name:
        return path.parent.name
    return stem


def _module_matches(source: str, candidate_path: str) -> bool:
    """Check whether an import source plausibly names a file.

    Args:
        source: Import source string (``pkg.mod.name``, ``a::b::c``).
        candidate_path: Repo-relative path of a candidate symbol.

    Returns:
        True when the file's stem (or its directory, for index files
        like ``__init__.py`` / ``mod.rs``) appears in the source.

    A *bare* (non-relative) JS/TS import source naming a Node core
    module (``_NODE_BUILTIN_MODULE_NAMES``) never matches, regardless
    of stem collision -- round 22 claude-buddy.md §2.1: ``import {
    join } from "path"`` was matching a repo's own ``server/path.ts``
    purely because both reduce to the segment ``"path"``. A genuine
    relative import (``"./path"``) is unaffected -- only the bare
    specifier is denylisted -- and the ``candidate_path`` extension
    gate keeps this JS/TS-only, leaving every other language's
    stem-matching untouched.

    A *dotted* stem (``catalog.generated-access.ts``, Angular's
    ``user.service.ts``, a C++ ``foo.pb.h``) gets a second,
    component-wise check -- see ``_dotted_components``. Round 31
    cline.md §4.1 Bug B: ``_import_segments`` splits on ``.``, so such
    a stem could never appear among the segments, and a plain relative
    import of a dotted filename silently lost its import hint. Gated
    on the stem actually containing a dot, so every undotted file
    matches exactly as before.

    Checked against ``source.split("/", 1)[0]``, not the whole
    ``source`` string -- ``extractor._imports_js`` encodes every
    *named* import's ``source`` as ``f"{module}/{name}"`` (e.g.
    ``"path/join"`` for the ``join`` example above, confirmed via
    direct extraction: only the rarer side-effect-import shape
    (``import "path";``, no local binding) keeps ``source`` as the
    bare module string with no suffix). Comparing the whole string
    against the denylist would silently miss every named/default/
    namespace import -- the dominant shape in the actual claude-buddy
    repro -- and only catch the side-effect case.
    """
    if (
        not source.startswith((".", "/"))
        and source.split("/", 1)[0] in _NODE_BUILTIN_MODULE_NAMES
        and candidate_path.endswith(_JS_TS_EXTENSIONS)
    ):
        return False
    stem = _repo_stem(PurePosixPath(candidate_path))
    if stem in _import_segments(source):
        return True
    return "." in stem and stem in _dotted_components(source)


_SYNTHETIC_CRATE_DIR_MARKERS = frozenset(
    {
        "test_fixture",
        "test_fixtures",
        "fixture",
        "fixtures",
        "testdata",
        "mock",
        "mocks",
        "vendor",
        "third_party",
    }
)


def _rust_crate_dir(candidate_path: str) -> str:
    """Best-effort crate-root directory (``src/``'s own parent) for a
    candidate's own file path, derived purely from path shape.

    Walks upward from the candidate file's own directory looking for
    the nearest ancestor directory literally named ``src`` and returns
    *its* parent -- the same convention ``_rust_crate_roots_index_all``
    keys its index by (a crate name maps to its ``src/`` directory),
    applied here in reverse to an arbitrary candidate file
    that may be nested arbitrarily deep inside that ``src/`` tree
    (unlike the root-file-only shape ``_rust_crate_roots_index_all``
    itself scans for), so a candidate several submodules deep still
    resolves to its crate's own root directory, not some intermediate
    submodule directory.

    Args:
        candidate_path: Repo-relative path of a candidate symbol
            (e.g. ``"tooling/lints/test_fixture/gpui/src/lib.rs"``).

    Returns:
        The crate directory (``src/``'s parent), e.g.
        ``"tooling/lints/test_fixture/gpui"``. Falls back to the
        candidate's own parent directory when no ``src`` ancestor is
        found -- a path shape this heuristic can't classify either
        way, which ``_looks_like_synthetic_crate_root`` will then
        correctly find no markers in.
    """
    d = _dirname(candidate_path)
    while d:
        if d.rsplit("/", 1)[-1] == "src":
            return _dirname(d)
        d = _dirname(d)
    return _dirname(candidate_path)


def _looks_like_synthetic_crate_root(crate_dir: str) -> bool:
    """Whether a Rust crate root's own *ancestor* path segments look
    like a lint-testing/vendor stand-in rather than a real workspace
    member.

    Deliberately checks every path segment *above* the crate's own
    leaf directory name (``crate_dir.rsplit("/", 1)[-1]``), never the
    leaf itself -- a real, published crate can legitimately be named
    ``test-utils``/``fixtures``/``mock-server`` (common package names
    in the wild); it is the *containing* directory trail
    (``tooling/lints/test_fixture/gpui`` -- the ``gpui`` leaf is fine,
    ``test_fixture`` one level up is the tell) that signals "this
    crate exists to test something else," per the exact zed shape this
    is scoped against (round 23 design doc's own "preferring a root
    that looks more like a real Cargo workspace member over one nested
    under a `test_fixture`/`vendor`-shaped path" suggestion).

    Args:
        crate_dir: A crate root's ``src/``-parent directory (see
            ``_rust_crate_dir``), e.g.
            ``"tooling/lints/test_fixture/gpui"``.

    Returns:
        True when any ancestor segment (excluding the crate's own leaf
        directory) matches a known synthetic/vendor marker.
    """
    segments = crate_dir.split("/")[:-1]
    return any(seg in _SYNTHETIC_CRATE_DIR_MARKERS for seg in segments)


def _prefer_non_synthetic_crate_match(
    crate_matched: list[Symbol],
    caller_path: str,
    tiebreak_hits: list[int] | None = None,
) -> Symbol | None:
    """Round 24 heritage crate-decoy tiebreak (``.features/plans/
    round24/03-heritage-crate-decoy-tiebreak.md``): break a 2+-way
    crate-root collision when exactly one candidate's crate root
    doesn't look synthetic (test-fixture/vendor-shaped).

    Only ever returns non-``None`` when a step below leaves *exactly*
    one survivor -- a genuine collision between two real, non-synthetic
    crates (or two synthetic ones) stays correctly ambiguous, matching
    this module's existing "resolve only on an unambiguous signal"
    contract throughout (see ``_pick_candidate``'s own docstring and
    round 23's "report as ambiguous rather than guessed" philosophy).
    This is a strictly additive fallback tried only *after*
    ``_rust_crate_hint_matches``'s own ``len(crate_matched) == 1``
    check has already failed -- it can only ever turn a previously-
    ambiguous edge into a resolved one, never the reverse.

    Checked *before* the synthetic-path filter: if the clause's own
    caller file lives inside one of ``crate_matched``'s own crate
    roots, that candidate wins outright, synthetic-looking or not. A
    decoy crate's own test code legitimately writing
    ``impl gpui::Render for Y`` from *inside*
    ``tooling/lints/test_fixture/gpui`` itself (the crate's own
    external name is always in scope for self-reference, Rust 2018+)
    must resolve to the decoy's own ``Render``, not get silently
    redirected to a different, unrelated real crate just because that
    other crate's path looks more legitimate -- the design doc's own
    test plan calls this out explicitly: "the tiebreak must not
    blindly prefer 'not synthetic' when the caller itself lives inside
    the synthetic crate's own tree." This self-crate check does not
    increment ``tiebreak_hits`` -- unlike the marker-based guess below,
    "the caller's own file lives in this exact candidate's crate" is a
    structural fact, not a convention-based guess.

    Args:
        crate_matched: Every candidate whose path matched the crate
            name hint across 2+ registered crate roots (round 23 Fix
            B) -- the set this tiebreak narrows.
        caller_path: Repo-relative path of the file declaring the
            heritage clause being resolved (``call.path``) -- used
            only for the self-crate check above.
        tiebreak_hits: When given, incremented by one each time the
            synthetic-marker tiebreak actually fires (returns
            non-``None`` via that path) -- lets ``resolve_heritage()``
            count how many resolved edges rest on this convention-based
            guess rather than a structural match, for disclosure to
            callers (``query subtypes``/``supertypes``).

    Returns:
        The winning candidate, or ``None`` when neither step above
        narrows ``crate_matched`` to exactly one.
    """
    caller_crate_dir = _rust_crate_dir(caller_path)
    same_crate = [
        c for c in crate_matched if _rust_crate_dir(c.path) == caller_crate_dir
    ]
    if len(same_crate) == 1:
        return same_crate[0]
    non_synthetic = [
        c
        for c in crate_matched
        if not _looks_like_synthetic_crate_root(_rust_crate_dir(c.path))
    ]
    if len(non_synthetic) == 1:
        if tiebreak_hits is not None:
            tiebreak_hits[0] += 1
        return non_synthetic[0]
    return None


def _prefer_non_synthetic_crate_root(
    crate_dirs: list[str], importer_path: str
) -> str | None:
    """Round 25 ``dekko deps`` crate-decoy tiebreak (``.features/plans/
    round25/02-deps-crate-decoy-tiebreak.md``): directory-level sibling
    of ``_prefer_non_synthetic_crate_match``, for
    ``_resolve_import_rust``'s bare-crate-name lookup.

    Import resolution has no ``Symbol`` candidates the way heritage
    resolution does at this point -- only the crate-root directory
    strings ``_rust_crate_roots_index_all`` collected for a colliding
    crate name -- so this mirrors ``_prefer_non_synthetic_crate_match``'s
    two-step logic (self-crate wins outright; otherwise the sole
    non-synthetic-looking root wins) over that different shape rather
    than reusing it directly.

    Args:
        crate_dirs: Every crate-root ``src/`` directory registered
            under the colliding crate name (``ctx.crate_roots``'s
            value for that name, already known to have 2+ entries by
            the caller).
        importer_path: Repo-relative path of the file declaring the
            ``use`` import being resolved -- used for the self-crate
            check, mirroring ``_prefer_non_synthetic_crate_match``'s
            ``caller_path``.

    Returns:
        The winning crate-root directory, or ``None`` when neither the
        self-crate check nor the synthetic-marker filter narrows
        ``crate_dirs`` to exactly one -- a genuine, still-ambiguous
        collision, left for the caller to treat as unresolved.
    """
    importer_crate_dir = _rust_crate_dir(importer_path)
    same_crate = [
        d for d in crate_dirs if _rust_crate_dir(d) == importer_crate_dir
    ]
    if len(same_crate) == 1:
        return same_crate[0]
    non_synthetic = [
        d
        for d in crate_dirs
        if not _looks_like_synthetic_crate_root(_rust_crate_dir(d))
    ]
    if len(non_synthetic) == 1:
        return non_synthetic[0]
    return None


def _rust_crate_hint_matches(
    hint: str, candidate_path: str, crate_roots: dict[str, list[str]]
) -> bool:
    """Check a Rust import hint against a candidate via its crate root.

    ``_module_matches`` only ever compares an import source against
    the *candidate's own declaring file's* stem (or its immediate
    parent directory, for index files) — round 22 zed.md §3.2: a
    trait re-exported at its crate root via ``pub use
    submodule::*;`` (e.g. ``gpui``'s ``pub use element::*;``) is
    imported elsewhere as ``use gpui::{..., Render, ...};``, whose
    source ``"gpui::Render"`` contains neither ``"element"`` (the
    real declaring file's stem) nor anything else ``_module_matches``
    can key off, so a same-named repo-wide collision on the trait
    name (e.g. a same-named fixture crate) falls all the way through
    to ambiguous even though the import hint, read correctly, points
    unambiguously at the real trait.

    This reuses ``_rust_crate_roots_index_all`` (built once per
    ``resolve_heritage()`` call, a repo-wide crate-name -> *every*
    matching crate-root-``src/``-directory index) to check the
    *crate*, not just the file: does the hint's leading path segment
    name a known crate, and does the candidate's own file live under
    any of that crate name's root directories.

    Args:
        hint: One import source string from ``_import_match``'s hint
            list (e.g. ``"gpui::Render"``), or (round 23 Fix A) a bare
            heritage-clause receiver segment used directly as a
            crate-name hint.
        candidate_path: Repo-relative path of a candidate symbol.
        crate_roots: Crate name to every matching crate's ``src/``
            directory, as built by ``_rust_crate_roots_index_all``.

    Returns:
        True when ``candidate_path`` is a Rust file living under any
        root registered for the crate name in ``hint``'s leading
        segment.

    Round 23 Fix B (``.features/plans/round23/
    09-subtypes-ambiguous-resolution-rate.md``) -- see that design
    doc's "Implemented" note for the live measurement this decision
    is based on. Before Fix B, ``crate_roots`` held one root per crate
    *name* (``dict[str, str]``), so two same-named crates (an
    in-workspace one and an unrelated same-named fixture/vendor crate
    elsewhere in the repo -- the exact zed shape this was verified
    against, ``tooling/lints/test_fixture/gpui`` shadowing the real
    ``gpui`` crate) silently collapsed onto whichever one
    ``_rust_crate_roots_index`` happened to index last, which measured
    live as a genuine 50/50 coin flip across process hash seeds: about
    half the time every affected clause resolved correctly (the real
    crate won), the other half every one of them silently resolved to
    the *wrong* (fixture) crate's same-named symbol -- confidently
    wrong data, not merely missing data. Matching *any* registered
    root instead removes the coin flip entirely: a genuine collision
    (both roots match, ``len(crate_matched) > 1``) now deterministically
    falls through to ``heritage_ambiguous`` instead of a 50%-chance
    silent misattribution, matching this module's own stated
    philosophy ("report as ambiguous rather than guessed" -- see the
    module docstring). This trades away the "lucky half" of the old
    coin flip's resolved-count along with the "unlucky half"'s silent
    wrong answers -- an intentional, examined tradeoff, not an
    oversight; see the design doc for the exact numbers.

    Round 24 (``.features/plans/round24/
    03-heritage-crate-decoy-tiebreak.md``) narrows the residual gap
    Fix B's "genuine collision -> ambiguous" left unattempted: when
    ``len(crate_matched) > 1`` here, this function's own caller
    (``_hint_match``/``_rust_receiver_crate_match``) now tries
    ``_prefer_non_synthetic_crate_match`` before giving up -- if
    exactly one matched candidate's crate root avoids a test-fixture/
    vendor-shaped ancestor path (``_looks_like_synthetic_crate_root``),
    that one wins instead of falling through to ambiguous. A genuine
    collision between two equally-plausible-looking real crates (round
    23's own regression fixture) is untouched by this and still
    resolves ambiguous, exactly as before -- the narrowing only ever
    fires on the specific, narrower shape of "one candidate's path
    looks like a lint-testing/vendor stand-in, the other doesn't."
    """
    if not candidate_path.endswith(".rs"):
        return False
    segments = _PATH_SPLIT.split(hint)
    if not segments or not segments[0]:
        return False
    crate_dirs = crate_roots.get(segments[0], [])
    return any(
        candidate_path == d or candidate_path.startswith(f"{d}/")
        for d in crate_dirs
    )


def _build_index(files: list[FileMap]) -> dict[str, list[Symbol]]:
    """Map bare symbol name → all symbols with that name."""
    index: dict[str, list[Symbol]] = {}
    for fm in files:
        for sym in fm.symbols:
            index.setdefault(sym.name, []).append(sym)
    return index


def _build_name_path_index(
    files: list[FileMap],
) -> dict[tuple[str, str], list[Symbol]]:
    """Map ``(bare name, file path)`` → the like-named symbols in that file.

    Lets the resolver's same-file and self-container checks be O(1)
    dict lookups instead of scanning every repo-wide candidate for a
    very common name.
    """
    index: dict[tuple[str, str], list[Symbol]] = {}
    for fm in files:
        for sym in fm.symbols:
            index.setdefault((sym.name, sym.path), []).append(sym)
    return index


# ---------------------------------------------------------------------
# Workspace packages (JS/TS monorepos)
#
# ``import { ApiHandler } from "@cline/llms"`` names a *package*, not a
# file: nothing in the specifier is a file stem, so the stem-based
# "does this import point into the repo" test every external guard
# relied on (``_import_segments(source) & repo_stems``) called it an
# npm dependency, and the call/heritage clause landed in ``external``
# before the candidate ladder ever ran. Round 31 cline.md §4.1 Bug A
# found it as one missing ``query subtypes`` row; measured against
# cline it was ~900 call edges, 12 heritage edges and 2,610 import
# bindings across 690 files -- the dominant import shape of the whole
# TS-monorepo repo class.
#
# The table below (package ``name`` -> package directory, declared
# workspace members only) is the JS/TS analog of
# ``_rust_crate_roots_index_all``: the import names a package root, so
# candidates are narrowed to the ones living under it. Membership is
# attached to the import binding itself (``_WorkspaceImport``) rather
# than threaded as yet another parameter, so it reaches every ladder
# step -- and every pool worker, via the already-pickled
# ``imports_by_file`` -- with no signature changes.

_WORKSPACE_MANIFESTS = frozenset({"package.json", "pnpm-workspace.yaml"})


@dataclass
class _WorkspaceImport(Import):
    """An import binding whose source names an in-repo workspace package.

    Attributes:
        package_dir: Repo-relative directory of the workspace package
            the source resolves into.
    """

    package_dir: str = ""


def load_workspace_packages(root: Path) -> dict[str, str]:
    """Discover the repo's JS/TS workspace packages.

    Only *declared workspace members* count: a ``package.json`` whose
    directory matches a ``workspaces`` glob (npm/yarn/bun, array or
    ``{"packages": [...]}`` form) or a ``pnpm-workspace.yaml``
    ``packages`` entry of some manifest above it. A bare "any
    ``package.json`` with a ``name``" rule is unsound -- cline itself
    ships a stub package literally named ``vscode`` (``apps/vscode/
    standalone/runtime-files/vscode``) that is no workspace member, and
    would otherwise capture every real ``import * as vscode from
    "vscode"`` in the repo.

    Discovery reuses ``walker.find_config_files``, so ``node_modules``
    and other vendored/ignored trees are excluded exactly as they are
    for source files. A name claimed by two member directories is
    dropped: no evidence beats a coin flip.

    Args:
        root: Repository root.

    Returns:
        Package ``name`` → repo-relative package directory. Empty when
        the repo declares no workspaces, which leaves every resolution
        path byte-identical to a repo this feature never touched.
    """
    manifests = _load_workspace_manifests(root)
    return {name: m.package_dir for name, m in manifests.items()}


@dataclass(frozen=True)
class _WorkspaceManifest:
    """One workspace member's directory plus its parsed ``package.json``.

    Attributes:
        package_dir: Repo-relative package directory.
        data: The parsed manifest, kept whole: the module graph reads
            its entry-point fields (``exports``/``main``/``types``/...)
            to turn a bare package specifier into a source file.
    """

    package_dir: str
    data: dict


def _load_workspace_manifests(root: Path) -> dict[str, _WorkspaceManifest]:
    """Package ``name`` → manifest, for every declared workspace member.

    The single discovery pass behind both ``load_workspace_packages``
    (name → directory, all the symbol-level passes need) and
    ``resolve_imports`` (which also needs each member's entry-point
    fields). See ``load_workspace_packages`` for the membership rules.
    """
    named: dict[str, list[_WorkspaceManifest]] = {}
    patterns: list[tuple[str, str]] = []
    for rel in walker.find_config_files(root, _WORKSPACE_MANIFESTS):
        base = _dirname(rel)
        if rel.endswith(".yaml"):
            globs = _pnpm_workspace_globs(root, rel)
        else:
            data = _read_jsonc_config(root, rel)
            if data is None:
                continue
            name = data.get("name")
            if isinstance(name, str) and name:
                named.setdefault(name, []).append(
                    _WorkspaceManifest(package_dir=base, data=data)
                )
            globs = _package_json_workspace_globs(data)
        patterns.extend((base, g) for g in globs)

    if not patterns:
        return {}

    manifests: dict[str, _WorkspaceManifest] = {}
    for name, found in named.items():
        members = [
            m for m in found if _is_workspace_member(m.package_dir, patterns)
        ]
        if len(members) == 1:
            manifests[name] = members[0]
    return manifests


def workspace_fingerprint(root: Path) -> str:
    """Digest of the workspace package table, for cache invalidation.

    The cached call pass (``storage.resolvecache``) is gated on the
    repo's *source* path set and symbols being unchanged, neither of
    which moves when a ``package.json`` is renamed or a ``workspaces``
    glob edited -- yet either changes what the ladder resolves.

    Returns:
        A stable hex digest, or ``""`` for a repo with no workspaces.
    """
    packages = load_workspace_packages(root)
    if not packages:
        return ""
    payload = json.dumps(sorted(packages.items())).encode()
    return hashlib.sha256(payload).hexdigest()


def _package_json_workspace_globs(data: dict) -> list[str]:
    """A ``package.json``'s ``workspaces`` globs, either spelling."""
    spec = data.get("workspaces")
    if isinstance(spec, dict):
        spec = spec.get("packages")
    if not isinstance(spec, list):
        return []
    return [g for g in spec if isinstance(g, str) and g]


def _pnpm_workspace_globs(root: Path, rel: str) -> list[str]:
    """The ``packages:`` block-list entries of a ``pnpm-workspace.yaml``.

    A deliberately tiny reader rather than a YAML dependency (dekko
    ships none): the file's real-world shape is one top-level
    ``packages:`` key holding a block list of quoted globs. The rarer
    flow spelling (``packages: ["a", "b"]``) yields nothing, which
    degrades to "no workspaces declared here", never to a wrong answer.
    """
    try:
        text = (root / rel).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []

    globs: list[str] = []
    in_packages = False
    for raw in text.splitlines():
        line = raw.split(" #", 1)[0].rstrip()
        item = line.strip()
        if not item or item.startswith("#"):
            continue
        if item.startswith("-"):
            if in_packages:
                globs.append(item[1:].strip().strip("'\""))
            continue
        if not raw[0].isspace():
            in_packages = item.split(":", 1)[0].strip() == "packages"
    return [g for g in globs if g]


def _is_workspace_member(
    package_dir: str, patterns: list[tuple[str, str]]
) -> bool:
    """Whether ``package_dir`` matches a declared workspace glob.

    Args:
        package_dir: Repo-relative directory holding a ``package.json``.
        patterns: ``(declaring manifest's directory, glob)`` pairs. A
            glob is relative to its own manifest; a leading ``!``
            excludes, and an exclusion from the same manifest wins.
    """
    included = False
    for base, pattern in patterns:
        if base and not package_dir.startswith(base + "/"):
            continue
        rel = package_dir[len(base) + 1 :] if base else package_dir
        if not rel:
            continue
        negated = pattern.startswith("!")
        glob = pattern.lstrip("!").removeprefix("./").strip("/")
        if not _glob_segments_match(glob.split("/"), rel.split("/")):
            continue
        if negated:
            return False
        included = True
    return included


def _glob_segments_match(pattern: list[str], parts: list[str]) -> bool:
    """Match path segments against glob segments (``**`` spans any)."""
    if not pattern:
        return not parts
    if pattern[0] == "**":
        return any(
            _glob_segments_match(pattern[1:], parts[i:])
            for i in range(len(parts) + 1)
        )
    if not parts:
        return False
    return fnmatch.fnmatchcase(parts[0], pattern[0]) and _glob_segments_match(
        pattern[1:], parts[1:]
    )


def _workspace_package_name(source: str) -> str | None:
    """The package-name prefix of a bare JS/TS import source.

    npm names are ``name`` or ``@scope/name``, so the prefix is read
    straight off the source in O(1) rather than probing every known
    package -- this runs once per import binding in the repo.
    """
    if not source or source[0] in "./":
        return None
    parts = source.split("/")
    if source[0] != "@":
        return parts[0]
    if len(parts) < 2:
        return None
    return f"{parts[0]}/{parts[1]}"


def _workspace_tagged(
    imp: Import, workspace_pkgs: dict[str, str] | None
) -> Import:
    """``imp``, upgraded to a ``_WorkspaceImport`` when it names one."""
    if not workspace_pkgs or not imp.path.endswith(_JS_TS_EXTENSIONS):
        return imp
    name = _workspace_package_name(imp.source)
    package_dir = workspace_pkgs.get(name) if name else None
    if package_dir is None:
        return imp
    return _WorkspaceImport(
        path=imp.path,
        name=imp.name,
        source=imp.source,
        package_dir=package_dir,
    )


_RUST_IN_CRATE_PREFIXES = ("crate::", "super::", "self::")


_RELATIVE_SOURCE_PREFIXES = ("./", "../")


def _import_is_in_repo(imp: Import, repo_stems: set[str]) -> bool:
    """Whether an import binding plausibly points into this repo.

    True for a workspace-package import (see ``_WorkspaceImport``), or
    when any segment of the source is a repo file stem -- the original,
    file-shaped test, which a package-shaped specifier can only ever
    pass by coincidence. A dotted filename's stem is never a single
    segment, hence the second, component-wise check (see
    ``_dotted_components``).
    """
    if isinstance(imp, _WorkspaceImport):
        return True
    if imp.source.startswith(_RUST_IN_CRATE_PREFIXES):
        # ``use crate::{AgentTool};`` is in-repo by definition, whatever
        # the file stems say. The stem test fails it whenever the crate
        # root merely *re-exports* the name (``pub use thread::*;``):
        # ``_import_segments`` drops ``crate`` itself, leaving only
        # ``AgentTool``, which is no file's stem. Round 31 zed coverage
        # pass F1: 218 of zed's 2,503 ``heritage_external`` entries
        # named an in-repo trait this way, and ``query subtypes
        # AgentTool`` showed 11 of 35 implementors with no hint that 24
        # were missing. A file reaching the same trait through a glob
        # resolved fine, which was the tell.
        return True
    if imp.source.startswith(_RELATIVE_SOURCE_PREFIXES):
        # ``import { run } from "./index"`` / ``from "."`` / ``from
        # ".."``: a relative specifier is in-repo by definition, and
        # the stem test can't see it. An index file's matching stem is
        # its *directory* name (``acp`` for ``acp/index.ts``, see
        # ``_repo_stem``), which ``./index`` and ``.`` never spell, so
        # the binding looked external and ``_shadowed_by_external_
        # import`` threw the call to noise. Round 32 Track 5 found it:
        # recording ``await import("./index")`` as an import cost cline
        # 9 correct test-to-module call edges until this was fixed.
        return True
    if _import_segments(imp.source) & repo_stems:
        return True
    return bool(_dotted_components(imp.source) & repo_stems)


def _workspace_narrowed(
    call: _Referable,
    candidates: list[Symbol],
    file_imports: dict[str, Import],
) -> tuple[list[Symbol], bool]:
    """Narrow ``candidates`` to the workspace package the name came from.

    Only the call's *own name* binding is used (``import { X } from
    "@scope/pkg"`` then ``X(...)`` / ``implements X``). A receiver
    binding (``Ns.fn()``) is deliberately not: ``Ns`` is routinely a
    namespace one package re-exports from another, where the imported
    package's directory says nothing about where ``fn`` lives.

    Within the package, exported candidates are preferred -- a name
    importable from outside the package is by definition exported, so
    a same-named private helper or method is not the target.

    Zero in-package candidates means a cross-package re-export (the
    name is defined elsewhere and re-exported through this package's
    barrel): that is *no* evidence, not negative evidence, so the
    candidate list is returned untouched.

    Returns:
        ``(candidates, narrowed)`` -- ``narrowed`` is True only when
        the returned list is a strict in-package subset.
    """
    imp = file_imports.get(call.name)
    if not isinstance(imp, _WorkspaceImport):
        return candidates, False
    prefix = f"{imp.package_dir}/" if imp.package_dir else ""
    inside = [c for c in candidates if c.path.startswith(prefix)]
    if not inside:
        return candidates, False
    exported = [c for c in inside if c.exported]
    return (exported or inside), True


def _rust_own_crate_narrowed(
    call: _Referable,
    candidates: list[Symbol],
    file_imports: dict[str, Import],
) -> tuple[list[Symbol], bool]:
    """Narrow to the importing file's own crate for a ``crate::`` import.

    The Rust counterpart of ``_workspace_narrowed``. ``use crate::Foo;``
    says ``Foo`` is reachable from *this* crate's root, so when the
    repo has two ``Foo``s, the one inside this crate is the one meant
    (round 31 zed coverage pass F6: ``Foo::build()`` landed on an
    unrelated crate's ``Foo.build``, a crate that isn't even a
    dependency). Checked for the call's own name and for its
    receiver's leading segment.

    Zero in-crate candidates is the common, legitimate case of a crate
    root re-exporting another crate's type (``editor``: ``pub use
    multi_buffer::MultiBuffer;``), so it is no evidence and the list is
    returned untouched.
    """
    # A bare call's own name is the imported binding. With a receiver,
    # the name is a member *of that receiver* and an import of the same
    # word is a coincidence: ``proto::view::Variant::Editor(..)`` is not
    # the ``Editor`` this file imported from ``crate::``.
    # ``RawRef`` carries neither ``receiver`` nor ``text``.
    receiver = getattr(call, "receiver", None)
    text = getattr(call, "text", "") or ""
    names = [] if receiver else [call.name]
    if receiver and text == f"{receiver}::{call.name}":
        # Only a pure ``Type::name`` path. For a chained receiver
        # (``Store::global(cx).read(cx)``) the import says where
        # ``Store`` lives, which is nothing about ``read``: live-testing
        # on zed had that landing on the crate's one free ``fn read``.
        names.append(_PATH_SPLIT.split(receiver)[0])
    # ``crate::point(..)`` spells the crate root out at the call site
    # itself: no import needed to know it means this crate.
    literal = text.startswith(_RUST_IN_CRATE_PREFIXES)
    if not literal and not any(
        (imp := file_imports.get(n)) is not None
        and imp.source.startswith(_RUST_IN_CRATE_PREFIXES)
        for n in names
    ):
        return candidates, False
    own = _rust_crate_dir(call.path)
    inside = [c for c in candidates if _rust_crate_dir(c.path) == own]
    if not inside or len(inside) == len(candidates):
        return candidates, False
    return inside, True


def _imports_by_file(
    files: list[FileMap], workspace_pkgs: dict[str, str] | None = None
) -> dict[str, dict[str, Import]]:
    """Map file path → local name → import record.

    ``workspace_pkgs`` (see ``load_workspace_packages``), when given,
    upgrades every JS/TS binding that names a workspace package to a
    ``_WorkspaceImport``. ``None``/empty leaves every record as-is.
    """
    out: dict[str, dict[str, Import]] = {}
    for fm in files:
        table = out.setdefault(fm.path, {})
        for imp in fm.imports:
            if imp.name not in table:
                table[imp.name] = _workspace_tagged(imp, workspace_pkgs)
    return out


def _build_adjacency(graph: CallGraph) -> None:
    """Fill ``calls_out`` / ``calls_in`` from the edge list."""
    for edge in graph.edges:
        graph.calls_out.setdefault(edge.caller, []).append(edge.callee)
        graph.calls_in.setdefault(edge.callee, []).append(edge.caller)
    for table in (graph.calls_out, graph.calls_in):
        for key in table:
            table[key] = sorted(set(table[key]))


# ---------------------------------------------------------------------
# Module-level dependency graph (``dekko deps``)
#
# A structurally different resolution problem from everything above:
# every ladder step in this file resolves a bare *name* (a call,
# reference, or heritage clause) against repo-wide symbol candidates.
# An import source string names a *module/file*, not a symbol — there
# is no "same-file"/"typed-parameter"/"receiver-type" candidate ladder
# to run, since a source string built from dots/slashes either maps to
# exactly one real file once resolved, or it doesn't (see each
# per-language resolver's own docstring for its construction rule).
# ``resolve_imports`` is therefore its own self-contained pass, not a
# thin wrapper around ``_pick_candidate``.


@dataclass(frozen=True)
class _ImportResolveContext:
    """Precomputed, read-only lookup structures for import resolution.

    Built once per ``resolve_imports`` call (O(files)) and shared
    read-only across every import in the repo, so no per-language
    resolver ever re-scans the full file list — every lookup below is
    O(1) or bounded by a file's own path depth, never O(files) per
    import (see ``resolve_imports``'s own docstring for why this
    matters at scale).

    Attributes:
        paths: Every file path known to the map, for direct membership
            checks.
        py_package_roots: Top-level Python package name (the basename
            of a directory containing ``__init__.py`` whose own parent
            does not) → the directory path(s) with that basename. Used
            to resolve absolute (non-relative) Python imports against
            the repo's real package layout, wherever that layout lives
            (``src/pkg/...``, ``pkg/...``, ...) rather than assuming
            packages sit directly at the repo root.
        java_suffix_index: Path suffix (with any of the well-known
            Maven/Gradle source-root prefixes stripped, plus the raw
            path itself) → matching real ``.java`` file path(s). Lets
            ``import com.foo.Bar;`` resolve against ``src/main/java/
            com/foo/Bar.java``-style layouts without hardcoding one
            specific root.
        cpp_basename_index: C/C++ header/source basename → matching
            real path(s), scoped to C/C++-shaped extensions only (a
            same-named Python/JS file must never satisfy a ``#include``
            filename search).
        crate_roots: Rust workspace-member crate name → every matching
            crate root directory (its ``src/``), for every crate this
            repo's convention-based detection can find (see
            ``_rust_crate_roots_index_all``). Lets a bare, non-``crate``/
            ``self``/``super`` ``use`` path be recognized as a
            cross-crate, in-workspace import rather than assumed
            external by default. Collision-aware (round 25, ``.features/
            plans/round25/02-deps-crate-decoy-tiebreak.md``): a crate
            name matching two or more directories (e.g. a real crate
            plus a same-named test-fixture/vendor stand-in) keeps every
            match here rather than picking one, so
            ``_resolve_import_rust`` can apply the same synthetic-crate
            tiebreak round 24 already applies to heritage resolution
            instead of silently guessing.
        ts_path_aliases: Each discovered ``tsconfig.json``/
            ``jsconfig.json``'s own directory (its scope) → the
            resolved ``_TsConfigAliasTable`` (``baseUrl``/``paths``,
            with its own ``extends`` chain already merged in) that
            governs JS/TS/TSX files under that directory. Empty when
            ``resolve_imports`` was called with no ``root`` (see its
            docstring) — every existing in-memory-``FileMap`` caller
            keeps today's "bare specifier is always external" behavior
            for ``_resolve_import_js`` exactly, since this dict never
            gets populated without a real filesystem root to search.
        workspace_manifests: JS/TS workspace package name → its
            directory and parsed ``package.json`` (see
            ``_load_workspace_manifests``), so a bare ``@scope/pkg``
            specifier resolves to that package's source entry file
            instead of reading as an npm dependency. Empty without a
            root or without declared workspaces.
    """

    paths: frozenset[str]
    py_package_roots: dict[str, list[str]] = field(default_factory=dict)
    java_suffix_index: dict[str, list[str]] = field(default_factory=dict)
    cpp_basename_index: dict[str, list[str]] = field(default_factory=dict)
    crate_roots: dict[str, list[str]] = field(default_factory=dict)
    workspace_manifests: dict[str, "_WorkspaceManifest"] = field(
        default_factory=dict
    )
    ts_path_aliases: dict[str, "_TsConfigAliasTable"] = field(
        default_factory=dict
    )


def _dirname(path: str) -> str:
    """Repo-relative parent directory of ``path`` (``""`` at the root)."""
    head, _, _ = path.rpartition("/")
    return head


def _ancestor_dir(path: str, levels: int) -> str:
    """Walk ``levels`` directories up from ``path`` (clamped at root)."""
    if levels <= 0 or not path:
        return path
    parts = path.split("/")
    if levels >= len(parts):
        return ""
    return "/".join(parts[:-levels])


def _first_match(paths: frozenset[str], candidates: list[str]) -> str | None:
    """First of ``candidates`` that names a real file, else ``None``."""
    for c in candidates:
        if c in paths:
            return c
    return None


def _dir_module_candidates(
    base_dir: str,
    parts: list[str],
    file_ext: str,
    index_names: tuple[str, ...],
) -> list[str]:
    """File-path candidates for ``parts`` as a module path under
    ``base_dir``, in a directory-per-package language (Python/Rust).

    ``parts`` is treated as fully naming a module: either a leaf file
    (``base_dir/parts/joined.ext``) or a package/sub-module directory
    (``base_dir/parts/joined/<one of index_names>``). With ``parts``
    empty, only the package-index form is meaningful (there is no
    bare-file candidate for "the current directory itself").
    ``index_names`` takes more than one entry only for Rust's crate
    root, whose own index file is ``lib.rs``/``main.rs`` rather than
    the ordinary ``mod.rs`` every other package directory uses.

    Args:
        base_dir: Directory the module path is rooted at (``""`` for
            the repo root).
        parts: Dotted/``::``-separated path segments, already split.
        file_ext: Leaf-module file extension (``.py``, ``.rs``).
        index_names: Package-index filename(s) to try, in order
            (``("__init__.py",)``, ``("mod.rs",)``, or
            ``("lib.rs", "main.rs")`` for a Rust crate root).

    Returns:
        Candidate repo-relative paths, most-specific first.
    """
    if not parts:
        return [
            f"{base_dir}/{name}" if base_dir else name for name in index_names
        ]
    joined = "/".join(parts)
    base = f"{base_dir}/{joined}" if base_dir else joined
    return [f"{base}{file_ext}"] + [f"{base}/{name}" for name in index_names]


def _resolve_two_candidate_lists(
    paths: frozenset[str], full: list[str], dropped_last: list[str] | None
) -> str | None:
    """Pick between "last segment is a submodule" vs. "last segment is
    a symbol inside the parent module" — the ambiguity every dotted/
    ``::``-path import source shares (see ``_resolve_import_python``'s
    docstring for why this ambiguity exists at all).

    ``full`` (the submodule reading) always wins when it matches a
    real file: if ``pkg/sub/mod.py`` genuinely exists, ``from pkg.sub
    import mod`` names that real submodule, full stop — real Python
    import semantics have no actual ambiguity here, regardless of
    whether ``pkg/sub/__init__.py`` also happens to exist (it almost
    always does, for any properly laid-out package, which would make
    "both readings match a real file" fire on virtually every import
    if treated as a coin-flip ambiguity instead of a strict fallback).
    ``dropped_last`` is only consulted when ``full`` matches nothing.

    Args:
        paths: Every known file path.
        full: Candidates treating every segment as part of the module
            path (the "last segment is itself a submodule" reading),
            tried first.
        dropped_last: Candidates with the last segment dropped (the
            "last segment is a symbol imported from the parent module"
            reading), tried only when ``full`` finds nothing; ``None``
            when that reading doesn't apply.

    Returns:
        The resolved path, or ``None`` when neither reading matches a
        real file.
    """
    match = _first_match(paths, full)
    if match is not None:
        return match
    if dropped_last is not None:
        return _first_match(paths, dropped_last)
    return None


def _resolve_import_python(
    imp: Import, importer_path: str, ctx: _ImportResolveContext
) -> str | None:
    """Resolve a Python import source to a repo file.

    ``Import.source`` for a Python ``from`` import is ``"{base}{sep}
    {imported_name}"`` (see ``extractor._imports_python``) — the
    *imported name* is always appended to the module path, whether
    that name is itself a submodule (``from . import sibling``) or a
    plain symbol defined inside the named module (``from .mod import
    Foo``). Static text alone can't tell those two shapes apart (this
    is the real gap in the ideation doc's "source is already the
    resolved module path" assumption — even the corrected design
    doc's own worked example undercounts it), so every segment split
    is tried both ways via ``_resolve_two_candidate_lists``: the full
    dotted path as a submodule, and the path with its last segment
    dropped (the parent module) plus that segment treated as a symbol
    living inside it. A plain ``import a.b.c`` has no such ambiguity
    (every segment is definitely a module-path segment), but since
    ``Import`` doesn't record which of the two statement shapes
    produced a given record, the same two-reading check is applied
    uniformly — the "submodule" reading is tried first and is the one
    that matches for ``import`` statements in practice.

    Relative imports (leading dots) resolve against the importer's own
    directory, walking up one level per dot beyond the first. Absolute
    imports are only attempted when the first dotted segment names a
    real top-level package in this repo (a directory with its own
    ``__init__.py`` whose parent isn't itself a package) — found via
    ``ctx.py_package_roots``, which is layout-agnostic (works for a
    package sitting at the repo root or nested under ``src/``), unlike
    a literal repo-root-only check.
    """
    source = imp.source
    ndots = len(source) - len(source.lstrip("."))
    rest = source[ndots:]
    parts = rest.split(".") if rest else []

    if ndots:
        base_dir = _ancestor_dir(_dirname(importer_path), ndots - 1)
    else:
        if not parts:
            return None
        roots = ctx.py_package_roots.get(parts[0], [])
        if len(roots) != 1:
            return None
        base_dir = roots[0]
        parts = parts[1:]
        if not parts:
            return None

    full = _dir_module_candidates(base_dir, parts, ".py", ("__init__.py",))
    dropped = (
        _dir_module_candidates(base_dir, parts[:-1], ".py", ("__init__.py",))
        if parts
        else None
    )
    return _resolve_two_candidate_lists(ctx.paths, full, dropped)


_JS_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx")

# Only the canonical basenames are discovered for v1 -- a repo whose
# real ``paths`` config lives only in a non-canonically-named variant
# (``tsconfig.build.json``, TS "solution style" ``references``) keeps
# today's external-by-default behavior for those aliases. See
# ``.features/plans/round25/07-tsconfig-path-alias-resolution.md``
# "Explicit non-goals".
_TSCONFIG_BASENAMES = frozenset({"tsconfig.json", "jsconfig.json"})

# Bounded recursion depth for an ``extends`` chain, paired with a
# visited-set cycle guard (``_merge_tsconfig_scope``) -- a malformed
# config chain must degrade to "no inherited paths", never hang or
# blow the recursion limit.
_TSCONFIG_EXTENDS_DEPTH_CAP = 5


@dataclass(frozen=True)
class _TsConfigAliasTable:
    """One ``tsconfig.json``/``jsconfig.json`` scope's resolved
    ``baseUrl``/``paths``, its own ``extends`` chain already merged in.

    Attributes:
        base_dir: ``compilerOptions.baseUrl``, resolved to a
            repo-relative path. Alias targets in ``paths`` are joined
            against this, mirroring ``_resolve_import_js``'s own
            relative-import ``base_dir``-joining idiom rather than a
            new path-joining convention.
        paths: ``compilerOptions.paths``, as ``(pattern, targets)``
            pairs in declaration order -- order matters, since a JS
            import matches the *first* pattern that fits, not the most
            specific one (this follows tsc's actual documented
            first-declared-pattern-wins behavior, not
            longest-prefix-wins).
    """

    base_dir: str
    paths: tuple[tuple[str, tuple[str, ...]], ...] = ()


def _strip_jsonc_comments(text: str) -> str:
    """Strip ``//`` and ``/* */`` comments from JSONC text.

    String-boundary-aware: a scanner state tracks whether the cursor is
    currently inside a ``"..."`` string literal (respecting ``\\"``
    escapes), so a ``//`` or ``/*`` occurring *inside* a string value
    (a URL, a path containing ``//``) is never mistaken for a comment
    start -- the one failure mode a naive regex-based stripper would
    get wrong on a real-world config. A block comment is replaced with
    a single space (not deleted outright) so two tokens separated only
    by ``/* ... */`` don't get accidentally joined; a line comment is
    dropped along with its trailing newline-less remainder, leaving the
    line break itself intact so line numbers in a ``json.loads`` error
    (for a config that's still malformed after stripping) stay useful.

    Trailing commas are a separate, later pass
    (``_strip_trailing_commas``) -- kept apart so each pass has one
    job, per this module's own single-responsibility convention.

    Args:
        text: Raw ``tsconfig.json``/``jsconfig.json`` file contents.

    Returns:
        ``text`` with every comment removed, ready for
        ``_strip_trailing_commas`` and then ``json.loads``.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            i += 2
            while i < n and text[i] not in "\r\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i = min(i + 2, n)
            out.append(" ")
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _strip_trailing_commas(text: str) -> str:
    """Drop a trailing comma right before a closing ``}``/``]``.

    Runs after ``_strip_jsonc_comments``, so only whitespace (never a
    comment) can separate the comma from its closer. Kept
    string-boundary-aware for the same reason ``_strip_jsonc_comments``
    is -- a naive ``re.sub(r",(\\s*[}\\]])", ...)`` would also fire on
    a literal ``",}"``-shaped substring inside a JSON string *value*
    (unlikely in a tsconfig path string, but not a risk worth taking
    for a hand-rolled parser this module leans on for a "skip rather
    than guess" discipline elsewhere).

    Args:
        text: JSONC text with comments already stripped.

    Returns:
        ``text`` with every trailing comma removed, safe to hand to
        ``json.loads``.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == ",":
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] in "}]":
                i += 1
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _read_jsonc_config(root: Path, rel: str) -> dict | None:
    """Read and parse one JSONC config file, or ``None`` on any
    failure.

    Never raises -- an unreadable file, a parse failure even after
    comment/trailing-comma stripping, or a non-object top level all
    cause this one config to be silently skipped (matching this
    module's "skip rather than guess" discipline): one broken
    ``tsconfig.json`` in a monorepo must not take down ``dekko map``.

    Args:
        root: Repository root.
        rel: Repo-relative path to the config file.

    Returns:
        The parsed top-level object, or ``None``.
    """
    try:
        text = (root / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    try:
        data = json.loads(_strip_trailing_commas(_strip_jsonc_comments(text)))
    except (json.JSONDecodeError, RecursionError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _normalize_ts_paths(
    paths_raw: object,
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """``compilerOptions.paths`` (a JSON object) → declaration-ordered
    ``(pattern, targets)`` pairs, malformed entries dropped rather than
    raising.
    """
    if not isinstance(paths_raw, dict):
        return ()
    out: list[tuple[str, tuple[str, ...]]] = []
    for pattern, targets in paths_raw.items():
        if not isinstance(pattern, str) or not isinstance(targets, list):
            continue
        cleaned = tuple(t for t in targets if isinstance(t, str))
        if cleaned:
            out.append((pattern, cleaned))
    return tuple(out)


def _resolve_ts_extends_target(
    config_rel: str, extends_spec: str
) -> str | None:
    """Repo-relative path an ``extends`` entry names, or ``None``.

    Only a repo-relative ``extends`` value (``"./..."``/``"../..."``)
    is followed -- an ``extends`` target that would resolve through
    ``node_modules`` package resolution (e.g. ``"@tsconfig/node18/
    tsconfig.json"``) is a real, common convention this module doesn't
    attempt to resolve (out of scope: no package resolution), and is
    silently skipped, contributing nothing to the merge.

    Args:
        config_rel: Repo-relative path of the config declaring
            ``extends``.
        extends_spec: One raw ``extends`` string value.

    Returns:
        The resolved repo-relative config path (``.json`` appended when
        missing, matching tsc's own extension-inference), or ``None``
        when ``extends_spec`` isn't repo-relative.
    """
    if not (extends_spec.startswith("./") or extends_spec.startswith("../")):
        return None
    config_dir = _dirname(config_rel)
    joined = posixpath.normpath(
        f"{config_dir}/{extends_spec}" if config_dir else extends_spec
    )
    if not joined.endswith(".json"):
        joined = f"{joined}.json"
    return joined


_TsPathsOrNone = tuple[tuple[str, tuple[str, ...]], ...] | None


def _inherited_tsconfig_scope(
    root: Path, raw: dict, rel: str, depth: int, visited: frozenset[str]
) -> tuple[str | None, _TsPathsOrNone]:
    """Merge every ``extends`` target of one config, in array order.

    Split out of ``_merge_tsconfig_scope`` to keep that function's own
    branch count under this project's complexity cap -- a later
    ``extends`` array entry overriding an earlier one's contribution
    mirrors ``tsc``'s own last-wins merge order for multiple base
    configs (TS 5+ array ``extends``).

    Args:
        root: Repository root.
        raw: This config's already-parsed top-level object.
        rel: Repo-relative path of the config declaring ``extends``.
        depth: This config's own recursion depth (its targets recurse
            at ``depth + 1``).
        visited: Config paths already visited in this chain.

    Returns:
        ``(base_dir, paths)`` inherited from the ``extends`` chain,
        either ``None`` when nothing in the chain declares one.
    """
    inherited_base_dir: str | None = None
    inherited_paths: _TsPathsOrNone = None
    extends = raw.get("extends")
    extends_specs = (
        extends
        if isinstance(extends, list)
        else [extends]
        if isinstance(extends, str)
        else []
    )
    for spec in extends_specs:
        if not isinstance(spec, str):
            continue
        target = _resolve_ts_extends_target(rel, spec)
        if target is None:
            continue
        parent_base, parent_paths = _merge_tsconfig_scope(
            root, target, depth + 1, visited
        )
        if parent_base is not None:
            inherited_base_dir = parent_base
        if parent_paths is not None:
            inherited_paths = parent_paths
    return inherited_base_dir, inherited_paths


def _own_tsconfig_scope(
    raw: dict, config_dir: str
) -> tuple[str | None, _TsPathsOrNone]:
    """This config's *own* (non-inherited) ``baseUrl``/``paths``.

    Args:
        raw: This config's already-parsed top-level object.
        config_dir: This config's own repo-relative directory, for
            resolving a relative ``baseUrl`` against.

    Returns:
        ``(base_dir, paths)`` — ``base_dir`` is ``None`` when this
        config declares no ``baseUrl`` of its own; ``paths`` is
        ``None`` when this config's ``compilerOptions`` has no
        ``paths`` key at all (as opposed to an empty ``paths: {}``,
        which is a real declaration that must still replace an
        inherited table — see ``_merge_tsconfig_scope``).
    """
    compiler_options = raw.get("compilerOptions")
    if not isinstance(compiler_options, dict):
        return None, None
    base_dir = None
    base_url_raw = compiler_options.get("baseUrl")
    if isinstance(base_url_raw, str):
        joined = posixpath.normpath(
            f"{config_dir}/{base_url_raw}" if config_dir else base_url_raw
        )
        base_dir = "" if joined == "." else joined
    paths = (
        _normalize_ts_paths(compiler_options.get("paths"))
        if "paths" in compiler_options
        else None
    )
    return base_dir, paths


def _merge_tsconfig_scope(
    root: Path, rel: str, depth: int, visited: frozenset[str]
) -> tuple[str | None, _TsPathsOrNone]:
    """Resolve one config's own ``baseUrl``/``paths``, with its
    ``extends`` chain (if any) merged in first.

    Recurses into each ``extends`` target before reading this config's
    own ``compilerOptions``, per real tsc merge semantics: ``paths``
    inherits whole-key-replaces (a config that declares
    ``compilerOptions.paths`` at all replaces the parent's ``paths``
    entirely, never merged per-alias) and ``baseUrl`` inherits only
    when this config doesn't declare its own. A depth cap plus a
    visited-set cycle guard means a malformed chain (an ``extends``
    cycle, a chain deeper than realistic) degrades to "no inherited
    paths" rather than recursing forever.

    Args:
        root: Repository root.
        rel: Repo-relative path of the config to resolve.
        depth: Current recursion depth (0 at the entry point).
        visited: Config paths already visited in this chain, for the
            cycle guard.

    Returns:
        ``(base_dir, paths)`` -- either may be ``None`` when neither
        this config nor its ``extends`` chain declares one.
        ``base_dir`` is *not* defaulted here (see
        ``_load_tsconfig_alias_tables`` for the "." default, applied
        once at the scope's own leaf level).
    """
    if rel in visited or depth > _TSCONFIG_EXTENDS_DEPTH_CAP:
        return None, None
    raw = _read_jsonc_config(root, rel)
    if raw is None:
        return None, None
    visited = visited | {rel}

    inherited_base_dir, inherited_paths = _inherited_tsconfig_scope(
        root, raw, rel, depth, visited
    )
    own_base_dir, own_paths = _own_tsconfig_scope(raw, _dirname(rel))

    base_dir = own_base_dir if own_base_dir is not None else inherited_base_dir
    paths = own_paths if own_paths is not None else inherited_paths
    return base_dir, paths


def _load_tsconfig_alias_tables(root: Path) -> dict[str, _TsConfigAliasTable]:
    """Discover and parse every ``tsconfig.json``/``jsconfig.json`` in
    the repo into a per-directory alias table, each config's own
    ``extends`` chain already merged in.

    Discovery reuses ``walker.find_config_files`` (the same
    exclude-aware enumeration ``discover()`` uses for source files), so
    a ``node_modules/some-pkg/tsconfig.json`` decoy is excluded the
    same way any other vendored file already is. A config that
    resolves to no ``paths`` anywhere in its own ``extends`` chain
    contributes nothing (no entry in the returned dict) -- there is
    nothing for ``_nearest_ts_config_scope`` to usefully find there.

    Args:
        root: Repository root.

    Returns:
        Each discovered config's own directory (``""`` for a
        repo-root ``tsconfig.json``) → its resolved
        ``_TsConfigAliasTable``.
    """
    tables: dict[str, _TsConfigAliasTable] = {}
    for rel in walker.find_config_files(root, _TSCONFIG_BASENAMES):
        base_dir, paths = _merge_tsconfig_scope(root, rel, 0, frozenset())
        if not paths:
            continue
        config_dir = _dirname(rel)
        if base_dir is None:
            base_dir = config_dir
        tables[config_dir] = _TsConfigAliasTable(
            base_dir=base_dir, paths=paths
        )
    return tables


def _nearest_ts_config_scope(
    importer_path: str, ts_path_aliases: dict[str, _TsConfigAliasTable]
) -> _TsConfigAliasTable | None:
    """Deepest ``ts_path_aliases`` scope governing ``importer_path``.

    Walks from the importer's own directory upward (the same
    repeated-ancestor-directory-walk shape ``_rust_crate_root`` already
    uses for its own nearest-scope lookup, applied here to
    ``ts_path_aliases``'s directory keys instead of crate-root
    markers), so a monorepo's per-package ``tsconfig.json`` takes
    priority over a root-level one for files under that package --
    matching real ``tsc`` resolution order.

    Args:
        importer_path: Repo-relative path of the importing file.
        ts_path_aliases: Every discovered scope, keyed by its own
            directory.

    Returns:
        The nearest governing ``_TsConfigAliasTable``, or ``None`` when
        no config governs this file at all.
    """
    if not ts_path_aliases:
        return None
    d = _dirname(importer_path)
    while True:
        table = ts_path_aliases.get(d)
        if table is not None:
            return table
        if not d:
            return None
        d = _dirname(d)


def _match_ts_path_pattern(module_source: str, pattern: str) -> str | None:
    """Match ``module_source`` against one ``paths`` key.

    Args:
        module_source: The bare import specifier being resolved.
        pattern: One ``compilerOptions.paths`` key -- either a literal
            string, or a string with at most one ``*`` wildcard (per
            the TS spec).

    Returns:
        The wildcard capture (the substring matched by ``*``) for a
        wildcard pattern, ``""`` for a non-wildcard exact match, or
        ``None`` when ``pattern`` doesn't apply to ``module_source``.
    """
    if "*" not in pattern:
        return "" if module_source == pattern else None
    prefix, _, suffix = pattern.partition("*")
    if not (
        module_source.startswith(prefix) and module_source.endswith(suffix)
    ):
        return None
    if len(module_source) < len(prefix) + len(suffix):
        return None
    return module_source[len(prefix) : len(module_source) - len(suffix)]


def _js_module_candidates(joined: str) -> list[str]:
    """Extension/index-file candidate ladder for a resolved JS/TS
    module path.

    Shared by relative-import resolution (``_resolve_import_js``) and
    tsconfig path-alias resolution (``_resolve_ts_path_alias``) so both
    apply the exact same NodeNext/ESM specifier-extension-stripping
    second pass, rather than a second, parallel candidate-generation
    implementation.

    Args:
        joined: A resolved, ``posixpath.normpath``-clean module path
            with no extension guaranteed yet.

    Returns:
        Candidate repo-relative paths, most-specific (as written)
        first.
    """
    candidates = [joined]
    candidates += [f"{joined}{ext}" for ext in _JS_EXTENSIONS]
    candidates += [f"{joined}/index{ext}" for ext in _JS_EXTENSIONS]
    # NodeNext/ESM-style TypeScript requires relative specifiers to
    # carry the *compiled* extension ("./foo.js") even when the real
    # source is foo.ts -- the specifier and the source file's actual
    # extension deliberately disagree. Appending an extension onto
    # `joined` as-is never reaches foo.ts in that case (it would only
    # try foo.js.ts, foo.js.tsx, ...), so a second candidate stem with
    # the specifier's own JS/TS extension stripped is tried too,
    # re-running the same extension ladder against it.
    stem, specifier_ext = posixpath.splitext(joined)
    if specifier_ext in _JS_EXTENSIONS:
        candidates += [f"{stem}{ext}" for ext in _JS_EXTENSIONS]
    return candidates


def _resolve_ts_path_alias(
    module_source: str, scope: _TsConfigAliasTable, paths: frozenset[str]
) -> str | None:
    """Resolve a bare specifier against one tsconfig scope's alias
    table.

    Tries each ``(pattern, targets)`` pair in the scope's own
    declaration order (first-declared-pattern-that-matches wins, not
    longest-prefix -- matches documented ``tsc`` behavior); within a
    matching pattern, each target is tried in order (first real-file
    match wins, mirroring ``_first_match``'s existing "try candidates
    in order" convention used everywhere else in this module). Each
    candidate target is joined against ``scope.base_dir`` and run
    through the same extension/index-file ladder
    (``_js_module_candidates``) relative-import resolution already
    applies.

    Args:
        module_source: The bare import specifier being resolved.
        scope: The governing tsconfig scope (see
            ``_nearest_ts_config_scope``).
        paths: Every file path known to the map.

    Returns:
        The resolved repo-relative path, or ``None`` when no pattern
        matches, or a matching pattern's targets all miss.
    """
    for pattern, targets in scope.paths:
        capture = _match_ts_path_pattern(module_source, pattern)
        if capture is None:
            continue
        for target in targets:
            resolved_target = (
                target.replace("*", capture, 1) if "*" in target else target
            )
            joined = posixpath.normpath(
                f"{scope.base_dir}/{resolved_target}"
                if scope.base_dir
                else resolved_target
            )
            match = _first_match(paths, _js_module_candidates(joined))
            if match is not None:
                return match
    return None


# Build-output directory names a manifest's entry points routinely
# point into. None of them is source, and none is in the map (build
# output is gitignored), so each is swapped for ``src`` -- or dropped --
# to find the file the entry was compiled *from*.
_JS_BUILD_DIRS = frozenset(
    {"dist", "build", "lib", "out", "esm", "cjs", "es", "umd", "types"}
)
# Manifest fields naming the package's root entry, most source-like
# first. ``source`` is the (informal, bundler-honored) pointer at real
# source; the rest normally name build output.
_JS_ENTRY_FIELDS = ("source", "types", "typings", "module", "main")
_JS_DECLARATION_SUFFIXES = (".d.ts", ".d.mts", ".d.cts")
# ``exports`` conditions selecting a specific runtime/bundler target
# rather than the package's general entry -- tried last.
_JS_NICHE_EXPORT_CONDITIONS = frozenset(
    {
        "browser", "worker", "workerd", "deno", "bun", "react-native",
        "react-server", "edge-light", "electron", "development",
    }
)  # fmt: skip


def _resolve_workspace_entry(
    module_source: str, ctx: _ImportResolveContext
) -> str | None:
    """Resolve a bare specifier naming a workspace package to a file.

    ``"@cline/llms"`` -> the package's root entry; ``"@cline/llms/
    browser"`` -> its ``./browser`` subpath. Entry targets come from
    the manifest (``exports``, then ``source``/``types``/``module``/
    ``main`` for the root), each mapped from build output back to
    source (see ``_workspace_source_candidates``), then from the
    near-universal conventions ``src/<subpath>`` and ``<subpath>``.
    The first candidate that is a real mapped file wins.

    Returns:
        The entry file's repo-relative path, or ``None`` when the
        specifier names no workspace package or none of its candidate
        entries exists in the map -- which leaves it ``external``,
        honestly, rather than guessing a file.
    """
    if not ctx.workspace_manifests:
        return None
    name = _workspace_package_name(module_source)
    manifest = ctx.workspace_manifests.get(name) if name else None
    if manifest is None or name is None:
        return None

    subpath = module_source[len(name) :].strip("/")
    targets = _manifest_entry_targets(manifest.data, subpath)
    targets += [f"src/{subpath}" if subpath else "src/index"]
    targets += [subpath or "index"]
    for target in targets:
        for rel in _workspace_source_candidates(target):
            joined = posixpath.normpath(
                f"{manifest.package_dir}/{rel}"
                if manifest.package_dir
                else rel
            )
            found = _first_match(ctx.paths, _js_module_candidates(joined))
            if found is not None:
                return found
    return None


def _manifest_entry_targets(data: dict, subpath: str) -> list[str]:
    """Package-relative entry targets a manifest declares for a subpath.

    Args:
        data: Parsed ``package.json``.
        subpath: ``""`` for the package root, else the specifier's
            remainder (``"browser"`` for ``"@scope/pkg/browser"``).

    Returns:
        Every string target found, in preference order: the matching
        ``exports`` entry's leaves (all conditions -- dekko has no
        "active condition", and every one of them was compiled from
        the same source), then for the root only the classic
        single-entry fields.
    """
    key = f"./{subpath}" if subpath else "."
    targets: list[str] = []
    exports = data.get("exports")
    if isinstance(exports, str) and not subpath:
        targets.append(exports)
    elif isinstance(exports, dict):
        if not any(k.startswith(".") for k in exports):
            # A bare conditions object is sugar for {".": {...}}.
            exports = {".": exports}
        if key in exports:
            targets += _export_leaves(exports[key])
        else:
            targets += _export_pattern_targets(exports, key)
    if not subpath:
        targets += [
            data[f] for f in _JS_ENTRY_FIELDS if isinstance(data.get(f), str)
        ]
    return [t for t in targets if t]


def _export_leaves(node: object) -> list[str]:
    """Every string leaf of an ``exports`` value, conditions flattened.

    General-purpose conditions (``import``/``require``/``default``/
    ``types``/...) come before environment-specific ones
    (``_JS_NICHE_EXPORT_CONDITIONS``), whatever order the manifest
    lists them in. dekko has no "active condition" to evaluate, and a
    manifest conventionally lists its most specific condition first --
    cline's ``@cline/llms`` leads with ``browser``, which would send
    every importer's edge to ``index.browser.ts`` instead of the
    ``index.ts`` a Node/extension-host consumer actually loads.
    """
    general: list[str] = []
    niche: list[str] = []
    _collect_export_leaves(node, False, general, niche)
    return general + niche


def _collect_export_leaves(
    node: object, in_niche: bool, general: list[str], niche: list[str]
) -> None:
    """Depth-first walk behind ``_export_leaves``."""
    if isinstance(node, str):
        (niche if in_niche else general).append(node)
    elif isinstance(node, list):
        for item in node:
            _collect_export_leaves(item, in_niche, general, niche)
    elif isinstance(node, dict):
        for condition, value in node.items():
            _collect_export_leaves(
                value,
                in_niche or condition in _JS_NICHE_EXPORT_CONDITIONS,
                general,
                niche,
            )


def _export_pattern_targets(exports: dict, key: str) -> list[str]:
    """Targets from ``exports`` subpath patterns (``"./*"``) for ``key``."""
    targets: list[str] = []
    for pattern, node in exports.items():
        if pattern.count("*") != 1:
            continue
        head, tail = pattern.split("*")
        if not (key.startswith(head) and key.endswith(tail)):
            continue
        if len(key) < len(head) + len(tail):
            continue
        middle = key[len(head) : len(key) - len(tail)]
        targets += [leaf.replace("*", middle) for leaf in _export_leaves(node)]
    return targets


def _workspace_source_candidates(target: str) -> list[str]:
    """Package-relative source paths a manifest entry target may mean.

    ``./dist/index.js`` -> ``dist/index.js`` as written (a package may
    point straight at source), then with its leading build-output
    directories swapped for ``src`` (``src/index.js``) and dropped
    (``index.js``). A ``.d.ts`` suffix is reduced to its stem first;
    ``_js_module_candidates`` then re-runs its own extension ladder
    (including the ``.js`` -> ``.ts`` pass) over each result.
    """
    rel = posixpath.normpath(target.removeprefix("./"))
    for suffix in _JS_DECLARATION_SUFFIXES:
        if rel.endswith(suffix):
            rel = rel[: -len(suffix)]
            break

    candidates = [rel]
    parts = rel.split("/")
    stripped = list(parts)
    while len(stripped) > 1 and stripped[0] in _JS_BUILD_DIRS:
        stripped = stripped[1:]
    if stripped != parts:
        candidates.append("/".join(["src", *stripped]))
        candidates.append("/".join(stripped))
    return candidates


def _resolve_import_js(
    imp: Import, importer_path: str, ctx: _ImportResolveContext
) -> str | None:
    """Resolve a JS/TS/TSX import source to a repo file.

    ``Import.source`` is ``"{module_source}/{imported_name}"`` (see
    ``extractor._imports_js``) — the module path is recovered by
    dropping the last ``/``-segment, safe regardless of how many
    slashes the real module path itself contains (the appended name is
    always exactly the last segment). A side-effect import
    (``imp.name == ""``, no local binding) stores ``source`` bare with
    no appended name to strip, so the drop is skipped for that shape —
    stripping unconditionally would truncate a real path segment (e.g.
    ``"opentui-spinner/react"`` down to ``"opentui-spinner"``).

    Bare specifiers (no leading ``.``/``..``) are resolved first
    against a ``tsconfig.json``/``jsconfig.json``
    ``compilerOptions.paths`` alias governing this importer's directory
    (``ctx.ts_path_aliases``, empty whenever ``resolve_imports`` was
    called with no filesystem ``root`` — see
    ``_ImportResolveContext``'s docstring), then, failing that, as a
    path relative to the repo root (round 28 claude-code.md §3.3: a
    bare, repo-root-relative specifier with no leading ``./`` and no
    governing tsconfig alias, e.g. ``import { x } from
    'src/bootstrap/state.js'``, is a real third convention seen in the
    wild, not just "relative" or "alias-configured"). The root-relative
    attempt is gated on the specifier containing at least one ``/`` — a
    single-segment bare specifier (``"react"``, ``"lodash"``) is
    overwhelmingly a real npm package name in practice, so leaving it
    external bounds the false-positive risk of an npm package name
    coincidentally matching an in-repo path. Only once every attempt
    misses does the specifier fall through to "external".

    Between those two sits the workspace-package attempt (round 31
    P1.1b, see ``_resolve_workspace_entry``): ``"@cline/llms"`` is an
    in-repo package in a monorepo that declares it as a workspace
    member, and resolves to that package's source entry file. After
    the tsconfig alias (explicit per-scope config outranks a
    repo-wide convention), before root-relative (a declared package
    name is stronger evidence than a path that happens to exist).
    """
    module_source = imp.source.rsplit("/", 1)[0] if imp.name else imp.source
    if not (module_source.startswith("./") or module_source.startswith("../")):
        scope = _nearest_ts_config_scope(importer_path, ctx.ts_path_aliases)
        if scope is not None:
            resolved = _resolve_ts_path_alias(module_source, scope, ctx.paths)
            if resolved is not None:
                return resolved
        workspace_entry = _resolve_workspace_entry(module_source, ctx)
        if workspace_entry is not None:
            return workspace_entry
        if "/" in module_source:
            joined = posixpath.normpath(module_source)
            root_relative = _first_match(
                ctx.paths, _js_module_candidates(joined)
            )
            if root_relative is not None:
                return root_relative
        return None
    base_dir = _dirname(importer_path)
    joined = posixpath.normpath(
        f"{base_dir}/{module_source}" if base_dir else module_source
    )
    return _first_match(ctx.paths, _js_module_candidates(joined))


_RUST_INDEX_STEMS = ("mod", "lib", "main")
# Every directory's own index-file name is "mod.rs" except the crate
# root, which uses "lib.rs" (library crates) or "main.rs" (binary
# crates) instead — tried in this order for every directory reached
# during resolution, since only the crate-root case ever matches
# lib.rs/main.rs and it's cheaper to try all three unconditionally
# than to track "is this directory the crate root" separately.
_RUST_INDEX_NAMES = ("mod.rs", "lib.rs", "main.rs")


def _rust_self_base(importer_path: str) -> str:
    """The directory ``self::``-relative Rust paths resolve against.

    An index-style file (``mod.rs``/``lib.rs``/``main.rs``) *is* its
    directory's own module, so ``self::`` there means "this directory".
    A leaf file (``foo.rs``) is a module named ``foo`` whose own
    submodules (Rust 2018+ per-file modules) live in a sibling ``foo/``
    directory, so ``self::`` there means that virtual ``foo/`` path,
    not ``foo.rs``'s own containing directory.
    """
    d = _dirname(importer_path)
    stem = importer_path.rsplit("/", 1)[-1].removesuffix(".rs")
    if stem in _RUST_INDEX_STEMS:
        return d
    return f"{d}/{stem}" if d else stem


def _rust_crate_root(importer_path: str, paths: frozenset[str]) -> str | None:
    """Nearest ancestor directory of ``importer_path`` containing
    ``lib.rs``/``main.rs`` — or, failing that, a ``src/`` directory
    whose immediate parent directory name matches a ``.rs`` file
    directly inside it (``crates/editor/src/editor.rs`` for a
    ``crates/editor/`` crate) — this repo's best-effort stand-in for
    the crate root ``crate::`` paths resolve against, absent any
    ``Cargo.toml``/workspace parsing (out of scope; dekko does not
    parse build manifests).

    The second heuristic exists because Cargo.toml's ``[lib] path =
    "src/<name>.rs"`` (or ``[[bin]] path = ...``) override is common in
    real-world workspaces (confirmed against zed: 216/222 of its
    crates using a ``[lib] path`` override follow exactly this
    crate-dir-name-matches-filename-stem shape) — without it, a
    literal-``lib.rs``-only check silently misresolves the vast
    majority of ``crate::``-prefixed imports as external on any repo
    using this convention. It's also exactly what ``cargo`` itself
    infers by default when no ``[lib]`` override exists, and matches
    the filename zed's own override *chooses* — not an arbitrary
    guess.

    Returns ``None`` when neither shape is found in any ancestor (e.g.
    a fixture with no crate-root file, a Rust ``tests/`` integration-
    test file compiled as its own crate, or a crate whose Cargo.toml
    points somewhere this heuristic doesn't cover, such as zed's
    ``language_onboarding`` crate mapping ``[lib] path =
    "src/python.rs"`` — still an honest "can't tell", not a wrong
    answer).
    """
    d = _dirname(importer_path)
    while True:
        if f"{d}/lib.rs" in paths or f"{d}/main.rs" in paths:
            return d
        base = d.rsplit("/", 1)[-1] if d else ""
        parent = _dirname(d)
        if base == "src" and parent:
            crate_name = parent.rsplit("/", 1)[-1]
            if f"{d}/{crate_name}.rs" in paths:
                return d
        if not d:
            return None
        d = _dirname(d)


def _rust_crate_root_index_names(
    base: str, paths: frozenset[str]
) -> tuple[str, ...]:
    """``_RUST_INDEX_NAMES``, plus the crate's own custom root filename
    when ``base`` was found via ``_rust_crate_root``'s second
    (named-file) heuristic rather than a literal ``lib.rs``/``main.rs``
    -- e.g. ``"gpui.rs"`` for ``crates/gpui/src``.

    Needed because ``_dir_module_candidates`` has no other way to know
    a ``crate::X`` item re-exported at crate-root scope (not a
    submodule) might live in that custom-named file rather than one of
    the three standard index names (round 22 zed.md §3.1: ``crate::
    App`` for a ``[lib] path = "src/gpui.rs"`` crate never resolved,
    since the fixed ``_RUST_INDEX_NAMES`` tuple has no way to know
    this crate's own root file isn't named ``lib.rs``/``main.rs``/
    ``mod.rs``).

    Args:
        base: The crate root directory, as found by
            ``_rust_crate_root`` (or looked up in ``ctx.crate_roots``
            for a cross-crate import).
        paths: Every known file path.

    Returns:
        ``_RUST_INDEX_NAMES``, extended with the crate's own
        custom-named root file when one exists in ``paths`` and isn't
        already one of the standard names.
    """
    if f"{base}/lib.rs" in paths or f"{base}/main.rs" in paths:
        return _RUST_INDEX_NAMES
    parent = _dirname(base)
    crate_name = parent.rsplit("/", 1)[-1] if parent else ""
    custom = f"{base}/{crate_name}.rs"
    if crate_name and custom in paths:
        return (*_RUST_INDEX_NAMES, f"{crate_name}.rs")
    return _RUST_INDEX_NAMES


def _rust_crate_roots_index_all(paths: frozenset[str]) -> dict[str, list[str]]:
    """Repo-wide Rust crate name → every matching crate-root directory.

    Applies convention-based detection across every ``src/`` directory
    in the repo, proactively rather than reactively (mirroring
    ``_rust_crate_root``'s own per-importer logic): a workspace-member
    crate's name is the name of the directory containing its ``src/
    lib.rs``/``src/main.rs`` (or, for a custom ``[lib] path`` override,
    its ``src/<crate-name>.rs``) -- round 19's own convention, reused
    rather than reinvented. Lets a cross-crate ``use other_crate::X;``
    import in a Cargo workspace resolve against the sibling crate's
    real root directory, without parsing ``Cargo.toml`` (out of scope;
    see ``_rust_crate_root``'s own docstring for why).

    Scoped to the ``<crate>/src/...`` nesting shape only -- the same
    shape confirmed dominant against zed in ``_rust_crate_root``'s own
    docstring. A crate root file sitting directly at its crate
    directory with no ``src/`` nesting, or a crate whose external/
    public name (a ``[package] name = "..."`` override) differs from
    its directory name, is not found here -- an acceptable,
    documentable residual gap, the same "honest can't tell" shape
    ``_rust_crate_root`` itself already accepts for its own edge case
    (round 22 zed.md §3.1).

    Collision-aware: retains *every* directory that matches a given
    crate name rather than letting one silently win. Built once per
    ``resolve_heritage()`` call and threaded through
    ``_pick_candidate``/``_import_match`` as that path's ``crate_roots``
    (round 23), and, as of round 25, also built once per
    ``resolve_imports()`` call and threaded through
    ``_ImportResolveContext.crate_roots`` for ``_resolve_import_rust``'s
    bare-crate-name lookup -- both call sites now share this single
    collision-aware index rather than ``resolve_imports()`` using an
    earlier single-winner variant.

    Round 23 (``.features/plans/round23/
    09-subtypes-ambiguous-resolution-rate.md`` Fix B): zed's
    ``crates/gpui`` (the real crate) and
    ``tooling/lints/test_fixture/gpui`` (a same-named synthetic test
    fixture) both convention-match crate name ``"gpui"`` -- a
    since-removed single-winner predecessor of this index meant that
    collision was a genuine, measured 50/50 coin flip per process hash
    seed (see ``_rust_crate_hint_matches``'s docstring and this design
    doc's "Implemented" note for the live zed measurement): winning
    half the time resolved every affected clause correctly, losing the
    other half silently misattributed every one of them to the wrong
    (fixture) crate's same-named symbol. Kept as every candidate
    (``_rust_crate_hint_matches``) rather than resolved to one here,
    since only that function's caller has the full candidate-symbol
    list needed to tell "matches one real root" apart from "matches
    two, still genuinely ambiguous" -- converting the coin flip into a
    deterministic, honest "ambiguous" for the genuinely unresolvable
    case, at the cost of the coin flip's lucky-draw resolved count.

    Round 24 (``.features/plans/round24/
    03-heritage-crate-decoy-tiebreak.md``) narrows that residual gap
    without touching this index's own shape: a genuine 2+-root
    collision here still deterministically falls through to
    ``_rust_crate_hint_matches``'s ``len(crate_matched) > 1`` branch
    unchanged, but that branch's own caller now gets one more chance
    (``_prefer_non_synthetic_crate_match``) to recover a resolution
    when exactly one matched root's path doesn't look like a
    test-fixture/vendor stand-in -- see that function's docstring.

    Round 25 (``.features/plans/round25/
    02-deps-crate-decoy-tiebreak.md``) threads this same index and an
    analogous directory-level tiebreak
    (``_prefer_non_synthetic_crate_root``) through
    ``_resolve_import_rust`` too, closing the identical gap in
    ``dekko deps``'s module-dependency-graph resolution that round 24
    closed for heritage resolution -- the single-winner predecessor
    this function replaced there is gone; every reader of
    ``crate_roots`` throughout this module (heritage and import
    resolution both) now sees the same collision-aware
    ``dict[str, list[str]]`` shape.

    Args:
        paths: Every known file path.

    Returns:
        Crate name → every ``src/`` directory this convention finds
        matching that name, in path-iteration order (deduplicated).
    """
    roots: dict[str, list[str]] = {}
    for p in paths:
        if not p.endswith(".rs"):
            continue
        src_dir = _dirname(p)
        if src_dir.rsplit("/", 1)[-1] != "src":
            continue
        crate_dir = _dirname(src_dir)
        if not crate_dir:
            continue
        crate_name = crate_dir.rsplit("/", 1)[-1]
        name = p.rsplit("/", 1)[-1]
        if name in ("lib.rs", "main.rs") or name == f"{crate_name}.rs":
            dirs = roots.setdefault(crate_name, [])
            if src_dir not in dirs:
                dirs.append(src_dir)
    return roots


def _rust_local_module_base(
    seg: str, importer_path: str, paths: frozenset[str]
) -> str | None:
    """The base ``seg`` resolves against, when it names a child module
    of the importer's own module position — shadowing a same-named
    workspace crate.

    Round 31 zed coverage pass F12: Rust 2018+ resolves a bare
    leading ``use`` segment against the local scope before a crate
    name — ``mod localmod;`` (or its per-file sibling directory,
    ``localmod/``) declared alongside the importing file wins over a
    workspace crate that happens to share the same name. File
    existence is used as the evidence (not ``mod`` declaration
    parsing, out of scope — see ``_resolve_import_rust``'s docstring):
    a Rust 2018+ per-file submodule either has a matching
    ``<base>/<seg>.rs`` file or a matching ``<base>/<seg>/mod.rs``
    (the pre-2018 directory-module shape), and one of those two must
    exist on disk for the module to be reachable at all — an inline
    ``mod x { ... }`` block has neither and is correctly not matched
    here, leaving it to fall through to the crate lookup below
    unchanged.

    Tries ``_rust_self_base(importer_path)`` first (the ordinary case,
    and the same base ``self::``/``super::`` already resolve
    against). ``_rust_self_base`` only recognizes the literal
    ``lib.rs``/``main.rs``/``mod.rs`` index names as "this file's own
    base is its own directory", though — a crate whose Cargo ``[lib]
    path`` override gives its root file a custom name (zed's own
    ``crates/gpui/src/gpui.rs``, ~216/222 of its crates per
    ``_rust_crate_root``'s own docstring) is treated as an ordinary
    leaf module one directory level too deep, exactly the file this
    rule's own motivating example (``mod util;`` inside ``gpui.rs``
    itself) needs. Retried against the importer's own directory when
    the importer's filename is itself a recognized crate-root index
    name for that directory (``_rust_crate_root_index_names``, which
    only needs the directory, not a separate crate-root discovery
    walk — the importer's own directory already *is* the candidate
    crate root being tested here).

    Args:
        seg: The ``use`` path's first segment.
        importer_path: The importing file's repo-relative path.
        paths: Every known file path.

    Returns:
        The base ``seg`` resolves against (``_rust_self_base``'s
        result, or the importer's own directory for the custom-named
        crate-root case above), or ``None`` when neither shape names
        an on-disk local module.
    """
    self_base = _rust_self_base(importer_path)
    if (
        f"{self_base}/{seg}.rs" in paths
        or f"{self_base}/{seg}/mod.rs" in paths
    ):
        return self_base
    own_dir = _dirname(importer_path)
    own_name = importer_path.rsplit("/", 1)[-1]
    if (
        own_dir
        and own_name in _rust_crate_root_index_names(own_dir, paths)
        and (
            f"{own_dir}/{seg}.rs" in paths
            or f"{own_dir}/{seg}/mod.rs" in paths
        )
    ):
        return own_dir
    return None


def _resolve_import_rust(
    imp: Import, importer_path: str, ctx: _ImportResolveContext
) -> str | None:
    """Resolve a Rust ``use`` path to a repo file.

    Dispatches on the leading path segment: ``crate::`` resolves
    against the crate root (``_rust_crate_root``), ``self::``/
    ``super::`` (one or more, e.g. ``super::super::foo``) against the
    importer's own module position (``_rust_self_base``, walked up
    once per ``super``). A bare crate name recognized as another
    workspace member (looked up in ``ctx.crate_roots``, see
    ``_rust_crate_roots_index_all``) resolves against *that* crate's
    own root the same way ``crate::`` does -- a Cargo workspace's
    sibling crates are referenced by bare crate name too, not just
    genuine third-party dependencies (round 22 zed.md §3.1). Any other
    bare crate name is external by construction.

    ``ctx.crate_roots`` is collision-aware (``dict[str, list[str]]``):
    a crate name matching exactly one directory resolves against it
    directly; a crate name matching two or more (a real crate plus a
    same-named test-fixture/vendor stand-in, e.g. zed's ``gpui``) falls
    back to ``_prefer_non_synthetic_crate_root`` -- round 25's
    directory-level sibling of round 24's heritage-resolution tiebreak
    (``_prefer_non_synthetic_crate_match``) -- rather than guessing;
    a genuine, still-ambiguous collision (or no synthetic-marker signal
    to break the tie) resolves to ``None`` here and is correctly
    reported external below, not misattributed.

    Like Python, the trailing segment is ambiguous between "a
    submodule" and "an item defined in the parent module" — resolved
    the same way, via ``_resolve_two_candidate_lists``.

    Round 31 zed coverage pass F12: a bare first segment was always
    looked up in ``ctx.crate_roots`` first, but Rust 2018+ resolves a
    bare path against the *local scope first* — a sibling ``mod
    localmod;`` declared in (or reachable from) the importing file's
    own module shadows a same-named workspace crate. Confirmed on zed:
    ``crates/gpui/src/gpui.rs`` declares ``mod util;`` and does ``pub
    use util::{FutureExt, Timeout};`` — the bare segment ``util``
    matched the sibling workspace crate ``crates/util`` (gpui has no
    such dependency) instead of ``crates/gpui/src/util.rs``, which
    really exists. 13 such impossible cross-crate module edges were
    fabricating a 100-file/12-crate false dependency cycle in ``dekko
    deps --cycles``. Checked before the crate-name lookup, via
    ``_rust_local_module_base``'s plain file-existence test — no
    ``mod``-declaration parsing, so an inline ``mod x { ... }`` (no
    file) is correctly untouched.
    """
    segs = imp.source.split("::")
    if not segs:
        return None

    at_crate_root = False
    if segs[0] == "crate":
        base = _rust_crate_root(importer_path, ctx.paths)
        rest = segs[1:]
        at_crate_root = True
    elif segs[0] in ("self", "super"):
        base = _rust_self_base(importer_path)
        i = 0
        while i < len(segs) and segs[i] == "super":
            base = _dirname(base)
            i += 1
        if i < len(segs) and segs[i] == "self":
            i += 1
        rest = segs[i:]
    elif (
        local_base := _rust_local_module_base(
            segs[0], importer_path, ctx.paths
        )
    ) is not None:
        base = local_base
        rest = segs
    else:
        crate_dirs = ctx.crate_roots.get(segs[0])
        if not crate_dirs:
            base = None
        elif len(crate_dirs) == 1:
            base = crate_dirs[0]
        else:
            base = _prefer_non_synthetic_crate_root(crate_dirs, importer_path)
        rest = segs[1:]
        at_crate_root = True

    if base is None or not rest:
        return None
    # A nested package directory's own index file is always mod.rs —
    # lib.rs/main.rs (or a custom-named crate root, see
    # _rust_crate_root_index_names) only ever names the crate root
    # itself, reached here when ``base`` is a crate root (``crate::``
    # or a cross-crate import) and ``rest``/``rest[:-1]`` is empty.
    # Trying every applicable index name whenever the remaining
    # segment list is empty covers both shapes without needing to
    # track "is base the crate root" separately.
    index_names = (
        _rust_crate_root_index_names(base, ctx.paths)
        if at_crate_root
        else _RUST_INDEX_NAMES
    )
    full = _dir_module_candidates(base, rest, ".rs", index_names)
    dropped = _dir_module_candidates(base, rest[:-1], ".rs", index_names)
    return _resolve_two_candidate_lists(ctx.paths, full, dropped)


def _resolve_import_java(
    imp: Import, importer_path: str, ctx: _ImportResolveContext
) -> str | None:
    """Resolve a Java ``import`` to a repo file.

    Java's package-equals-directory convention is fully mechanical
    (``com.foo.Bar`` → ``.../com/foo/Bar.java``) — the only real work
    is finding *which* source root the path is relative to, since
    Maven/Gradle nest Java sources under ``src/main/java``/``src/test/
    java`` rather than the repo root. ``ctx.java_suffix_index`` (built
    once, see ``_java_suffix_index``) already indexes every file under
    both its raw path and its root-stripped suffix, so this is a
    single dict lookup, not a per-import scan.
    """
    del importer_path
    target = imp.source.replace(".", "/") + ".java"
    matches = sorted(set(ctx.java_suffix_index.get(target, [])))
    return matches[0] if len(matches) == 1 else None


_CPP_EXTENSIONS = (
    ".h", ".hpp", ".hh", ".hxx", ".c", ".cpp", ".cc", ".cxx",
)  # fmt: skip


def _resolve_import_cpp(
    imp: Import, importer_path: str, ctx: _ImportResolveContext
) -> str | None:
    """Resolve a C/C++ ``#include`` to a repo file by filename search.

    A ``#include`` binds no package-qualified path the way Java's
    ``import`` does (per ``extractor._imports_cpp``'s own docstring),
    so this is a basename search over every C/C++-shaped file in the
    repo (``ctx.cpp_basename_index``), not a direct path check.
    Resolves only when the basename is unique — two headers with the
    same name in different directories are conservatively left
    external rather than guessed (this design's "skip rather than
    guess" rule).

    Deliberately does not distinguish ``#include "local.h"`` (quoted)
    from ``#include <system.h>`` (angle-bracket) — ``Import.source``
    has already had both delimiter forms stripped by extraction time
    (see ``extractor._strip_quotes``) with no record of which one was
    used, and adding that distinction would mean touching
    ``extractor.py``/``languages.py``, out of this design's stated
    scope (no new tree-sitter work). In practice this only matters if
    a repo happens to have its own file literally named the same as a
    system header, which the basename-uniqueness check above already
    guards against for the common case.

    The importing file itself is excluded from its own basename
    candidate pool — a header including its own basename is not a
    meaningful construct in valid C/C++ (it would either be an
    include-guard bug or, more commonly under this design, an
    artifact of the *real* same-basename file being vendored/excluded
    from the map, which must not silently resolve to "self" instead
    of correctly falling through to external; see the design doc's D1
    fix for the concrete repro this guards against).
    """
    basename = imp.source.rsplit("/", 1)[-1]
    matches = [
        m
        for m in ctx.cpp_basename_index.get(basename, [])
        if m != importer_path
    ]
    return matches[0] if len(matches) == 1 else None


_IMPORT_RESOLVERS: dict[
    str, Callable[[Import, str, _ImportResolveContext], str | None]
] = {
    "python": _resolve_import_python,
    "javascript": _resolve_import_js,
    "typescript": _resolve_import_js,
    "tsx": _resolve_import_js,
    "rust": _resolve_import_rust,
    "java": _resolve_import_java,
    "c": _resolve_import_cpp,
    "cpp": _resolve_import_cpp,
}


def _external_label_js(imp: Import) -> str:
    """External-disclosure label for a JS/TS/TSX import.

    ``Import.source`` has the imported name appended (see
    ``_resolve_import_js``'s docstring), so two named imports from the
    same external package (``import {useState, useEffect} from
    "react"``) would otherwise show up as two differently-suffixed
    "external sources" (``react/useState``, ``react/useEffect``)
    instead of the one external dependency they actually are. Strips
    the appended name back off for display, the same recovery
    ``_resolve_import_js`` already does before attempting resolution.
    A side-effect import (``imp.name == ""``) has no appended name to
    strip — the source is already bare, so stripping is skipped for
    that shape (same guard as ``_resolve_import_js``).
    """
    return imp.source.rsplit("/", 1)[0] if imp.name else imp.source


_EXTERNAL_LABELS: dict[str, Callable[[Import], str]] = {
    "javascript": _external_label_js,
    "typescript": _external_label_js,
    "tsx": _external_label_js,
}


def bare_import_source(imp: Import, language: str) -> str:
    """The bare, name-suffix-stripped module source for an import —
    ``imp.source`` verbatim for every language except JS/TS/TSX (see
    ``_external_label_js``).

    Used both for ``ModuleGraph.external``'s disclosure label and for
    ``query.py``'s ``--exact`` import matching (see
    ``query._source_matches``) — both want "the string a developer
    would write to mean this module", not the raw stored
    ``Import.source``, which for JS/TS has an arbitrary local binding
    name appended (see ``extractor._imports_js``).
    """
    labeler = _EXTERNAL_LABELS.get(language)
    return labeler(imp) if labeler is not None else imp.source


# Go is deliberately absent: real Go import-path resolution needs
# ``go.mod``'s module-prefix declaration, which dekko does not parse
# (out of scope for this design — see the design doc's Feasibility
# section). Every Go import is reported external rather than guessed
# from a bare directory-name match, which would silently misresolve
# as often as it helped. Any other/generic language falls through the
# same way.


def import_resolution_supported(language: str) -> bool:
    """Whether ``resolve_imports`` can resolve this language's imports
    to in-repo files at all.

    ``False`` for Go (and any Tier-2/generic-grammar language) — see
    the comment just above this function for why Go specifically has
    no entry in :data:`_IMPORT_RESOLVERS`: every import for such a
    language reports external unconditionally, which reads
    identically to "import resolution is broken" on an all-Go repo
    (``dekko deps``'s "0 resolved import edges" headline, awesome-go
    rounds 27/28/29) unless a caller checks this first and discloses
    the gap explicitly.

    Args:
        language: A ``FileMap.language``/``Symbol.language`` value.

    Returns:
        ``True`` if ``language`` has a real per-language resolver
        registered, ``False`` otherwise.
    """
    return language in _IMPORT_RESOLVERS


def _py_package_roots(paths: frozenset[str]) -> dict[str, list[str]]:
    """Top-level Python package name → directory path(s).

    A directory counts as a top-level package root when it has its own
    ``__init__.py`` and its parent directory does not (so a nested
    subpackage like ``pkg/sub`` is reached by walking from ``pkg``,
    never listed as its own independent root named ``sub``).

    Args:
        paths: Every file path known to the map.

    Returns:
        Basename → matching root directory path(s) (almost always
        zero or one; more than one means two same-named top-level
        packages exist, left for the caller's own ambiguity handling).
    """
    init_dirs = {
        p[: -len("/__init__.py")] if "/" in p else ""
        for p in paths
        if p.endswith("__init__.py")
    }
    roots: dict[str, list[str]] = {}
    for d in init_dirs:
        if not d:
            continue
        parent = _dirname(d)
        parent_init = f"{parent}/__init__.py" if parent else "__init__.py"
        if parent in init_dirs and parent_init in paths:
            continue
        basename = d.rsplit("/", 1)[-1]
        roots.setdefault(basename, []).append(d)
    return roots


# Segment sequences tried in order — a directory-boundary subsequence
# match anywhere in the path, not just a literal prefix at position 0,
# since a real multi-module Maven/Gradle repo nests each module's own
# "src/main/java" under a module directory (``spring-core/src/main/
# java/...``, confirmed live against ``test-repos/spring-boot``), not
# at the repo root.
_JAVA_ROOT_SEGMENTS = (
    ("src", "main", "java"),
    ("src", "test", "java"),
    ("src",),
)


def _java_suffix_index(paths: frozenset[str]) -> dict[str, list[str]]:
    """Java file path/root-stripped-suffix → matching real path(s).

    Every ``.java`` file is indexed under its own full path *and*
    (when one of the well-known Maven/Gradle source-root segment
    sequences appears anywhere in its path, at a directory boundary)
    the path with everything up to and including that root stripped —
    so ``_resolve_import_java``'s single dict lookup works whether the
    repo nests sources directly under ``src/main/java`` or under
    ``<module>/src/main/java``, without hardcoding one specific
    layout or module-directory depth as "the" root.

    Args:
        paths: Every file path known to the map.

    Returns:
        Lookup key → matching path(s) (almost always zero or one).
    """
    index: dict[str, list[str]] = {}
    for p in paths:
        if not p.endswith(".java"):
            continue
        index.setdefault(p, []).append(p)
        segs = p.split("/")
        suffix = _strip_java_root(segs)
        if suffix is not None:
            index.setdefault(suffix, []).append(p)
    return index


def _strip_java_root(segs: list[str]) -> str | None:
    """First matching source-root segment sequence stripped from
    ``segs``, or ``None`` when none of ``_JAVA_ROOT_SEGMENTS`` appears.
    """
    for root_segs in _JAVA_ROOT_SEGMENTS:
        n = len(root_segs)
        for i in range(len(segs) - n):
            if tuple(segs[i : i + n]) == root_segs:
                return "/".join(segs[i + n :])
    return None


def _cpp_basename_index(paths: frozenset[str]) -> dict[str, list[str]]:
    """C/C++ file basename → matching real path(s), scoped to
    C/C++-shaped extensions only (a same-named file in another
    language must never satisfy a ``#include`` filename search).
    """
    index: dict[str, list[str]] = {}
    for p in paths:
        if not p.endswith(_CPP_EXTENSIONS):
            continue
        index.setdefault(p.rsplit("/", 1)[-1], []).append(p)
    return index


def resolve_imports(
    files: list[FileMap],
    root: Path | None = None,
    workspace_manifests: dict[str, _WorkspaceManifest] | None = None,
) -> ModuleGraph:
    """Resolve every file's raw imports into a file-to-file dependency
    graph.

    Per-language resolution (see each ``_resolve_import_*`` function)
    turns ``Import.source`` — raw, unresolved text at extraction time
    (a dotted Python path, a JS/TS relative specifier, a Rust ``use``
    path, a Java package path, or a C/C++ include path) — into an
    in-repo file path, or leaves it unresolved (external: stdlib,
    third-party, or a source string this pass can't confidently place).
    A self-import (a file importing itself) is recorded as a genuine
    edge, not filtered out — ``find_cycles`` reports it as a distinct
    1-node self-cycle. An import matching more than one plausible
    in-repo file is left unresolved rather than guessed, the same
    "skip rather than guess" discipline the call/heritage resolvers
    already apply everywhere.

    Every per-language resolver is O(1) (a handful of dict lookups and
    bounded ancestor-directory walks) once ``_ImportResolveContext``'s
    indices are built, so the whole pass is O(total imports) after one
    O(files) index-build pass — no O(imports x files) scan anywhere,
    the shape that would make this pathologically slow on a large
    repo (verified live against ``test-repos/spring-boot``'s Java-heavy
    corpus; see the implementation report).

    Args:
        files: Per-file extraction results.
        root: Repository root, used only to discover and parse
            ``tsconfig.json``/``jsconfig.json`` (see
            ``_load_tsconfig_alias_tables``) so a JS/TS ``@/*``-style
            path-alias import can resolve to a real in-repo file
            instead of staying external by construction. ``None`` (the
            default) skips that discovery entirely, matching this
            function's own long-standing "pure function of
            already-extracted ``FileMap``s" shape exactly for every
            caller that doesn't opt in — including the entire
            in-memory-``FileMap`` test suite. Also used to discover
            the JS/TS workspace package table, unless the caller
            already has it.
        workspace_manifests: Already-loaded workspace manifests (see
            ``_load_workspace_manifests``), so ``resolve()`` pays for
            one discovery pass rather than two. ``None`` loads them
            from ``root`` (or nothing, without a root).

    Returns:
        The resolved ``ModuleGraph``.
    """
    if workspace_manifests is None:
        workspace_manifests = (
            _load_workspace_manifests(root) if root is not None else {}
        )

    paths = frozenset(fm.path for fm in files)
    ctx = _ImportResolveContext(
        paths=paths,
        py_package_roots=_py_package_roots(paths),
        java_suffix_index=_java_suffix_index(paths),
        cpp_basename_index=_cpp_basename_index(paths),
        crate_roots=_rust_crate_roots_index_all(paths),
        ts_path_aliases=(
            _load_tsconfig_alias_tables(root) if root is not None else {}
        ),
        workspace_manifests=workspace_manifests,
    )

    edge_names: dict[tuple[str, str], set[str]] = {}
    external: dict[str, set[str]] = {}
    for fm in files:
        resolver = _IMPORT_RESOLVERS.get(fm.language)
        for imp in fm.imports:
            target = resolver(imp, fm.path, ctx) if resolver else None
            if target is None:
                external.setdefault(fm.path, set()).add(
                    bare_import_source(imp, fm.language)
                )
                continue
            edge_names.setdefault((fm.path, target), set()).add(imp.name)

    edges = [
        ModuleEdge(importer=i, imported=j, names=sorted(names))
        for (i, j), names in sorted(edge_names.items())
    ]
    deps_out: dict[str, list[str]] = {}
    deps_in: dict[str, list[str]] = {}
    for edge in edges:
        deps_out.setdefault(edge.importer, []).append(edge.imported)
        deps_in.setdefault(edge.imported, []).append(edge.importer)
    for table in (deps_out, deps_in):
        for key in table:
            table[key] = sorted(set(table[key]))

    return ModuleGraph(
        edges=edges,
        deps_out=deps_out,
        deps_in=deps_in,
        external={path: sorted(names) for path, names in external.items()},
    )


def find_cycles(deps_out: dict[str, list[str]]) -> list[list[str]]:
    """Find every circular-dependency cluster in a module graph.

    Tarjan's strongly-connected-components algorithm over ``deps_out``
    — chosen over a simpler "does any cycle exist" DFS because the
    useful answer for a refactor question ("can I safely split this
    file without breaking a cycle") is *which* files are mutually
    entangled, not just yes/no: an SCC of size 2+ *is* the answer to
    "these files can't be split apart without addressing the cycle
    first". A size-1 SCC is only interesting when its sole member
    imports itself (a genuine, if rare, self-import — e.g. a re-export
    gone wrong); reported as a distinct 1-file cycle, not conflated
    with a real multi-file SCC.

    Implemented iteratively (an explicit work stack, not recursive
    call frames) — a straightforward recursive Tarjan would blow
    Python's default recursion limit on a large repo's genuinely deep
    import chain (confirmed a real risk, not a theoretical one, when
    verifying against ``test-repos``' larger corpora; see the
    implementation report). O(V + E), the same complexity class as
    ``trace.py``'s own BFS.

    Args:
        deps_out: File path → sorted paths it imports (``ModuleGraph.
            deps_out``, or the same-shaped field loaded onto
            ``MapIndex.module_deps_out`` — this takes the plain dict
            rather than a ``ModuleGraph``/``MapIndex`` object so both
            the resolver-time and query-time callers can share it).

    Returns:
        Every cycle (an SCC with 2+ members, or a 1-member SCC with a
        self-loop), each as its member paths sorted ascending. Sorted
        by descending size then ascending members for deterministic
        output.
    """
    nodes = sorted(
        set(deps_out) | {n for targets in deps_out.values() for n in targets}
    )
    indices: dict[str, int] = {}
    low_link: dict[str, int] = {}
    on_stack: set[str] = set()
    tarjan_stack: list[str] = []
    sccs: list[list[str]] = []
    counter = 0

    for root in nodes:
        if root in indices:
            continue
        counter = _strongconnect(
            root,
            deps_out,
            indices,
            low_link,
            on_stack,
            tarjan_stack,
            sccs,
            counter,
        )

    cycles = [
        sorted(comp)
        for comp in sccs
        if len(comp) >= 2
        or (len(comp) == 1 and comp[0] in deps_out.get(comp[0], []))
    ]
    cycles.sort(key=lambda c: (-len(c), c))
    return cycles


def _strongconnect(
    root: str,
    deps_out: dict[str, list[str]],
    indices: dict[str, int],
    low_link: dict[str, int],
    on_stack: set[str],
    tarjan_stack: list[str],
    sccs: list[list[str]],
    counter: int,
) -> int:
    """One iterative Tarjan traversal rooted at ``root``.

    Split out of ``find_cycles`` purely to keep that function's
    cyclomatic complexity under the project's Ruff limit — behaviorally
    this is the classic iterative-Tarjan work-stack loop: each frame is
    ``(node, iterator-over-node's-successors)``, resumed in place
    (the same ``Iterator`` object) rather than recursing, so a single
    frame per call depth level never accumulates on Python's own call
    stack regardless of how deep the import graph's DFS tree goes.

    Args:
        root: Unvisited node to start this traversal from.
        deps_out: File path → paths it imports.
        indices: Discovery-order index per visited node (mutated).
        low_link: Low-link value per visited node (mutated).
        on_stack: Nodes currently on ``tarjan_stack`` (mutated).
        tarjan_stack: The algorithm's own node stack (mutated).
        sccs: Completed strongly-connected components (mutated).
        counter: Next discovery-order index to assign.

    Returns:
        The updated discovery-order counter.
    """
    call_stack: list[tuple[str, Iterator[str]]] = [
        (root, iter(deps_out.get(root, [])))
    ]
    indices[root] = counter
    low_link[root] = counter
    counter += 1
    tarjan_stack.append(root)
    on_stack.add(root)

    while call_stack:
        node, it = call_stack[-1]
        descended = False
        for succ in it:
            if succ not in indices:
                indices[succ] = counter
                low_link[succ] = counter
                counter += 1
                tarjan_stack.append(succ)
                on_stack.add(succ)
                call_stack.append((succ, iter(deps_out.get(succ, []))))
                descended = True
                break
            if succ in on_stack:
                low_link[node] = min(low_link[node], indices[succ])
        if descended:
            continue

        call_stack.pop()
        if low_link[node] == indices[node]:
            component: list[str] = []
            while True:
                w = tarjan_stack.pop()
                on_stack.discard(w)
                component.append(w)
                if w == node:
                    break
            sccs.append(component)
        if call_stack:
            parent = call_stack[-1][0]
            low_link[parent] = min(low_link[parent], low_link[node])

    return counter
