"""Task-aware relevance scoring (Pillar B).

dekko's pull tools rank by *structure* — fan-in, churn, call degree. That
is the right default when there is no task in hand, but once an agent is
working a concrete change the structurally-central symbol is often not
the relevant one. This module adds a second, *task-conditioned* signal:
given a free-text prompt and the working diff, score candidate symbols or
files by how relevant they are to that task, then blend the relevance
with the existing centrality so the two reinforce rather than replace.

The scorer is deliberately split into a **pure core** and a thin
**assembly helper**:

* :class:`LexicalScorer` and :func:`blended_scores` are pure — no I/O, no
  git, deterministic for a fixed input — so they are fully testable
  offline and stable under the ``chars4`` tokenizer pin.
* :func:`task_context` is the only part that touches git (best-effort);
  it gathers the diff and recent-file signals and degrades to a
  prompt-only context when there is no repo.

:class:`Scorer` is a ``Protocol`` so a scorer can be swapped without
touching any call site. :class:`BM25Scorer` (used by ``search.py``'s
``dekko search``) is the first concrete alternative to
:class:`LexicalScorer` — it stays a separate class rather than an
in-place upgrade so ``workset``/``context``/``lean``'s existing
``--task`` blend (and their pinned ranking tests) are untouched; see
``.features/plans/SEMANTIC-SEARCH-PLAN.md`` §3.2 for the tradeoff. A
future embedding-based scorer (Phase 2, optional ``dekko[search]``
extra) can follow the same seam. When no task is supplied the call
sites simply skip this module, so structural ranking is the zero-task
special case and existing output is byte-for-byte unchanged.

Both scorers' ``value / top`` min-max normalization is *relative to
the current batch* — whatever candidate happens to score highest gets
rescaled to exactly ``1.00``, even when that top score reflects a weak
field (e.g. a filtered-out candidate pool) rather than a genuinely
strong match. For a 2+-term task, both scorers additionally discount
their top-of-batch score by :func:`coverage_factor` on
:func:`term_coverage` — the fraction of query terms the candidate's
text actually contains — so a `1.00` requires covering the query, not
just outscoring a weak field (round-08 §2.2). ``search.rank`` layers a
second, scorer-agnostic use of the same coverage curve on top of
*any* scorer's output, correcting a different failure mode: one
lexically-dominant common query term crowding out a candidate that
covers every distinctive term more lightly (round-08 §2.3).
"""

import math
import re
import subprocess
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Protocol

# Default weight on the relevance signal when blending with centrality.
# 0.5 gives the task and the structure an equal say; raise it to let the
# prompt dominate, lower it to keep structure in charge.
DEFAULT_W_REL = 0.5

# Recency window for the recent-files boost, in days. Matches the spirit
# of the churn window the rest of dekko uses for "recently touched".
_RECENT_WINDOW_DAYS = 90

# Identifier-aware word splitter: keeps acronyms (``HTTP``), splits
# camelCase (``parseInput`` -> ``parse``, ``input``), and snake/kebab via
# the non-alnum fallback in :func:`normalize_terms`.
_WORD_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z0-9]+|[A-Z]+|[0-9]+")

# Tiny, fixed stop list. Kept minimal on purpose: enough to drop the
# noisiest English glue from a prompt without pretending to be a real
# stemmer, and small enough to stay obvious and deterministic.
_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "to",
        "of",
        "in",
        "on",
        "and",
        "or",
        "for",
        "is",
        "it",
        "this",
        "that",
        "with",
        "as",
        "at",
        "by",
        "be",
        "add",
        "use",
        "fix",
        "make",
    }
)

_MIN_TERM_LEN = 2
_MIN_PARTIAL_LEN = 3

# Cache size for the tokenization memoization below. Sized generously
# above the largest symbol count seen in the 7-repo eval (tensorflow,
# 157,845 symbols) so a full-corpus search over even the biggest real
# repo keeps every candidate's terms cached for the lifetime of a
# process, not just a rolling recent-window subset.
_TERM_CACHE_SIZE = 200_000


