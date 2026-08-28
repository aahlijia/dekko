"""Context packs: the minimal neighborhood needed to work on a target.

A pack contains the target's signature, location, and doc line, its
file's imports, and the signatures of callers/callees within N hops.
``with_source`` additionally inlines the target's body and hop-1
call-site lines. An optional token budget trims the farthest,
least-connected neighbors first, then the source from the bottom; the
target's signature is never dropped.
"""

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from dekko.analysis import relevance
from dekko.render.mapfile import MapIndex
from dekko.core.model import Import, Symbol
from dekko.analysis.relevance import TaskContext
from dekko.analysis.query import (
    EXIT_AMBIGUOUS,
    EXIT_OK,
    ambiguous_counts,
    paths_matching,
    report_unresolved,
    resolve_target,
)
from dekko.source import read_lines
from dekko.textutil import signature
from dekko.core.resolver import MODULE_CALLER_SUFFIX, bare_import_source
from dekko.textutil import Meter, estimate_tokens

# Call-site excerpts shown per hop-1 caller entry.
_MAX_SITES_PER_ENTRY = 3

# Default token cap for a context pack when the caller passes no
# budget. Without this, a symbol-mode pack's import list (before
# relevance filtering) or a wide neighbor set can dump far more than
# an edit task needs — the caller can always pass a larger budget
# explicitly.
DEFAULT_PACK_BUDGET = 800


@dataclass
class PackEntry:
    """One neighbor in a context pack.

    Attributes:
        sites: ``(line, source text)`` call-site excerpts; filled only
            for hop-1 callers in ``with_source`` mode.
    """

    sym: Symbol
    hop: int
    direction: str
    sites: list[tuple[int, str]] = field(default_factory=list)


@dataclass
class Pack:
    """A built context pack, ready to render.

    Attributes:
        label: Human label, ``path:qualname`` or a bare file path.
        target: Target symbol, or ``None`` in file mode.
        file_path: File the target (or pack) belongs to.
        language: ``file_path``'s language, for display-only source
            stripping (see ``bare_import_source``) — never empty
            except on an index that predates language tracking.
        file_symbols: All symbols of the file (file mode only).
        imports: Imports declared in ``file_path``. In symbol mode
            this is filtered to imports referenced by the target or
            its neighbors — see ``_relevant_imports``.
        entries: Neighboring symbols with hop distance and direction.
        module_callers: Files whose top level calls into the pack.
        trimmed: Content items (neighbors, imports, file symbols)
            dropped to satisfy the token budget.
        imports_dropped: Imports discarded by relevance filtering
            (distinct from ``trimmed``, which counts budget cuts).
        source_lines: The target's body in ``with_source`` mode, else
            ``None``.
        source_truncated: Whether budget trimming dropped source lines.
        notes: Note texts anchored to the target symbol.
        ambig_in: Additional call sites named after the target that
            resolved ambiguously and so never became a ``calls_in``
            edge (symbol mode only; always 0 in file mode — see
            ``build_file_pack``).
        ambig_out: Names the target itself called that resolved
            ambiguously and so never became a ``calls_out`` edge
            (symbol mode only; always 0 in file mode).
    """

    label: str
    target: Symbol | None
    file_path: str
    language: str = ""
    file_symbols: list[Symbol] = field(default_factory=list)
    imports: list[Import] = field(default_factory=list)
    entries: list[PackEntry] = field(default_factory=list)
    module_callers: list[str] = field(default_factory=list)
    trimmed: int = 0
    imports_dropped: int = 0
    source_lines: list[str] | None = None
    source_truncated: bool = False
    notes: list[str] = field(default_factory=list)
    ambig_in: int = 0
    ambig_out: int = 0


def _neighbors(index: MapIndex, sym_id: str) -> list[tuple[str, str]]:
    """Adjacent symbol ids of one node, tagged with direction."""
    pairs = [(sid, "caller") for sid in index.calls_in.get(sym_id, [])]
    pairs += [(sid, "callee") for sid in index.calls_out.get(sym_id, [])]
    return pairs


