"""Trace shortest call path(s) between two symbols.

``dekko trace <from> <to>`` answers "how does X reach Y?" by walking the
resolved call graph (``calls_out``) breadth-first from the source and
reconstructing the shortest path(s) to the target. Endpoints use the same
target syntax as ``query``/``context``; ambiguous or unknown endpoints are
reported, never guessed. "No path" is a clean result, not an error.
"""

import json
import sys
from collections import deque

from .mapfile import MapIndex
from .model import Symbol
from .query import (
    EXIT_OK,
    report_unresolved,
    resolve_target,
)
from .textutil import signature
from .resolver import MODULE_CALLER_SUFFIX

EXIT_NO_PATH = 1

# Cap on graph nodes explored before giving up, so a pathological graph
# can never hang the command.
_MAX_VISITED = 100_000


def _shortest_paths(
    index: MapIndex, start: str, goal: str, max_paths: int
) -> list[list[str]]:
    """Find up to ``max_paths`` shortest call paths from start to goal.

    Args:
        index: Loaded map index.
        start: Source symbol id.
        goal: Target symbol id.
        max_paths: Maximum number of distinct shortest paths to return.

    Returns:
        A list of paths (each a list of symbol ids, start..goal). Empty
        when no path exists. All returned paths share the minimal length.
    """
    if start == goal:
        return [[start]]
    preds = _bfs_preds(index, start, goal)
    if goal not in preds:
        return []
    return _reconstruct(preds, start, goal, max_paths)


def _bfs_preds(index: MapIndex, start: str, goal: str) -> dict[str, list[str]]:
    """BFS from start, recording every minimal-depth predecessor.

    Returns a ``node -> parents`` map covering all shortest-path edges;
    ``goal`` is a key iff it is reachable. Bounded by ``_MAX_VISITED``.
    """
    depth = {start: 0}
    preds: dict[str, list[str]] = {}
    frontier = deque([start])
    goal_depth: int | None = None
    while frontier and len(depth) <= _MAX_VISITED:
        node = frontier.popleft()
        if goal_depth is not None and depth[node] >= goal_depth:
            continue
        reached = _relax(index, node, goal, depth, preds, frontier)
        if reached is not None:
            goal_depth = reached
    return preds


def _relax(
    index: MapIndex,
    node: str,
    goal: str,
    depth: dict[str, int],
    preds: dict[str, list[str]],
    frontier: deque,
) -> int | None:
    """Expand one node's callees, returning the goal depth if first hit."""
    found = None
    base = depth[node] + 1
    for nid in index.calls_out.get(node, []):
        if nid.endswith(MODULE_CALLER_SUFFIX):
            continue
        if nid not in index.symbols_by_id:
            continue
        if nid not in depth:
            depth[nid] = base
            preds[nid] = [node]
            if nid == goal:
                found = base
            else:
                frontier.append(nid)
        elif base == depth[nid]:
            preds[nid].append(node)
    return found


def _reconstruct(
    preds: dict[str, list[str]], start: str, goal: str, max_paths: int
) -> list[list[str]]:
    """Rebuild up to ``max_paths`` paths from a predecessor map."""
    paths: list[list[str]] = []
    stack: list[tuple[str, list[str]]] = [(goal, [goal])]
    while stack and len(paths) < max_paths:
        node, suffix = stack.pop()
        if node == start:
            paths.append(list(reversed(suffix)))
            continue
        stack.extend(
            (parent, [*suffix, parent]) for parent in preds.get(node, [])
        )
    return paths


def _resolve_endpoint(
    index: MapIndex, target: str
) -> tuple[Symbol | None, int]:
    """Resolve one endpoint, printing and coding any failure."""
    sym, candidates = resolve_target(index, target)
    if sym is None:
        return None, report_unresolved(target, candidates)
    return sym, EXIT_OK


def _path_line(index: MapIndex, ids: list[str]) -> str:
    """Render one path as an arrow chain of ``path:line`` hops."""
    hops = []
    for sid in ids:
        sym = index.symbols_by_id[sid]
        hops.append(f"{sym.path}:{sym.start_line} {sym.qualname}")
    return " -> ".join(hops)


def _path_json(index: MapIndex, ids: list[str]) -> list[dict]:
    """Render one path as a list of symbol docs."""
    out = []
    for sid in ids:
        sym = index.symbols_by_id[sid]
        out.append(
            {
                "id": sym.id,
                "path": sym.path,
                "line": sym.start_line,
                "signature": signature(sym),
            }
        )
    return out


def run(
    index: MapIndex,
    frm: str,
    to: str,
    max_paths: int,
    as_json: bool,
) -> int:
    """Trace shortest call path(s) from one symbol to another.

    Args:
        index: Loaded map index.
        frm: Source symbol target string.
        to: Destination symbol target string.
        max_paths: Cap on the number of shortest paths to report.
        as_json: Emit structured JSON instead of text.

    Returns:
        Process exit code: 0 path found, 1 no path, 3 endpoint not
        found, 4 endpoint ambiguous.
    """
    src, code = _resolve_endpoint(index, frm)
    if src is None:
        return code
    dst, code = _resolve_endpoint(index, to)
    if dst is None:
        return code

    paths = _shortest_paths(index, src.id, dst.id, max_paths)

    if as_json:
        doc = {
            "from": src.id,
            "to": dst.id,
            "paths": [_path_json(index, p) for p in paths],
        }
        print(json.dumps(doc, indent=2))
        return EXIT_OK if paths else EXIT_NO_PATH

    if not paths:
        print(f"no call path from {src.id} to {dst.id}", file=sys.stderr)
        return EXIT_NO_PATH
    for path in paths:
        print(_path_line(index, path))
    return EXIT_OK
