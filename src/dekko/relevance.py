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

:class:`Scorer` is a ``Protocol`` so a future embedding-based scorer can
replace :class:`LexicalScorer` without touching any call site (noted
enhancement; not built). When no task is supplied the call sites simply
skip this module, so structural ranking is the zero-task special case and
existing output is byte-for-byte unchanged.
"""

import re
import subprocess
from dataclasses import dataclass
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
    for piece in _WORD_RE.findall(text):
        term = piece.lower()
        if len(term) < _MIN_TERM_LEN or term in _STOPWORDS:
            continue
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
            candidate matches the task at all.
        """
        raw = {c.id: self._raw(task, c) for c in candidates}
        top = max(raw.values(), default=0.0)
        if top <= 0:
            return {c.id: 0.0 for c in candidates}
        return {cid: value / top for cid, value in raw.items()}

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