def _anonymous_entries(
    index: MapIndex, module_id: str, callee_id: str, hop: int
) -> list[PackEntry]:
    """Promote a module-level pseudo-caller's call sites to real entries.

    ``module_id`` (``path::<module>``) is always the caller side of an
    edge — a module is never a resolvable callee — so each recorded
    call-site line becomes its own synthetic ``kind="module"`` symbol
    with a real line number, landing in the ``callers:`` list next to
    named-function callers instead of a separate, line-number-less
    ``module-level callers:`` summary that is easy to miss (bug #4).

    Maps written before doc version 3 have no ``edge_lines``; when the
    lookup is empty, the caller falls back to the bare
    ``pack.module_callers`` entry it already appended (no synthetic
    entries added here — same graceful-degradation pattern
    ``query.py``'s ``_site_rows`` uses).

    Args:
        index: Loaded map index (for ``edge_lines``/``languages_by_path``).
        module_id: The ``path::<module>`` pseudo-symbol id.
        callee_id: The real symbol id the module calls into.
        hop: BFS hop distance to record on each entry.

    Returns:
        One ``PackEntry`` per call site, or an empty list.
    """
    path = module_id[: -len(MODULE_CALLER_SUFFIX)]
    lines = index.edge_lines.get((module_id, callee_id), [])
    return [
        PackEntry(
            sym=Symbol(
                id=module_id,
                name="<anonymous>",
                qualname="<anonymous>",
                kind="module",
                path=path,
                language=index.languages_by_path.get(path, ""),
                start_line=line,
                end_line=line,
            ),
            hop=hop,
            direction="caller",
        )
        for line in lines
    ]


def _relevant_imports(pack: Pack) -> list[Import]:
    """Filter ``pack.imports`` to names referenced by the pack itself.

    Cheap heuristic, no source re-scan: an import survives if its
    local binding name appears as a whole word in the target's own
    signature/doc, or in any collected neighbor's signature/doc — the
    same text ``render_text`` already assembles via ``_target_lines``/
    ``_entry_lines``. Call after ``pack.entries`` is populated.

    Args:
        pack: Pack whose imports to filter.

    Returns:
        The subset of ``pack.imports`` judged relevant.
    """
    haystack_lines = _target_lines(pack)
    for entry in pack.entries:
        haystack_lines += _entry_lines(entry)
    tokens = set(re.findall(r"\w+", " ".join(haystack_lines)))
    return [imp for imp in pack.imports if imp.name in tokens]


def build_pack(
    index: MapIndex,
    target: Symbol,
    hops: int,
    all_imports: bool = False,
) -> Pack:
    """BFS the call graph around a symbol up to ``hops``.

    Args:
        index: Loaded map index.
        target: Resolved target symbol.
        hops: Neighborhood radius (>= 1).
        all_imports: Skip the ``_relevant_imports`` relevance filter
            and keep every import in the target's file.

    Returns:
        The assembled pack (untrimmed).
    """
    pack = Pack(
        label=f"{target.path}:{target.qualname}",
        target=target,
        file_path=target.path,
        language=target.language,
        imports=index.imports_by_path.get(target.path, []),
        notes=list(index.notes.get(target.id, [])),
    )
    pack.ambig_in, pack.ambig_out = ambiguous_counts(index, target)
    seen = {target.id}
    frontier = [target.id]
    for hop in range(1, hops + 1):
        next_frontier: list[str] = []
        for sym_id in frontier:
            for nid, direction in _neighbors(index, sym_id):
                if nid in seen:
                    continue
                seen.add(nid)
                if nid.endswith(MODULE_CALLER_SUFFIX):
                    pack.module_callers.append(
                        nid[: -len(MODULE_CALLER_SUFFIX)]
                    )
                    pack.entries.extend(
                        _anonymous_entries(index, nid, sym_id, hop)
                    )
                    continue
                sym = index.symbols_by_id.get(nid)
                if sym is None:
                    continue
                pack.entries.append(PackEntry(sym, hop, direction))
                next_frontier.append(nid)
        frontier = next_frontier
    pack.module_callers = sorted(set(pack.module_callers))
    all_imports_list = pack.imports
    if all_imports:
        pack.imports_dropped = 0
    else:
        pack.imports = _relevant_imports(pack)
        pack.imports_dropped = len(all_imports_list) - len(pack.imports)
    return pack