@lru_cache(maxsize=_TERM_CACHE_SIZE)
def _raw_terms(text: str) -> list[str]:
    """Identifier-aware terms, in order, without deduplication.

    The shared tokenization core behind :func:`normalize_terms` (which
    dedupes, for the set-based ``LexicalScorer``/query-term use case)
    and :class:`BM25Scorer`'s document-side term-frequency counting,
    where genuine repetition must actually inflate the count rather
    than collapse to bare presence/absence — otherwise BM25's ``f(t,
    d)`` term is always 0 or 1 and ``search.py``'s field-weighting-by-
    repetition trick (repeating a symbol's name in its candidate text
    to approximate a name-field boost) would silently do nothing.

    Memoized on the input text (mirrors ``textutil._count_fragment``'s
    ``lru_cache`` pattern): ``BM25Scorer.score`` previously re-ran this
    regex tokenization pass over *every* candidate's text on *every*
    call, with no reuse across repeated searches in the same process —
    the confirmed dominant cost on large repos (2.5 in the eval
    analysis). Callers must treat the returned list as read-only —
    identical input text returns the exact same cached list object.
    """
    terms = []
    for piece in _WORD_RE.findall(text):
        term = piece.lower()
        if len(term) < _MIN_TERM_LEN or term in _STOPWORDS:
            continue
        terms.append(term)
    return terms


def normalize_terms(text: str) -> list[str]:
    """Split text into lowercase, identifier-aware search terms.

    Splits camelCase and acronym runs, then folds on any non-alphanumeric
    boundary (snake_case, kebab-case, paths, punctuation). Drops terms
    shorter than two characters and a small fixed stop list, and
    deduplicates while preserving first-seen order for determinism.

    Args:
        text: Arbitrary text — a prompt, a signature, a path.

    Returns:
        Distinct search terms, in first-seen order.
    """
    seen: dict[str, None] = {}
    for term in _raw_terms(text):
        seen.setdefault(term, None)
    return list(seen)


@dataclass(frozen=True)
class TaskContext:
    """The live task an emission should be ranked against.

    Attributes:
        terms: Normalized prompt terms (may be empty).
        diff_paths: Repo-relative paths touched in the working diff;
            membership is a strong relevance boost.
        recent_paths: Repo-relative recently-changed paths; membership is
            a weaker boost.
    """

    terms: tuple[str, ...] = ()
    diff_paths: frozenset[str] = frozenset()
    recent_paths: frozenset[str] = frozenset()

    @property
    def is_empty(self) -> bool:
        """Whether there is no usable task signal at all."""
        return not (self.terms or self.diff_paths or self.recent_paths)


@dataclass(frozen=True)
class Candidate:
    """A rankable item (a symbol or a file).

    Attributes:
        id: Stable identity (symbol id or path) — the key in score maps.
        text: Searchable text (name, signature, path, doc one-liner).
        path: Repo-relative path the candidate belongs to, for the
            diff/recent path boosts.
    """

    id: str
    text: str
    path: str


class Scorer(Protocol):
    """A relevance scorer: task + candidates -> normalized [0, 1] scores.

    The seam for swapping the lexical scorer for an embedding-based one
    without touching callers.
    """

    def score(
        self, task: TaskContext, candidates: list[Candidate]
    ) -> dict[str, float]:
        """Score each candidate in ``[0, 1]``, keyed by ``Candidate.id``."""
        ...


class LexicalScorer:
    """Pure lexical relevance: term overlap plus a path boost.

    Raw relevance is exact term overlap, plus a half-weighted substring
    (partial) match, plus a boost when the candidate's file appears in the
    task's diff or recent set. Raw scores are min-normalized to ``[0, 1]``
    across the candidate set so the blend in :func:`blended_scores` mixes
    comparable ranges. Deterministic; no I/O.
    """

    DIFF_BOOST = 2.0
    RECENT_BOOST = 1.0
    PARTIAL_WEIGHT = 0.5

    def score(
        self, task: TaskContext, candidates: list[Candidate]
    ) -> dict[str, float]:
        """Score candidates by lexical overlap with the task.

        Args:
            task: The task to rank against.
            candidates: Items to score.

        Returns:
            ``candidate.id -> score`` in ``[0, 1]``; all-zero when no
            candidate matches the task at all. For a 2+-term task, the
            top-of-batch score is additionally discounted by
            :func:`coverage_factor` so "best of a weak field" can't
            renormalize to a misleading ``1.00`` the way plain
            ``value / top`` min-max normalization would on its own —
            see round-08 §2.2.
        """
        raw = {c.id: self._raw(task, c) for c in candidates}
        top = max(raw.values(), default=0.0)
        if top <= 0:
            return {c.id: 0.0 for c in candidates}
        normalized = {cid: value / top for cid, value in raw.items()}
        if len(task.terms) < 2:
            return normalized
        return {
            c.id: normalized[c.id]
            * coverage_factor(term_coverage(task.terms, c.text))
            for c in candidates
        }

    def _raw(self, task: TaskContext, candidate: Candidate) -> float:
        """Unnormalized relevance of one candidate."""
        terms = set(normalize_terms(candidate.text))
        exact = sum(1 for t in task.terms if t in terms)
        partial = sum(
            1
            for t in task.terms
            if t not in terms
            and len(t) >= _MIN_PARTIAL_LEN
            and any(t in term for term in terms)
        )
        return (
            exact
            + self.PARTIAL_WEIGHT * partial
            + self._path_boost(task, candidate.path)
        )

    def _path_boost(self, task: TaskContext, path: str) -> float:
        """Boost for a candidate whose file is in the diff or recent set."""
        if path in task.diff_paths:
            return self.DIFF_BOOST
        if path in task.recent_paths:
            return self.RECENT_BOOST
        return 0.0


