"""The lean map: a budget-capped navigation map for agents.

Built in layers off the shared ``MapIndex`` (extract once, render many):

- **FR1 backbone** — every in-scope file, one line + purpose, grouped by
  directory. Production paths are the never-elided *floor*.
- **FR2/FR4 atoms** — each symbol's name and signature, ranked by Q1
  centrality (fan-in x churn).
- **FR3 module edges** — the coarse directory dependency shape.

The **NFR2 degradation ladder** (:func:`render`) composes these under a
hard, repo-scaled cap (:func:`effective_cap`), shedding depth in a fixed
preservation order — mermaid, collapse tests, signatures, names, module
edges, purpose width, then the path-only floor — until the document
fits, and reports what it dropped (:class:`LeanReport`, NFR5). Every step
is a pure, deterministic function of the map so the same input yields
byte-stable output (NFR3).
"""

import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import export, relevance, summary
from .classify import is_test_path
from .mapfile import MapIndex
from .relevance import TaskContext
from .textutil import count_lines, dir_of, oneline, signature

# Default (and maximum) purpose width. The render layer may narrow this
# toward 0 as the budget tightens (the FR1 floor sub-ladder); it never
# widens past what was captured at compute time.
LEAN_PURPOSE_WIDTH = 72
# Separator between a file's basename and its purpose. Two spaces, no
# column alignment: padding would spend tokens on whitespace for a
# reader (Claude) that does not need it.
SEP = "  "
# Indent for a symbol atom rendered under its file row.
ATOM_INDENT = "    "

# Cap scaling (Q3): cap = max(min(MAX, BASE + PER_FILE * files), floor).
# Scaling grows the map with the repo; MAX keeps "lean" lean; the floor
# (path-only backbone) guarantees the cap is never below what FR1 needs.
LEAN_CAP_BASE = 3000
LEAN_CAP_PER_FILE = 12
LEAN_CAP_MAX = 12000
# Tokens reserved for the self-describing header so the body fit leaves
# room for it (NFR5). Generous: the header is at most a few lines.
_HEADER_RESERVE_TOK = 64
# FR6/Q5: cap the mermaid block by directory-node count. Independent of
# the token budget — it stops an unreadable hairball even when the
# budget would allow it. The block is the ladder's first drop regardless.
LEAN_MERMAID_MAX_NODES = 40
# FR-D1 dense mode: keep signatures only on this many most-central atoms
# (names for the rest), regardless of budget headroom. The tersest skin.
LEAN_DENSE_SIGNATURES = 30


@dataclass(frozen=True)
class BackboneRow:
    """One file's line in the navigation index.

    Attributes:
        path: Repo-relative POSIX path (the stable id).
        purpose: One-line purpose, already collapsed and truncated to
            ``LEAN_PURPOSE_WIDTH``, or ``""`` when the file has no
            module doc.
        demotable: True for test/fixture/vendored files. The ladder may
            collapse these to a per-directory line; production rows
            never collapse (the FR1 floor guarantee).
    """

    path: str
    purpose: str
    demotable: bool


@dataclass(frozen=True)
class BackboneGroup:
    """A directory and its rows, for path-amortized rendering.

    Attributes:
        directory: Repo-relative directory, or ``.`` for the root.
        rows: The directory's files, in ascending path order.
        demotable: True when every row is demotable, so the whole
            directory may be collapsed to one line.
    """

    directory: str
    rows: tuple[BackboneRow, ...]
    demotable: bool


