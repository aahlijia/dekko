"""Task-scoped work-set bundles: one budgeted call for a whole change.

``dekko workset [REV]`` (or ``--symbol NAME``) bundles everything needed
to work on a change under a single token budget: the impacted tests, a
roster and outlines of the touched files, and call-graph packs for the
most central touched symbols. It replaces the manual ``affected`` + N
``outline`` + N ``context`` dance with one deterministic call.

The bundle is pure composition over existing machinery — ``affected``
(impacts + the diff), ``outline`` (file shape), ``contextpack`` (symbol
neighborhoods), and ``textutil.fit_to_budget`` (the shared budget). One
flat list of rows in three value tiers — file roster, then packs, then
full outline detail — is trimmed from the tail so breadth survives a
tight budget and detail is the first to go.
"""

import json
from dataclasses import dataclass, field, replace
from pathlib import Path

from dekko import repo_ops
from dekko.analysis import affected
from dekko.analysis import outline
from dekko.analysis import query
from dekko.analysis import relevance
from dekko.classify import relevance_key
from dekko.analysis.contextpack import Pack, build_pack
from dekko.render.mapfile import MapIndex
from dekko.core.model import TYPE_KINDS, Symbol
from dekko.analysis.query import report_unresolved, resolve_target
from dekko.analysis.relevance import TaskContext
from dekko.textutil import fit_to_budget, oneline, signature

EXIT_OK = 0
EXIT_ERROR = 2

DEFAULT_BUDGET = 6000
DEFAULT_PACKS = 5

_DOC_LIMIT = 80
_TIER_TITLES = {
    "tests": "impacted tests:",
    "files": "files:",
    "packs": "packs:",
    "detail": "detail:",
}


@dataclass
class BlastRadius:
    """The widened-touched-set breakdown ``--type-impact`` discloses.

    Set on a symbol-mode :class:`Seed` whenever ``type_impact=True``
    was requested — including when the target isn't a type-kind
    symbol, in which case ``type_usage``/``heritage`` are both ``0``
    (a harmless no-op, not an error; see ``_type_impact_touched``).

    Attributes:
        direct: Always ``1`` — the target symbol itself.
        type_usage: Raw count of type-usage-matching rows found
            (``query.type_usage_rows``), before deduping against
            heritage overlap — a symbol can appear in this count more
            than once (e.g. taking and returning the same type).
        heritage: Raw count of transitive implementors found
            (``query.walk_heritage``), before deduping.
        note: Undercount caveat when the target's heritage has
            inbound edges the resolver couldn't disambiguate, or
            ``None``.
    """

    direct: int
    type_usage: int
    heritage: int
    note: str | None = None


@dataclass
class Seed:
    """What a change (rev or symbol) resolves to before composition.

    Attributes:
        mode: ``"rev"`` or ``"symbol"``.
        label: Human description for the manifest line.
        rev: Resolved git rev (rev mode), else ``None``.
        symbol: Resolved qualified name (symbol mode), else ``None``.
        touched: Changed/added symbols, or the single seed symbol,
            ranked most-central first.
        files: Touched files, ranked by aggregate centrality.
        impacts: Impacted test files (strongest evidence first).
        blast_radius: ``--type-impact``'s widened-set breakdown
            (symbol mode only), or ``None`` when the flag wasn't used.
    """

    mode: str
    label: str
    rev: str | None
    symbol: str | None
    touched: list[Symbol]
    files: list[str]
    impacts: list[affected.TestImpact] = field(default_factory=list)
    blast_radius: BlastRadius | None = None


@dataclass
class Workset:
    """A built bundle, ready to render."""

    seed: Seed
    outlines: list[outline.FileOutline] = field(default_factory=list)
    packs: list[Pack] = field(default_factory=list)


@dataclass
class _Row:
    """One droppable output row, tagged for budget tiering and JSON."""

    tier: str
    text: str
    file: str | None = None
    sym: Symbol | None = None
    pack: int | None = None
    test: affected.TestImpact | None = None


def _rank_files(touched: list[Symbol], index: MapIndex) -> list[str]:
    """Touched files, most-central (highest aggregate degree) first."""
    by_file: dict[str, int] = {}
    for sym in touched:
        by_file[sym.path] = by_file.get(sym.path, 0) + index.degree(sym.id)
    return sorted(by_file, key=lambda p: (-by_file[p], p))


