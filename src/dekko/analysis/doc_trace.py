"""Shortest link paths between two docs, the prose side of ``trace``.

A breadth-first search over the doc link graph in the direction links
point. Tables of contents link to everything, so a path may start or
end at one but never pass through it. Each hop prints as the doc (with
the heading the incoming link named) and the line that links onward.
"""

from __future__ import annotations

import json
import sys
from collections import deque
from dataclasses import dataclass

from dekko.analysis import docs as docs_mod
from dekko.analysis.doc_graph import DocGraph
from dekko.core.doc_edges import EXTRACTED, DocEdge
from dekko.render.mapfile import MapIndex

EXIT_OK = 0
EXIT_NO_PATH = 1
EXIT_USAGE = 2
EXIT_NOT_FOUND = 3
EXIT_AMBIGUOUS = 4


@dataclass(frozen=True)
class Hop:
    """One doc on a path: the heading arrived at, the line linking on."""

    doc: str
    anchor: str | None
    line: int | None


def names_a_doc(index: MapIndex, target: str) -> bool:
    """Whether ``target`` names an indexed doc."""
    return bool(docs_mod.match_doc(index, target.removeprefix("./")))


def run(
    index: MapIndex, frm: str, to: str, max_paths: int, as_json: bool
) -> int:
    """Print the shortest link paths from one doc to another.

    Args:
        index: Loaded map index.
        frm: The doc to start from (a path or unique suffix).
        to: The doc to reach.
        max_paths: Cap on the number of shortest paths printed.
        as_json: Emit JSON instead of text.

    Returns:
        ``0`` path found, ``1`` none, ``2`` one end is not a doc, ``3``
        an end names no doc, ``4`` an end names several.
    """
    if not (names_a_doc(index, frm) and names_a_doc(index, to)):
        print(
            "dekko: trace: both ends must be docs or both symbols",
            file=sys.stderr,
        )
        return EXIT_USAGE
    start, code = _endpoint(index, frm)
    if start is None:
        return code
    goal, code = _endpoint(index, to)
    if goal is None:
        return code
    graph = DocGraph.from_index(index, tests=True)
    paths = _shortest(graph, start, goal, max_paths)
    hops = [_hops(index, path) for path in paths]
    if as_json:
        doc = {
            "from": start,
            "to": goal,
            "paths": [[h.__dict__ for h in path] for path in hops],
        }
        print(json.dumps(doc, indent=2))
        return EXIT_OK if paths else EXIT_NO_PATH
    if not paths:
        print(f"no link path from {start} to {goal}", file=sys.stderr)
        return EXIT_NO_PATH
    for path in hops:
        print(" -> ".join(_hop_text(h) for h in path))
    return EXIT_OK


def _endpoint(index: MapIndex, target: str) -> tuple[str | None, int]:
    found = docs_mod.match_doc(index, target.removeprefix("./"))
    if len(found) == 1:
        return found[0], EXIT_OK
    print(f"dekko: '{target}' is ambiguous; candidates:", file=sys.stderr)
    for p in found:
        print(f"  {p}", file=sys.stderr)
    return None, EXIT_AMBIGUOUS


def _shortest(
    graph: DocGraph, start: str, goal: str, max_paths: int
) -> list[list[str]]:
    """Up to ``max_paths`` shortest paths, never through a nav doc."""
    depth = {start: 0}
    preds: dict[str, list[str]] = {}
    frontier = deque([start])
    while frontier:
        node = frontier.popleft()
        if node == goal:
            break
        if node in graph.nav and node != start:
            continue
        for nxt in sorted(graph.targets_of(node)):
            if nxt not in depth:
                depth[nxt] = depth[node] + 1
                frontier.append(nxt)
            if depth[nxt] == depth[node] + 1:
                preds.setdefault(nxt, []).append(node)
    if goal not in depth or start == goal:
        return []
    return _unwind(preds, start, goal, max_paths)


def _unwind(
    preds: dict[str, list[str]], start: str, goal: str, max_paths: int
) -> list[list[str]]:
    paths: list[list[str]] = []
    stack = [[goal]]
    while stack and len(paths) < max_paths:
        path = stack.pop()
        if path[-1] == start:
            paths.append(path[::-1])
            continue
        stack.extend([*path, p] for p in reversed(preds.get(path[-1], [])))
    return paths


def _hops(index: MapIndex, path: list[str]) -> list[Hop]:
    hops = []
    arrived: str | None = None
    for here, nxt in zip(path, [*path[1:], None]):
        edge = _first_edge(index, here, nxt) if nxt else None
        hops.append(Hop(here, arrived, edge.line if edge else None))
        arrived = _anchor(edge) if edge else None
    return hops


def _first_edge(index: MapIndex, source: str, target: str) -> DocEdge:
    edges = [
        index.doc_edges[i]
        for i in index.doc_edges_out.get(source, [])
        if index.doc_edges[i].target == target
        and index.doc_edges[i].tier == EXTRACTED
    ]
    return min(edges, key=lambda e: e.line)


def _anchor(edge: DocEdge) -> str | None:
    if edge.target_section is None:
        return None
    return edge.text.rpartition("#")[2] or None


def _hop_text(hop: Hop) -> str:
    text = hop.doc + (f"#{hop.anchor}" if hop.anchor else "")
    return f"{text}:{hop.line}" if hop.line is not None else text