def compute_backbone(index: MapIndex) -> list[BackboneGroup]:
    """Build the deterministic file backbone (the FR1 floor).

    Reads only the in-scope file set (``languages_by_path``), each
    file's purpose (``docs_by_path``), and its test classification. No
    call-graph data is consulted: the backbone is the navigation index,
    not the dependency view.

    Args:
        index: Loaded map index.

    Returns:
        Directory groups in ascending directory order, each with its
        rows in ascending path order.
    """
    rows_by_dir: dict[str, list[BackboneRow]] = {}
    for path in index.languages_by_path:
        doc = index.docs_by_path.get(path) or ""
        purpose = oneline(doc, LEAN_PURPOSE_WIDTH) if doc else ""
        row = BackboneRow(
            path=path, purpose=purpose, demotable=is_test_path(path)
        )
        rows_by_dir.setdefault(dir_of(path), []).append(row)
    groups: list[BackboneGroup] = []
    for directory in sorted(rows_by_dir):
        rows = tuple(sorted(rows_by_dir[directory], key=lambda r: r.path))
        groups.append(
            BackboneGroup(
                directory=directory,
                rows=rows,
                demotable=all(r.demotable for r in rows)
            )
        )
    return groups


def render_backbone(
    groups: list[BackboneGroup],
    width: int = LEAN_PURPOSE_WIDTH,
    collapse_demotable: bool = False
) -> list[str]:
    """Render backbone groups to dense lean lines.

    Production groups are always expanded — their file paths are the
    floor. ``collapse_demotable`` folds each all-demotable directory to
    a single ``dir/  (N files)`` line; the *decision* to collapse
    belongs to the budget ladder, but the rendering of either shape
    lives here.

    Args:
        groups: Groups from :func:`compute_backbone`.
        width: Purpose-text width. ``0`` drops purposes, leaving
            basenames only (the floor's narrowest rung).
        collapse_demotable: Collapse all-demotable directories to one
            line each.

    Returns:
        Output lines, ready to join with newlines.
    """
    lines: list[str] = []
    for group in groups:
        if collapse_demotable and group.demotable:
            lines.append(_collapsed_line(group))
            continue
        lines.append(f"{group.directory}/")
        lines += [_row_line(row, width) for row in group.rows]
    return lines


def _row_line(row: BackboneRow, width: int) -> str:
    """A single indented file row, with purpose when width allows."""
    base = row.path.rsplit("/", 1)[-1]
    if width and row.purpose:
        return f"  {base}{SEP}{oneline(row.purpose, width)}"
    return f"  {base}"


def _collapsed_line(group: BackboneGroup) -> str:
    """One-line summary of an all-demotable directory."""
    n = len(group.rows)
    noun = "file" if n == 1 else "files"
    return f"{group.directory}/  ({n} {noun})"


# --- FR2/FR4: per-symbol atoms + Q1 centrality -----------------------


@dataclass(frozen=True)
class SymbolAtom:
    """A sheddable per-symbol unit: its FR2 name and FR4 signature.

    The degradation ladder drops a symbol's signature, then its name,
    lowest ``centrality`` first — so this carries both renderings plus
    the score that orders their removal.

    Attributes:
        sym_id: Stable symbol id (``path::Qualified.name``).
        name: Bare name — the FR2 atom (the cheaper rendering).
        signature: One-line ``name(params) -> ret`` — the FR4 atom.
        centrality: Fan-in weighted by file churn (Q1); higher survives
            longer. Degrades to plain fan-in without churn data.
        path: Repo-relative POSIX path of the defining file.
        demotable: True for test/fixture/vendored code (FR5).
    """

    sym_id: str
    name: str
    signature: str
    centrality: float
    path: str
    demotable: bool


def _centrality(fan_in: int, churn_count: int, max_churn: int) -> float:
    """Fan-in weighted by normalized file churn (Q1, ladder §5).

    Degrades to plain fan-in when there is no churn signal (non-git
    root, churn disabled), so the ranking always has a stable meaning.

    Args:
        fan_in: Incoming call edges to the symbol.
        churn_count: Commits that touched the symbol's file.
        max_churn: Highest per-file churn in the repo (the normalizer).

    Returns:
        The centrality score; ``0.0`` for an uncalled symbol.
    """
    if max_churn <= 0:
        return float(fan_in)
    return fan_in * (1 + churn_count / max_churn)