# BM25 defaults (Robertson/Sparck-Jones); standard values, not exposed
# as user-facing flags — same treatment as LexicalScorer.DIFF_BOOST.
_BM25_K1 = 1.5
_BM25_B = 0.75

# Suffixes that typically mark an ``-es`` plural/verb form worth
# collapsing (``boxes`` -> ``box``, ``matches`` -> ``match``) as
# opposed to a bare trailing ``s`` (``names`` -> ``name``, handled by
# the plain ``s``-strip rule instead).
_ES_TRIGGERS = ("s", "x", "z", "ch", "sh")


def _stem(term: str) -> str:
    """Tiny, deterministic suffix stripper for inflection tolerance.

    Not a real stemmer — five rule-based cases so ``retry``/
    ``retries``/``retrying``/``retried`` all collapse to the same
    matching key, per the search feature plan's "obvious and
    deterministic over linguistically complete" philosophy (matches
    :data:`_STOPWORDS`'s own stated bar). Used only internally by
    :class:`BM25Scorer` for term/document-frequency grouping; never
    changes what :func:`normalize_terms` returns publicly.

    Args:
        term: An already-normalized (lowercase) search term.

    Returns:
        The stemmed key, or ``term`` unchanged when no rule applies.
    """
    if len(term) <= 4:
        return term
    if term.endswith("ies"):
        return term[:-3] + "y"
    if term.endswith("ied"):
        return term[:-3] + "y"
    if term.endswith("ing") and len(term) > 6:
        return term[:-3]
    if term.endswith("es") and term[:-2].endswith(_ES_TRIGGERS):
        return term[:-2]
    if term.endswith("ed"):
        return term[:-2]
    if term.endswith("s") and not term.endswith("ss"):
        return term[:-1]
    return term


# Coverage-factor curve: ``floor + scale * coverage``. Full coverage
# (every query term present) leaves the score unchanged (``1.0``);
# zero coverage discounts to the floor rather than zeroing outright —
# a zero-coverage candidate was already dropped wherever that matters
# (e.g. ``search.rank``'s zero-raw-relevance filter), so this only
# needs to discount a weak match, not eliminate it. Shared, tunable
# constants rather than inlined literals so both call sites below (and
# ``search.py``'s separate, scorer-agnostic use of the same curve —
# round-08 §2.3) stay in lockstep if the curve is ever retuned.
_COVERAGE_FLOOR = 0.4
_COVERAGE_SCALE = 0.6


def term_coverage(terms: tuple[str, ...], text: str) -> float:
    """Fraction of ``terms`` present in ``text``, exact or stemmed.

    Symmetric inflection tolerance: a query term matches a candidate
    token when either side's stem equals the other's raw or stemmed
    form (``retries`` in the query matches a candidate token
    ``retry``, and vice versa), so coverage isn't sensitive to which
    side happens to carry the inflected spelling.

    Shared by :class:`LexicalScorer` (discounting a false ``1.00`` on
    a weak field, round-08 §2.2) and ``search.rank`` (discounting a
    lexically-dominant common term that crowds out a candidate
    covering every distinctive query term, round-08 §2.3) — one shared
    notion of "how much of the query does this text actually cover"
    so both fixes agree on what coverage means. A flat special case of
    :func:`weighted_term_coverage` (every term weighted equally); see
    that function for the IDF-weighted variant :class:`BM25Scorer`
    uses instead (round-12 §3.13), which this delegates to so the two
    can never drift apart.

    Args:
        terms: Normalized query terms (already stopword-filtered).
        text: Candidate text to check coverage against.

    Returns:
        ``hits / len(terms)`` in ``[0, 1]``; ``1.0`` when ``terms`` is
        empty (nothing to fail to cover).
    """
    return weighted_term_coverage(terms, text)


