"""Free-text relevance search over every symbol in the map.

``dekko search "<free text query>"`` ranks every symbol in the map
against a natural-language description, using
:class:`relevance.BM25Scorer` by default — for when an agent knows
what the code should do but not its name. Unlike ``query``, a search
that matches nothing is a legitimate zero-hit result, not an error:
there is no "ambiguous"/"not found" exit code here, only a ranked
(possibly empty) list.

Composition mirrors :mod:`workset`: build a
``list[relevance.Candidate]`` from the full symbol universe, blend the
chosen scorer's relevance with structural centrality via
:func:`relevance.blended_scores`, budget-fit the ranked rows with
:func:`textutil.fit_to_budget`, and render text or JSON. See
``.features/plans/SEMANTIC-SEARCH-PLAN.md`` for the design.

``--scorer {lexical,embedding}`` (Phase 2) swaps in
:class:`embedding.EmbeddingScorer` — a deterministic, dependency-light
hashing-trick embedding, opt-in via the ``dekko[search]`` extra — for
:class:`relevance.BM25Scorer`. The lexical scorer stays the unflagged
default: a base install and every existing ``dekko search`` call are
completely unaffected by Phase 2's addition. See ``embedding.py``'s
module docstring for the scorer itself and the plan's §8 for why this
diverges from the plan's original ``sentence-transformers`` sketch.

Two round-08 corrections layer on top of whichever scorer runs:
:class:`_CoverageAdjustedScorer` (wrapped around ``scorer`` in
:func:`rank`) discounts a candidate that only covers some of a 2+-term
query's distinct terms, so one lexically-dominant common term can't
crowd out a candidate matching every distinctive term more lightly
(§2.3); and ``--include-tests`` defaulting to off means a genuinely
relevant test-path symbol can be silently excluded before ranking ever
sees it — :func:`run` prints a ``note:`` hint (a JSON ``"note"`` key)
when the surviving top hit is weak and symbols were in fact excluded,
so a low-confidence result doesn't read as a confident one (§2.2).
"""

import json
import sys
from dataclasses import dataclass
from pathlib import Path

from . import embedding, relevance
from .mapfile import MapIndex
from .model import TYPE_KINDS, Symbol
from .relevance import BM25Scorer, Candidate, TaskContext
from .textutil import fit_to_budget, oneline, signature

EXIT_OK = 0
# A bad/unavailable --scorer choice. Same value as workset.EXIT_ERROR
# (2) — both mean "the request itself can't be satisfied," distinct
# from EXIT_STALE (5, cli._load_or_regen's --no-regen convention).
EXIT_ERROR = 2

DEFAULT_LIMIT = 15
DEFAULT_BUDGET = 800

# Below this blended score, the top hit is "weak" — worth telling the
# caller that test-path symbols were excluded, in case the real match
# lives in one. Reuses relevance.py's own coverage-factor floor (0.4)
# so there's one threshold in the codebase to reason about, not two.
LOW_CONFIDENCE_THRESHOLD = 0.4

# Phase 1 (BM25, always available) vs. Phase 2 (hashing-trick
# embedding, requires `pip install dekko[search]`). See embedding.py's
# module docstring for why the latter isn't a pretrained model.
DEFAULT_SCORER = "lexical"
SCORER_CHOICES = ("lexical", "embedding")

# Lexical relevance dominates ranking; centrality only breaks
# near-ties between otherwise-comparable matches. Contrast with
# relevance.DEFAULT_W_REL (0.5), tuned for workset's already-curated
# candidate set where centrality carries most of the initial signal —
# here the query *is* the whole task, so there is no such prior.
SEARCH_W_REL = 0.85

_DOC_LIMIT = 80

# Natural-language question words a symbol's own name/doc would never
# contain. Deliberately separate from relevance._STOPWORDS, which is
# shared with --task blending elsewhere (workset/context/lean) and is
# kept minimal on purpose — this overlay only ever applies here.
_SEARCH_STOPWORDS = frozenset(
    {
        "what",
        "how",
        "does",
        "which",
        "find",
        "code",
        "that",
        "where",
        "who",
        "when",
        "why",
    }
)


