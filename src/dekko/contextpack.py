"""Context packs: the minimal neighborhood needed to work on a target.

A pack contains the target's signature, location, and doc line, its
file's imports, and the signatures of callers/callees within N hops.
``with_source`` additionally inlines the target's body and hop-1
call-site lines. An optional token budget trims the farthest,
least-connected neighbors first, then the source from the bottom; the
target's signature is never dropped.
"""

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .mapfile import MapIndex
from .model import Import, Symbol
from .query import (
    EXIT_AMBIGUOUS,
    EXIT_OK,
    paths_matching,
    report_unresolved,
    resolve_target,
)
from .source import read_lines
from .textutil import signature
from .resolver import MODULE_CALLER_SUFFIX
from .textutil import Meter, estimate_tokens

# Call-site excerpts shown per hop-1 caller entry.
_MAX_SITES_PER_ENTRY = 3


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
        file_symbols: All symbols of the file (file mode only).
        imports: Imports declared in ``file_path``.
        entries: Neighboring symbols with hop distance and direction.
        module_callers: Files whose top level calls into the pack.
        trimmed: Symbols dropped to satisfy the token budget.
        source_lines: The target's body in ``with_source`` mode, else
            ``None``.
        source_truncated: Whether budget trimming dropped source lines.
        notes: Note texts anchored to the target symbol.
    """

    label: str
    target: Symbol | None
    file_path: str
    file_symbols: list[Symbol] = field(default_factory=list)
    imports: list[Import] = field(default_factory=list)
    entries: list[PackEntry] = field(default_factory=list)
    module_callers: list[str] = field(default_factory=list)
    trimmed: int = 0
    source_lines: list[str] | None = None
    source_truncated: bool = False
    notes: list[str] = field(default_factory=list)


def _neighbors(index: MapIndex, sym_id: str) -> list[tuple[str, str]]:
    """Adjacent symbol ids of one node, tagged with direction."""
    pairs = [(sid, "caller") for sid in index.calls_in.get(sym_id, [])]
    pairs += [(sid, "callee") for sid in index.calls_out.get(sym_id, [])]
    return pairs


def build_pack(index: MapIndex, target: Symbol, hops: int) -> Pack:
    """BFS the call graph around a symbol up to ``hops``.

    Args:
        index: Loaded map index.
        target: Resolved target symbol.
        hops: Neighborhood radius (>= 1).

    Returns:
        The assembled pack (untrimmed).
    """
    pack = Pack(
        label=f"{target.path}:{target.qualname}",
        target=target,
        file_path=target.path,
        imports=index.imports_by_path.get(target.path, []),
        notes=list(index.notes.get(target.id, [])),
    )
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
                    continue
                sym = index.symbols_by_id.get(nid)
                if sym is None:
                    continue
                pack.entries.append(PackEntry(sym, hop, direction))
                next_frontier.append(nid)
        frontier = next_frontier
    pack.module_callers = sorted(set(pack.module_callers))
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
                pack.module_callers.append(nid[: -len(MODULE_CALLER_SUFFIX)])
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


def render_text(pack: Pack) -> str:
    """Render a pack as compact text."""
    lines = [f"context: {pack.label}"]
    lines += _target_lines(pack)
    lines += _source_lines(pack)
    if pack.imports:
        lines.append(f"imports ({pack.file_path}):")
        lines += [f"  {imp.name}  (from {imp.source})" for imp in pack.imports]
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
    if pack.module_callers:
        joined = ", ".join(pack.module_callers)
        lines.append(f"module-level callers: {joined}")
    return "\n".join(lines)


def _pack_meter(pack: Pack, text: str, budget: int | None) -> Meter:
    """Cost meter for a pack, with trimmed neighbors as omissions.

    Token cost is measured from the text rendering — the same basis
    ``trim_to_budget`` uses — so the reported figure matches the budget
    that was applied on either output surface.
    """
    kept = len(pack.entries) + len(pack.file_symbols)
    return Meter(
        tokens=estimate_tokens(text),
        returned=kept,
        total=kept + pack.trimmed,
        budget=budget,
        limit=None,
    )


def _estimate_tokens(pack: Pack) -> int:
    """Crude token estimate of the rendered pack."""
    return estimate_tokens(render_text(pack))


def trim_to_budget(index: MapIndex, pack: Pack, budget: int | None) -> Pack:
    """Drop pack content until it fits the token budget.

    Neighbors go first (farthest hops, then least-connected), then the
    file-mode symbol list from the end, then inlined source from the
    bottom. The target's signature and location are never dropped.

    Args:
        index: Loaded map index (for degree ranking).
        pack: Pack to trim in place.
        budget: Approximate token budget, or ``None`` for no limit.

    Returns:
        The same pack, trimmed.
    """
    if budget is None:
        return pack
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
        "file_symbols": [sym_doc(s) for s in pack.file_symbols],
        "neighbors": [neighbor_doc(e) for e in pack.entries],
        "module_callers": pack.module_callers,
        "trimmed": pack.trimmed,
        "meta": meter.as_dict(),
    }
    if pack.notes:
        doc["notes"] = pack.notes
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
) -> int:
    """Build, trim, and print a context pack for a target.

    Args:
        index: Loaded map index.
        target: Symbol target, or a file path in file mode.
        hops: Neighborhood radius.
        budget: Approximate token budget, or ``None``.
        as_json: Emit structured JSON instead of text.
        root: Repository root, required for ``with_source``.
        with_source: Inline the target's body and hop-1 call-site
            lines (strictly opt-in; counts against ``budget``).
        notes: Include the target's notes (default on).

    Returns:
        Process exit code.
    """
    sym, candidates = resolve_target(index, target)
    if sym is not None:
        pack = build_pack(index, sym, hops)
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
            return report_unresolved(target, candidates)
        pack = build_file_pack(index, paths[0])
    else:
        return report_unresolved(target, candidates)

    if with_source and root is not None:
        attach_source(index, pack, root)
    trim_to_budget(index, pack, budget)
    text = render_text(pack)
    meter = _pack_meter(pack, text, budget)
    if as_json:
        print(_render_json(pack, meter))
        return EXIT_OK
    print(text)
    print(meter.footer())
    return EXIT_OK