def weighted_term_coverage(
    terms: tuple[str, ...],
    text: str,
    term_weights: dict[str, float] | None = None,
) -> float:
    """IDF-weighted fraction of ``terms`` present in ``text``.

    Like :func:`term_coverage`, but a missing rare/distinctive term
    costs more than a missing common one, when ``term_weights`` (e.g.
    each query term's BM25 IDF over the current candidate batch, from
    :func:`idf_term_weights`) is supplied. A term absent from
    ``term_weights`` falls back to a flat weight of ``1.0`` for that
    term specifically; passing ``term_weights=None`` (the default)
    makes every term flat, which is numerically identical to
    :func:`term_coverage` — the two must never diverge, since
    :class:`LexicalScorer` (no natural corpus-wide IDF signal of its
    own) relies on that equivalence by continuing to call the
    unweighted name.

    Round-12 §3.13: introduced because coverage-fraction ties don't
    discriminate a candidate missing a rare, distinguishing term (e.g.
    "yaml" in a Java/Kotlin codebase) from one missing a common term
    (e.g. "parse") — both cost the same under a flat fraction, even
    though BM25's own IDF machinery already knows the former is a more
    telling miss than the latter.

    Args:
        terms: Normalized query terms (already stopword-filtered).
        text: Candidate text to check coverage against.
        term_weights: Per-term importance weight (typically IDF),
            keyed by the raw entries of ``terms``. ``None`` (or a
            missing entry for a given term) means "flat, weight 1.0."

    Returns:
        ``hit_weight / total_weight`` in ``[0, 1]``; ``1.0`` when
        ``terms`` is empty or every weight is non-positive (nothing
        meaningful left to fail to cover).
    """
    if not terms:
        return 1.0
    weights = term_weights or {}
    present = set(normalize_terms(text))
    stemmed_present = {_stem(t) for t in present}
    total = sum(weights.get(t, 1.0) for t in terms)
    if total <= 0:
        return 1.0
    hit_weight = sum(
        weights.get(t, 1.0)
        for t in terms
        if t in present or _stem(t) in stemmed_present
    )
    return hit_weight / total


def coverage_factor(coverage: float) -> float:
    """Map a term-coverage fraction to a bounded ``[floor, 1.0]`` scale.

    Args:
        coverage: A term-coverage fraction in ``[0, 1]``, typically
            from :func:`term_coverage`.

    Returns:
        ``_COVERAGE_FLOOR + _COVERAGE_SCALE * coverage``.
    """
    return _COVERAGE_FLOOR + _COVERAGE_SCALE * coverage


@lru_cache(maxsize=_TERM_CACHE_SIZE)
def _stemmed_terms(text: str) -> tuple[str, ...]:
    """Tokenized and stemmed terms for one candidate's text, cached.

    The exact per-candidate sequence :class:`BM25Scorer` needs for its
    ``doc_terms`` table. Combines :func:`_raw_terms` (itself cached)
    with :func:`_stem` and memoizes the combination too, so a repeat
    ``BM25Scorer.score`` call against the same candidate text skips
    both the tokenization *and* the stemming pass, not just the
    former.
    """
    return tuple(_stem(t) for t in _raw_terms(text))


def _idf(n: int, n_t: int) -> float:
    """BM25's smoothed inverse document frequency for one term.

    Always non-negative: the ``+ 1`` inside the log keeps its argument
    at or above ``1`` for any ``0 <= n_t <= n``, so a term appearing
    in every candidate contributes ~0 weight instead of going
    negative. The one IDF formula in this module -- :meth:`BM25Scorer.
    _bm25` and :func:`idf_term_weights` both call this rather than
    each inlining their own copy, so a future retune only has one
    place to change.

    Args:
        n: Candidate batch size (corpus size for this computation).
        n_t: Number of candidates containing the term.

    Returns:
        The smoothed IDF value.
    """
    return math.log((n - n_t + 0.5) / (n_t + 0.5) + 1)


