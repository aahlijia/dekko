"""Render the call graph as Mermaid or Graphviz DOT.

The full symbol graph is large, so exports default to a node cap and a
``--scope`` switch: ``symbol`` draws one node per definition, ``file``
collapses to one node per file with edges between files.
"""

import sys
from pathlib import Path

from dekko.analysis import communities
from dekko.render.mapfile import MapIndex, path_of

EXIT_OK = 0
EXIT_TOO_BIG = 2

FORMATS = ("mermaid", "dot", "html")
SCOPES = ("symbol", "file")
DEFAULT_MAX_NODES = 300


def _dir_of(path: str) -> str:
    """Directory portion of a repo-relative path (``.`` for the root)."""
    head, _, _ = path.rpartition("/")
    return head or "."


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
        src = path_of(index, caller)
        if src is None:
            continue
        for callee in callees:
            dst = path_of(index, callee)
            if dst is not None and dst != src:
                edges.add((src, dst))
    labels = {node: node for edge in edges for node in edge}
    return labels, sorted(edges)


def dir_graph(
    index: MapIndex,
) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """Build ``(labels, edges)`` at directory scope (no self-loops).

    The coarse, directory-level dependency view shared by MAP.md's
    mermaid diagram and the lean map's module-edge text: both are
    skins of this one graph.
    """
    edges: set[tuple[str, str]] = set()
    for caller, callees in index.calls_out.items():
        src = path_of(index, caller)
        if src is None:
            continue
        src_dir = _dir_of(src)
        for callee in callees:
            dst = path_of(index, callee)
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


Groups = dict[str, tuple[int, str]]


def _clusters(
    labels: dict[str, str], groups: Groups
) -> tuple[list[tuple[int, str, list[str]]], list[str]]:
    """Nodes by community (``(id, title, nodes)``, in id order) and the
    nodes in none."""
    by_id: dict[int, tuple[str, list[str]]] = {}
    loose = []
    for node in sorted(labels):
        if node in groups:
            cid, title = groups[node]
            by_id.setdefault(cid, (title, []))[1].append(node)
        else:
            loose.append(node)
    clusters = [(cid, *by_id[cid]) for cid in sorted(by_id)]
    return clusters, loose


def _mermaid_text(text: str) -> str:
    return text.replace('"', "'")


def render_mermaid(
    labels: dict[str, str],
    edges: list[tuple[str, str]],
    groups: Groups | None = None,
) -> str:
    """Render a flowchart in Mermaid syntax, one subgraph per group."""
    ids = _ids(labels)

    def node(n: str, indent: str) -> str:
        return f'{indent}{ids[n]}["{_mermaid_text(labels[n])}"]'

    clusters, loose = _clusters(labels, groups or {})
    lines = ["flowchart LR"]
    for cid, title, members in clusters:
        lines.append(f'  subgraph c{cid}["{_mermaid_text(title)}"]')
        lines += [node(n, "    ") for n in members]
        lines.append("  end")
    lines += [node(n, "  ") for n in loose]
    for src, dst in edges:
        lines.append(f"  {ids[src]} --> {ids[dst]}")
    return "\n".join(lines)


def _dot_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def render_dot(
    labels: dict[str, str],
    edges: list[tuple[str, str]],
    groups: Groups | None = None,
) -> str:
    """Render a digraph in Graphviz DOT syntax, one cluster per group."""
    ids = _ids(labels)

    def node(n: str, indent: str) -> str:
        return f'{indent}{ids[n]} [label="{_dot_text(labels[n])}"];'

    clusters, loose = _clusters(labels, groups or {})
    lines = ["digraph dekko {", "  rankdir=LR;"]
    for cid, title, members in clusters:
        lines.append(f"  subgraph cluster_{cid} {{")
        lines.append(f'    label="{_dot_text(title)}";')
        lines += [node(n, "    ") for n in members]
        lines.append("  }")
    lines += [node(n, "  ") for n in loose]
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
        suggestions = []
        if scope != "file":
            suggestions.append("--granularity file")
        suggestions += ["a subtree map", "raise --max-nodes"]
        print(
            f"dekko: graph has {len(labels)} nodes (limit {max_nodes}); "
            f"use {', '.join(suggestions)}",
            file=sys.stderr,
        )
        return EXIT_TOO_BIG

    text = render_dot if fmt == "dot" else render_mermaid
    groups = communities.file_titles(index) if scope == "file" else {}
    output = text(labels, edges, groups)
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output + "\n", encoding="utf-8")
        print(f"dekko: wrote {out_path}")
    else:
        print(output)
    return EXIT_OK