def build_atoms(
    index: MapIndex, churn: Counter[str]
) -> dict[str, list[SymbolAtom]]:
    """Build per-file FR2/FR4 atoms with Q1 centrality.

    Pure in ``churn``: pass ``summary.file_churn(root)`` for the real
    signal, or an empty counter to rank on fan-in alone. Atoms stay in
    definition (line) order within a file for readable rendering; the
    ladder sorts a flattened view by :func:`centrality_key` when it
    sheds depth.

    Args:
        index: Loaded map index.
        churn: Per-file commit-touch counts; empty disables the churn
            weight (centrality becomes plain fan-in).

    Returns:
        File path → its symbols as atoms, in definition order.
    """
    max_churn = max(churn.values(), default=0)
    by_path: dict[str, list[SymbolAtom]] = {}
    for path, syms in index.symbols_by_path.items():
        ordered = sorted(syms, key=lambda s: (s.start_line, s.qualname))
        by_path[path] = [
            SymbolAtom(
                sym_id=s.id,
                name=s.name,
                signature=signature(s),
                centrality=_centrality(
                    len(index.calls_in.get(s.id, [])),
                    churn.get(path, 0),
                    max_churn
                ),
                path=path,
                demotable=is_test_path(path)
            )
            for s in ordered
        ]
    return by_path


def centrality_key(atom: SymbolAtom) -> tuple[float, str]:
    """Drop-order key for the ladder: shed lowest centrality first.

    Ascending order puts the least central atoms first (dropped first);
    ``sym_id`` breaks ties for byte-stable output (NFR3).
    """
    return (atom.centrality, atom.sym_id)


# --- FR3: module-edge text -------------------------------------------


def module_edges(index: MapIndex) -> list[tuple[str, str]]:
    """The coarse directory-level dependency edges (FR3).

    A thin accessor over ``export.dir_graph`` — the same generator that
    backs MAP.md's mermaid and (later) the lean map's mermaid block, so
    the textual and visual edge views never disagree. External and
    ambiguous edges are already excluded by the resolver; same-directory
    edges are dropped as self-loops.

    Args:
        index: Loaded map index.

    Returns:
        Sorted ``(src_dir, dst_dir)`` pairs, directories without a
        trailing slash (``.`` for the root).
    """
    return export.dir_graph(index)[1]


def render_module_edges(edges: list[tuple[str, str]]) -> list[str]:
    """Render module edges as dense per-source lines (FR3).

    One line per source directory, its targets joined on that line, so
    the dependency shape reads top-to-bottom without a table::

        src/dekko/ → tests/, ./

    Args:
        edges: ``(src_dir, dst_dir)`` pairs (e.g. from
            :func:`module_edges`).

    Returns:
        One line per source directory, in ascending source order; empty
        when there are no cross-directory edges.
    """
    targets: dict[str, list[str]] = {}
    for src, dst in edges:
        targets.setdefault(src, []).append(dst)
    lines = []
    for src in sorted(targets):
        dsts = ", ".join(f"{d}/" for d in sorted(set(targets[src])))
        lines.append(f"{src}/ → {dsts}")
    return lines


# --- NFR2: the model, the cap, the degradation ladder ----------------


@dataclass
class LeanModel:
    """Everything the ladder can render, at full fidelity.

    Attributes:
        groups: FR1 backbone groups.
        atoms_by_path: FR2/FR4 symbol atoms, per file.
        module_edges: FR3 directory dependency edges.
        mermaid: FR6 pre-rendered mermaid lines (empty until FR6 lands).
    """

    groups: list[BackboneGroup]
    atoms_by_path: dict[str, list[SymbolAtom]]
    module_edges: list[tuple[str, str]]
    mermaid: list[str]


@dataclass(frozen=True)
class CapConfig:
    """Cap-scaling knobs (Q3); ``override`` is the ``--budget`` flag."""

    base: int = LEAN_CAP_BASE
    per_file: int = LEAN_CAP_PER_FILE
    maximum: int = LEAN_CAP_MAX
    override: int | None = None


