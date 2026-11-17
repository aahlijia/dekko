"""Module communities: files grouped by how much they use each other.

Louvain modularity clustering over a weighted, undirected file graph.
Everything is deterministic: nodes and candidate communities are
visited in sorted order, weights are integers, and gains are compared
exactly, so the same map always gives the same communities.
"""

from collections import defaultdict

from dekko.render.mapfile import MapIndex, path_of

Edges = dict[tuple[str, str], int]
_Weights = dict[str, dict[str, int]]


def cluster(index: MapIndex) -> dict[str, list[list[str]]]:
    """Communities for the test-free (``default``) and full
    (``with_tests``) views, as ``map.json`` stores them."""
    return {
        "default": louvain(*file_graph(index, tests=False)),
        "with_tests": louvain(*file_graph(index, tests=True)),
    }


def file_graph(index: MapIndex, *, tests: bool) -> tuple[list[str], Edges]:
    """Every mapped file and the weighted edges between files.

    A pair's weight is its call sites plus reference sites plus one per
    import and one per heritage clause, in either direction.

    Args:
        index: Loaded map index.
        tests: Keep test files; otherwise read ``without_tests()``.

    Returns:
        ``(nodes, edges)``: sorted file paths, and weights keyed
        ``(a, b)`` with ``a < b``.
    """
    view = index if tests else index.without_tests()
    edges: Edges = defaultdict(int)
    for out, lines in (
        (view.calls_out, view.edge_lines),
        (view.referenced_out, view.ref_lines),
    ):
        for src, targets in out.items():
            for dst in targets:
                sites = len(lines.get((src, dst), ())) or 1
                _add(edges, path_of(view, src), path_of(view, dst), sites)
    for importer, imported in view.module_deps_out.items():
        for path in imported:
            _add(edges, importer, path, 1)
    for sub, supers in view.heritage_out.items():
        for sup in supers:
            _add(edges, path_of(view, sub), path_of(view, sup), 1)
    return sorted(view.languages_by_path), dict(edges)


def _add(edges: Edges, a: str | None, b: str | None, weight: int) -> None:
    if a and b and a != b:
        edges[(a, b) if a < b else (b, a)] += weight


def louvain(nodes: list[str], edges: Edges) -> list[list[str]]:
    """Group ``nodes`` into communities that maximize modularity.

    Args:
        nodes: Every node, including ones with no edges.
        edges: Undirected weights keyed ``(a, b)``; self-pairs ignored.

    Returns:
        Communities, each a sorted list of nodes and each connected,
        largest first (ties: heavier inside, then first node).
    """
    weights = _symmetric(nodes, edges)
    groups = {node: [node] for node in sorted(weights)}
    while (moves := _local_moves(weights)) is not None:
        merged: dict[str, list[str]] = defaultdict(list)
        for label in sorted(groups):
            merged[moves[label]] += groups[label]
        groups = dict(merged)
        weights = _aggregate(weights, moves)
    return _ordered(split_components(list(groups.values()), edges), edges)


def split_components(groups: list[list[str]], edges: Edges) -> list[list[str]]:
    """Split each group into its connected parts, using only the edges
    inside it. Louvain can leave a group held together by nothing."""
    links: _Weights = defaultdict(dict)
    for (a, b), weight in edges.items():
        if a != b and weight:
            links[a][b] = links[b][a] = weight
    out: list[list[str]] = []
    for group in groups:
        members = set(group)
        unseen = sorted(members)
        seen: set[str] = set()
        for start in unseen:
            if start not in seen:
                part = _reach(start, links, members)
                seen |= part
                out.append(sorted(part))
    return out


def _reach(start: str, links: _Weights, allowed: set[str]) -> set[str]:
    found = {start}
    stack = [start]
    while stack:
        for nb in links.get(stack.pop(), {}):
            if nb in allowed and nb not in found:
                found.add(nb)
                stack.append(nb)
    return found


def _symmetric(nodes: list[str], edges: Edges) -> _Weights:
    weights: _Weights = {node: {} for node in nodes}
    for (a, b), weight in edges.items():
        if a == b or not weight:
            continue
        weights.setdefault(a, {})
        weights.setdefault(b, {})
        weights[a][b] = weights[a].get(b, 0) + weight
        weights[b][a] = weights[b].get(a, 0) + weight
    return weights


def _local_moves(weights: _Weights) -> dict[str, str] | None:
    """One Louvain pass: node → community label, or ``None`` if no node
    moved (the partition can't improve further).

    A node's self entry holds twice the weight already inside it (an
    aggregated community), so degrees and totals stay consistent.
    """
    degree = {n: sum(nbs.values()) for n, nbs in weights.items()}
    total = sum(degree.values())
    if total == 0:
        return None
    comm = {n: n for n in weights}
    comm_degree = dict(degree)
    moved = False
    improved = True
    while improved:
        improved = False
        for node in sorted(weights):
            best = _best_community(
                node, weights, comm, comm_degree, degree, total
            )
            if best != comm[node]:
                comm[node] = best
                moved = improved = True
    return comm if moved else None


def _best_community(
    node: str,
    weights: _Weights,
    comm: dict[str, str],
    comm_degree: dict[str, int],
    degree: dict[str, int],
    total: int,
) -> str:
    """The neighboring community with the largest modularity gain.

    Gains are scaled by ``total`` to stay integers. A move needs a
    strictly larger gain than staying, so ties keep the node put.
    """
    links: dict[str, int] = defaultdict(int)
    for nb, weight in weights[node].items():
        if nb != node:
            links[comm[nb]] += weight
    own = comm[node]
    comm_degree[own] -= degree[node]

    def gain(label: str) -> int:
        return links.get(label, 0) * total - comm_degree[label] * degree[node]

    best, best_gain = own, gain(own)
    for label in sorted(links):
        if (g := gain(label)) > best_gain:
            best, best_gain = label, g
    comm_degree[best] += degree[node]
    return best


def _aggregate(weights: _Weights, comm: dict[str, str]) -> _Weights:
    """Collapse each community into one node, keeping every weight."""
    out: _Weights = {label: {} for label in sorted(set(comm.values()))}
    for a, nbs in weights.items():
        row = out[comm[a]]
        for b, weight in nbs.items():
            row[comm[b]] = row.get(comm[b], 0) + weight
    return out


def _ordered(groups: list[list[str]], edges: Edges) -> list[list[str]]:
    """Largest first, then heavier inside, then by first member."""
    of = {node: i for i, group in enumerate(groups) for node in group}
    inside = [0] * len(groups)
    for (a, b), weight in edges.items():
        if a != b and of.get(a) is not None and of.get(a) == of.get(b):
            inside[of[a]] += weight
    order = sorted(
        range(len(groups)),
        key=lambda i: (-len(groups[i]), -inside[i], groups[i][0]),
    )
    return [groups[i] for i in order]