def _make_seed(
    mode: str,
    label: str,
    rev: str | None,
    symbol: str | None,
    touched: list[Symbol],
    impacts: list[affected.TestImpact],
    index: MapIndex,
    blast_radius: BlastRadius | None = None,
) -> Seed:
    """Rank a raw touched set into a fully-populated ``Seed``."""
    ranked = sorted(touched, key=lambda s: relevance_key(s, index))
    return Seed(
        mode=mode,
        label=label,
        rev=rev,
        symbol=symbol,
        touched=ranked,
        files=_rank_files(touched, index),
        impacts=impacts,
        blast_radius=blast_radius,
    )


def _type_impact_touched(
    index: MapIndex, sym: Symbol
) -> tuple[list[Symbol], BlastRadius]:
    """Symbols/counts ``--type-impact`` adds beyond the target itself.

    Returns an empty extras list and all-zero counts for a non-type
    target (function/method/variable) — ``--type-impact`` is a no-op,
    not an error, when the target isn't a ``TYPE_KINDS`` symbol, since
    a caller might reasonably pass ``--type-impact`` defensively
    without checking the target's kind first.

    Args:
        index: Loaded map index.
        sym: The already-resolved target symbol.

    Returns:
        ``(extra, blast_radius)`` — ``extra`` is the deduped union of
        every type-usage-matching function/method and every transitive
        implementor, excluding ``sym`` itself; ``blast_radius`` is the
        disclosed breakdown (see :class:`BlastRadius`).
    """
    if sym.kind not in TYPE_KINDS:
        return [], BlastRadius(direct=1, type_usage=0, heritage=0)
    usage_rows = query.type_usage_rows(index, sym.name, exact=False)
    heritage_hits = query.walk_heritage(
        index, sym.id, direction="in", relation=None
    )
    extra: dict[str, Symbol] = {}
    for row_sym, _usage, _param, _raw in usage_rows:
        extra[row_sym.id] = row_sym
    for sub_sym, _relation, _depth in heritage_hits:
        extra[sub_sym.id] = sub_sym
    # Heritage's own ambiguity disclosure (`_run_heritage`'s ambig_in
    # for `subtypes`) is scoped to the target itself only, not every
    # hop of the transitive walk — mirrored here for the same reason:
    # an ambiguous inbound edge has no symbol id to keep walking from.
    ambig_in = len(index.heritage_ambiguous_in.get(sym.id, []))
    note = None
    if ambig_in:
        note = (
            f"{ambig_in} additional subtype(s) named this type "
            "ambiguously — not counted here; blast radius may be an "
            "undercount"
        )
    blast_radius = BlastRadius(
        direct=1,
        type_usage=len(usage_rows),
        heritage=len(heritage_hits),
        note=note,
    )
    return list(extra.values()), blast_radius


def seed_from_rev(
    index: MapIndex, root: Path, rev: str | None, jobs: int = 1
) -> Seed | None:
    """Seed from a worktree-vs-rev diff; ``None`` on an unexportable rev.

    Args:
        index: Already-loaded current-tree index (reused, not
            reloaded — see ``affected.changes``).
        root: Repository root.
        rev: Git rev for the old side, or ``None`` to derive a default.
        jobs: Resolved worker count for a rev-cache-miss old-side
            re-parse/resolve — see ``diff.snapshot``. Round-12 master
            report §3.3.
    """
    outcome = affected.changes(root, rev, index=index, jobs=jobs)
    if outcome is None:
        return None
    impacts, result, _new, target_rev, _prov = outcome
    touched = [d.symbol for d in result.added + result.changed]
    label = f"changed vs {target_rev[:12]}"
    return _make_seed("rev", label, target_rev, None, touched, impacts, index)


def seed_from_symbol(
    index: MapIndex, target: str, type_impact: bool = False
) -> tuple[Seed | None, list[Symbol]]:
    """Seed from a single symbol; ``(None, candidates)`` if unresolved.

    Args:
        index: Loaded map index.
        target: Symbol target string (see ``query.resolve_target``).
        type_impact: When true, also union in every type-usage site
            and transitive implementor into the touched set — the
            full blast radius of changing a shared type's shape, not
            just its direct callers. No-op when the resolved target
            isn't a class/interface/struct/trait/enum/record symbol.
    """
    sym, candidates = resolve_target(index, target)
    if sym is None:
        return None, candidates
    touched = [sym]
    blast_radius = None
    if type_impact:
        extra, blast_radius = _type_impact_touched(index, sym)
        touched += extra
    impacts = affected.impacts_from_symbol(index, {s.id for s in touched})
    label = f"symbol {sym.path}:{sym.qualname}"
    if type_impact:
        label += " (+ type-impact)"
    seed = _make_seed(
        "symbol",
        label,
        None,
        sym.qualname,
        touched,
        impacts,
        index,
        blast_radius=blast_radius,
    )
    return seed, candidates


