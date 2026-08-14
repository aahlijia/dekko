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

import re
from concurrent.futures import ProcessPoolExecutor
from pathlib import PurePosixPath

from .classify import is_test_path
from .model import (
    TYPE_KINDS,
    CallGraph,
    Edge,
    ExternalCall,
    FileMap,
    Import,
    RawCall,
    RawRef,
    Symbol,
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
# Either raw-usage shape the shared candidate ladder resolves — a
# call and a bare-value reference expose the same fields
# (name/receiver/caller_id/path) and only differ in what table the
# result lands in.
_Referable = RawCall | RawRef

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

    chunks = _chunk_files(files, workers)
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
    with ProcessPoolExecutor(max_workers=len(chunks)) as pool:
        futures = [
            pool.submit(
                _resolve_files_chunk,
                chunk,
                index,
                by_name_path,
                imports_by_file,
                repo_stems,
                symbols_by_id,
            )
            for chunk in chunks
        ]
        for future in futures:
            chunk_edges, chunk_ambiguous, chunk_external = future.result()
            for key, lines in chunk_edges.items():
                edges.setdefault(key, set()).update(lines)
            for key, cands in chunk_ambiguous.items():
                ambiguous.setdefault(key, cands)
            for key, lines in chunk_external.items():
                external.setdefault(key, set()).update(lines)
    return edges, ambiguous, external


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


def resolve_refs(
    files: list[FileMap], workers: int = 1
) -> tuple[list[Edge], dict[str, list[str]], dict[str, list[str]]]:
    """Resolve every raw value reference across the repo.

    Mirrors ``resolve()``'s resolution ladder, but for ``RawRef``s
    (bare identifiers used as values — see ``model.RawRef``) instead
    of ``RawCall``s. Kept as a distinct pass with its own return
    shape/tables rather than folding into ``edges``/``calls_in``/
    ``calls_out`` — see the module docstring for why.

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
    chunks = (
        _chunk_files(files, workers)
        if workers > 1 and total_refs >= _RESOLVE_PARALLEL_MIN_ITEMS
        else [files]
    )
    edges: dict[tuple[str, str], set[int]] = {}
    if len(chunks) < 2:
        edges = _resolve_refs_chunk(
            files, index, by_name_path, imports_by_file, symbols_by_id
        )
    else:
        with ProcessPoolExecutor(max_workers=len(chunks)) as pool:
            futures = [
                pool.submit(
                    _resolve_refs_chunk,
                    chunk,
                    index,
                    by_name_path,
                    imports_by_file,
                    symbols_by_id,
                )
                for chunk in chunks
            ]
            for future in futures:
                for key, lines in future.result().items():
                    edges.setdefault(key, set()).update(lines)

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
    """
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
    call: RawCall,
    file_imports: dict[str, Import],
    repo_stems: set[str],
) -> bool:
    """Check whether a call's receiver is bound to a non-repo import.

    Runs before the bare-name index lookup so a call like
    ``subprocess.run(...)`` is recorded as external even when the
    repo happens to define its own ``run`` symbols elsewhere — the
    bare-name ladder never gets a chance to misresolve or strand the
    call in the ``ambiguous`` bucket.

    Args:
        call: The raw call being resolved.
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
