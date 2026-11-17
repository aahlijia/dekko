"""Module communities: files grouped by how much they use each other.

Louvain modularity clustering over a weighted, undirected file graph.
Everything is deterministic: nodes and candidate communities are
visited in sorted order, weights are integers, and gains are compared
exactly, so the same map always gives the same communities.
"""

import json
import posixpath
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field

from dekko.render.mapfile import MapIndex, path_of
from dekko.textutil import fit_to_budget, strip_dot_slash

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


# --- what the listing derives on read ------------------------------------

_MIN_FILES = 3
_TOP_FILES = 3
_KEY_SYMBOLS = 3
_MAX_BRIDGES = 5
_MAX_SPLITS = 5


@dataclass
class Community:
    """One community as the listing shows it.

    Attributes:
        id: 1-based position among the listed communities.
        label: Directory-based name (see ``label_groups``).
        files: Member paths, sorted.
        symbols: How many symbols the files define.
        top_files: The files with the most weight inside the community.
        key_symbols: The most-called symbols defined in it (fan-in).
        cohesion: Share of its edge weight that stays inside it.
        spans: Top-level directories, when it spans 3 or more.
    """

    id: int
    label: str
    files: list[str]
    symbols: int
    top_files: list[str]
    key_symbols: list[str]
    cohesion: float
    spans: list[str]


@dataclass
class Bridge:
    """Cross weight between two listed communities and the files that
    carry most of it on each side."""

    first: int
    second: int
    weight: int
    first_files: list[str]
    second_files: list[str]


@dataclass
class Report:
    """Everything ``dekko communities`` lists for one view.

    Attributes:
        communities: Communities of 3 or more files, largest first.
        small_groups: How many smaller groups were folded away.
        small_files: Files in those groups.
        unconnected: Files with no edge to another file.
        total_files: Files in the view.
        bridges: The heaviest community pairs.
        splits: ``(directory, communities)`` for directories cut 3+ ways.
        community_of: File → listed community id.
        edges: The file graph the report was built from.
    """

    communities: list[Community]
    small_groups: int
    small_files: int
    unconnected: int
    total_files: int
    bridges: list[Bridge]
    splits: list[tuple[str, int]]
    community_of: dict[str, int]
    edges: Edges = field(repr=False)


def describe(index: MapIndex, *, tests: bool) -> Report:
    """Build the listing for the stored communities of one view.

    Args:
        index: Loaded map index with communities.
        tests: Read the with-tests partition instead of the default.
    """
    nodes, edges = file_graph(index, tests=tests)
    view = index if tests else index.without_tests()
    stored = index.communities_with_tests if tests else index.communities
    linked = {node for pair in edges for node in pair}
    listed = [g for g in stored if len(g) >= _MIN_FILES]
    small = [g for g in stored if len(g) < _MIN_FILES and linked & set(g)]
    inside = [_inside_weight(g, edges) for g in listed]
    hubs = [_ranked(w)[0] if w else g[0] for g, w in zip(listed, inside)]
    labels = label_groups(listed, hubs, nodes)
    skip = _shared_depth(nodes)
    communities = [
        Community(
            id=i,
            label=label,
            files=group,
            symbols=sum(len(view.symbols_by_path.get(p, [])) for p in group),
            top_files=_ranked(weights)[:_TOP_FILES],
            key_symbols=_key_symbols(view, group),
            cohesion=cohesion(group, edges),
            spans=spans(group, skip),
        )
        for i, (group, label, weights) in enumerate(
            zip(listed, labels, inside), start=1
        )
    ]
    return Report(
        communities=communities,
        small_groups=len(small),
        small_files=sum(len(g) for g in small),
        unconnected=len(nodes) - len(linked & set(nodes)),
        total_files=len(nodes),
        bridges=bridges(listed, edges),
        splits=splits(listed),
        community_of={p: c.id for c in communities for p in c.files},
        edges=edges,
    )