def _apply_task(seed: Seed, index: MapIndex, task: TaskContext) -> Seed:
    """Re-rank a seed's touched symbols and files by blended relevance.

    Composition (:func:`build`) draws packs from ``touched`` and outlines
    from ``files`` in order and trims from the tail under budget, so
    re-ordering both by task relevance is enough to make the whole bundle
    task-aware without touching the renderers.
    """
    return replace(
        seed,
        touched=_rerank_touched(seed.touched, index, task),
        files=_rerank_files(seed, index, task),
    )


def _rerank_touched(
    touched: list[Symbol], index: MapIndex, task: TaskContext
) -> list[Symbol]:
    """Touched symbols, most task-relevant first (centrality blended in)."""
    candidates = [
        relevance.Candidate(
            id=s.id, text=f"{s.qualname} {signature(s)}", path=s.path
        )
        for s in touched
    ]
    centrality = {s.id: float(index.degree(s.id)) for s in touched}
    scores = relevance.blended_scores(task, candidates, centrality)
    return sorted(
        touched,
        key=lambda s: (-scores.get(s.id, 0.0), *relevance_key(s, index)),
    )


def _rerank_files(seed: Seed, index: MapIndex, task: TaskContext) -> list[str]:
    """Touched files, most task-relevant first (aggregate degree blended)."""
    by_file: dict[str, int] = {}
    for sym in seed.touched:
        by_file[sym.path] = by_file.get(sym.path, 0) + index.degree(sym.id)
    candidates = [
        relevance.Candidate(id=p, text=p, path=p) for p in seed.files
    ]
    centrality = {p: float(by_file.get(p, 0)) for p in seed.files}
    scores = relevance.blended_scores(task, candidates, centrality)
    return sorted(seed.files, key=lambda p: (-scores.get(p, 0.0), p))


def build(index: MapIndex, seed: Seed, packs: int) -> Workset:
    """Compose a seed into outlines and top-centrality symbol packs."""
    outlines = [
        outline.build(index, path)
        for path in seed.files
        if path in index.languages_by_path
    ]
    in_map = [s for s in seed.touched if s.id in index.symbols_by_id]
    pack_objs = [build_pack(index, s, 1) for s in in_map[:packs]]
    return Workset(seed=seed, outlines=outlines, packs=pack_objs)


def _manifest(ws: Workset, root: Path) -> list[str]:
    """The non-droppable header: seed, counts, and the test-runner hint."""
    seed = ws.seed
    lines = [
        f"workset: {seed.label} — {len(ws.outlines)} files, "
        f"{len(seed.touched)} symbols, "
        f"{len(seed.impacts)} impacted tests"
    ]
    if seed.blast_radius is not None:
        lines.append(_blast_radius_line(seed.blast_radius))
        if seed.blast_radius.note:
            lines.append(f"  note: {seed.blast_radius.note}")
    hint = affected._test_hint(seed.impacts, root)
    if hint:
        lines.append(hint)
    return lines


def _blast_radius_line(br: BlastRadius) -> str:
    """The ``--type-impact`` disclosure line under the manifest."""
    usage_noun = "site" if br.type_usage == 1 else "sites"
    heritage_noun = "implementor" if br.heritage == 1 else "implementors"
    return (
        f"  blast radius: {br.direct} direct target, "
        f"{br.type_usage} type-usage {usage_noun}, "
        f"{br.heritage} {heritage_noun}"
    )


def _test_row(impact: affected.TestImpact) -> str:
    """One condensed impacted-test row: path and evidence tier."""
    return f"  {impact.path}  [{impact.tier}]"


def _roster_row(fo: outline.FileOutline) -> str:
    """One condensed breadth row: path, language, doc, symbol count."""
    row = f"  {fo.path}  [{fo.language}]"
    if fo.doc:
        row += f"  {oneline(fo.doc, _DOC_LIMIT)}"
    count = len(fo.symbols)
    row += f" · {count} symbol{'' if count == 1 else 's'}"
    return row


