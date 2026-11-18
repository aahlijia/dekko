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

from dekko.classify import is_test_path
from dekko.core.model import Symbol
from dekko.render.mapfile import MapIndex, path_of, prod_id
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
    import and one per heritage clause, in either direction. Test code
    is skipped edge by edge, the way ``without_tests()`` drops it,
    without building that whole view.

    Args:
        index: Loaded map index.
        tests: Keep test files and test symbols.

    Returns:
        ``(nodes, edges)``: sorted file paths, and weights keyed
        ``(a, b)`` with ``a < b`` between two of those files.
    """
    by_id = index.symbols_by_id

    def keep(node_id: str) -> bool:
        return tests or prod_id(node_id, by_id)

    nodes = sorted(
        p for p in index.languages_by_path if tests or not is_test_path(p)
    )
    known = set(nodes)
    edges: Edges = defaultdict(int)
    for out, lines in (
        (index.calls_out, index.edge_lines),
        (index.referenced_out, index.ref_lines),
        (index.heritage_out, {}),
    ):
        for src, targets in out.items():
            if not keep(src):
                continue
            for dst in targets:
                if keep(dst):
                    sites = len(lines.get((src, dst), ())) or 1
                    _add(
                        edges,
                        known,
                        path_of(index, src),
                        path_of(index, dst),
                        sites,
                    )
    for importer, imported in index.module_deps_out.items():
        for path in imported:
            _add(edges, known, importer, path, 1)
    return nodes, dict(edges)


def _add(
    edges: Edges, known: set[str], a: str | None, b: str | None, weight: int
) -> None:
    # Only files the view maps: an edge to anything else would put an
    # unmapped path into a community that map.json can't store.
    if a in known and b in known and a != b:
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
_KEYS_PER_FILE = 2
_MAX_BRIDGES = 5
_MAX_SPLITS = 5
_MAX_SPANS = 5


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
        spans: Top-level directories (up to 5), when it spans 3 or more.
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
        group_of: File → its stored group's position, listed or not.
        nodes: Files in the view.
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
    group_of: dict[str, int] = field(repr=False)
    nodes: set[str] = field(repr=False)
    edges: Edges = field(repr=False)


@dataclass
class _Listed:
    """The groups a listing shows and what naming them takes."""

    nodes: list[str]
    edges: Edges
    stored: list[list[str]]
    groups: list[list[str]]
    inside: list[Counter[str]]
    labels: list[str]


def _listed(index: MapIndex, tests: bool) -> _Listed:
    nodes, edges = file_graph(index, tests=tests)
    stored = index.communities_with_tests if tests else index.communities
    groups = [g for g in stored if len(g) >= _MIN_FILES]
    inside = [_inside_weight(g, edges) for g in groups]
    ranked = [_ranked(w) or g for g, w in zip(groups, inside)]
    labels = label_groups(groups, ranked, nodes)
    return _Listed(nodes, edges, stored, groups, inside, labels)


def largest(index: MapIndex, n: int) -> list[tuple[int, str, int, float]]:
    """The ``n`` largest test-free communities as ``(id, label, files,
    cohesion)``, without the symbol work a full listing does."""
    if not index.has_communities:
        return []
    base = _listed(index, tests=False)
    return [
        (i, label, len(group), cohesion(group, base.edges))
        for i, (group, label) in enumerate(
            zip(base.groups[:n], base.labels), start=1
        )
    ]


def file_titles(index: MapIndex) -> dict[str, tuple[int, str]]:
    """File → ``(id, "community N: label")`` for the listed test-free
    communities; empty for a map without them."""
    if not index.has_communities:
        return {}
    base = _listed(index, tests=False)
    return {
        path: (i, f"community {i}: {label}")
        for i, (group, label) in enumerate(
            zip(base.groups, base.labels), start=1
        )
        for path in group
    }


def describe(index: MapIndex, *, tests: bool) -> Report:
    """Build the listing for the stored communities of one view.

    Args:
        index: Loaded map index with communities.
        tests: Read the with-tests partition instead of the default.
    """
    base = _listed(index, tests)
    view = index if tests else index.without_tests()
    linked = {node for pair in base.edges for node in pair}
    small = [g for g in base.stored if len(g) < _MIN_FILES and linked & set(g)]
    skip = _shared_depth(base.nodes)
    communities = [
        Community(
            id=i,
            label=label,
            files=group,
            symbols=sum(len(view.symbols_by_path.get(p, [])) for p in group),
            top_files=_ranked(weights)[:_TOP_FILES],
            key_symbols=_key_symbols(view, group),
            cohesion=cohesion(group, base.edges),
            spans=spans(group, skip),
        )
        for i, (group, label, weights) in enumerate(
            zip(base.groups, base.labels, base.inside), start=1
        )
    ]
    return Report(
        communities=communities,
        small_groups=len(small),
        small_files=sum(len(g) for g in small),
        unconnected=len(base.nodes) - len(linked & set(base.nodes)),
        total_files=len(base.nodes),
        bridges=bridges(base.groups, base.edges),
        splits=splits(base.groups),
        community_of={p: c.id for c in communities for p in c.files},
        group_of={p: i for i, g in enumerate(base.stored) for p in g},
        nodes=set(base.nodes),
        edges=base.edges,
    )


def label_groups(
    groups: list[list[str]], ranked: list[list[str]], paths: list[str]
) -> list[str]:
    """A directory-based name for each group.

    Args:
        groups: The groups to name.
        ranked: Each group's files, most weight inside it first.
        paths: Every file in the view, to find the prefix they share.

    The directory prefix every path shares is dropped. From the top,
    a directory holding at least half the group's files is taken and
    the walk goes deeper inside it, until files sitting in it directly
    make up half the group. Where no directory holds half, two directories
    that hold half together name the group ``a + b``, if the second
    is at least half the first and nothing else ties it; one directory
    with twice the files of any other and a third of the group names
    it alone. Otherwise the files scatter there, so the hub (the
    heaviest file in the directory taken) is added (``src (cli.py)``),
    or names the group alone when they scatter from the top. When two
    or more directories are named, the leading folders they all share
    are dropped too. Groups that still share a name, or name a folder
    another label names too, get their hub added, as its path when the
    name already holds it.
    """
    skip = _shared_depth(paths)
    names = [_name(g, skip) for g in groups]
    prefix = _named_prefix(
        [d for dirs, scattered in names if not scattered for d in dirs]
    )
    labels, hubs = [], []
    for (dirs, scattered), files in zip(names, ranked):
        hub = _hub(files, skip, dirs[0] if len(dirs) == 1 else None)
        hubs.append(hub)
        labels.append(_text(dirs, scattered, hub, prefix))
    seen = Counter(labels)
    named = Counter(d for dirs, _ in names for d in set(dirs))
    return [
        _distinct(label, hub, skip)
        if seen[label] > 1
        or (not scattered and any(named[d] > 1 for d in dirs))
        else label
        for label, hub, (dirs, scattered) in zip(labels, hubs, names)
    ]


def _hub(files: list[str], skip: int, directory: str | None) -> str:
    """The heaviest of ``files`` inside ``directory``, if one is."""
    if directory is not None:
        for path in files:
            if _under(path, skip, directory):
                return path
    return files[0]


def _named_prefix(dirs: list[str]) -> int:
    """Leading folders every named directory shares, each keeping its
    last; 0 with fewer than two to compare."""
    # _shared_depth reads the last segment as a file name, which is
    # what keeps each directory's last folder.
    return _shared_depth(dirs) if len(dirs) >= 2 else 0


def _text(dirs: list[str], scattered: bool, hub: str, prefix: int) -> str:
    shown = [_short("/".join(d.split("/")[prefix:])) for d in dirs]
    name = posixpath.basename(hub)
    if not scattered:
        return " + ".join(shown)
    if not dirs or len(dirs[0].split("/")) <= prefix:
        return name
    return f"{shown[0]} ({name})"


def _distinct(label: str, hub: str, skip: int) -> str:
    """``label`` told apart by its hub: the file name, or its path when
    the file name is already in the label."""
    name = posixpath.basename(hub)
    where = _short("/".join(hub.split("/")[skip:]))
    if label == name:
        return where
    if label.endswith(f" ({name})"):
        return f"{label[: -len(name) - 3]} ({where})"
    return f"{label} ({name})"


def _name(group: list[str], skip: int) -> tuple[list[str], bool]:
    """The directories that name ``group`` (below the shared prefix),
    and whether its files scatter below the last of them."""
    size = len(group)
    best: list[str] = []
    scope = group
    depth = 0
    while ranked := _dirs_at(scope, skip, depth := depth + 1):
        top, count = ranked[0]
        counts = [n for _, n in ranked[1:]]
        runner = counts[0] if counts else 0
        # Files sitting in the directory taken compete with its children.
        loose = len(scope) - sum(n for _, n in ranked)
        third = max(counts[1] if len(counts) > 1 else 0, loose)
        if best and loose * 2 >= size:
            return best, False
        if count * 2 >= size and runner * 2 < size:
            best = [top]
            scope = [p for p in scope if _under(p, skip, top)]
            continue
        pair = runner * 2 >= count and third < runner
        if pair and (count + runner) * 2 >= size:
            return [top, ranked[1][0]], False
        if count >= 2 * max(runner, loose) and count * 3 >= size:
            return [top], False
        return best, True
    return best, not best


def _under(path: str, skip: int, directory: str) -> bool:
    """Whether ``path`` sits in ``directory`` (below the shared prefix)."""
    return "/".join(path.split("/")[skip:]).startswith(directory + "/")


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
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [d for d, _ in ranked[:_MAX_SPANS]]


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
    """The symbols the most other files call or reference.

    Calls from a symbol's own file don't count, so a busy private
    helper ranks below what the rest of the group uses. Ties go to
    the most use sites, then the id.
    """
    users: dict[str, set[str]] = defaultdict(set)
    sites: Counter[str] = Counter()
    home: dict[str, str] = {}
    for path in group:
        for symbol in view.symbols_by_path.get(path, []):
            key = _owner(view, symbol)
            callers = [
                *view.calls_in.get(symbol.id, ()),
                *view.referenced_in.get(symbol.id, ()),
            ]
            sites[key] += len(callers)
            users[key] |= {path_of(view, c) for c in callers} - {path, None}
            home[key] = path
    ranked = sorted(
        (k for k in users if users[k]),
        key=lambda k: (-len(users[k]), -sites[k], k),
    )
    return _capped(ranked, home)


def _owner(view: MapIndex, symbol: Symbol) -> str:
    """A constructor (``T.T``) counts as its type; else the symbol."""
    head, _, last = symbol.qualname.rpartition(".")
    if symbol.kind == "method" and head.rpartition(".")[2] == last:
        owner = f"{symbol.path}::{head}"
        if owner in view.symbols_by_id:
            return owner
    return symbol.id


def _capped(ranked: list[str], home: dict[str, str]) -> list[str]:
    """The first ``_KEY_SYMBOLS`` of ``ranked``, two per file at most,
    so one hub file can't fill the line."""
    per_file: Counter[str] = Counter()
    out = []
    for key in ranked:
        if per_file[home[key]] < _KEYS_PER_FILE:
            out.append(key)
            per_file[home[key]] += 1
            if len(out) == _KEY_SYMBOLS:
                break
    return out


