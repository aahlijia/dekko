"""Optional embedding-based scorer for ``dekko search`` (Phase 2).

Gated behind the ``dekko[search]`` extra (``pip install dekko[search]``).
This module must import cleanly with or without that extra present —
:func:`available` is the only thing a caller checks before opting in,
mirroring how ``textutil.py`` isolates its optional ``tiktoken`` import
behind ``_tokenizer_mode()``/``_encoder()``. A missing extra never
causes a silent quality downgrade on an *explicit* ``--scorer
embedding`` request (``search.py`` surfaces a clear error instead); the
*default* scorer choice stays lexical, so a base install is completely
unaffected either way.

**Model choice.** Rather than a pretrained sentence-transformer model
(the plan's original sketch — ``sentence-transformers``, ~1GB+ once its
``torch`` dependency resolves, plus a model-weights download on first
use), this ships a deterministic "hashing trick" embedding: character
n-gram feature hashing with a signed random projection (Weinberger et
al. 2009; the same idea behind scikit-learn's ``HashingVectorizer``),
built on ``numpy`` alone. This keeps dekko's "no model download, fully
offline after ``pip install``" pitch intact (``README.md``'s "Why
dekko?", quoted verbatim in the plan's §8 "model choice tension") at
the cost of being closer to fuzzy subword/typo-tolerant similarity
than true semantic (synonym-level) matching — the plan's own
hashing-trick alternative, chosen here because §8 explicitly left the
model-choice decision to Phase 2's implementer rather than resolving
it speculatively. See ``.features/plans/SEMANTIC-SEARCH-PLAN.md`` §8
and the "Implementation status" section for the full reasoning and any
further deviations.

:class:`EmbeddingCache` mirrors :class:`cache.IncrementalCache`'s
read-old/write-new, hash-invalidated reuse pattern, persisted to
``.dekko/embeddings.json`` alongside the extraction cache.
"""

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from dekko.analysis import relevance
from dekko.storage.cache import CACHE_DIR, _tool_version, ensure_dir
from dekko.analysis.relevance import Candidate, TaskContext

EMBEDDING_CACHE_FILE = "embeddings.json"
EMBEDDING_CACHE_VERSION = 1

# Hashing-trick embedding parameters. Fixed, not user-facing flags —
# same treatment as relevance.py's BM25 constants. Changing either
# invalidates every cached vector (see `load`'s `dim` check).
_DIM = 256
_NGRAM_N = 3


@lru_cache(maxsize=1)
def _numpy() -> Any:
    """Lazily import ``numpy``, or ``None`` if it isn't installed.

    Cached so a missing/present extra is only probed once per process,
    same shape as ``textutil._encoder``.
    """
    try:
        import numpy

        return numpy
    except Exception:
        return None


def available() -> bool:
    """Whether the embedding scorer's dependency (``numpy``) is usable."""
    return _numpy() is not None


def _char_ngrams(term: str, n: int = _NGRAM_N) -> list[str]:
    """Boundary-padded character n-grams of one (already-stemmed) term."""
    padded = f"#{term}#"
    if len(padded) <= n:
        return [padded]
    return [padded[i : i + n] for i in range(len(padded) - n + 1)]


def _hash_gram(gram: str, dim: int) -> tuple[int, float]:
    """Stable ``(index, sign)`` for one n-gram via a keyless hash.

    Uses ``blake2b`` rather than Python's built-in ``hash()`` because
    the latter is randomized per-process (``PYTHONHASHSEED``) — the
    whole scorer must be deterministic across runs, both for the
    ``Scorer`` protocol's testability contract and so cached vectors
    from one process are valid to reuse in the next.
    """
    digest = hashlib.blake2b(gram.encode("utf-8"), digest_size=9).digest()
    index = int.from_bytes(digest[:8], "big") % dim
    sign = 1.0 if digest[8] & 1 else -1.0
    return index, sign


