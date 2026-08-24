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

import multiprocessing
import posixpath
import re
import sys
from collections.abc import Callable, Iterator
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures import TimeoutError as PoolTimeoutError
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import TypeVar

from dekko.classify import is_test_path
from dekko.core import languages
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
_RESOLVE_PARALLEL_MIN_ITEMS = 5_000

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


def _pool_retry_note(what: str, retry_workers: int) -> None:
    """Print the process-pool-retry disclosure note to stderr.

    Mirrors round 15's ``_maybe_warn_sequential`` pattern (a one-line
    ``note:`` on stderr before a slower fallback path runs) so a
    caller that ends up waiting longer for a reduced-parallelism retry
    isn't left in the dark about why.
    """
    plural = "" if retry_workers == 1 else "s"
    print(
        f"note: process pool failed during {what} (likely CPU "
        "contention from another concurrent dekko process on this "
        f"machine) -- retrying with reduced parallelism "
        f"({retry_workers} worker{plural})",
        file=sys.stderr,
    )


def run_pooled_with_retry(
    run: Callable[[int], _PoolResultT], workers: int, what: str
) -> _PoolResultT:
    """Run a process-pool step, retrying once at reduced parallelism if
    the pool itself breaks.

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
    smaller). If that attempt also raises ``BrokenProcessPool``, it
    propagates unchanged -- a genuinely wedged or resource-exhausted
    machine should surface a clear error, not hang retrying
    indefinitely.

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
    try:
        try:
            return run(workers)
        except BrokenProcessPool:
            retry_workers = min(workers, _POOL_RETRY_WORKERS)
            _pool_retry_note(what, retry_workers)
            multiprocessing.set_executable(sys.executable)
            return run(retry_workers)
    except PoolTimeoutError as exc:
        raise PoolStalledError(
            f"process pool made no progress during {what} within "
            f"{POOL_RESULT_TIMEOUT_S}s -- a worker likely failed to "
            "start or stalled (e.g. under heavy CPU contention from "
            "another concurrent dekko process on this machine). "
            "Retry with --jobs 1, or after system load has "
            "subsided."
        ) from exc


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


def resolve(files: list[FileMap], workers: int = 1) -> CallGraph:
    """Resolve every raw call across the repo into a call graph.

    Args:
        files: Per-file extraction results.
        workers: Worker count for parallel call-graph resolution
            (1 = sequential, the default — every caller except
            ``cli.py``'s ``run_map`` leaves this at 1, matching prior
            behavior exactly). See ``_resolve_all`` for how chunking
            and the parallelization threshold work.

    Returns:
        The resolved ``CallGraph`` with bidirectional adjacency.
    """
    index = _build_index(files)
    by_name_path = _build_name_path_index(files)
    imports_by_file = _imports_by_file(files)
    symbols_by_id = {sym.id: sym for fm in files for sym in fm.symbols}
    repo_stems = {_repo_stem(PurePosixPath(fm.path)) for fm in files}

    edges, ambiguous, external = _resolve_all(
        files,
        index,
        by_name_path,
        imports_by_file,
        repo_stems,
        symbols_by_id,
        workers,
    )

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
    graph.referenced, graph.referenced_in, graph.referenced_out = resolve_refs(
        files, workers
    )
    (
        graph.heritage,
        graph.heritage_out,
        graph.heritage_in,
        graph.heritage_ambiguous,
        graph.heritage_external,
    ) = resolve_heritage(files)
    graph.modules = resolve_imports(files)
    (
        graph.throws,
        graph.throws_out,
        graph.throws_ambiguous,
        graph.throws_external,
        graph.throws_bare,
    ) = resolve_throws(files, workers)
    graph.catches = resolve_catches(files, workers)
    # No resolution pass needed — a literal env-var key is already the
    # fully-resolved fact (see model.EnvRead's docstring), so this is
    # a plain flatten across files, not a call into a dedicated
    # resolve_env_reads() the way every other section above is.
    graph.env_reads = [r for fm in files for r in fm.env_reads]
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

    Below ``_RESOLVE_PARALLEL_MIN_ITEMS`` total raw calls, or with
    ``workers <= 1``, this is exactly the old single-process loop (via
    ``_resolve_files_chunk`` called once on the full file list) — same
    result, same cost, no pool startup overhead paid for nothing.
    Above the threshold, ``files`` is split into up to ``workers``
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
    if workers <= 1 or total_calls < _RESOLVE_PARALLEL_MIN_ITEMS:
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
        with ProcessPoolExecutor(
            max_workers=w,
            initializer=_init_resolve_worker,
            initargs=(
                index,
                by_name_path,
                imports_by_file,
                repo_stems,
                symbols_by_id,
            ),
        ) as pool:
            futures = [
                pool.submit(_resolve_files_chunk_worker, chunk)
                for chunk in chunks
            ]
            for future in futures:
                chunk_edges, chunk_ambiguous, chunk_external = future.result(
                    timeout=POOL_RESULT_TIMEOUT_S
                )
                for key, lines in chunk_edges.items():
                    edges.setdefault(key, set()).update(lines)
                for key, cands in chunk_ambiguous.items():
                    ambiguous.setdefault(key, cands)
                for key, lines in chunk_external.items():
                    external.setdefault(key, set()).update(lines)
        return edges, ambiguous, external

    return run_pooled_with_retry(_run, workers, "call resolution")


def _resolve_refs_chunk(
    files: list[FileMap],
    index: dict[str, list[Symbol]],
    by_name_path: dict[tuple[str, str], list[Symbol]],
    imports_by_file: dict[str, dict[str, Import]],
    symbols_by_id: dict[str, Symbol],
) -> dict[tuple[str, str], set[int]]:
    """Resolve every reference in ``files`` into a fresh, local ``edges``
    dict — the reference-resolution analog of ``_resolve_files_chunk``,
    same pure-function/worker-safety shape."""
    edges: dict[tuple[str, str], set[int]] = {}
    for fm in files:
        file_imports = imports_by_file.get(fm.path, {})
        for ref in fm.refs:
            _resolve_ref(
                ref,
                index=index,
                by_name_path=by_name_path,
                file_imports=file_imports,
                symbols_by_id=symbols_by_id,
                edges=edges,
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
    return _resolve_refs_chunk(
        files,
        _worker_index,
        _worker_by_name_path,
        _worker_imports_by_file,
        _worker_symbols_by_id,
    )


def resolve_refs(
    files: list[FileMap], workers: int = 1
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

    Returns:
        ``(edges, referenced_in, referenced_out)``, the same shape
        ``resolve()`` builds for calls, but for references.
    """
    index = _build_index(files)
    by_name_path = _build_name_path_index(files)
    imports_by_file = _imports_by_file(files)
    symbols_by_id = {sym.id: sym for fm in files for sym in fm.symbols}

    total_refs = sum(len(fm.refs) for fm in files)
    use_pool = workers > 1 and total_refs >= _RESOLVE_PARALLEL_MIN_ITEMS

    def _run(w: int) -> dict[tuple[str, str], set[int]]:
        chunks = (
            _chunk_files(files, w * _RESOLVE_CHUNK_OVERSUBSCRIPTION)
            if use_pool
            else [files]
        )
        if len(chunks) < 2:
            return _resolve_refs_chunk(
                files, index, by_name_path, imports_by_file, symbols_by_id
            )

        edges: dict[tuple[str, str], set[int]] = {}
        with ProcessPoolExecutor(
            max_workers=w,
            initializer=_init_resolve_worker,
            initargs=(
                index,
                by_name_path,
                imports_by_file,
                None,
                symbols_by_id,
            ),
        ) as pool:
            futures = [
                pool.submit(_resolve_refs_chunk_worker, chunk)
                for chunk in chunks
            ]
            for future in futures:
                result = future.result(timeout=POOL_RESULT_TIMEOUT_S)
                for key, lines in result.items():
                    edges.setdefault(key, set()).update(lines)
        return edges

    edges = run_pooled_with_retry(_run, workers, "reference resolution")

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
    if target is not None and target.id != caller_id:
        edges.setdefault((caller_id, target.id), set()).add(ref.line)