def label_groups(
    groups: list[list[str]], hubs: list[str], paths: list[str]
) -> list[str]:
    """A directory-based name for each group.

    The directory prefix every path shares is dropped. From the top,
    a directory holding at least half the group's files is taken and
    the walk goes deeper; where none does, the top two together name
    the group if they hold half (``a + b``). Otherwise the files
    scatter there, so the hub file is added (``src (cli.py)``), or
    names the group alone when they scatter from the top. Groups that
    still share a name get their hub file added too.
    """
    skip = _shared_depth(paths)
    labels = [
        _label(g, posixpath.basename(h), skip) for g, h in zip(groups, hubs)
    ]
    seen = Counter(labels)
    out = []
    for label, hub in zip(labels, hubs):
        suffix = f" ({posixpath.basename(hub)})"
        clash = seen[label] > 1 and not label.endswith(suffix)
        out.append(label + suffix if clash else label)
    return out


def _label(group: list[str], hub: str, skip: int) -> str:
    best = None
    depth = 0
    while True:
        depth += 1
        ranked = _dirs_at(group, skip, depth)
        if not ranked:
            return best or hub
        (top, count), rest = ranked[0], ranked[1:]
        if count * 2 >= len(group):
            best = _short(top)
            continue
        if rest and (count + rest[0][1]) * 2 >= len(group):
            return f"{_short(top)} + {_short(rest[0][0])}"
        return f"{best} ({hub})" if best else hub


def _dirs_at(group: list[str], skip: int, depth: int) -> list[tuple[str, int]]:
    """Directories ``depth`` levels below the shared prefix, by how many
    of the group's files sit under each (most first, then by name)."""
    counts: Counter[str] = Counter()
    for path in group:
        parts = path.split("/")[skip:]
        if len(parts) > depth:
            counts["/".join(parts[:depth])] += 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def _short(directory: str) -> str:
    """Keep the first two segments and the last of a long directory."""
    parts = directory.split("/")
    if len(parts) <= 3:
        return directory
    return f"{parts[0]}/{parts[1]}/…/{parts[-1]}"


def _shared_depth(paths: list[str]) -> int:
    """How many leading directories every path has in common."""
    dirs = [p.split("/")[:-1] for p in paths]
    depth = 0
    for segment in zip(*dirs):
        if len(set(segment)) != 1:
            break
        depth += 1
    return depth


def cohesion(group: list[str], edges: Edges) -> float:
    """Inside weight over inside plus boundary weight, two decimals."""
    members = set(group)
    inside = boundary = 0
    for (a, b), weight in edges.items():
        hits = (a in members) + (b in members)
        if hits == 2:
            inside += weight
        elif hits == 1:
            boundary += weight
    if inside + boundary == 0:
        return 1.0
    return round(inside / (inside + boundary), 2)


def bridges(groups: list[list[str]], edges: Edges) -> list[Bridge]:
    """The heaviest pairs of groups, with each side's top two files."""
    of = {p: i for i, group in enumerate(groups, start=1) for p in group}
    pairs: dict[tuple[int, int], Counter[str]] = defaultdict(Counter)
    for (a, b), weight in edges.items():
        ca, cb = of.get(a), of.get(b)
        if ca is None or cb is None or ca == cb:
            continue
        key = (ca, cb) if ca < cb else (cb, ca)
        pairs[key][a] += weight
        pairs[key][b] += weight
    out = []
    for (first, second), by_file in pairs.items():
        weight = sum(by_file.values()) // 2
        out.append(
            Bridge(
                first=first,
                second=second,
                weight=weight,
                first_files=_side(by_file, set(groups[first - 1])),
                second_files=_side(by_file, set(groups[second - 1])),
            )
        )
    out.sort(key=lambda b: (-b.weight, b.first, b.second))
    return out[:_MAX_BRIDGES]


def _side(by_file: Counter[str], members: set[str]) -> list[str]:
    return _ranked(
        Counter({p: w for p, w in by_file.items() if p in members})
    )[:2]