@dataclass
class SearchHit:
    """One ranked search result.

    Attributes:
        symbol: The matched symbol.
        score: Blended score in ``[0, 1]`` (scorer relevance +
            structural centrality), the ranking key.
        relevance: The raw scorer component, pre-blend, for
            debuggability — BM25 lexical relevance by default, or
            embedding cosine similarity under ``--scorer embedding``
            (see ``embedding.EmbeddingScorer``).
    """

    symbol: Symbol
    score: float
    relevance: float


def parse_kinds(text: str | None) -> frozenset[str] | None:
    """Parse a comma-separated ``--kind``/``kind`` argument.

    Args:
        text: Raw comma-separated symbol kinds (``"function,class"``),
            or ``None``/empty for "all kinds".

    Returns:
        A frozenset of kind names, or ``None`` when unrestricted.
    """
    if not text:
        return None
    kinds = frozenset(
        piece.strip() for piece in text.split(",") if piece.strip()
    )
    return kinds or None


def _query_terms(query_text: str) -> tuple[str, ...]:
    """Normalized query terms, minus the search-only stopword overlay."""
    return tuple(
        t
        for t in relevance.normalize_terms(query_text)
        if t not in _SEARCH_STOPWORDS
    )


def _candidate_text(sym: Symbol) -> str:
    """Weighted searchable text: name > doc > signature.

    ``relevance.Candidate`` is deliberately one flat text field, so
    field weighting is approximated by term repetition at
    candidate-construction time rather than by widening the shared
    dataclass. Repeat counts (name x3, doc x2, signature x1) are a
    starting guess per the plan, not derived from tuning data.
    """
    name_part = f"{sym.name} {sym.qualname}"
    doc_part = sym.doc or ""
    sig_part = signature(sym)
    return " ".join([name_part] * 3 + [doc_part] * 2 + [sig_part])


# Attribute name for the per-``MapIndex``-instance candidate cache
# below. A plain string key (not a WeakKeyDictionary/id()-keyed global
# registry) because ``MapIndex`` is a non-frozen ``@dataclass`` and so
# has ``__hash__ = None`` — it can't be a dict key at all. Attaching
# the cache directly to the instance ties its lifetime to the index's
# own lifetime (no separate registry to leak or go stale on id()
# reuse) and needs no import beyond what's already here.
_CANDIDATE_CACHE_ATTR = "_dekko_search_candidate_cache"


def _build_candidates(
    index: MapIndex, kinds: frozenset[str] | None
) -> list[Candidate]:
    """Every symbol (optionally kind-filtered) as a search candidate.

    Cached on the ``index`` instance itself, keyed by ``kinds``, so a
    repeat ``rank()`` call against the *same, already-loaded*
    ``MapIndex`` — e.g. multiple ``dekko search`` calls served by one
    long-lived MCP session — skips rebuilding every candidate's text
    (an O(N) pass over the whole symbol table) after the first call.
    Safe because nothing mutates ``index.symbols_by_id`` after
    ``load_map``/``index_from_maps`` hands it back (both populate it
    only during construction); a single CLI invocation only ever calls
    this once per process either way, so the cache costs one dict
    lookup there and never pays off — it's the persisted-index case
    (Track J's territory) this exists for.
    """
    cache = getattr(index, _CANDIDATE_CACHE_ATTR, None)
    if cache is None:
        cache = {}
        setattr(index, _CANDIDATE_CACHE_ATTR, cache)
    cached = cache.get(kinds)
    if cached is not None:
        return cached
    candidates = [
        Candidate(id=s.id, text=_candidate_text(s), path=s.path)
        for s in index.symbols_by_id.values()
        if kinds is None or s.kind in kinds
    ]
    cache[kinds] = candidates
    return candidates