def resolve_heritage(
    files: list[FileMap],
) -> tuple[
    list[HeritageEdge],
    dict[str, list[str]],
    dict[str, list[str]],
    list[tuple[str, str, list[str]]],
    list[ExternalCall],
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

    Args:
        files: Per-file extraction results.

    Returns:
        ``(heritage_edges, heritage_out, heritage_in,
        heritage_ambiguous, heritage_external)`` — the same shapes
        ``resolve()`` assigns onto ``CallGraph.heritage``/
        ``heritage_out``/``heritage_in``/``heritage_ambiguous``/
        ``heritage_external``. Built as a plain tuple return (mirroring
        ``resolve_refs()``'s own return shape) rather than a
        ``CallGraph`` method, since ``resolve()`` just assigns the
        pieces onto the graph it already built, exactly as it already
        does for ``resolve_refs()``'s result.
    """
    index = _build_index(files)
    by_name_path = _build_name_path_index(files)
    imports_by_file = _imports_by_file(files)
    repo_stems = {_repo_stem(PurePosixPath(fm.path)) for fm in files}

    edges: dict[tuple[str, str], set[int]] = {}
    relations: dict[tuple[str, str], str] = {}
    ambiguous: dict[tuple[str, str], list[str]] = {}
    external: dict[tuple[str, str], set[int]] = {}
    for fm in files:
        file_imports = imports_by_file.get(fm.path, {})
        for h in fm.heritage:
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
    )


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
) -> None:
    """Resolve one heritage clause; mirrors ``_resolve_call``'s shape.

    Candidates are pre-filtered to ``TYPE_KINDS`` at every step (the
    bare-name index lookup, the same-file lookup, and the alias-import
    recovery) before ``_pick_candidate``'s ladder runs.
    """
    if _receiver_is_external(h, file_imports, repo_stems):
        external.setdefault((h.subtype_id, h.text), set()).add(h.line)
        return

    candidates = [c for c in index.get(h.name, []) if c.kind in TYPE_KINDS]
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
    target = _pick_candidate(
        h,
        candidates,
        same_file,
        file_imports,
        None,
        by_name_path,
        index,
        repo_stems,
    )
    if target is not None:
        _add_heritage_edge(h, target.id, edges, relations)
        return
    _record_ambiguous(h.subtype_id, h.name, candidates, ambiguous)


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
    use_pool = workers > 1 and total_throws >= _RESOLVE_PARALLEL_MIN_ITEMS

    def _run(
        w: int,
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
        with ProcessPoolExecutor(
            max_workers=w,
            initializer=_init_resolve_worker,
            initargs=(index, by_name_path, imports_by_file, None, None),
        ) as pool:
            futures = [
                pool.submit(_resolve_throws_chunk_worker, chunk)
                for chunk in chunks
            ]
            for future in futures:
                c_edges, c_ambiguous, c_external, c_bare = future.result(
                    timeout=POOL_RESULT_TIMEOUT_S
                )
                for key, lines in c_edges.items():
                    edges.setdefault(key, set()).update(lines)
                for key, cands in c_ambiguous.items():
                    ambiguous.setdefault(key, cands)
                for key, lines in c_external.items():
                    external.setdefault(key, set()).update(lines)
                bare.extend(c_bare)
        return edges, ambiguous, external, bare

    edges, ambiguous, external, bare = run_pooled_with_retry(
        _run, workers, "throw resolution"
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
    use_pool = workers > 1 and total_catches >= _RESOLVE_PARALLEL_MIN_ITEMS

    def _run(w: int) -> list[CatchSite]:
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
        with ProcessPoolExecutor(
            max_workers=w,
            initializer=_init_resolve_worker,
            initargs=(index, by_name_path, imports_by_file, None, None),
        ) as pool:
            futures = [
                pool.submit(_resolve_catches_chunk_worker, chunk)
                for chunk in chunks
            ]
            for future in futures:
                sites.extend(future.result(timeout=POOL_RESULT_TIMEOUT_S))
        return sites

    sites = run_pooled_with_retry(_run, workers, "catch resolution")
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

    Falls through to the full, unfiltered ``candidates`` whenever the
    filter would leave nothing at all — either the call site's own
    language couldn't be determined, or (a legitimate, if rare, case)
    every candidate is in a different language than the call site,
    e.g. a C header declaring something a C++ file uses. Removing
    candidates that can never legitimately be the right answer must
    never turn a resolvable call into an unresolvable one.
    """
    spec = languages.spec_for_path(call.path)
    if spec is None:
        return candidates

    same_language = [c for c in candidates if c.language == spec.name]
    if not same_language:
        return candidates

    return same_language


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
) -> Symbol | None:
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
    ``_is_noise_call``) — only ``_resolve_call`` passes it, so
    ``_resolve_ref``'s reference resolution (a separate table from
    ``calls_in``/fan-in, not affected by the bug this check targets)
    is left byte-for-byte unchanged.

    ``raw_imports``, when given, is the calling file's full (undeduped)
    import list — passed only for whole-file-include languages
    (C/C++), and used by ``_import_match``'s fallback step. See
    ``_WHOLE_FILE_IMPORT_LANGUAGES``.

    Before any of the above runs, ``candidates`` is narrowed to those
    matching the call site's own language (see ``_language_filtered``)
    — a same-bare-name candidate in a language that can never
    legitimately be the target is removed before it gets a chance to
    win one of the later, weaker heuristics (round 21 tensorflow.md
    §5). ``same_file`` needs no equivalent filtering: every symbol in
    it is already, by construction, in the same file (and therefore
    the same language) as the call site.
    """
    candidates = _language_filtered(call, candidates)

    container_match = _container_match(call, caller, same_file)
    if container_match is not None:
        return container_match

    receiver_type = _receiver_type_match(call, candidates, index)
    if receiver_type is not None:
        return receiver_type

    typed = _typed_param_match(call, candidates, caller)
    if typed is not None:
        return typed

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

    hinted = _import_match(call, candidates, file_imports, raw_imports)
    if hinted is not None:
        return hinted

    if repo_stems is not None and _is_noise_call(
        call, file_imports, repo_stems
    ):
        return None

    if len(candidates) == 1:
        return candidates[0]

    return _last_resort_match(call, candidates, by_name_path)


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
_BUILTIN_METHOD_NAMES = frozenset(
    {
        "trim", "trimStart", "trimEnd", "toString", "valueOf",
        "toLowerCase", "toUpperCase", "includes", "indexOf", "slice",
        "splice", "concat", "join", "push", "pop", "shift", "unshift",
        "forEach", "map", "filter", "reduce", "startsWith", "endsWith",
        "padStart", "padEnd", "repeat", "charAt", "substring",
        "replace", "replaceAll", "split", "flat", "hasOwnProperty",
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
_RUST_STD_METHOD_NAMES = frozenset(
    {
        "then", "then_some", "iter", "iter_mut", "into_iter", "map",
        "map_err", "and_then", "or_else", "unwrap", "unwrap_or",
        "unwrap_or_else", "unwrap_or_default", "expect", "clone",
        "into", "as_ref", "as_mut", "as_str", "as_slice", "to_string",
        "to_owned", "to_vec", "borrow", "borrow_mut", "lock", "read",
        "write", "collect", "filter", "for_each", "fold", "is_some",
        "is_none", "is_ok", "is_err", "ok", "err", "take", "replace",
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
    return (
        call.name in _BUILTIN_METHOD_NAMES
        or call.name in _CHAIN_BUILDER_METHOD_NAMES
        or call.name in _RUST_STD_METHOD_NAMES
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
    return not (_import_segments(imp.source) & repo_stems)


# Type-annotation tokens that never name the receiver's own class —
# generic/optional/collection wrappers and null-like literals a
# declared type can be dressed in (``Optional[Controller]``,
# ``Controller | undefined``, ``List<Controller>``, ``Box<Controller>``).
# Filtered out before trying each remaining identifier token as a
# candidate bare class name, rather than parsing the type expression
# properly (best-effort, not a type-language parser).
_TYPE_NOISE_WORDS = frozenset(
    {
        "Optional", "Promise", "List", "Array", "Vec", "Box", "Rc",
        "Arc", "Option", "Result", "Ok", "Err", "None", "null",
        "undefined", "readonly", "const", "mut", "ref",
    }
)  # fmt: skip
_TYPE_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


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


def _typed_param_match(
    call: _Referable, candidates: list[Symbol], caller: Symbol | None
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

    Args:
        call: The raw call or reference being resolved.
        candidates: Every same-named symbol repo-wide (already looked
            up by the caller).
        caller: The enclosing symbol, or ``None`` for module-level
            calls — which have no declared parameters to check.

    Returns:
        The uniquely-matching method, or ``None`` when the receiver
        isn't one of ``caller``'s declared, typed parameters, or the
        type doesn't narrow ``candidates`` to exactly one.
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
    for token in _TYPE_TOKEN_RE.findall(param_type):
        if token in _TYPE_NOISE_WORDS:
            continue
        target_qual = f"{token}.{call.name}"
        matched = [c for c in candidates if c.qualname == target_qual]
        if len(matched) == 1:
            return matched[0]
    return None


# Constructor method names this resolver recognizes for a class-shaped
# symbol, checked in order against ``(name, cls.path)`` — JS/TS/TSX's
# fixed ``constructor``, Python's fixed ``__init__``, and last, the
# class's own bare name (Java's ``constructor_declaration`` has no
# distinct keyword: its extracted ``name`` field is the class name).
_CONSTRUCTOR_NAMES = ("constructor", "__init__")


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
    """
    hints: list[str] = []
    imp = file_imports.get(call.name)
    if imp is not None:
        hints.append(imp.source)
    if call.receiver:
        first = _PATH_SPLIT.split(call.receiver)[0]
        rec_imp = file_imports.get(first)
        if rec_imp is not None:
            hints.append(rec_imp.source)
    for hint in hints:
        matched = [c for c in candidates if _module_matches(hint, c.path)]
        if len(matched) == 1:
            return matched[0]
    if raw_imports:
        matched = [
            c
            for c in candidates
            if any(_module_matches(i.source, c.path) for i in raw_imports)
        ]
        if len(matched) == 1:
            return matched[0]
    return None


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
    original = _PATH_SPLIT.split(imp.source)[-1]
    return [
        c
        for c in index.get(original, [])
        if _module_matches(imp.source, c.path)
    ]


