"""Louvain clustering: deterministic, connected communities."""

import random

from dekko.analysis.communities import louvain, split_components


def _clique(names: list[str], weight: int = 3) -> dict[tuple[str, str], int]:
    return {
        (a, b): weight for i, a in enumerate(names) for b in names[i + 1 :]
    }


def _two_cliques() -> tuple[list[str], dict[tuple[str, str], int]]:
    left = ["a1", "a2", "a3", "a4"]
    right = ["b1", "b2", "b3", "b4"]
    edges = {**_clique(left), **_clique(right), ("a4", "b1"): 1}
    return left + right, edges


def test_two_cliques_joined_by_one_edge_are_two_communities() -> None:
    nodes, edges = _two_cliques()

    groups = louvain(nodes, edges)

    assert groups == [["a1", "a2", "a3", "a4"], ["b1", "b2", "b3", "b4"]]


def test_input_order_does_not_change_the_result() -> None:
    nodes, edges = _two_cliques()
    expected = louvain(nodes, edges)
    rng = random.Random(7)

    for _ in range(5):
        shuffled_nodes = nodes[:]
        rng.shuffle(shuffled_nodes)
        items = list(edges.items())
        rng.shuffle(items)
        assert louvain(shuffled_nodes, dict(items)) == expected


def test_empty_graph_has_no_communities() -> None:
    assert louvain([], {}) == []


def test_isolated_nodes_stay_alone() -> None:
    nodes = ["solo", "x", "y"]

    groups = louvain(nodes, {("x", "y"): 2})

    assert groups == [["x", "y"], ["solo"]]


def test_a_disconnected_group_is_split_into_its_parts() -> None:
    edges = {("a", "b"): 1, ("c", "d"): 1}

    groups = split_components([["a", "b", "c", "d"]], edges)

    assert groups == [["a", "b"], ["c", "d"]]


def test_larger_and_heavier_groups_come_first() -> None:
    edges = {
        **_clique(["p", "q"], weight=1),
        **_clique(["r", "s"], weight=5),
        **_clique(["t", "u", "v"], weight=1),
    }

    groups = louvain(["p", "q", "r", "s", "t", "u", "v"], edges)

    assert groups == [["t", "u", "v"], ["r", "s"], ["p", "q"]]