class _CoverageAdjustedScorer:
    """Wraps a scorer, discounting candidates with low query-term coverage.

    A scorer-agnostic post-processing layer (round-08 §2.3): a
    candidate that matches one query term very strongly (e.g. a short
    symbol whose name/doc repeats a common word) can otherwise outrank
    a candidate that covers every distinctive query term more
    lightly, under both BM25 and the hashing-trick embedding scorer
    alike — the raw relevance number alone can't tell the difference,
    so the correction is layered on top of whichever scorer produced
    it rather than duplicated inside each one (any future scorer
    inherits the fix automatically, instead of needing to remember to
    implement it itself). Deliberately separate from
    ``relevance.py``'s own per-scorer coverage discount (round-08
    §2.2, which fixes a different failure mode — a false ``1.00`` on a
    weak candidate field): this wraps *whatever* score a scorer
    already returned, compounding with any discount the scorer already
    applied internally.
    """

    def __init__(self, inner: relevance.Scorer) -> None:
        self._inner = inner

    def score(
        self, task: TaskContext, candidates: list[Candidate]
    ) -> dict[str, float]:
        """Score via the wrapped scorer, then discount by term coverage.

        Only engages for 2+-term queries — a one-term query has no
        "crowded out by a different term" failure mode to correct.
        The coverage discount is IDF-weighted (round-12 §3.13,
        :func:`relevance.idf_term_weights`/``weighted_term_coverage``)
        rather than a flat fraction, so missing a rare, distinctive
        query term costs more than missing a common one — this is a
        second, independent IDF computation from whatever the wrapped
        scorer may have used internally (e.g. :class:`BM25Scorer`
        already computes its own for the same reason), since this
        wrapper is deliberately scorer-agnostic and can't assume the
        inner scorer exposes one.
        """
        scores = self._inner.score(task, candidates)
        if len(task.terms) < 2:
            return scores
        term_weights = relevance.idf_term_weights(
            task.terms, [c.text for c in candidates]
        )
        return {
            c.id: scores.get(c.id, 0.0)
            * relevance.coverage_factor(
                relevance.weighted_term_coverage(
                    task.terms, c.text, term_weights
                )
            )
            for c in candidates
        }


def rank(
    index: MapIndex,
    query_text: str,
    kinds: frozenset[str] | None = None,
    scorer: relevance.Scorer | None = None,
) -> list[SearchHit]:
    """Rank every candidate symbol against a free-text query.

    A candidate with zero raw relevance is dropped before blending:
    ``blended_scores`` still returns a nonzero score for a
    structurally-central symbol with no term overlap at all (its
    centrality component alone), and surfacing that as a "search
    result" would be actively misleading for a tool whose whole job is
    text relevance.

    ``scorer`` is wrapped in :class:`_CoverageAdjustedScorer` for a
    2+-term query, discounting any candidate that only covers some of
    the query's distinct terms — otherwise a candidate matching one
    common term very strongly can outrank one that covers every term
    lightly, under any scorer (round-08 §2.3).

    Args:
        index: Loaded map index. Callers wanting to exclude test-path
            symbols should pass ``index.without_tests()``.
        query_text: Free-text description of the code being sought.
        kinds: Restrict candidates to these symbol kinds, or ``None``
            for all kinds.
        scorer: The relevance scorer to use; defaults to
            :class:`relevance.BM25Scorer` (Phase 1). Pass an
            ``embedding.EmbeddingScorer`` for Phase 2's opt-in
            ``--scorer embedding``.

    Returns:
        Hits sorted by descending blended score; ties broken by
        ``(path, start_line)`` for determinism.
    """
    candidates = _build_candidates(index, kinds)
    task = TaskContext(terms=_query_terms(query_text))
    if not candidates or not task.terms:
        return []
    scorer = _CoverageAdjustedScorer(scorer or BM25Scorer())
    raw_relevance = scorer.score(task, candidates)
    survivors = [c for c in candidates if raw_relevance.get(c.id, 0.0) > 0.0]
    if not survivors:
        return []
    centrality = {c.id: float(index.degree(c.id)) for c in survivors}
    blended = relevance.blended_scores(
        task, survivors, centrality, scorer=scorer, w_rel=SEARCH_W_REL
    )
    hits = [
        SearchHit(
            symbol=index.symbols_by_id[c.id],
            score=blended.get(c.id, 0.0),
            relevance=raw_relevance.get(c.id, 0.0),
        )
        for c in survivors
    ]
    hits.sort(key=lambda h: (-h.score, h.symbol.path, h.symbol.start_line))
    return hits