def _receiver_is_external(
    call: _Referable,
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
    is read, which every ``_Referable`` shape exposes.

    Args:
        call: The raw call or heritage clause being resolved.
        file_imports: Local name to import record for the calling
            file.
        repo_stems: Every repo file's matching stem (see
            ``_repo_stem``), used to test whether an import's source
            plausibly names a module in this repo.

    Returns:
        True when the receiver resolves to an import whose source
        matches no file in the repo. False when there is no receiver,
        the receiver isn't an import (local variable, ``self``, ...),
        or the import does plausibly point into the repo.
    """
    if not call.receiver:
        return False
    first = _PATH_SPLIT.split(call.receiver)[0]
    imp = file_imports.get(first)
    if imp is None:
        return False
    return not (_import_segments(imp.source) & repo_stems)


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
    """
    stem = _repo_stem(PurePosixPath(candidate_path))
    return stem in _import_segments(source)


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


def _imports_by_file(files: list[FileMap]) -> dict[str, dict[str, Import]]:
    """Map file path → local name → import record."""
    out: dict[str, dict[str, Import]] = {}
    for fm in files:
        table = out.setdefault(fm.path, {})
        for imp in fm.imports:
            table.setdefault(imp.name, imp)
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
    """

    paths: frozenset[str]
    py_package_roots: dict[str, list[str]] = field(default_factory=dict)
    java_suffix_index: dict[str, list[str]] = field(default_factory=dict)
    cpp_basename_index: dict[str, list[str]] = field(default_factory=dict)


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
    ``"opentui-spinner/react"`` down to ``"opentui-spinner"``). Bare
    specifiers (no leading ``.``/``..``) are external by construction —
    an npm package name never collides with a relative path, so no
    repo-root search is needed the way Python's absolute imports
    require one.
    """
    module_source = imp.source.rsplit("/", 1)[0] if imp.name else imp.source
    if not (module_source.startswith("./") or module_source.startswith("../")):
        return None
    base_dir = _dirname(importer_path)
    joined = posixpath.normpath(
        f"{base_dir}/{module_source}" if base_dir else module_source
    )
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
    return _first_match(ctx.paths, candidates)


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