def spans(group: list[str], skip: int) -> list[str]:
    """The group's top-level directories (below the shared prefix),
    most files first, when there are 3 or more of them."""
    counts = Counter((p.split("/")[skip:-1] or ["."])[0] for p in group)
    if len(counts) < _MIN_FILES:
        return []
    return [
        d for d, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


def splits(groups: list[list[str]]) -> list[tuple[str, int]]:
    """Directories with 3+ files spread over 3+ groups, most cut first."""
    files: Counter[str] = Counter()
    owners: dict[str, set[int]] = defaultdict(set)
    for i, group in enumerate(groups):
        for path in group:
            parent = posixpath.dirname(path) or "."
            files[parent] += 1
            owners[parent].add(i)
    cut = [
        (d, len(owners[d]))
        for d in files
        if files[d] >= _MIN_FILES and len(owners[d]) >= _MIN_FILES
    ]
    cut.sort(key=lambda dc: (-dc[1], dc[0]))
    return cut[:_MAX_SPLITS]


def _inside_weight(group: list[str], edges: Edges) -> Counter[str]:
    """Each member's weight to the other members."""
    members = set(group)
    weights: Counter[str] = Counter()
    for (a, b), weight in edges.items():
        if a in members and b in members:
            weights[a] += weight
            weights[b] += weight
    return weights


def _ranked(weights: Counter[str]) -> list[str]:
    return sorted(weights, key=lambda p: (-weights[p], p))


def _key_symbols(view: MapIndex, group: list[str]) -> list[str]:
    """The group's most-called symbols, as ``stats`` ranks fan-in."""
    ids = [s.id for path in group for s in view.symbols_by_path.get(path, [])]
    called = [i for i in ids if view.calls_in.get(i)]
    called.sort(key=lambda i: (-len(view.calls_in[i]), i))
    return called[:_KEY_SYMBOLS]


# --- dekko communities ---------------------------------------------------

EXIT_OK = 0
EXIT_NO_DATA = 2
EXIT_NOT_FOUND = 3


def run(
    index: MapIndex,
    target: str | None,
    *,
    tests: bool,
    limit: int | None,
    budget: int | None,
    as_json: bool,
) -> int:
    """Print the communities listing, or one file's place in it.

    Args:
        index: Loaded map index.
        target: A file path or symbol, or ``None`` for the listing.
        tests: Read the partition that includes test files.
        limit: List at most this many communities.
        budget: Approximate token cap for the text listing; trailing
            sections (mismatches, bridges, then communities) go first.
        as_json: Emit JSON (never budgeted).

    Returns:
        ``0``; ``2`` when the map predates communities; ``3`` when
        ``target`` names nothing in the map.
    """
    if not index.has_communities:
        print(
            "dekko: map predates communities; run dekko map",
            file=sys.stderr,
        )
        return EXIT_NO_DATA
    report = describe(index, tests=tests)
    view = "with_tests" if tests else "default"
    if target is not None:
        return _run_target(index, report, target, view, as_json)
    if as_json:
        print(json.dumps(_report_json(report, view), indent=2))
        return EXIT_OK
    if not report.edges:
        n = report.total_files
        noun = "file" if n == 1 else "files"
        print(f"dekko: no communities ({n} {noun}, no cross-file edges)")
        return EXIT_OK
    header, sections = _listing(report, limit)
    kept, meter = fit_to_budget(sections, budget, None, prefix=header)
    print("\n".join([header, *kept]))
    if budget:
        print(meter.footer())
    return EXIT_OK


def _listing(report: Report, limit: int | None) -> tuple[str, list[str]]:
    """The header, then one section per community, then bridges and
    directory mismatches, so a budget sheds those first."""
    header = (
        f"dekko: {len(report.communities)} communities over "
        f"{report.total_files} files ({report.unconnected} unconnected, "
        f"{report.small_groups} small groups)"
    )
    shown = report.communities[:limit] if limit else report.communities
    sections = [_block(c) for c in shown]
    if report.bridges:
        sections.append(
            "\n".join(["bridges:", *(_bridge_line(b) for b in report.bridges)])
        )
    if report.splits:
        sections.append(
            "\n".join(
                [
                    "directory mismatches:",
                    *(
                        f"  split: {d} ({n} communities)"
                        for d, n in report.splits
                    ),
                ]
            )
        )
    return header, sections


def _block(c: Community) -> str:
    lines = [
        f"#{c.id} {c.label}  {len(c.files)} files, {c.symbols} symbols, "
        f"cohesion {c.cohesion:.2f}",
        f"   top: {', '.join(c.top_files)}",
    ]
    if c.key_symbols:
        lines.append(f"   key: {', '.join(c.key_symbols)}")
    if c.spans:
        lines.append(f"   spans: {', '.join(c.spans)}")
    return "\n".join(lines)


def _bridge_line(b: Bridge) -> str:
    return (
        f"  #{b.first} <-> #{b.second}  {b.weight}  "
        f"{', '.join(b.first_files)} | {', '.join(b.second_files)}"
    )


def _report_json(report: Report, view: str) -> dict:
    return {
        "view": view,
        "files": report.total_files,
        "unconnected": report.unconnected,
        "small_groups": report.small_groups,
        "small_files": report.small_files,
        "communities": [asdict(c) for c in report.communities],
        "bridges": [asdict(b) for b in report.bridges],
        "splits": [
            {"directory": d, "communities": n} for d, n in report.splits
        ],
    }


def _run_target(
    index: MapIndex, report: Report, target: str, view: str, as_json: bool
) -> int:
    path = _target_file(index, target)
    if path is None:
        print(f"dekko: {target!r} not found in the map", file=sys.stderr)
        return EXIT_NOT_FOUND
    cid = report.community_of.get(path)
    links = _links_of(report, path)
    sits_on = [
        b for b in report.bridges if path in (*b.first_files, *b.second_files)
    ]
    if as_json:
        doc = {
            "view": view,
            "file": path,
            "community": cid,
            "links": [{"community": c, "weight": w} for c, w in links],
            "bridges": [asdict(b) for b in sits_on],
        }
        print(json.dumps(doc, indent=2))
        return EXIT_OK
    print(_target_text(report, path, cid, links, sits_on))
    return EXIT_OK


def _target_text(
    report: Report,
    path: str,
    cid: int | None,
    links: list[tuple[int | None, int]],
    sits_on: list[Bridge],
) -> str:
    by_id = {c.id: c for c in report.communities}
    if cid is None:
        why = "small group" if links else "unconnected"
        lines = [f"{path} is in no listed community ({why})"]
    else:
        c = by_id[cid]
        lines = [
            f"{path} is in #{cid} {c.label} ({len(c.files)} files, "
            f"cohesion {c.cohesion:.2f})"
        ]
    if links:
        lines.append("links to other communities:")
        for other, weight in links:
            name = f"#{other} {by_id[other].label}" if other else "unlisted"
            lines.append(f"  {name}  {weight}")
    if sits_on:
        lines.append("bridges it sits on:")
        lines += [_bridge_line(b) for b in sits_on]
    return "\n".join(lines)


def _links_of(report: Report, path: str) -> list[tuple[int | None, int]]:
    """``path``'s weight to each other community, heaviest first;
    ``None`` collects files in no listed community."""
    own = report.community_of.get(path)
    totals: Counter[int | None] = Counter()
    for (a, b), weight in report.edges.items():
        if path not in (a, b):
            continue
        other = report.community_of.get(b if a == path else a)
        if other != own or other is None:
            totals[other] += weight
    return sorted(totals.items(), key=lambda cw: (-cw[1], cw[0] or 0))


def _target_file(index: MapIndex, target: str) -> str | None:
    """The file a path or symbol target names, or ``None``."""
    # Imported here: query imports this module for ``cohesion``.
    from dekko.analysis import query

    path = strip_dot_slash(target)
    if path in index.languages_by_path:
        return path
    match, candidates = query.resolve_target(index, target)
    files = {s.path for s in ([match] if match else candidates)}
    return files.pop() if len(files) == 1 else None