def _manifest(query_text: str, total: int) -> str:
    """The non-droppable header: query text and total candidate count.

    "N hits" reads as alarming even when the shown results are
    excellent -- it implies every one of them matched strongly. "N
    candidates scored" is honest about what the number actually means
    (everything considered, not everything relevant); the existing
    budget-omission line lower in the output already covers "showing
    top N" when results were trimmed.
    """
    noun = "candidate" if total == 1 else "candidates"
    return f'search: "{query_text}"  —  {total} {noun} scored'


def _hit_row(hit: SearchHit) -> str:
    """One droppable row: score, signature, kind, location, doc."""
    sym = hit.symbol
    sig = signature(sym)
    kind_suffix = "" if sym.kind in TYPE_KINDS else f"  ({sym.kind})"
    row = f"  {hit.score:.2f}  {sig}{kind_suffix}  {sym.path}:{sym.start_line}"
    if sym.doc:
        row += f"\n        {oneline(sym.doc, _DOC_LIMIT)}"
    return row


def _hit_json(hit: SearchHit) -> dict:
    """Structured rendering of one hit."""
    sym = hit.symbol
    return {
        "id": sym.id,
        "path": sym.path,
        "line": sym.start_line,
        "qualname": sym.qualname,
        "kind": sym.kind,
        "signature": signature(sym),
        "doc": sym.doc,
        "score": round(hit.score, 4),
    }


def _fit(
    query_text: str, hits: list[SearchHit], budget: int | None, limit: int
) -> tuple[list[SearchHit], object]:
    """Apply the shared budget over the manifest prefix plus hit rows."""
    prefix = _manifest(query_text, len(hits))
    rows = [_hit_row(h) for h in hits]
    kept_rows, meter = fit_to_budget(rows, budget, limit, prefix)
    return hits[: len(kept_rows)], meter


def _exclusion_note(
    hits: list[SearchHit], excluded_test_count: int
) -> str | None:
    """A hint when test-path exclusion may have hidden the real match.

    Fires only when there *were* excluded test-path symbols (the
    caller passes ``0`` when ``--include-tests`` was already given, so
    this never fires redundantly) and the surviving top hit is weak —
    below :data:`LOW_CONFIDENCE_THRESHOLD`, or there are no hits at
    all. A strong, confident top hit means exclusion almost certainly
    didn't matter, so staying silent there keeps the common case
    quiet (round-08 §2.2).

    Args:
        hits: The ranked hits, already scored (pre-budget-fit).
        excluded_test_count: Symbols dropped by ``.without_tests()``
            before ranking, or ``0`` if nothing was excluded.

    Returns:
        A one-line hint, or ``None`` when nothing is worth flagging.
    """
    if excluded_test_count <= 0:
        return None
    if hits and hits[0].score >= LOW_CONFIDENCE_THRESHOLD:
        return None
    noun = "symbol" if excluded_test_count == 1 else "symbols"
    return (
        f"{excluded_test_count} test-file {noun} excluded — "
        "low-confidence result, re-run with --include-tests"
    )


def _render_text(
    query_text: str,
    hits: list[SearchHit],
    budget: int | None,
    limit: int,
    note: str | None,
) -> int:
    """Render hits as text: manifest, ranked rows, cost footer, hint."""
    kept, meter = _fit(query_text, hits, budget, limit)
    print(_manifest(query_text, len(hits)))
    print()
    if not kept:
        print("(no matches)")
    else:
        for hit in kept:
            print(_hit_row(hit))
    print(meter.footer())
    if note:
        print(f"note: {note}")
    return EXIT_OK