def idf_term_weights(
    terms: tuple[str, ...], texts: list[str]
) -> dict[str, float]:
    """Per-``terms`` entry BM25 IDF weight across a batch of texts.

    For use with :func:`weighted_term_coverage`: a term rare across
    ``texts`` gets a higher weight (missing it costs the coverage
    discount more) than a term that's common. :class:`BM25Scorer`
    already builds an equivalent document-frequency table internally
    for scoring and reuses it directly instead of calling this a
    second time (see its ``score()``); this standalone version exists
    for callers with no such internal state of their own --
    ``search.py``'s scorer-agnostic ``_CoverageAdjustedScorer``
    wrapper, which discounts whatever scorer it's given (lexical,
    embedding, or otherwise) and needs to derive the same notion of
    term rarity independently, from the same candidate batch.

    Args:
        terms: Normalized query terms (already stopword-filtered).
        texts: Every candidate's searchable text in the current batch
            -- the corpus this IDF is computed relative to.

    Returns:
        ``term -> idf`` for every entry in ``terms``, using
        :func:`_stem`-folded document-frequency counting so it agrees
        with :func:`weighted_term_coverage`'s own inflection
        tolerance.
    """
    if not terms:
        return {}
    n = len(texts)
    if n == 0:
        return dict.fromkeys(terms, 1.0)
    stemmed_docs = [set(_stemmed_terms(text)) for text in texts]
    doc_freq = {
        stem: sum(1 for doc in stemmed_docs if stem in doc)
        for stem in {_stem(t) for t in terms}
    }
    return {t: _idf(n, doc_freq[_stem(t)]) for t in terms}


class BM25Scorer:
    """BM25 lexical relevance, recomputed fresh over each candidate batch.

    Unlike :class:`LexicalScorer`'s exact-overlap count, a query term's
    contribution is weighted by how rare it is across the *batch*
    (inverse document frequency) and normalized for candidate length,
    so a rare, distinguishing term outweighs a common one and a short,
    precisely-matching candidate isn't buried by a long one that
    contains the term only incidentally. Term matching additionally
    folds simple inflections together via :func:`_stem` (``retry`` /
    ``retries`` / ``retrying``), on both the query and candidate side.
    Path boost (diff/recent) is layered on after BM25 normalization,
    same as :class:`LexicalScorer`. Deterministic; no I/O.
    """

    K1 = _BM25_K1
    B = _BM25_B
    DIFF_BOOST = LexicalScorer.DIFF_BOOST
    RECENT_BOOST = LexicalScorer.RECENT_BOOST

    def score(
        self, task: TaskContext, candidates: list[Candidate]
    ) -> dict[str, float]:
        """Score candidates by BM25 relevance to the task's terms.

        Args:
            task: The task to rank against.
            candidates: Items to score (also the corpus for IDF/avgdl).

        Returns:
            ``candidate.id -> score`` in ``[0, 1]``; all-zero when no
            candidate matches any query term at all. For a 2+-term
            task, the top-of-batch score is additionally discounted
            by :func:`coverage_factor` on an *IDF-weighted* coverage
            fraction (round-12 §3.13; :func:`weighted_term_coverage`)
            rather than a flat one, so missing a rare, distinctive
            query term costs more than missing a common one — "best
            of a weak field" can't renormalize to a misleading
            ``1.00`` (round-08 §2.2) *and* a coverage tie between two
            candidates now breaks toward the more specific match.
        """
        if not candidates:
            return {}
        if not task.terms:
            return {c.id: 0.0 for c in candidates}
        query_terms = [_stem(t) for t in task.terms]
        doc_terms = {c.id: _stemmed_terms(c.text) for c in candidates}
        lengths = {cid: len(terms) for cid, terms in doc_terms.items()}
        n = len(candidates)
        avgdl = sum(lengths.values()) / n if n else 0.0
        doc_freq = {
            qt: sum(1 for terms in doc_terms.values() if qt in terms)
            for qt in set(query_terms)
        }
        raw = {
            c.id: self._bm25(
                c, task, query_terms, doc_terms, doc_freq, lengths, n, avgdl
            )
            for c in candidates
        }
        top = max(raw.values(), default=0.0)
        if top <= 0:
            return {c.id: 0.0 for c in candidates}
        normalized = {cid: value / top for cid, value in raw.items()}
        if len(task.terms) < 2:
            return normalized
        # Reuse this same batch's already-computed doc_freq for the
        # coverage weights, rather than calling idf_term_weights (which
        # would redo an equivalent document-frequency pass) — the two
        # agree because both are keyed by _stem(t) over the same
        # candidate batch.
        term_weights = {t: _idf(n, doc_freq[_stem(t)]) for t in task.terms}
        return {
            c.id: normalized[c.id]
            * coverage_factor(
                weighted_term_coverage(task.terms, c.text, term_weights)
            )
            for c in candidates
        }

    def _bm25(
        self,
        candidate: Candidate,
        task: TaskContext,
        query_terms: list[str],
        doc_terms: dict[str, tuple[str, ...]],
        doc_freq: dict[str, int],
        lengths: dict[str, int],
        n: int,
        avgdl: float,
    ) -> float:
        """Unnormalized BM25 score of one candidate against the query."""
        freq = Counter(doc_terms[candidate.id])
        dl = lengths[candidate.id]
        norm_len = (dl / avgdl) if avgdl else 0.0
        total = 0.0
        for qt in query_terms:
            f = freq.get(qt, 0)
            if f == 0:
                continue
            n_t = doc_freq[qt]
            idf = _idf(n, n_t)
            denom = f + self.K1 * (1 - self.B + self.B * norm_len)
            total += idf * (f * (self.K1 + 1)) / denom
        return total + self._path_boost(task, candidate.path)

    def _path_boost(self, task: TaskContext, path: str) -> float:
        """Boost for a candidate whose file is in the diff or recent set."""
        if path in task.diff_paths:
            return self.DIFF_BOOST
        if path in task.recent_paths:
            return self.RECENT_BOOST
        return 0.0


