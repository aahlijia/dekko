"""Best-effort static call resolution: raw calls → graph edges.

Resolution order for each call: same class/container → same file →
imported names → unique repo-wide name match. Anything still unclear
is reported as ambiguous rather than guessed; names with no in-repo
candidates are external.
"""

import re
from pathlib import PurePosixPath

from .model import CallGraph, Edge, FileMap, Import, RawCall, Symbol

_SELF_RECEIVERS = {"self", "this", "Self", "cls"}
_PATH_SPLIT = re.compile(r"::|\.|/")
_INDEX_STEMS = {"__init__", "mod", "lib", "index"}

MODULE_CALLER_SUFFIX = "::<module>"


def resolve(files: list[FileMap]) -> CallGraph:
    """Resolve every raw call across the repo into a call graph.

    Args:
        files: Per-file extraction results.

    Returns:
        The resolved ``CallGraph`` with bidirectional adjacency.
    """
    index = _build_index(files)
    by_name_path = _build_name_path_index(files)
    imports_by_file = _imports_by_file(files)
    symbols_by_id = {sym.id: sym for fm in files for sym in fm.symbols}

    edges: set[tuple[str, str]] = set()
    ambiguous: dict[tuple[str, str], list[str]] = {}
    external: set[tuple[str | None, str]] = set()

    for fm in files:
        file_imports = imports_by_file.get(fm.path, {})
        for call in fm.calls:
            _resolve_call(
                call,
                index=index,
                by_name_path=by_name_path,
                file_imports=file_imports,
                symbols_by_id=symbols_by_id,
                edges=edges,
                ambiguous=ambiguous,
                external=external,
            )

    graph = CallGraph(
        edges=[Edge(caller=c, callee=e) for c, e in sorted(edges)],
        ambiguous=[
            (caller, name, cands)
            for (caller, name), cands in sorted(ambiguous.items())
        ],
        external=sorted(external, key=lambda t: (t[0] or "", t[1])),
    )
    _build_adjacency(graph)
    return graph


def _resolve_call(
    call: RawCall,
    index: dict[str, list[Symbol]],
    by_name_path: dict[tuple[str, str], list[Symbol]],
    file_imports: dict[str, Import],
    symbols_by_id: dict[str, Symbol],
    edges: set[tuple[str, str]],
    ambiguous: dict[tuple[str, str], list[str]],
    external: set[tuple[str | None, str]],
) -> None:
    """Resolve one call and record it in the right bucket."""
    caller_id = call.caller_id or f"{call.path}{MODULE_CALLER_SUFFIX}"
    candidates = index.get(call.name, [])
    if not candidates:
        external.add((call.caller_id, call.text))
        return

    same_file = by_name_path.get((call.name, call.path), [])
    target = _pick_candidate(
        call,
        candidates,
        same_file,
        file_imports,
        symbols_by_id.get(call.caller_id or ""),
    )
    if target is not None:
        if target.id != caller_id:
            edges.add((caller_id, target.id))
        return
    key = (caller_id, call.name)
    ambiguous.setdefault(key, sorted(c.id for c in candidates))


def _pick_candidate(
    call: RawCall,
    candidates: list[Symbol],
    same_file: list[Symbol],
    file_imports: dict[str, Import],
    caller: Symbol | None,
) -> Symbol | None:
    """Apply the resolution ladder; ``None`` means ambiguous.

    ``same_file`` is the pre-bucketed list of like-named symbols in the
    calling file, so the same-file and container steps avoid rescanning
    every repo-wide candidate for a common name.
    """
    container = _self_container(call, caller)
    if container is not None:
        target_qual = f"{container}.{call.name}"
        same = [c for c in same_file if c.qualname == target_qual]
        if len(same) == 1:
            return same[0]

    if len(same_file) == 1:
        return same_file[0]

    hinted = _import_match(call, candidates, file_imports)
    if hinted is not None:
        return hinted

    if len(candidates) == 1:
        return candidates[0]
    return None


def _self_container(call: RawCall, caller: Symbol | None) -> str | None:
    """Container qualname when the call goes through self/this."""
    if caller is None or call.receiver is None:
        return None
    first = _PATH_SPLIT.split(call.receiver)[0]
    if first not in _SELF_RECEIVERS:
        return None
    if "." not in caller.qualname:
        return None
    return caller.qualname.rsplit(".", 1)[0]


def _import_match(
    call: RawCall, candidates: list[Symbol], file_imports: dict[str, Import]
) -> Symbol | None:
    """Match candidates against import hints for this file."""
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
    return None


def _module_matches(source: str, candidate_path: str) -> bool:
    """Check whether an import source plausibly names a file.

    Args:
        source: Import source string (``pkg.mod.name``, ``a::b::c``).
        candidate_path: Repo-relative path of a candidate symbol.

    Returns:
        True when the file's stem (or its directory, for index files
        like ``__init__.py`` / ``mod.rs``) appears in the source.
    """
    segments = {
        s
        for s in _PATH_SPLIT.split(source)
        if s and s not in ("crate", "super", "self")
    }
    path = PurePosixPath(candidate_path)
    stem = path.stem
    if stem in _INDEX_STEMS and path.parent.name:
        stem = path.parent.name
    return stem in segments


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