def build_file_pack(index: MapIndex, path: str) -> Pack:
    """Assemble a file-mode pack: own symbols + outside callers.

    Args:
        index: Loaded map index.
        path: Repo-relative file path (already validated).

    Returns:
        The assembled pack (untrimmed).
    """
    pack = Pack(
        label=path,
        target=None,
        file_path=path,
        language=index.languages_by_path.get(path, ""),
        file_symbols=list(index.symbols_by_path.get(path, [])),
        imports=index.imports_by_path.get(path, []),
    )
    seen: set[str] = set()
    for sym in pack.file_symbols:
        for nid in index.calls_in.get(sym.id, []):
            if nid in seen:
                continue
            seen.add(nid)
            if nid.endswith(MODULE_CALLER_SUFFIX):
                caller_path = nid[: -len(MODULE_CALLER_SUFFIX)]
                pack.module_callers.append(caller_path)
                # Mirrors the "outside callers" framing this function
                # promises: a file's own top-level code calling its
                # own function isn't a caller worth surfacing here.
                if caller_path != path:
                    pack.entries.extend(
                        _anonymous_entries(index, nid, sym.id, 1)
                    )
                continue
            other = index.symbols_by_id.get(nid)
            if other is not None and other.path != path:
                pack.entries.append(PackEntry(other, 1, "caller"))
    pack.module_callers = sorted(
        p for p in set(pack.module_callers) if p != path
    )
    return pack


def attach_source(index: MapIndex, pack: Pack, root: Path) -> None:
    """Attach the target's body and hop-1 caller call-site excerpts.

    Best-effort: unreadable files simply leave the pack without
    source. File-mode packs (no target symbol) are left untouched —
    inlining a whole file would defeat the pack's purpose.

    Args:
        index: Loaded map index (for edge call-site lines).
        pack: Pack to enrich in place.
        root: Repository root the map was generated from.
    """
    if pack.target is None:
        return
    body = read_lines(root, pack.target.path)[
        pack.target.start_line - 1 : pack.target.end_line
    ]
    if body:
        pack.source_lines = body
    cache: dict[str, list[str]] = {}
    for entry in pack.entries:
        if entry.hop != 1 or entry.direction != "caller":
            continue
        lines = index.edge_lines.get((entry.sym.id, pack.target.id), [])
        if not lines:
            continue
        if entry.sym.path not in cache:
            cache[entry.sym.path] = read_lines(root, entry.sym.path)
        file_lines = cache[entry.sym.path]
        for line_no in lines[:_MAX_SITES_PER_ENTRY]:
            if 1 <= line_no <= len(file_lines):
                entry.sites.append((line_no, file_lines[line_no - 1].strip()))


def _entry_lines(entry: PackEntry) -> list[str]:
    """Render one neighbor entry (with doc and call-site lines)."""
    sym = entry.sym
    rows = [f"  [{entry.hop}] {sym.path}:{sym.start_line}  {signature(sym)}"]
    if sym.doc:
        rows.append(f"      doc: {sym.doc}")
    rows += [f"      > {line}: {text}" for line, text in entry.sites]
    return rows


def _target_lines(pack: Pack) -> list[str]:
    """The target's signature/location/doc block, if any."""
    if pack.target is None:
        return []
    t = pack.target
    lines = [
        signature(t),
        f"  {t.kind} ({t.language}) at {t.path}:{t.start_line}-{t.end_line}",
    ]
    if t.doc:
        lines.append(f"  doc: {t.doc}")
    lines += [f"  note: {text}" for text in pack.notes]
    return lines


def _source_lines(pack: Pack) -> list[str]:
    """The inlined source section, if any."""
    if not pack.source_lines:
        return []
    lines = ["source:"]
    lines += [f"  {src}" for src in pack.source_lines]
    if pack.source_truncated:
        lines.append("  … (source truncated)")
    return lines


def _residual_module_callers(pack: Pack) -> list[str]:
    """module_callers paths with no corresponding promoted entry.

    A path fully covered by a promoted anonymous PackEntry (see
    ``_anonymous_entries``) has nothing left to say in the terser
    trailing line — printing it there too just duplicates what
    ``callers:`` already shows with a real line number. A path is
    still printed here when promotion produced nothing for it (pre-v3
    maps with no ``edge_lines``, or a file-mode pack's own
    self-caller, which ``build_file_pack`` deliberately never
    promotes) — that's the only place the information still lives.

    Args:
        pack: Pack whose ``module_callers`` to filter.

    Returns:
        The subset of ``pack.module_callers`` not already represented
        by a promoted ``kind="module"`` entry.
    """
    covered = {e.sym.path for e in pack.entries if e.sym.kind == "module"}
    return [p for p in pack.module_callers if p not in covered]