def _vectorize(text: str, dim: int = _DIM) -> Any:
    """Embed arbitrary text as an L2-normalized hashing-trick vector.

    Tokenizes with the same identifier-aware splitter and inflection
    stemmer :class:`relevance.BM25Scorer` uses (``retry``/``retries``/
    ``retrying`` collapse together here too), then hashes each stemmed
    term's character trigrams into a fixed-width signed projection.

    Args:
        text: Arbitrary text (a query, or a candidate's searchable
            text from ``search._candidate_text``).
        dim: Projection dimension.

    Returns:
        A ``dim``-length ``numpy`` float32 vector, L2-normalized (the
        zero vector when ``text`` has no usable terms).

    Raises:
        RuntimeError: If ``numpy`` is not installed. Callers should
            check :func:`available` first.
    """
    np = _numpy()
    if np is None:
        raise RuntimeError(
            "the embedding scorer requires the 'dekko[search]' extra "
            "(`pip install dekko[search]`)"
        )
    vec = np.zeros(dim, dtype=np.float32)
    for term in relevance._raw_terms(text):
        stem = relevance._stem(term)
        for gram in _char_ngrams(stem):
            index, sign = _hash_gram(gram, dim)
            vec[index] += sign
    norm = float(np.linalg.norm(vec))
    if norm > 0:
        vec = vec / norm
    return vec


def _text_hash(text: str) -> str:
    """Content hash of the exact text a vector was computed from."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _decode_vector(entry: dict) -> Any | None:
    """Rebuild a cached entry's vector, or ``None`` if malformed/unusable."""
    vector = entry.get("vector")
    if not isinstance(vector, list) or len(vector) != _DIM:
        return None
    np = _numpy()
    if np is None:
        return None
    return np.asarray(vector, dtype=np.float32)


class EmbeddingCache:
    """A read-old / write-new view over the per-symbol embedding cache.

    Mirrors :class:`cache.IncrementalCache`'s reuse/invalidate shape
    (``cache.py:68-136``): entries are keyed by candidate id and
    invalidated by a hash of the *exact text that was embedded*
    (``search._candidate_text``'s output), rather than a file content
    hash — a symbol's cached vector goes stale exactly when its name,
    doc, or signature changes (or the field-weighting recipe itself
    changes), with no separate content-hash plumbing needed since the
    embedded text already encodes everything relevant.

    Attributes:
        entries: Cache entries to persist after the run — populated by
            both reused and freshly embedded candidates.
        reused: Count of vectors served from the prior cache this run.
        embedded: Count of vectors freshly computed this run.
    """

    def __init__(self, old: dict[str, dict]) -> None:
        """Initialize with the entries loaded from a prior run.

        Args:
            old: Previous ``candidate_id -> {"hash", "vector"}``
                entries, or an empty dict to force every candidate to
                re-embed.
        """
        self._old = old
        self.entries: dict[str, dict] = {}
        self.reused = 0
        self.embedded = 0

    def get(self, candidate_id: str, text: str) -> Any | None:
        """Return the cached vector for unchanged candidate text.

        Args:
            candidate_id: The candidate's stable id.
            text: The candidate's current searchable text.

        Checks this run's own freshly-populated ``entries`` before
        falling back to the prior run's ``_old`` — a scorer is scored
        against overlapping candidate sets more than once per
        ``dekko search`` call (:func:`relevance.blended_scores` scores
        again internally), so without this in-run check every
        candidate's vector would be recomputed a second time even
        within the *same* call, defeating the point of caching for the
        common single-process case.

        Returns:
            The cached vector when a prior entry's hash matches
            ``text``, else ``None``.
        """
        current = self.entries.get(candidate_id)
        if current is not None and current.get("hash") == _text_hash(text):
            return _decode_vector(current)
        entry = self._old.get(candidate_id)
        if entry is None or entry.get("hash") != _text_hash(text):
            return None
        vector = _decode_vector(entry)
        if vector is None:
            return None
        self.entries[candidate_id] = entry
        self.reused += 1
        return vector

    def put(self, candidate_id: str, text: str, vector: Any) -> None:
        """Record a freshly computed vector for persistence.

        Args:
            candidate_id: The candidate's stable id.
            text: The exact text the vector was computed from.
            vector: The embedding vector (a ``numpy`` array).
        """
        self.entries[candidate_id] = {
            "hash": _text_hash(text),
            "vector": [round(float(x), 6) for x in vector],
        }
        self.embedded += 1


