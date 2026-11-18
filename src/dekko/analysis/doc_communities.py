"""Doc communities: docs grouped by the links between them.

The same clustering as code communities (Louvain, split into connected
pieces, groups under three folded), run on read over the doc link
graph instead of stored at map time: doc graphs are small and docs
change more often than code. Tables of contents link to everything, so
they take no part.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict

from dekko.analysis import communities as comm
from dekko.analysis import docs as docs_mod
from dekko.analysis.doc_graph import DocGraph
from dekko.render.mapfile import MapIndex
from dekko.textutil import fit_to_budget

EXIT_OK = 0
EXIT_NOT_FOUND = 3
EXIT_AMBIGUOUS = 4


def describe(index: MapIndex) -> comm.Report:
    """Cluster the docs and build the listing, as for code files."""
    graph = DocGraph.from_index(index)
    nodes = [d for d in graph.docs if d not in graph.nav]
    edges = graph.cluster_edges
    stored = comm._ordered(
        comm.split_components(comm.louvain(nodes, edges), edges), edges
    )
    groups = [g for g in stored if len(g) >= comm._MIN_FILES]
    labels = comm.label_groups(
        groups, [_by_links(graph, g) for g in groups], nodes
    )
    listed = [
        comm.Community(
            id=i,
            label=label,
            files=sorted(group),
            symbols=0,
            top_files=_by_links(graph, group)[: comm._TOP_FILES],
            key_symbols=[],
            cohesion=comm.cohesion(group, edges),
            spans=[],
        )
        for i, (group, label) in enumerate(zip(groups, labels), start=1)
    ]
    linked = {node for pair in edges for node in pair}
    small = [g for g in stored if len(g) < comm._MIN_FILES and linked & set(g)]
    return comm.Report(
        communities=listed,
        small_groups=len(small),
        small_files=sum(len(g) for g in small),
        unconnected=len([n for n in nodes if n not in linked]),
        total_files=len(nodes),
        bridges=[],
        splits=[],
        community_of={p: c.id for c in listed for p in c.files},
        group_of={p: i for i, g in enumerate(stored) for p in g},
        nodes=set(nodes),
        edges=edges,
    )


def run(
    index: MapIndex,
    target: str | None,
    *,
    limit: int | None,
    budget: int | None,
    as_json: bool,
) -> int:
    """Print the doc communities, or one doc's place among them.

    Args:
        index: Loaded map index.
        target: A doc path (or a unique suffix of one), or ``None``.
        limit: List at most this many communities.
        budget: Approximate token cap for the text listing.
        as_json: Emit JSON (never budgeted).

    Returns:
        ``0``; ``3`` when ``target`` names no doc, ``4`` when it names
        several.
    """
    report = describe(index)
    if target is not None:
        return _run_target(index, report, target, as_json)
    if as_json:
        doc = {
            "view": "docs",
            "docs": report.total_files,
            "unconnected": report.unconnected,
            "small_groups": report.small_groups,
            "communities": [_community_json(c) for c in report.communities],
        }
        print(json.dumps(doc, indent=2))
        return EXIT_OK
    header, blocks = _listing(report, limit)
    kept, meter = fit_to_budget(blocks, budget, None, prefix=header)
    print("\n".join([header, *kept]))
    if budget:
        print(meter.footer())
    return EXIT_OK


def _by_links(graph: DocGraph, group: list[str]) -> list[str]:
    """Members, the most linked-to first."""
    return sorted(group, key=lambda p: (-len(graph.sources_of(p)), p))


def _listing(report: comm.Report, limit: int | None) -> tuple[str, list[str]]:
    n = len(report.communities)
    header = (
        f"dekko: {n} doc {'community' if n == 1 else 'communities'} over "
        f"{report.total_files} docs ({report.unconnected} unconnected, "
        f"{report.small_groups} small groups)"
    )
    shown = report.communities if limit is None else report.communities[:limit]
    return header, [_block(c) for c in shown]


def _block(c: comm.Community) -> str:
    return "\n".join(
        [
            f"#{c.id} {c.label}  {len(c.files)} docs, "
            f"cohesion {c.cohesion:.2f}",
            f"   top: {', '.join(c.top_files)}",
        ]
    )


def _community_json(c: comm.Community) -> dict:
    doc = asdict(c)
    for unused in ("symbols", "key_symbols", "spans"):
        del doc[unused]
    return doc


def _run_target(
    index: MapIndex, report: comm.Report, target: str, as_json: bool
) -> int:
    found = docs_mod.match_doc(index, target)
    if len(found) != 1:
        if not found:
            print(f"dekko: no indexed doc '{target}'", file=sys.stderr)
            return EXIT_NOT_FOUND
        print(f"dekko: '{target}' is ambiguous; candidates:", file=sys.stderr)
        for p in found:
            print(f"  {p}", file=sys.stderr)
        return EXIT_AMBIGUOUS
    path = found[0]
    cid = report.community_of.get(path)
    links = comm._links_of(report, path)
    if as_json:
        doc = {
            "view": "docs",
            "doc": path,
            "community": cid,
            "links": [{"community": c, "weight": w} for c, w in links],
        }
        print(json.dumps(doc, indent=2))
        return EXIT_OK
    print(_target_text(report, path, cid, links))
    return EXIT_OK


def _target_text(
    report: comm.Report,
    path: str,
    cid: int | None,
    links: list[tuple[int | None, int]],
) -> str:
    by_id = {c.id: c for c in report.communities}
    if path not in report.nodes:
        return f"{path} is a table of contents, left out of doc communities"
    if cid is None:
        why = "small group" if links else "unconnected"
        lines = [f"{path} is in no listed community ({why})"]
    else:
        lines = [f"{path} is in:", _block(by_id[cid])]
    if links:
        lines.append("links to other communities:")
        for other, weight in links:
            name = f"#{other} {by_id[other].label}" if other else "unlisted"
            lines.append(f"  {name}  {weight}")
    return "\n".join(lines)