def _entry_names(pack: Pack, direction: str) -> str:
    """Comma-joined ``qualname (path:line)`` for one edge direction."""
    entries = sorted(
        (e for e in pack.entries if e.direction == direction),
        key=lambda e: (e.sym.path, e.sym.start_line),
    )
    return ", ".join(
        f"{e.sym.qualname} ({e.sym.path}:{e.sym.start_line})" for e in entries
    )


def _pack_block(pack: Pack) -> list[str]:
    """Compact depth block: signature, doc, hop-1 callers/callees."""
    target = pack.target
    lines = [f"  {signature(target)}  ({target.path}:{target.start_line})"]
    if target.doc:
        lines.append(f"      {oneline(target.doc, _DOC_LIMIT)}")
    for direction, label in (("caller", "callers"), ("callee", "callees")):
        names = _entry_names(pack, direction)
        if names:
            lines.append(f"      {label}: {names}")
    if pack.module_callers:
        joined = ", ".join(pack.module_callers)
        lines.append(f"      module callers: {joined}")
    return lines


def _rows(ws: Workset) -> list[_Row]:
    """Flatten the four value tiers into one ordered droppable list.

    Impacted tests are their own tier, added *last* — after breadth
    (files), depth (packs), and full outline detail — so they
    participate in the same budget-fitting pass as every other
    section without displacing the existing "breadth survives a
    tight budget" guarantee those three tiers already give. They used
    to bypass budget-fitting entirely: a real ~1,500-impact repo
    dumped every path verbatim via the (unbudgeted) pytest hint,
    blowing 3.6x past the stated budget (bug #6/B6). The manifest's
    impacted-test count and the (separately capped) pytest hint are
    unaffected by this tier's own trimming.
    """
    rows: list[_Row] = [
        _Row("files", _roster_row(fo), file=fo.path) for fo in ws.outlines
    ]
    for index, pack in enumerate(ws.packs):
        rows.extend(
            _Row("packs", line, pack=index) for line in _pack_block(pack)
        )
    for fo in ws.outlines:
        if not (fo.symbols or fo.doc or fo.error):
            continue
        rows.extend(
            _Row("detail", line, file=fo.path)
            for line in outline._file_header(fo)
        )
        rows.extend(
            _Row("detail", outline._symbol_row(s), file=fo.path, sym=s)
            for s in fo.symbols
        )
    rows += [
        _Row("tests", _test_row(imp), test=imp) for imp in ws.seed.impacts
    ]
    return rows


def _fit(
    ws: Workset, budget: int | None, root: Path
) -> tuple[list[_Row], object]:
    """Apply the shared budget over the manifest prefix plus all rows."""
    rows = _rows(ws)
    prefix = "\n".join(_manifest(ws, root))
    kept, meter = fit_to_budget([r.text for r in rows], budget, None, prefix)
    return rows[: len(kept)], meter


def _render_text(ws: Workset, budget: int | None, root: Path) -> int:
    """Render the bundle as text: manifest, tiered rows, cost footer."""
    kept, meter = _fit(ws, budget, root)
    for line in _manifest(ws, root):
        print(line)
    current: str | None = None
    for row in kept:
        if row.tier != current:
            print(f"\n{_TIER_TITLES[row.tier]}")
            current = row.tier
        print(row.text)
    print(meter.footer())
    return EXIT_OK


def _kept_view(
    kept: list[_Row],
) -> tuple[dict[str, list[Symbol]], set[int], list[affected.TestImpact]]:
    """Reduce kept rows to surviving files (with symbols), packs, tests."""
    files: dict[str, list[Symbol]] = {}
    packs: set[int] = set()
    tests: list[affected.TestImpact] = []
    for row in kept:
        if row.file is not None:
            files.setdefault(row.file, [])
            if row.sym is not None:
                files[row.file].append(row.sym)
        if row.pack is not None:
            packs.add(row.pack)
        if row.test is not None:
            tests.append(row.test)
    return files, packs, tests


def _entry_json(entry: object) -> dict:
    """Structured neighbor: id, path, line, signature."""
    sym = entry.sym  # type: ignore[attr-defined]
    return {
        "id": sym.id,
        "path": sym.path,
        "line": sym.start_line,
        "signature": signature(sym),
    }