class EmbeddingScorer:
    """Hashing-trick embedding relevance, optionally cache-backed.

    Cosine similarity between the query vector and each candidate's
    vector, clamped to non-negative and top-normalized to ``[0, 1]``
    the same way :class:`relevance.BM25Scorer` normalizes — so the
    ``Scorer`` protocol's contract (``dict[str, float]`` in ``[0,
    1]``, all-zero when nothing matches) is identical regardless of
    which scorer ``search.py`` picked. Deterministic given a fixed
    cache; the only I/O is the optional cache read/write, done by the
    caller via :func:`load`/:func:`save`, not by this class.
    """

    DIM = _DIM

    def __init__(self, cache: EmbeddingCache | None = None) -> None:
        """Create a scorer, optionally backed by a reuse cache.

        Args:
            cache: An :class:`EmbeddingCache` to reuse/populate, or
                ``None`` to embed every candidate fresh each call.

        Raises:
            RuntimeError: If ``numpy`` is not installed. ``search.py``
                checks :func:`available` first and reports a clean
                CLI/MCP error rather than let this propagate; it's a
                defensive backstop for direct callers.
        """
        if not available():
            raise RuntimeError(
                "EmbeddingScorer requires the 'dekko[search]' extra "
                "(`pip install dekko[search]`)"
            )
        self._cache = cache

    def score(
        self, task: TaskContext, candidates: list[Candidate]
    ) -> dict[str, float]:
        """Score candidates by cosine similarity to the task's terms.

        Args:
            task: The task to rank against.
            candidates: Items to score.

        Returns:
            ``candidate.id -> score`` in ``[0, 1]``; all-zero when no
            candidate has any positive similarity to the query.
        """
        if not candidates:
            return {}
        if not task.terms:
            return {c.id: 0.0 for c in candidates}
        np = _numpy()
        query_vec = _vectorize(" ".join(task.terms))
        raw = {
            c.id: max(0.0, float(np.dot(query_vec, self._vector_for(c))))
            for c in candidates
        }
        top = max(raw.values(), default=0.0)
        if top <= 0:
            return {c.id: 0.0 for c in candidates}
        return {cid: value / top for cid, value in raw.items()}

    def _vector_for(self, candidate: Candidate) -> Any:
        """This candidate's vector, from the cache when available."""
        if self._cache is not None:
            cached = self._cache.get(candidate.id, candidate.text)
            if cached is not None:
                return cached
        vec = _vectorize(candidate.text)
        if self._cache is not None:
            self._cache.put(candidate.id, candidate.text, vec)
        return vec


def load(root: Path) -> dict[str, dict]:
    """Load the prior embedding cache entries for a repository.

    A cache written by a different dekko version, a different
    projection dimension, or the wrong format version is discarded, so
    an algorithm change always takes effect on the next run rather
    than silently reusing incompatible vectors.

    Args:
        root: Repository root.

    Returns:
        ``candidate_id -> entry`` mapping, or an empty dict when no
        usable cache exists.
    """
    path = root / CACHE_DIR / EMBEDDING_CACHE_FILE
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if doc.get("version") != EMBEDDING_CACHE_VERSION:
        return {}
    if doc.get("dim") != _DIM:
        return {}
    if doc.get("tool_version") != _tool_version():
        return {}
    symbols = doc.get("symbols")
    return symbols if isinstance(symbols, dict) else {}


def save(root: Path, cache: EmbeddingCache) -> None:
    """Persist an embedding cache under ``.dekko/``.

    Args:
        root: Repository root.
        cache: The cache whose ``entries`` should be written.
    """
    cache_dir = ensure_dir(root)
    doc = {
        "version": EMBEDDING_CACHE_VERSION,
        "tool_version": _tool_version(),
        "dim": _DIM,
        "symbols": cache.entries,
    }
    (cache_dir / EMBEDDING_CACHE_FILE).write_text(
        json.dumps(doc) + "\n", encoding="utf-8"
    )