@dataclass
class LeanReport:
    """What the ladder shed, for the self-describing header (NFR5).

    The Meter's richer cousin: F1's ``Meter`` tracks one omission axis;
    the ladder sheds along several, each with its own recovery path.
    """

    tokens: int
    cap: int
    mermaid_dropped: bool
    demotable_collapsed: bool
    signatures_dropped: int
    names_dropped: int
    module_edges_dropped: bool
    purpose_width: int
    total_symbols: int
    signals: int = 0
    already_seen: int = 0

    @property
    def per_signal(self) -> float | None:
        """Tokens spent per signal covered (FR-D3), or ``None``."""
        if self.signals <= 0:
            return None
        return round(self.tokens / self.signals, 1)

    @property
    def floored(self) -> bool:
        """Whether the path-only floor was reached."""
        return self.purpose_width == 0

    @property
    def dropped_any(self) -> bool:
        """Whether any depth was shed at all."""
        return bool(
            self.mermaid_dropped
            or self.demotable_collapsed
            or self.signatures_dropped
            or self.names_dropped
            or self.module_edges_dropped
            or self.purpose_width < LEAN_PURPOSE_WIDTH
        )

    def _drops(self) -> list[str]:
        """Human labels for each shed dimension, in ladder order."""
        parts: list[str] = []
        if self.mermaid_dropped:
            parts.append("mermaid")
        if self.demotable_collapsed:
            parts.append("tests collapsed")
        if self.signatures_dropped:
            parts.append(f"{self.signatures_dropped} signatures")
        if self.names_dropped:
            parts.append(f"{self.names_dropped} names")
        if self.module_edges_dropped:
            parts.append("module edges")
        if self.purpose_width < LEAN_PURPOSE_WIDTH:
            parts.append(f"purpose→{self.purpose_width}")
        if self.already_seen:
            parts.append(f"{self.already_seen} already in context")
        return parts

    def footer(self) -> str:
        """The header's first line: budget, density, and what was elided."""
        head = f"lean map · ~{self.tokens}/{self.cap} tok"
        if self.signals > 0:
            head += f" · {self.signals} signals"
        drops = self._drops()
        if drops:
            head += " · dropped: " + ", ".join(drops)
        return head

    def as_dict(self) -> dict:
        """Structured ``meta`` object for JSON output."""
        return {
            "tokens": self.tokens,
            "cap": self.cap,
            "mermaid_dropped": self.mermaid_dropped,
            "demotable_collapsed": self.demotable_collapsed,
            "signatures_dropped": self.signatures_dropped,
            "names_dropped": self.names_dropped,
            "module_edges_dropped": self.module_edges_dropped,
            "purpose_width": self.purpose_width,
            "floored": self.floored,
            "signals": self.signals,
            "tokens_per_signal": self.per_signal,
            "already_seen": self.already_seen,
        }


@dataclass
class _LeanState:
    """Mutable shed state the ladder advances through (internal)."""

    mermaid: bool = True
    collapse_demotable: bool = False
    module_edges: bool = True
    purpose_width: int = LEAN_PURPOSE_WIDTH
    dropped_sigs: set[str] = field(default_factory=set)
    dropped_names: set[str] = field(default_factory=set)
    seen: set[str] = field(default_factory=set)


def build_mermaid(
    index: MapIndex, max_nodes: int = LEAN_MERMAID_MAX_NODES
) -> list[str]:
    """A fenced mermaid block of the directory graph (FR6/Q5).

    The visual skin of the same ``export.dir_graph`` that FR3 renders as
    text, so the diagram and the module-edge lines never disagree.
    Capped by directory-node count (not the token budget); above the cap
    it is omitted, since FR3 text still carries the edges and
    ``dekko export --format mermaid`` draws the full graph. The ladder
    drops this block first under budget pressure.

    Args:
        index: Loaded map index.
        max_nodes: Maximum directory nodes before the block is omitted.

    Returns:
        Fenced ``mermaid`` lines, or an empty list when there are no
        cross-directory edges or the graph exceeds ``max_nodes``.
    """
    labels, edges = export.dir_graph(index)
    if not edges or len(labels) > max_nodes:
        return []
    return ["```mermaid", export.render_mermaid(labels, edges), "```"]


