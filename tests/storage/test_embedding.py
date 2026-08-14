"""Phase 2: the optional hashing-trick embedding scorer (dekko[search]).

Split like test_tokenizer.py: the graceful-degradation contract
(``available()`` False, a clear ``RuntimeError``/CLI error, base tests
unaffected) is asserted unconditionally by monkeypatching the internal
numpy probe, since it must hold whether or not the extra happens to be
installed in the environment running this suite. The scorer's actual
numeric behavior is only exercised when ``numpy`` is truly importable,
behind an explicit ``pytest.importorskip("numpy")`` per test — mirrors
``test_tokenizer.py``'s ``pytest.importorskip("tiktoken")`` split for
the same reason (the base test env has no obligation to install any
optional extra).
"""

import pytest

from dekko.storage import embedding
from dekko.analysis.relevance import Candidate, TaskContext


def _reset_numpy_cache() -> None:
    embedding._numpy.cache_clear()


# --- graceful degradation (no numpy required to run these) -------------


def test_not_available_when_numpy_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(embedding, "_numpy", lambda: None)
    assert embedding.available() is False


def test_embedding_scorer_raises_clearly_without_numpy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(embedding, "_numpy", lambda: None)
    with pytest.raises(RuntimeError, match=r"dekko\[search\]"):
        embedding.EmbeddingScorer()


def test_vectorize_raises_clearly_without_numpy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(embedding, "_numpy", lambda: None)
    with pytest.raises(RuntimeError, match=r"dekko\[search\]"):
        embedding._vectorize("anything")


# --- pure helpers (no numpy) ---------------------------------------------


def test_char_ngrams_pads_and_slices() -> None:
    # A single-char term pads to exactly one trigram-length window.
    assert embedding._char_ngrams("a") == ["#a#"]
    assert embedding._char_ngrams("ab") == ["#ab", "ab#"]
    assert embedding._char_ngrams("retry") == [
        "#re",
        "ret",
        "etr",
        "try",
        "ry#",
    ]


def test_hash_gram_is_deterministic() -> None:
    a = embedding._hash_gram("try", 256)
    b = embedding._hash_gram("try", 256)
    assert a == b


def test_hash_gram_index_in_range() -> None:
    index, sign = embedding._hash_gram("xyz", 64)
    assert 0 <= index < 64
    assert sign in (1.0, -1.0)


def test_text_hash_is_deterministic_and_content_sensitive() -> None:
    assert embedding._text_hash("abc") == embedding._text_hash("abc")
    assert embedding._text_hash("abc") != embedding._text_hash("abd")


# --- real vectorization / scoring (requires numpy) ------------------------


def test_vectorize_is_deterministic() -> None:
    pytest.importorskip("numpy")
    a = embedding._vectorize("retry request handler")
    b = embedding._vectorize("retry request handler")
    assert list(a) == list(b)


def test_vectorize_is_l2_normalized_for_nonempty_text() -> None:
    np = pytest.importorskip("numpy")
    vec = embedding._vectorize("retry request handler")
    assert float(np.linalg.norm(vec)) == pytest.approx(1.0, abs=1e-5)


def test_vectorize_is_zero_for_empty_text() -> None:
    np = pytest.importorskip("numpy")
    vec = embedding._vectorize("")
    assert float(np.linalg.norm(vec)) == pytest.approx(0.0, abs=1e-9)


def test_embedding_scorer_ranks_related_text_over_unrelated() -> None:
    pytest.importorskip("numpy")
    task = TaskContext(terms=("retry", "http", "request"))
    cands = [
        Candidate(
            "hit",
            "retry_request retry a failed http request with backoff",
            "a.py",
        ),
        Candidate("miss", "connect to the database pool", "b.py"),
    ]
    scores = embedding.EmbeddingScorer().score(task, cands)
    assert scores["hit"] > scores["miss"]
    assert scores["hit"] == pytest.approx(1.0)


def test_embedding_scorer_all_zero_when_no_task_terms() -> None:
    pytest.importorskip("numpy")
    task = TaskContext()
    cands = [Candidate("a", "alpha", "a.py"), Candidate("b", "beta", "b.py")]
    assert embedding.EmbeddingScorer().score(task, cands) == {
        "a": 0.0,
        "b": 0.0,
    }