def render_text(pack: Pack) -> str:
    """Render a pack as compact text."""
    lines = [f"context: {pack.label}"]
    lines += _target_lines(pack)
    if pack.ambig_in:
        lines.append(
            f"  note: {pack.ambig_in} additional call site(s) named "
            f"'{pack.target.name}' resolved ambiguously — not "
            "counted here"
        )
    if pack.ambig_out:
        lines.append(
            f"  note: {pack.ambig_out} outgoing call(s) from this "
            "symbol resolved ambiguously (name matched 2+ "
            "candidates) — not counted here"
        )
    lines += _source_lines(pack)
    if pack.imports or pack.imports_dropped:
        lines.append(f"imports ({pack.file_path}):")
        lines += [
            f"  {imp.name}  (from {bare_import_source(imp, pack.language)})"
            for imp in pack.imports
        ]
        if pack.imports_dropped:
            n = pack.imports_dropped
            lines.append(
                f"  +{n} more imports (name not referenced in the "
                "target's or its neighbors' signatures — rerun with "
                "--all-imports to include them)"
            )
    if pack.file_symbols:
        lines.append("symbols:")
        lines += [
            f"  {s.start_line}  {signature(s)}" for s in pack.file_symbols
        ]
    for direction, title in (("caller", "callers:"), ("callee", "callees:")):
        group = [e for e in pack.entries if e.direction == direction]
        if group:
            lines.append(title)
            for e in sorted(group, key=lambda e: (e.hop, e.sym.path)):
                lines += _entry_lines(e)
    residual = _residual_module_callers(pack)
    if residual:
        lines.append(f"module-level callers: {', '.join(residual)}")
    return "\n".join(lines)


def _pack_meter(pack: Pack, text: str, budget: int | None) -> Meter:
    """Cost meter for a pack, with trimmed neighbors as omissions.

    Token cost is measured from the text rendering — the same basis
    ``trim_to_budget`` uses — so the reported figure matches the budget
    that was applied on either output surface.
    """
    kept = len(pack.entries) + len(pack.file_symbols)
    # Signals: the symbols this pack puts in context — neighbors, file
    # symbols, and the target itself (FR-D3 density).
    signals = kept + (1 if pack.target is not None else 0)
    return Meter(
        tokens=estimate_tokens(text),
        returned=kept,
        total=kept + pack.trimmed,
        budget=budget,
        limit=None,
        signals=signals,
    )


def _estimate_tokens(pack: Pack) -> int:
    """Crude token estimate of the rendered pack."""
    return estimate_tokens(render_text(pack))


def _entry_scores(
    index: MapIndex, pack: Pack, task: TaskContext
) -> dict[str, float]:
    """Blend task relevance with call degree over a pack's neighbors."""
    candidates = [
        relevance.Candidate(
            id=e.sym.id,
            text=f"{e.sym.qualname} {signature(e.sym)}",
            path=e.sym.path,
        )
        for e in pack.entries
    ]
    centrality = {
        e.sym.id: float(index.degree(e.sym.id)) for e in pack.entries
    }
    return relevance.blended_scores(task, candidates, centrality)


def trim_to_budget(
    index: MapIndex,
    pack: Pack,
    budget: int | None,
    task: TaskContext | None = None,
) -> Pack:
    """Drop pack content until it fits the token budget.

    Imports go first (least load-bearing for an edit task —
    ``outline`` or reading the file already answers "what's imported
    here"), then neighbors (farthest hops, then least-connected), then
    the file-mode symbol list from the end, then inlined source from
    the bottom. The target's signature and location are never
    dropped. With a ``task`` signal, neighbors are dropped
    least-relevant first so the task-relevant callers/callees survive
    a tight budget.

    Imports are trimmed *before* neighbors specifically so a high-
    fan-out symbol's import list can never fully starve the callers/
    callees a caller actually asked about (bug #5/B5 — four
    evaluators hit a context pack that spent its entire default
    budget on imports and returned 0% of the requested callers/
    callees; ``_relevant_imports`` already shrinks the import list to
    what the pack references, but a tight budget could previously
    still empty ``pack.entries`` to zero before touching a single
    import).

    Args:
        index: Loaded map index (for degree ranking).
        pack: Pack to trim in place.
        budget: Approximate token budget, or ``None`` for no limit.
        task: Optional task context for relevance-aware trimming.

    Returns:
        The same pack, trimmed.
    """
    if budget is None:
        return pack
    while pack.imports and _estimate_tokens(pack) > budget:
        pack.imports.pop()
        pack.trimmed += 1
    if task is not None and not task.is_empty and pack.entries:
        scores = _entry_scores(index, pack, task)
        droppable = sorted(
            pack.entries,
            key=lambda e: (
                scores.get(e.sym.id, 0.0),
                -e.hop,
                index.degree(e.sym.id),
            ),
        )
    else:
        droppable = sorted(
            pack.entries, key=lambda e: (-e.hop, index.degree(e.sym.id))
        )
    while droppable and _estimate_tokens(pack) > budget:
        pack.entries.remove(droppable.pop(0))
        pack.trimmed += 1
    while len(pack.file_symbols) > 1 and _estimate_tokens(pack) > budget:
        pack.file_symbols.pop()
        pack.trimmed += 1
    while pack.source_lines and _estimate_tokens(pack) > budget:
        pack.source_lines.pop()
        pack.source_truncated = True
    return pack