def build_model(index: MapIndex, root: Path) -> LeanModel:
    """Assemble the full-fidelity lean model from the map.

    Captures git churn once (best-effort) for the Q1 centrality of the
    symbol atoms. The ladder treats the mermaid block as its first,
    optional drop.

    Args:
        index: Loaded map index.
        root: Repository root (for the churn signal).

    Returns:
        A :class:`LeanModel` at full fidelity.
    """
    return LeanModel(
        groups=compute_backbone(index),
        atoms_by_path=build_atoms(index, summary.file_churn(root)),
        module_edges=module_edges(index),
        mermaid=build_mermaid(index)
    )


def effective_cap(model: LeanModel, config: CapConfig) -> int:
    """The hard token cap (Q3): repo-scaled and floor-aware.

    ``--budget`` (``config.override``) replaces the scaled target, but
    the result is never below the cost of the path-only floor — FR1's
    backbone must always be renderable, so the cap bends, not the floor.

    Args:
        model: The lean model.
        config: Cap-scaling knobs.

    Returns:
        The effective cap in tokens.
    """
    n_files = sum(len(g.rows) for g in model.groups)
    if config.override is not None:
        target = config.override
    else:
        target = min(
            config.maximum, config.base + config.per_file * n_files
        )
    return max(target, _floor_cost(model))


def _floor_cost(model: LeanModel) -> int:
    """Token cost of the path-only floor plus the header reserve."""
    floor = render_backbone(
        model.groups, width=0, collapse_demotable=True
    )
    return count_lines(floor) + _HEADER_RESERVE_TOK


def render(
    model: LeanModel,
    cap: int,
    scores: dict[str, float] | None = None,
    dense: bool = False,
    seen: set[str] | None = None,
) -> tuple[list[str], LeanReport]:
    """Render the lean map under ``cap`` via the NFR2 ladder (§3).

    Sheds depth in fixed preservation order, re-measuring after each
    action and stopping at the first shape that fits, so the richest
    fitting document wins. The path-only floor is guaranteed to fit by
    :func:`effective_cap`.

    Args:
        model: Full-fidelity lean model from :func:`build_model`.
        cap: Token budget from :func:`effective_cap`.
        scores: Optional per-symbol survival scores (task-aware, higher
            survives longer); ``None`` sheds by plain centrality.
        dense: FR-D1 — keep signatures only on the most-central atoms
            (names for the rest) regardless of budget headroom.
        seen: FR-D2 — symbol ids already in the agent's context; these
            atoms are omitted and counted, so a re-surfaced map carries
            only what is new.

    Returns:
        ``(lines, report)`` — the rendered map and what was shed.
    """
    state = _LeanState()
    if seen:
        state.seen = set(seen)
    body_budget = cap - _HEADER_RESERVE_TOK

    def fits() -> bool:
        return count_lines(_render_document(model, state)) <= body_budget

    live = _live_atoms(model, scores)
    if dense:                            # 0: pre-shed sigs off the long tail
        _force_dense_sigs(state, live)
    if not fits():                       # 1: mermaid
        state.mermaid = False
    if not fits():                       # 2: collapse demotable dirs
        state.collapse_demotable = True
    _shed_symbols(state, live, fits)     # 3-4: signatures then names
    if not fits():                       # 5: module edges
        state.module_edges = False
    _shed_purpose(state, fits)           # 6: purpose 72→40→0 (7: floor)

    body = _render_document(model, state)
    report = LeanReport(
        tokens=0,
        cap=cap,
        mermaid_dropped=not state.mermaid,
        demotable_collapsed=state.collapse_demotable,
        signatures_dropped=len(state.dropped_sigs),
        names_dropped=len(state.dropped_names),
        module_edges_dropped=not state.module_edges,
        purpose_width=state.purpose_width,
        total_symbols=len(live),
        signals=_count_signals(model, state, live),
        already_seen=sum(1 for a in live if a.sym_id in state.seen),
    )
    return _assemble(report, body)


