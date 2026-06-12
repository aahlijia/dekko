"""Export the call graph to mermaid or dot format."""

import sys
from collections import defaultdict

from pathlib import Path
from typing import TextIO

from .mapfile import MapIndex

MERMAID_NODE_LIMIT = 200
DOT_NODE_LIMIT = 1000


def run(root: Path, index: MapIndex, fmt: str, scope: str, as_json: bool = False) -> int:
    """Execute the export command.

    Args:
        root: The repository root.
        index: The loaded map index.
        fmt: 'mermaid' or 'dot'.
        scope: 'file' or 'symbol'.
        as_json: Emit structured JSON instead of text (not fully applicable for text formats, but kept for consistency).
    """
    if as_json:
        print("lidar export: --json is not supported for graph export formats", file=sys.stderr)
        return 2

    edges = set()
    nodes = set()

    if scope == "file":
        for caller, callees in index.calls_out.items():
            caller_sym = index.symbols_by_id.get(caller)
            if not caller_sym:
                continue
            for callee in callees:
                callee_sym = index.symbols_by_id.get(callee)
                if not callee_sym:
                    continue
                if caller_sym.path != callee_sym.path:
                    edges.add((caller_sym.path, callee_sym.path))
                    nodes.add(caller_sym.path)
                    nodes.add(callee_sym.path)
    else:
        for caller, callees in index.calls_out.items():
            for callee in callees:
                edges.add((caller, callee))
                nodes.add(caller)
                nodes.add(callee)

    limit = MERMAID_NODE_LIMIT if fmt == "mermaid" else DOT_NODE_LIMIT
    if len(nodes) > limit:
        print(f"lidar export: graph too large ({len(nodes)} nodes) for {fmt} format (limit {limit}).", file=sys.stderr)
        print("Try using --scope file to reduce node count.", file=sys.stderr)
        return 1

    lidar_dir = root / ".lidar"
    lidar_dir.mkdir(parents=True, exist_ok=True)
    out_file = lidar_dir / f"GRAPH-{fmt}.md"

    with out_file.open("w") as f:
        if fmt == "mermaid":
            _export_mermaid(nodes, edges, f)
        else:
            _export_dot(nodes, edges, f)

    print(f"lidar export: wrote graph to {out_file.relative_to(root) if out_file.is_relative_to(root) else out_file}")
    return 0


def _export_mermaid(nodes: set[str], edges: set[tuple[str, str]], f: TextIO):
    f.write("```mermaid\n")
    f.write("graph TD\n")
    # To avoid syntax errors with special characters in IDs, map them to simple IDs
    node_map = {name: f"N{i}" for i, name in enumerate(sorted(nodes))}
    
    for name, nid in node_map.items():
        # Escape quotes
        label = name.replace('"', "'")
        f.write(f"  {nid}[\"{label}\"]\n")
    
    for caller, callee in sorted(edges):
        f.write(f"  {node_map[caller]} --> {node_map[callee]}\n")
    f.write("```\n")


def _export_dot(nodes: set[str], edges: set[tuple[str, str]], f: TextIO):
    f.write("```dot\n")
    f.write("digraph G {\n")
    node_map = {name: f"N{i}" for i, name in enumerate(sorted(nodes))}
    
    for name, nid in node_map.items():
        label = name.replace('"', "'")
        f.write(f"  {nid} [label=\"{label}\"];\n")
    
    for caller, callee in sorted(edges):
        f.write(f"  {node_map[caller]} -> {node_map[callee]};\n")
    
    f.write("}\n")
    f.write("```\n")