# --- dekko communities ---------------------------------------------------

EXIT_OK = 0
EXIT_NO_DATA = 2


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
        ``target`` names nothing in the map, ``4`` when it names
        symbols in several files (as ``query`` reports them).
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
    directory mismatches, so a budget sheds those first. ``limit`` 0
    keeps the header only."""
    n = len(report.communities)
    header = (
        f"dekko: {n} {'community' if n == 1 else 'communities'} over "
        f"{report.total_files} files ({report.unconnected} unconnected, "
        f"{report.small_groups} small groups)"
    )
    if limit == 0:
        return header, []
    shown = report.communities if limit is None else report.communities[:limit]
    last = len(shown)
    sections = [_block(c) for c in shown]
    pairs = [b for b in report.bridges if b.second <= last]
    if pairs:
        sections.append(
            "\n".join(["bridges:", *(_bridge_line(b) for b in pairs)])
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
    path, candidates = _target_file(index, target)
    if path is None:
        # Imported here: query imports this module for ``cohesion``.
        from dekko.analysis import query

        return query.report_unresolved(target, candidates, index)
    if path not in report.nodes:
        print(
            f"{path} is test code, left out of the default view; pass --tests"
        )
        return EXIT_OK
    cid = report.community_of.get(path)
    links = _links_of(report, path)
    if as_json:
        doc = {
            "view": view,
            "file": path,
            "community": cid,
            "links": [{"community": c, "weight": w} for c, w in links],
        }
        print(json.dumps(doc, indent=2))
        return EXIT_OK
    print(_target_text(report, path, cid, links))
    return EXIT_OK


def _target_text(
    report: Report,
    path: str,
    cid: int | None,
    links: list[tuple[int | None, int]],
) -> str:
    by_id = {c.id: c for c in report.communities}
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


def _links_of(report: Report, path: str) -> list[tuple[int | None, int]]:
    """``path``'s weight to each other community, heaviest first;
    ``None`` collects files in no listed community. Files in the same
    stored group (listed or small) don't count."""
    own = report.group_of.get(path)
    totals: Counter[int | None] = Counter()
    for (a, b), weight in report.edges.items():
        if path not in (a, b):
            continue
        other = b if a == path else a
        if report.group_of.get(other) != own:
            totals[report.community_of.get(other)] += weight
    return sorted(totals.items(), key=lambda cw: (-cw[1], cw[0] or 0))


def _target_file(
    index: MapIndex, target: str
) -> tuple[str | None, list[Symbol]]:
    """The file a path or symbol target names, or ``None`` with the
    symbols it could mean (several files, or none)."""
    # Imported here: query imports this module for ``cohesion``.
    from dekko.analysis import query

    path = strip_dot_slash(target)
    if path in index.languages_by_path:
        return path, []
    match, candidates = query.resolve_target(index, target)
    files = {s.path for s in ([match] if match else candidates)}
    if len(files) == 1:
        return files.pop(), []
    return None, candidates