def _force_dense_sigs(state: _LeanState, live: list[SymbolAtom]) -> None:
    """FR-D1: drop signatures for all but the top-K central atoms.

    ``live`` is ascending by survival score, so the most-central atoms
    are its tail; everything before the last ``LEAN_DENSE_SIGNATURES``
    loses its signature (keeps its name).
    """
    if len(live) > LEAN_DENSE_SIGNATURES:
        for atom in live[:-LEAN_DENSE_SIGNATURES]:
            state.dropped_sigs.add(atom.sym_id)


def _count_signals(
    model: LeanModel, state: _LeanState, live: list[SymbolAtom]
) -> int:
    """Files + symbols actually rendered (FR-D3 density numerator)."""
    rendered_syms = sum(
        1
        for a in live
        if a.sym_id not in state.dropped_names and a.sym_id not in state.seen
    )
    n_files = sum(
        len(g.rows)
        for g in model.groups
        if not (state.collapse_demotable and g.demotable)
    )
    return n_files + rendered_syms


def generate(
    index: MapIndex,
    root: Path,
    config: CapConfig | None = None,
    task: TaskContext | None = None,
    dense: bool = False,
    seen: set[str] | None = None,
) -> tuple[list[str], LeanReport]:
    """One call: build the model, pick the cap, render the lean map.

    When ``task`` carries a signal, the symbol atoms are shed in a
    task-aware order (relevant atoms survive the ladder longer); without
    it the order is plain Q1 centrality and output is unchanged. ``dense``
    (FR-D1) and ``seen`` (FR-D2) tune the density independently of budget.
    """
    config = config or CapConfig()
    model = build_model(index, root)
    scores = None
    if task is not None and not task.is_empty:
        scores = _relevance_scores(model, task)
    return render(
        model, effective_cap(model, config), scores, dense=dense, seen=seen
    )


def _relevance_scores(
    model: LeanModel, task: TaskContext
) -> dict[str, float]:
    """Blend task relevance with Q1 centrality over the live atoms.

    Scores only the atoms the ladder can shed (those in expanded,
    non-demotable groups); demotable atoms are collapsed wholesale and
    never ranked. Higher score = survives longer.
    """
    candidates: list[relevance.Candidate] = []
    centrality: dict[str, float] = {}
    for group in model.groups:
        if group.demotable:
            continue
        for row in group.rows:
            for atom in model.atoms_by_path.get(row.path, []):
                candidates.append(
                    relevance.Candidate(
                        id=atom.sym_id,
                        text=f"{atom.name} {atom.signature}",
                        path=atom.path,
                    )
                )
                centrality[atom.sym_id] = atom.centrality
    return relevance.blended_scores(task, candidates, centrality)


def run(
    index: MapIndex,
    root: Path,
    budget: int | None = None,
    as_json: bool = False,
    out_path: Path | None = None,
    task: TaskContext | None = None,
    dense: bool = False,
) -> int:
    """Render the lean map to stdout, JSON, or a file.

    Args:
        index: Loaded map index.
        root: Repository root (for churn).
        budget: Hard token cap override; ``None`` scales with repo size.
        as_json: Emit ``{"map", "meta"}`` JSON to stdout instead of text.
        out_path: When set, write the text map there (and print a
            confirmation) instead of printing the map itself; the cached,
            optionally-committed artifact (e.g. ``.dekko/LEAN.md``).
        task: Optional task context; when set, the symbol atoms are shed
            in a task-aware order so relevant code survives the ladder.
        dense: Keep signatures only on the most-central atoms (FR-D1).

    Returns:
        Always ``0``.
    """
    lines, report = generate(
        index, root, CapConfig(override=budget), task, dense=dense
    )
    text = "\n".join(lines)
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text + "\n")
        print(f"dekko: wrote {out_path} (~{report.tokens} tokens)")
        return 0
    if as_json:
        print(json.dumps({"map": text, "meta": report.as_dict()}, indent=2))
        return 0
    print(text)
    return 0


def _shed_symbols(
    state: _LeanState, live: list[SymbolAtom], fits: Callable[[], bool]
) -> None:
    """Ladder rungs 3-4: drop signatures, then names, lowest first."""
    for atom in live:
        if fits():
            return
        state.dropped_sigs.add(atom.sym_id)
    for atom in live:
        if fits():
            return
        state.dropped_names.add(atom.sym_id)