def test_embedding_scorer_empty_when_no_candidates() -> None:
    pytest.importorskip("numpy")
    task = TaskContext(terms=("x",))
    assert embedding.EmbeddingScorer().score(task, []) == {}


def test_embedding_scorer_scores_in_unit_range() -> None:
    pytest.importorskip("numpy")
    task = TaskContext(terms=("retry", "request"))
    cands = [
        Candidate("a", "retry request handler", "a.py"),
        Candidate("b", "unrelated logging config", "b.py"),
        Candidate("c", "retry retry retry request", "c.py"),
    ]
    scores = embedding.EmbeddingScorer().score(task, cands)
    assert all(0.0 <= v <= 1.0 for v in scores.values())


def test_embedding_scorer_suffix_broadening_matches_inflections() -> None:
    pytest.importorskip("numpy")
    task = TaskContext(terms=("retrying",))
    cands = [
        Candidate("hit", "retry_request retries the call", "a.py"),
        Candidate("miss", "unrelated database pool", "b.py"),
    ]
    scores = embedding.EmbeddingScorer().score(task, cands)
    assert scores["hit"] > scores["miss"]


# --- EmbeddingCache reuse/invalidate (requires numpy) ---------------------


def test_cache_put_then_get_returns_same_vector() -> None:
    pytest.importorskip("numpy")
    cache = embedding.EmbeddingCache({})
    vec = embedding._vectorize("some text")
    cache.put("sym", "some text", vec)
    got = cache.get("sym", "some text")
    assert got is not None
    # `put` rounds to 6 decimals for the on-disk cache, so the
    # round-tripped vector is close but not bit-identical.
    assert list(got) == pytest.approx(list(vec), abs=1e-5)
    assert cache.embedded == 1


def test_cache_get_misses_on_changed_text() -> None:
    pytest.importorskip("numpy")
    cache = embedding.EmbeddingCache({})
    vec = embedding._vectorize("original text")
    cache.put("sym", "original text", vec)
    assert cache.get("sym", "different text") is None


def test_cache_reuses_prior_run_entries() -> None:
    pytest.importorskip("numpy")
    vec = embedding._vectorize("some text")
    prior = {
        "sym": {
            "hash": embedding._text_hash("some text"),
            "vector": [round(float(x), 6) for x in vec],
        }
    }
    cache = embedding.EmbeddingCache(prior)
    got = cache.get("sym", "some text")
    assert got is not None
    assert cache.reused == 1
    assert cache.embedded == 0


def test_cache_get_ignores_malformed_entry() -> None:
    pytest.importorskip("numpy")
    prior = {"sym": {"hash": embedding._text_hash("x"), "vector": [1.0]}}
    cache = embedding.EmbeddingCache(prior)
    assert cache.get("sym", "x") is None


def test_cache_in_run_hit_does_not_double_count_reused() -> None:
    pytest.importorskip("numpy")
    cache = embedding.EmbeddingCache({})
    vec = embedding._vectorize("some text")
    cache.put("sym", "some text", vec)
    cache.get("sym", "some text")
    assert cache.reused == 0
    assert cache.embedded == 1


# --- load/save round-trip (requires numpy; touches disk) ------------------


def test_save_then_load_round_trips_entries(tmp_path) -> None:  # noqa: ANN001
    pytest.importorskip("numpy")
    cache = embedding.EmbeddingCache({})
    vec = embedding._vectorize("retry request")
    cache.put("sym", "retry request", vec)
    embedding.save(tmp_path, cache)
    loaded = embedding.load(tmp_path)
    assert "sym" in loaded
    assert loaded["sym"]["hash"] == embedding._text_hash("retry request")


def test_load_rejects_wrong_dim(tmp_path) -> None:  # noqa: ANN001
    pytest.importorskip("numpy")
    cache = embedding.EmbeddingCache({})
    vec = embedding._vectorize("x")
    cache.put("sym", "x", vec)
    embedding.save(tmp_path, cache)
    cache_dir = tmp_path / ".dekko"
    doc_path = cache_dir / embedding.EMBEDDING_CACHE_FILE
    import json as _json

    doc = _json.loads(doc_path.read_text())
    doc["dim"] = 1
    doc_path.write_text(_json.dumps(doc))
    assert embedding.load(tmp_path) == {}


def test_load_missing_file_returns_empty(tmp_path) -> None:  # noqa: ANN001
    assert embedding.load(tmp_path) == {}
