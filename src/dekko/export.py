"""Render the call graph as Mermaid or Graphviz DOT.

The full symbol graph is large, so exports default to a node cap and a
``--scope`` switch: ``symbol`` draws one node per definition, ``file``
collapses to one node per file with edges between files.
"""

import sys
from pathlib import Path

from .mapfile import MapIndex
from .resolver import MODULE_CALLER_SUFFIX

EXIT_OK = 0
EXIT_TOO_BIG = 2

FORMATS = ("mermaid", "dot", "html")
SCOPES = ("symbol", "file")
DEFAULT_MAX_NODES = 300


def _dir_of(path: str) -> str:
    """Directory portion of a repo-relative path (``.`` for the root)."""
    head, _, _ = path.rpartition("/")
    return head or "."


def _path_of(index: MapIndex, node_id: str) -> str | None:
    """File path backing a graph node id (symbol or module origin)."""
    sym = index.symbols_by_id.get(node_id)
    if sym is not None:
        return sym.path
    if node_id.endswith(MODULE_CALLER_SUFFIX):
        return node_id[: -len(MODULE_CALLER_SUFFIX)]
    return None


def _symbol_graph(
    index: MapIndex,
) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """Build ``(labels, edges)`` at symbol scope.

    Module-level call origins are dropped here; both endpoints must be
    real symbols.
    """
    edges: set[tuple[str, str]] = set()
    for caller, callees in index.calls_out.items():
        if caller not in index.symbols_by_id:
            continue
        for callee in callees:
            if callee in index.symbols_by_id and callee != caller:
                edges.add((caller, callee))
    labels = {
        node: index.symbols_by_id[node].qualname
        for edge in edges
        for node in edge
    }
    return labels, sorted(edges)


def _file_graph(
    index: MapIndex,
) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """Build ``(labels, edges)`` at file scope (no self-loops)."""
    edges: set[tuple[str, str]] = set()
    for caller, callees in index.calls_out.items():
        src = _path_of(index, caller)
        if src is None:
            continue
        for callee in callees:
            dst = _path_of(index, callee)
            if dst is not None and dst != src:
                edges.add((src, dst))
    labels = {node: node for edge in edges for node in edge}
    return labels, sorted(edges)


def dir_graph(
    index: MapIndex,
) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """Build ``(labels, edges)`` at directory scope (no self-loops).

    The coarse, directory-level dependency view shared by MAP.md's
    mermaid diagram and the lean map's module-edge text (FR3): both are
    skins of this one graph.
    """
    edges: set[tuple[str, str]] = set()
    for caller, callees in index.calls_out.items():
        src = _path_of(index, caller)
        if src is None:
            continue
        src_dir = _dir_of(src)
        for callee in callees:
            dst = _path_of(index, callee)
            if dst is None:
                continue
            dst_dir = _dir_of(dst)
            if dst_dir != src_dir:
                edges.add((src_dir, dst_dir))
    labels = {node: node for edge in edges for node in edge}
    return labels, sorted(edges)


def build_graph(
    index: MapIndex, scope: str
) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """Dispatch graph construction by scope."""
    if scope == "file":
        return _file_graph(index)
    return _symbol_graph(index)


def overview_graph(
    index: MapIndex, max_nodes: int
) -> tuple[dict[str, str], list[tuple[str, str]], str]:
    """Pick the graph to embed in MAP.md's overview, with a scale guard.

    Tiers down as the repo grows: the file-scope graph while it fits
    under ``max_nodes``, then a directory-scope collapse, then nothing.

    Args:
        index: Loaded map index.
        max_nodes: Node ceiling shared with ``dekko export``.

    Returns:
        ``(labels, edges, status)`` where ``status`` is ``file`` or
        ``dir`` for a renderable graph, ``empty`` when there are no
        edges to draw, or ``too_big`` when even the directory graph
        exceeds ``max_nodes`` (caller should omit and point at
        ``dekko export``).
    """
    labels, edges = _file_graph(index)
    if not edges:
        return {}, [], "empty"
    if len(labels) <= max_nodes:
        return labels, edges, "file"
    labels, edges = dir_graph(index)
    if len(labels) <= max_nodes:
        return labels, edges, "dir"
    return labels, edges, "too_big"


def _ids(labels: dict[str, str]) -> dict[str, str]:
    """Assign stable ``n0``/``n1`` ids to graph nodes."""
    return {node: f"n{i}" for i, node in enumerate(sorted(labels))}


def render_mermaid(
    labels: dict[str, str], edges: list[tuple[str, str]]
) -> str:
    """Render a flowchart in Mermaid syntax."""
    ids = _ids(labels)
    lines = ["flowchart LR"]
    for node in sorted(labels):
        text = labels[node].replace('"', "'")
        lines.append(f'  {ids[node]}["{text}"]')
    for src, dst in edges:
        lines.append(f"  {ids[src]} --> {ids[dst]}")
    return "\n".join(lines)


def render_dot(labels: dict[str, str], edges: list[tuple[str, str]]) -> str:
    """Render a digraph in Graphviz DOT syntax."""
    ids = _ids(labels)
    lines = ["digraph dekko {", "  rankdir=LR;"]
    for node in sorted(labels):
        text = labels[node].replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'  {ids[node]} [label="{text}"];')
    for src, dst in edges:
        lines.append(f"  {ids[src]} -> {ids[dst]};")
    lines.append("}")
    return "\n".join(lines)


def run(
    index: MapIndex,
    fmt: str,
    scope: str,
    max_nodes: int,
    out_path: Path | None = None,
) -> int:
    """Emit the call graph in the requested format.

    Args:
        index: Loaded map index.
        fmt: ``mermaid`` or ``dot``.
        scope: ``symbol`` or ``file``.
        max_nodes: Refuse to render more nodes than this.
        out_path: Write to this file instead of stdout when given.

    Returns:
        ``0`` on success, ``2`` when the graph exceeds ``max_nodes``.
    """
    labels, edges = build_graph(index, scope)
    if len(labels) > max_nodes:
        print(
            f"dekko: graph has {len(labels)} nodes (limit {max_nodes}); "
            "use --scope file, a subtree map, or raise --max-nodes",
            file=sys.stderr,
        )
        return EXIT_TOO_BIG

    text = render_dot if fmt == "dot" else render_mermaid
    output = text(labels, edges)
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output + "\n")
        print(f"dekko: wrote {out_path}")
    else:
        print(output)
    return EXIT_OK