def _min_max(values: dict[str, float]) -> dict[str, float]:
    """Min-max normalize a score map to ``[0, 1]``.

    A flat input (every value equal, including empty) normalizes to all
    zeros, so a degenerate signal contributes nothing to the blend and
    the call site's secondary sort key keeps ordering deterministic.
    """
    if not values:
        return {}
    lo = min(values.values())
    hi = max(values.values())
    if hi <= lo:
        return dict.fromkeys(values, 0.0)
    span = hi - lo
    return {key: (value - lo) / span for key, value in values.items()}


def blended_scores(
    task: TaskContext,
    candidates: list[Candidate],
    centrality: dict[str, float],
    *,
    scorer: Scorer | None = None,
    w_rel: float = DEFAULT_W_REL,
) -> dict[str, float]:
    """Blend task relevance with structural centrality.

    ``blended = w_rel * relevance + (1 - w_rel) * centrality``, both terms
    normalized to ``[0, 1]`` over the candidate set. Higher means more
    important (survives a budget longer / ranks earlier). Pure and
    deterministic for a fixed candidate order.

    Args:
        task: The task to rank against.
        candidates: Items to score.
        centrality: Raw structural score per ``candidate.id`` (fan-in,
            churn-weighted centrality, call degree — caller's choice);
            min-max normalized here before blending.
        scorer: Relevance scorer; defaults to :class:`LexicalScorer`.
        w_rel: Weight on the relevance term in ``[0, 1]``.

    Returns:
        ``candidate.id -> blended score`` in ``[0, 1]``.
    """
    scorer = scorer or LexicalScorer()
    relevance = scorer.score(task, candidates)
    central_norm = _min_max(centrality)
    w_central = 1.0 - w_rel
    return {
        c.id: w_rel * relevance.get(c.id, 0.0)
        + w_central * central_norm.get(c.id, 0.0)
        for c in candidates
    }


def _git_diff_paths(root: Path) -> frozenset[str]:
    """Repo-relative paths in the working diff (staged + unstaged).

    Best-effort: any git failure (no repo, git missing) yields an empty
    set so the caller degrades to a prompt-only task context.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "diff", "HEAD", "--name-only"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return frozenset()
    if proc.returncode != 0:
        return frozenset()
    return frozenset(
        line.strip() for line in proc.stdout.splitlines() if line.strip()
    )


def task_context(
    prompt: str | None,
    root: Path,
    *,
    window_days: int = _RECENT_WINDOW_DAYS,
) -> TaskContext:
    """Assemble a :class:`TaskContext` from a prompt and the repo state.

    The prompt becomes the term set; the working diff and recently-changed
    files become the path-boost sets. Every git read is best-effort, so a
    non-repo or git-less environment yields a prompt-only context rather
    than failing.

    Args:
        prompt: Free-text task description, or ``None``.
        root: Repository root, for the diff and recency reads.
        window_days: Recency window for the recent-files boost.

    Returns:
        The assembled task context (possibly empty).
    """
    from . import summary

    terms = tuple(normalize_terms(prompt)) if prompt else ()
    diff_paths = _git_diff_paths(root)
    recent_paths = frozenset(summary.file_churn(root, window_days))
    return TaskContext(
        terms=terms, diff_paths=diff_paths, recent_paths=recent_paths
    )