def _resolve_import_rust(
    imp: Import, importer_path: str, ctx: _ImportResolveContext
) -> str | None:
    """Resolve a Rust ``use`` path to a repo file.

    Dispatches on the leading path segment: ``crate::`` resolves
    against the crate root (``_rust_crate_root``), ``self::``/
    ``super::`` (one or more, e.g. ``super::super::foo``) against the
    importer's own module position (``_rust_self_base``, walked up
    once per ``super``). A bare crate name (``serde::Deserialize``,
    no recognized prefix) is external by construction — real
    third-party crates never start with ``crate``/``self``/``super``.

    Like Python, the trailing segment is ambiguous between "a
    submodule" and "an item defined in the parent module" — resolved
    the same way, via ``_resolve_two_candidate_lists``.
    """
    segs = imp.source.split("::")
    if not segs:
        return None

    if segs[0] == "crate":
        base = _rust_crate_root(importer_path, ctx.paths)
        rest = segs[1:]
    elif segs[0] in ("self", "super"):
        base = _rust_self_base(importer_path)
        i = 0
        while i < len(segs) and segs[i] == "super":
            base = _dirname(base)
            i += 1
        if i < len(segs) and segs[i] == "self":
            i += 1
        rest = segs[i:]
    else:
        return None

    if base is None or not rest:
        return None
    # A nested package directory's own index file is always mod.rs —
    # lib.rs/main.rs only ever names the crate root itself, which is
    # only reachable here when ``rest``/``rest[:-1]`` is empty (base
    # itself is the target). Trying all three index names whenever
    # the remaining segment list is empty covers both shapes without
    # needing to track "is base the crate root" separately.
    full = _dir_module_candidates(base, rest, ".rs", _RUST_INDEX_NAMES)
    dropped = _dir_module_candidates(base, rest[:-1], ".rs", _RUST_INDEX_NAMES)
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


def resolve_imports(files: list[FileMap]) -> ModuleGraph:
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

    Returns:
        The resolved ``ModuleGraph``.
    """
    paths = frozenset(fm.path for fm in files)
    ctx = _ImportResolveContext(
        paths=paths,
        py_package_roots=_py_package_roots(paths),
        java_suffix_index=_java_suffix_index(paths),
        cpp_basename_index=_cpp_basename_index(paths),
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
