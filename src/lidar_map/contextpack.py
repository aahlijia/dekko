"""Context packs: the minimal neighborhood needed to work on a target.

A pack contains the target's signature and location, its file's
imports, and the signatures of callers/callees within N hops. An
optional token budget trims the farthest, least-connected neighbors
first; the target itself is never dropped.
"""

import json
import sys
from dataclasses import dataclass, field

from .mapfile import MapIndex
from .model import Import, Symbol
from .query import (
    EXIT_AMBIGUOUS,
    EXIT_OK,
    paths_matching,
    report_unresolved,
    resolve_target,
)
from .render_md import signature
from .resolver import MODULE_CALLER_SUFFIX


@dataclass
class PackEntry:
    """One neighbor in a context pack."""

    sym: Symbol
    hop: int
    direction: str


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
    """

    label: str
    target: Symbol | None
    file_path: str
    file_symbols: list[Symbol] = field(default_factory=list)
    imports: list[Import] = field(default_factory=list)
    entries: list[PackEntry] = field(default_factory=list)
    module_callers: list[str] = field(default_factory=list)
    trimmed: int = 0


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


def _entry_line(entry: PackEntry) -> str:
    """Render one neighbor line."""
    sym = entry.sym
    return f"  [{entry.hop}] {sym.path}:{sym.start_line}  {signature(sym)}"


def render_text(pack: Pack) -> str:
    """Render a pack as compact text."""
    lines = [f"context: {pack.label}"]
    if pack.target is not None:
        t = pack.target
        lines.append(signature(t))
        lines.append(
            f"  {t.kind} ({t.language}) at "
            f"{t.path}:{t.start_line}-{t.end_line}"
        )
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
            lines += [
                _entry_line(e)
                for e in sorted(group, key=lambda e: (e.hop, e.sym.path))
            ]
    if pack.module_callers:
        joined = ", ".join(pack.module_callers)
        lines.append(f"module-level callers: {joined}")
    if pack.trimmed:
        lines.append(f"(trimmed {pack.trimmed} symbols to fit budget)")
    return "\n".join(lines)


def _estimate_tokens(pack: Pack) -> int:
    """Crude token estimate of the rendered pack."""
    return len(render_text(pack)) // 4


def trim_to_budget(index: MapIndex, pack: Pack, budget: int | None) -> Pack:
    """Drop neighbors until the pack fits the token budget.

    Farthest hops go first; within a hop, the least-connected symbols.
    The target (and, in file mode, the file's own symbol list) is
    trimmed last and only from the end.

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
    return pack


def _render_json(pack: Pack) -> str:
    """Render a pack as structured JSON."""

    def sym_doc(sym: Symbol) -> dict:
        return {
            "id": sym.id,
            "path": sym.path,
            "line": sym.start_line,
            "kind": sym.kind,
            "signature": signature(sym),
        }

    doc = {
        "label": pack.label,
        "target": sym_doc(pack.target) if pack.target else None,
        "file": pack.file_path,
        "imports": [
            {"name": i.name, "source": i.source} for i in pack.imports
        ],
        "file_symbols": [sym_doc(s) for s in pack.file_symbols],
        "neighbors": [
            {"hop": e.hop, "direction": e.direction, **sym_doc(e.sym)}
            for e in pack.entries
        ],
        "module_callers": pack.module_callers,
        "trimmed": pack.trimmed,
    }
    return json.dumps(doc, indent=2)


def run(
    index: MapIndex, target: str, hops: int, budget: int | None, as_json: bool
) -> int:
    """Build, trim, and print a context pack for a target.

    Args:
        index: Loaded map index.
        target: Symbol target, or a file path in file mode.
        hops: Neighborhood radius.
        budget: Approximate token budget, or ``None``.
        as_json: Emit structured JSON instead of text.

    Returns:
        Process exit code.
    """
    sym, candidates = resolve_target(index, target)
    if sym is not None:
        pack = build_pack(index, sym, hops)
    elif not candidates and ":" not in target:
        paths = paths_matching(index, target)
        if len(paths) != 1:
            if len(paths) > 1:
                print(
                    f"lidar: '{target}' is ambiguous; candidates:",
                    file=sys.stderr,
                )
                for p in paths:
                    print(f"  {p}", file=sys.stderr)
                return EXIT_AMBIGUOUS
            return report_unresolved(target, candidates)
        pack = build_file_pack(index, paths[0])
    else:
        return report_unresolved(target, candidates)

    trim_to_budget(index, pack, budget)
    print(_render_json(pack) if as_json else render_text(pack))
    return EXIT_OK