def _render_json(pack: Pack, meter: Meter) -> str:
    """Render a pack as structured JSON."""

    def sym_doc(sym: Symbol) -> dict:
        return {
            "id": sym.id,
            "path": sym.path,
            "line": sym.start_line,
            "kind": sym.kind,
            "signature": signature(sym),
            "doc": sym.doc,
        }

    def neighbor_doc(e: PackEntry) -> dict:
        entry = {"hop": e.hop, "direction": e.direction, **sym_doc(e.sym)}
        if e.sites:
            entry["sites"] = [
                {"line": line, "text": text} for line, text in e.sites
            ]
        return entry

    doc = {
        "label": pack.label,
        "target": sym_doc(pack.target) if pack.target else None,
        "file": pack.file_path,
        "imports": [
            {"name": i.name, "source": i.source} for i in pack.imports
        ],
        "imports_dropped": pack.imports_dropped,
        "file_symbols": [sym_doc(s) for s in pack.file_symbols],
        "neighbors": [neighbor_doc(e) for e in pack.entries],
        "module_callers": pack.module_callers,
        "trimmed": pack.trimmed,
        "meta": meter.as_dict(),
    }
    if pack.notes:
        doc["notes"] = pack.notes
    if pack.ambig_in:
        doc["ambiguous_in"] = pack.ambig_in
    if pack.ambig_out:
        doc["ambiguous_out"] = pack.ambig_out
    if pack.source_lines is not None:
        doc["source"] = "\n".join(pack.source_lines)
        doc["source_truncated"] = pack.source_truncated
    return json.dumps(doc, indent=2)


def run(
    index: MapIndex,
    target: str,
    hops: int,
    budget: int | None,
    as_json: bool,
    root: Path | None = None,
    with_source: bool = False,
    notes: bool = True,
    task: TaskContext | None = None,
    all_imports: bool = False,
) -> int:
    """Build, trim, and print a context pack for a target.

    Args:
        index: Loaded map index.
        target: Symbol target, or a file path in file mode.
        hops: Neighborhood radius.
        budget: Approximate token budget. ``None`` falls back to
            ``DEFAULT_PACK_BUDGET`` rather than going unbounded, so an
            unscoped call can't return an unfiltered dump.
        as_json: Emit structured JSON instead of text.
        root: Repository root, required for ``with_source``.
        with_source: Inline the target's body and hop-1 call-site
            lines (strictly opt-in; counts against ``budget``).
        notes: Include the target's notes (default on).
        task: Optional task context; when set, neighbors are trimmed
            least-relevant first under a tight budget.
        all_imports: Skip the relevance filter and keep every import
            in the target's file (no effect in file mode, which
            never filters imports).

    Returns:
        Process exit code.
    """
    sym, candidates = resolve_target(index, target)
    if sym is not None:
        pack = build_pack(index, sym, hops, all_imports=all_imports)
        if not notes:
            pack.notes = []
    elif not candidates and ":" not in target:
        paths = paths_matching(index, target)
        if len(paths) != 1:
            if len(paths) > 1:
                print(
                    f"dekko: '{target}' is ambiguous; candidates:",
                    file=sys.stderr,
                )
                for p in paths:
                    print(f"  {p}", file=sys.stderr)
                return EXIT_AMBIGUOUS
            return report_unresolved(target, candidates, index)
        pack = build_file_pack(index, paths[0])
    else:
        return report_unresolved(target, candidates, index)

    if with_source and root is not None:
        attach_source(index, pack, root)
    effective_budget = DEFAULT_PACK_BUDGET if budget is None else budget
    trim_to_budget(index, pack, effective_budget, task)
    text = render_text(pack)
    meter = _pack_meter(pack, text, effective_budget)
    if as_json:
        print(_render_json(pack, meter))
        return EXIT_OK
    print(text)
    print(meter.footer())
    return EXIT_OK