def _render_json(
    query_text: str,
    hits: list[SearchHit],
    budget: int | None,
    limit: int,
    note: str | None,
) -> int:
    """Render hits as JSON, reflecting exactly what the budget kept."""
    kept, meter = _fit(query_text, hits, budget, limit)
    doc = {
        "query": query_text,
        "hits": [_hit_json(h) for h in kept],
        "meta": meter.as_dict(),
    }
    if note:
        doc["note"] = note
    print(json.dumps(doc, indent=2))
    return EXIT_OK


def _resolve_scorer(
    scorer_name: str, root: Path | None
) -> tuple[relevance.Scorer, embedding.EmbeddingCache | None]:
    """Build the requested scorer, or raise with a clear message.

    Args:
        scorer_name: One of :data:`SCORER_CHOICES`.
        root: Repository root, for the embedding cache's
            ``.dekko/embeddings.json``. ``None`` disables caching
            (every candidate is embedded fresh) but still works.

    Returns:
        ``(scorer, cache)`` — ``cache`` is ``None`` unless the
        embedding scorer was built with a persistable cache attached.

    Raises:
        ValueError: ``scorer_name`` isn't a known choice, or is
            ``"embedding"`` but the ``dekko[search]`` extra isn't
            installed.
    """
    if scorer_name == DEFAULT_SCORER:
        return BM25Scorer(), None
    if scorer_name != "embedding":
        raise ValueError(
            f"unknown --scorer {scorer_name!r} "
            f"(choose one of: {', '.join(SCORER_CHOICES)})"
        )
    if not embedding.available():
        raise ValueError(
            "--scorer embedding requires the 'dekko[search]' extra "
            "(`pip install dekko[search]`)"
        )
    cache = embedding.EmbeddingCache(embedding.load(root) if root else {})
    return embedding.EmbeddingScorer(cache=cache), cache


def run(
    index: MapIndex,
    query_text: str,
    kinds: frozenset[str] | None = None,
    limit: int = DEFAULT_LIMIT,
    budget: int | None = DEFAULT_BUDGET,
    as_json: bool = False,
    root: Path | None = None,
    scorer_name: str = DEFAULT_SCORER,
    excluded_test_count: int = 0,
) -> int:
    """Rank symbols by free-text relevance and render the result.

    Args:
        index: Loaded map index (already test-filtered by the caller
            if desired — see :func:`rank`).
        query_text: Free-text description of the code being sought.
        kinds: Restrict to these symbol kinds, or ``None`` for all.
        limit: Max hits to return.
        budget: Approximate token budget for the rendered rows, or
            ``None`` for unbounded.
        as_json: Emit structured JSON instead of text.
        root: Repository root. Only needed to persist an embedding
            cache under ``.dekko/`` when ``scorer_name`` is
            ``"embedding"`` — ignored for the default lexical scorer.
        scorer_name: One of :data:`SCORER_CHOICES`; defaults to
            :data:`DEFAULT_SCORER` (Phase 1's BM25 lexical scorer).
        excluded_test_count: Symbols the caller already dropped via
            ``.without_tests()`` before ``index`` was passed in, or
            ``0`` when nothing was excluded (including whenever
            ``--include-tests`` was given). Used only to decide
            whether to print the exclusion hint (round-08 §2.2); it
            plays no part in ranking.

    Returns:
        ``0`` on a completed search (a search matching nothing is a
        legitimate, non-error result — unlike ``query``'s
        ``EXIT_NOT_FOUND``, which means "you typo'd a name");
        :data:`EXIT_ERROR` when ``scorer_name`` can't be satisfied
        (unknown name, or ``embedding`` without the extra installed).
    """
    try:
        scorer, cache = _resolve_scorer(scorer_name, root)
    except ValueError as exc:
        print(f"dekko search: {exc}", file=sys.stderr)
        return EXIT_ERROR
    hits = rank(index, query_text, kinds, scorer=scorer)
    if cache is not None and root is not None:
        embedding.save(root, cache)
    note = _exclusion_note(hits, excluded_test_count)
    if as_json:
        return _render_json(query_text, hits, budget, limit, note)
    return _render_text(query_text, hits, budget, limit, note)