def _shed_purpose(state: _LeanState, fits: Callable[[], bool]) -> None:
    """Ladder rung 6: shrink purpose width toward the path-only floor."""
    for width in (40, 0):
        if fits():
            return
        state.purpose_width = width


def _live_atoms(
    model: LeanModel, scores: dict[str, float] | None = None
) -> list[SymbolAtom]:
    """Atoms in expanded (non-demotable) groups, lowest-survival first.

    These are the only atoms the ladder sheds: demotable groups are
    collapsed wholesale at rung 2, so their atoms never render. Without
    ``scores`` the order is ascending Q1 centrality; with task-aware
    ``scores`` it is ascending blended score, so relevant atoms sort to
    the tail and are shed last. ``sym_id`` breaks ties for byte-stable
    output (NFR3).
    """
    live: list[SymbolAtom] = []
    for group in model.groups:
        if group.demotable:
            continue
        for row in group.rows:
            live.extend(model.atoms_by_path.get(row.path, []))
    if scores is None:
        live.sort(key=centrality_key)
    else:
        live.sort(key=lambda a: (scores.get(a.sym_id, 0.0), a.sym_id))
    return live


def _render_document(model: LeanModel, state: _LeanState) -> list[str]:
    """Render the full lean body at the current shed state."""
    lines: list[str] = []
    for group in model.groups:
        lines += _group_block(model, group, state)
    lines += _edge_block(model, state)
    if state.mermaid and model.mermaid:
        lines += model.mermaid
    return lines


def _group_block(
    model: LeanModel, group: BackboneGroup, state: _LeanState
) -> list[str]:
    """One directory: collapsed line, or header + file/atom rows."""
    if state.collapse_demotable and group.demotable:
        return [_collapsed_line(group)]
    lines = [f"{group.directory}/"]
    for row in group.rows:
        lines.append(_row_line(row, state.purpose_width))
        lines += _atom_lines(model, row.path, state)
    return lines


def _atom_lines(
    model: LeanModel, path: str, state: _LeanState
) -> list[str]:
    """Symbol rows under a file, at their current shed form."""
    out: list[str] = []
    for atom in model.atoms_by_path.get(path, []):
        form = _atom_form(state, atom)
        if form == "sig":
            out.append(f"{ATOM_INDENT}{atom.signature}")
        elif form == "name":
            out.append(f"{ATOM_INDENT}{atom.name}")
    return out


def _atom_form(state: _LeanState, atom: SymbolAtom) -> str | None:
    """Rendering of an atom: ``sig``, ``name``, or dropped (``None``)."""
    if atom.sym_id in state.seen:        # FR-D2: already in context
        return None
    if atom.sym_id in state.dropped_names:
        return None
    if atom.sym_id in state.dropped_sigs:
        return "name"
    return "sig"


def _edge_block(model: LeanModel, state: _LeanState) -> list[str]:
    """The FR3 module-edge section, or nothing when shed/empty."""
    if not state.module_edges:
        return []
    edge_lines = render_module_edges(model.module_edges)
    if not edge_lines:
        return []
    return ["module edges:"] + [f"  {ln}" for ln in edge_lines]


def _assemble(
    report: LeanReport, body: list[str]
) -> tuple[list[str], LeanReport]:
    """Prepend the NFR5 header, recording the final token count.

    Uses the same ``count_lines`` measure as the fit decision so the
    reported figure never contradicts the cap the ladder fit to.
    """
    report.tokens = count_lines(_header_lines(report) + body)
    return _header_lines(report) + body, report


def _header_lines(report: LeanReport) -> list[str]:
    """The self-describing header (NFR5): budget, drops, recovery."""
    lines = [report.footer()]
    if report.dropped_any:
        lines.append(
            "  recover: `dekko outline <file>` · "
            "`dekko context <sym>` · `dekko query`"
        )
    lines.append("")
    return lines