def _pack_json(pack: Pack) -> dict:
    """Compact structured pack matching :func:`_pack_block`."""
    target = pack.target
    return {
        "target": {
            "id": target.id,
            "path": target.path,
            "line": target.start_line,
            "signature": signature(target),
            "doc": target.doc,
        },
        "callers": [
            _entry_json(e) for e in pack.entries if e.direction == "caller"
        ],
        "callees": [
            _entry_json(e) for e in pack.entries if e.direction == "callee"
        ],
        "module_callers": pack.module_callers,
    }


def _outline_json(fo: outline.FileOutline, syms: list[Symbol]) -> dict:
    """One file's structured outline, limited to its surviving symbols."""
    return {
        "path": fo.path,
        "language": fo.language,
        "doc": fo.doc,
        "error": fo.error,
        "symbols": [outline._sym_json(s) for s in syms],
    }


def _render_json(ws: Workset, budget: int | None, root: Path) -> int:
    """Render the bundle as JSON, reflecting exactly what the budget kept."""
    kept, meter = _fit(ws, budget, root)
    files, packs, tests = _kept_view(kept)
    seed = ws.seed
    seed_doc = {
        "mode": seed.mode,
        "rev": seed.rev,
        "symbol": seed.symbol,
        "touched_files": list(seed.files),
        "touched_symbols": len(seed.touched),
    }
    if seed.blast_radius is not None:
        br = seed.blast_radius
        seed_doc["blast_radius"] = {
            "direct": br.direct,
            "type_usage": br.type_usage,
            "heritage": br.heritage,
        }
        if br.note:
            seed_doc["blast_radius_note"] = br.note
    doc = {
        "seed": seed_doc,
        # `tests` is the budget-fitted subset (bug #6/B6 — this used
        # to be every impacted path unconditionally, regardless of
        # budget); `impacted_tests_total` is the true count so a
        # caller can tell the two apart.
        "impacted_tests": [affected._impact_json(i) for i in tests],
        "impacted_tests_total": len(seed.impacts),
        "pytest": affected._test_hint(seed.impacts, root),
        "outlines": [
            _outline_json(fo, files[fo.path])
            for fo in ws.outlines
            if fo.path in files
        ],
        "packs": [_pack_json(ws.packs[i]) for i in sorted(packs)],
        "meta": meter.as_dict(),
    }
    print(json.dumps(doc, indent=2))
    return EXIT_OK


def run(
    root: Path,
    rev: str | None,
    symbol: str | None,
    budget: int | None,
    packs: int,
    as_json: bool,
    no_regen: bool,
    task: TaskContext | None = None,
    jobs: int = 1,
    type_impact: bool = False,
) -> int:
    """Build and render a work-set bundle for a change or a symbol.

    Args:
        root: Repository root containing the map.
        rev: Git rev to diff against (rev seed), or ``None`` for default.
        symbol: Symbol target (symbol seed), mutually exclusive with rev.
        budget: Shared token budget for the whole bundle, or ``None``.
        packs: Number of top-centrality symbols to deep-pack.
        as_json: Emit structured JSON instead of text.
        no_regen: Fail instead of regenerating a stale map.
        task: Optional task context; when set, the bundle's packs and
            outlines are ordered most task-relevant first.
        jobs: Resolved worker count for a rev-cache-miss old-side
            re-parse/resolve on a rev seed — see ``seed_from_rev``. No
            effect on a ``symbol`` seed.
        type_impact: Symbol-seed only (see ``seed_from_symbol``); also
            union in every type-usage site and transitive implementor
            into the touched set. Callers must reject this combined
            with a rev seed before calling ``run`` — mirroring how
            ``rev``/``symbol`` mutual exclusivity is already checked by
            each caller (``cli.run_workset``, ``server.tool_workset``),
            not here.

    Returns:
        ``0`` ok, ``2`` bad rev, ``3`` symbol not found, ``4`` ambiguous,
        ``5`` stale map with ``--no-regen``.
    """
    index, code = repo_ops.load_or_regen(root, no_regen)
    if index is None:
        return code
    if symbol is not None:
        seed, candidates = seed_from_symbol(
            index, symbol, type_impact=type_impact
        )
        if seed is None:
            return report_unresolved(symbol, candidates, index)
    else:
        seed = seed_from_rev(index, root, rev, jobs=jobs)
        if seed is None:
            return EXIT_ERROR
    if task is not None and not task.is_empty:
        seed = _apply_task(seed, index, task)
    ws = build(index, seed, packs)
    if as_json:
        return _render_json(ws, budget, root)
    return _render_text(ws, budget, root)
