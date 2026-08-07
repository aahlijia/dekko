"""Pillar B: task-aware relevance scoring and the --task blend.

The pure core (:mod:`dekko.relevance`) is tested offline and
deterministically; the three blend points (lean, workset, context) are
tested for both the task-aware ordering and byte-for-byte backward
compatibility when no task is supplied.
"""

from pathlib import Path

from dekko import cli, contextpack, query, relevance, render_lean, workset
from dekko.mapfile import MapIndex, load_map
from dekko.relevance import (
    BM25Scorer,
    Candidate,
    LexicalScorer,
    TaskContext,
    blended_scores,
    normalize_terms,
)

from conftest import RepoFactory

# --- pure core: normalize_terms --------------------------------------


def test_normalize_splits_camel_and_snake() -> None:
    terms = set(normalize_terms("parseInput load_config"))
    assert {"parse", "input", "load", "config"} <= terms


def test_normalize_drops_stopwords_and_short_tokens() -> None:
    terms = normalize_terms("fix the LoginForm bug a x")
    assert "fix" not in terms  # stop word
    assert "the" not in terms  # stop word
    assert "x" not in terms  # too short
    assert "login" in terms
    assert "form" in terms
    assert "bug" in terms


def test_normalize_dedupes_preserving_order() -> None:
    assert normalize_terms("config config load config") == ["config", "load"]


# --- pure core: LexicalScorer ----------------------------------------


def test_exact_overlap_outranks_no_match() -> None:
    task = TaskContext(terms=("login", "form"))
    cands = [
        Candidate("hit", "login form handler", "a.py"),
        Candidate("miss", "database pool", "b.py"),
    ]
    scores = LexicalScorer().score(task, cands)
    assert scores["hit"] == 1.0  # normalized top
    assert scores["miss"] == 0.0


def test_no_candidate_matches_is_all_zero() -> None:
    task = TaskContext(terms=("nonexistent",))
    cands = [Candidate("a", "alpha", "a.py"), Candidate("b", "beta", "b.py")]
    assert LexicalScorer().score(task, cands) == {"a": 0.0, "b": 0.0}


def test_partial_substring_match_scores_above_zero() -> None:
    task = TaskContext(terms=("auth",))
    cands = [
        Candidate("hit", "authenticate user", "a.py"),
        Candidate("miss", "logout", "b.py"),
    ]
    scores = LexicalScorer().score(task, cands)
    assert scores["hit"] > scores["miss"] == 0.0


def test_diff_path_boost_beats_recent_beats_none() -> None:
    task = TaskContext(
        terms=(),
        diff_paths=frozenset({"a.py"}),
        recent_paths=frozenset({"b.py"}),
    )
    cands = [
        Candidate("diff", "x", "a.py"),
        Candidate("recent", "y", "b.py"),
        Candidate("cold", "z", "c.py"),
    ]
    scores = LexicalScorer().score(task, cands)
    assert scores["diff"] > scores["recent"] > scores["cold"] == 0.0


# --- pure core: BM25Scorer --------------------------------------------


def test_bm25_exact_match_outranks_no_overlap() -> None:
    task = TaskContext(terms=("login", "form"))
    cands = [
        Candidate("hit", "login form handler", "a.py"),
        Candidate("miss", "database pool", "b.py"),
    ]
    scores = BM25Scorer().score(task, cands)
    assert scores["hit"] > 0.0
    assert scores["miss"] == 0.0


def test_bm25_no_candidate_matches_is_all_zero() -> None:
    task = TaskContext(terms=("nonexistent",))
    cands = [Candidate("a", "alpha", "a.py"), Candidate("b", "beta", "b.py")]
    assert BM25Scorer().score(task, cands) == {"a": 0.0, "b": 0.0}


def test_bm25_rare_term_outweighs_common_term() -> None:
    # "retry" appears in every candidate (common -> low IDF); "auth"
    # appears in exactly one (rare -> high IDF). The concrete, testable
    # difference BM25 adds over LexicalScorer's plain overlap count —
    # see the plan's §3.2/§7 rationale for picking BM25 over a naive
    # count.
    task = TaskContext(terms=("retry", "auth"))
    cands = [
        Candidate("common_only", "retry retry retry", "a.py"),
        Candidate("rare_hit", "retry auth", "b.py"),
        Candidate("also_common", "retry logic here", "c.py"),
    ]
    scores = BM25Scorer().score(task, cands)
    assert scores["rare_hit"] > scores["common_only"]
    assert scores["rare_hit"] > scores["also_common"]


def test_bm25_short_precise_match_not_buried_by_long_candidate() -> None:
    task = TaskContext(terms=("retry",))
    cands = [
        Candidate("short_precise", "retry", "a.py"),
        Candidate(
            "long_incidental",
            "retry " + " ".join(f"word{i}" for i in range(40)),
            "b.py",
        ),
    ]
    scores = BM25Scorer().score(task, cands)
    assert scores["short_precise"] > scores["long_incidental"]


def test_bm25_suffix_broadening_matches_inflections() -> None:
    task = TaskContext(terms=("retrying",))
    cands = [
        Candidate("hit", "retry_request retries the call", "a.py"),
        Candidate("miss", "unrelated database pool", "b.py"),
    ]
    scores = BM25Scorer().score(task, cands)
    assert scores["hit"] > 0.0
    assert scores["miss"] == 0.0


# --- round-08 §2.2: coverage-factor discount on top-of-batch --------
#
# Both scorers' ``value / top`` min-max normalization rescales
# whatever survives filtering to exactly 1.00, regardless of how weak
# its actual match is. Reproduces the awesome-go shape: a query
# ("getAllFlaggedRepositories" -> get/all/flagged/repositories) whose
# only genuinely relevant symbol was filtered out upstream (e.g. by
# ``--include-tests`` defaulting to off), leaving an unrelated symbol
# that only shares one incidental term ("all") as the new best-in-
# batch. That symbol should no longer renormalize to a confident 1.00.


def test_term_coverage_is_fraction_of_terms_present() -> None:
    assert relevance.term_coverage(("get", "all"), "mkdir all things") == 0.5
    assert (
        relevance.term_coverage(
            ("get", "all", "flagged", "repositories"), "mkdir all things"
        )
        == 0.25
    )


def test_term_coverage_matches_stemmed_inflections_either_direction() -> None:
    # Query term inflected, candidate token bare.
    assert relevance.term_coverage(("retries",), "retry handler") == 1.0
    # Candidate token inflected, query term bare.
    assert relevance.term_coverage(("retry",), "retries handler") == 1.0


def test_term_coverage_empty_terms_is_full_coverage() -> None:
    assert relevance.term_coverage((), "anything") == 1.0


def test_coverage_factor_is_bounded_floor_to_one() -> None:
    assert relevance.coverage_factor(1.0) == 1.0
    assert relevance.coverage_factor(0.0) == relevance._COVERAGE_FLOOR
    assert relevance._COVERAGE_FLOOR < relevance.coverage_factor(0.5) < 1.0


def test_lexical_weak_field_top_hit_no_longer_reads_as_confident() -> None:
    # 4-term query; only "mkdir_all" shares one incidental term ("all")
    # with it — the real match isn't in this batch at all (as if it
    # had been filtered out upstream), so the best-in-batch score
    # should read as weak, not a confident 1.00.
    task = TaskContext(terms=("get", "all", "flagged", "repositories"))
    cands = [Candidate("mkdir_all", "mkdir_all creates directories", "a.py")]
    scores = LexicalScorer().score(task, cands)
    assert 0.0 < scores["mkdir_all"] < 1.0


def test_lexical_full_coverage_top_hit_still_reads_as_confident() -> None:
    # Same query, but now the genuinely relevant symbol is present and
    # covers every term — no regression for a real, complete match.
    task = TaskContext(terms=("get", "all", "flagged", "repositories"))
    cands = [
        Candidate(
            "get_all_flagged_repositories",
            "getAllFlaggedRepositories get all flagged repositories",
            "a.py",
        ),
        Candidate("mkdir_all", "mkdir_all creates directories", "b.py"),
    ]
    scores = LexicalScorer().score(task, cands)
    assert scores["get_all_flagged_repositories"] == 1.0
    assert scores["mkdir_all"] < scores["get_all_flagged_repositories"]


def test_lexical_single_term_query_is_unaffected_by_coverage_discount() -> (
    None
):
    # Single-term queries have no "crowded field" failure mode — a
    # top-of-batch match with 1/1 coverage stays at a full 1.00.
    task = TaskContext(terms=("retry",))
    cands = [Candidate("hit", "retry handler", "a.py")]
    scores = LexicalScorer().score(task, cands)
    assert scores["hit"] == 1.0


def test_bm25_weak_field_top_hit_no_longer_reads_as_confident() -> None:
    task = TaskContext(terms=("get", "all", "flagged", "repositories"))
    cands = [Candidate("mkdir_all", "mkdir_all creates directories", "a.py")]
    scores = BM25Scorer().score(task, cands)
    assert 0.0 < scores["mkdir_all"] < 1.0


def test_bm25_full_coverage_top_hit_still_reads_as_confident() -> None:
    task = TaskContext(terms=("get", "all", "flagged", "repositories"))
    cands = [
        Candidate(
            "get_all_flagged_repositories",
            "getAllFlaggedRepositories get all flagged repositories",
            "a.py",
        ),
        Candidate("mkdir_all", "mkdir_all creates directories", "b.py"),
    ]
    scores = BM25Scorer().score(task, cands)
    assert scores["get_all_flagged_repositories"] == 1.0
    assert scores["mkdir_all"] < scores["get_all_flagged_repositories"]


# --- 2.5: tokenization memoization (search/BM25 performance) ---------
#
# Track F (test-repos/reports/IMPLEMENTATION-PLAN.md #2.5): BM25Scorer
# used to re-tokenize every candidate's text from scratch on every
# call, with no reuse across repeated searches in the same process —
# the dominant cost on large repos. These assert the cache is actually
# hit, not just present, and that results stay identical either way.


def _large_candidate_batch(n: int) -> list[Candidate]:
    """``n`` distinct, moderately long candidates for cache-hit checks."""
    return [
        Candidate(
            id=f"sym{i}",
            text=f"handle_request_{i} process retry logic for item {i} "
            f"with backoff and timeout handling number {i}",
            path=f"src/mod{i}.py",
        )
        for i in range(n)
    ]


def test_raw_terms_is_memoized_across_calls() -> None:
    relevance._raw_terms.cache_clear()
    text = "retry_request handles the failed http call"
    first = relevance._raw_terms(text)
    before = relevance._raw_terms.cache_info().hits
    second = relevance._raw_terms(text)
    after = relevance._raw_terms.cache_info().hits
    assert second is first  # same cached list object, not rebuilt
    assert after == before + 1


def test_stemmed_terms_is_memoized_across_calls() -> None:
    relevance._stemmed_terms.cache_clear()
    text = "retrying requests with exponential backoff"
    first = relevance._stemmed_terms(text)
    before = relevance._stemmed_terms.cache_info().hits
    second = relevance._stemmed_terms(text)
    after = relevance._stemmed_terms.cache_info().hits
    assert second == first
    assert after == before + 1


def test_bm25_second_score_call_reuses_cached_tokenization() -> None:
    relevance._raw_terms.cache_clear()
    relevance._stemmed_terms.cache_clear()
    task = TaskContext(terms=("retry", "backoff"))
    cands = _large_candidate_batch(2000)

    first = BM25Scorer().score(task, cands)
    hits_after_first = relevance._stemmed_terms.cache_info().hits

    second = BM25Scorer().score(task, cands)
    hits_after_second = relevance._stemmed_terms.cache_info().hits

    # Every candidate's text is identical between the two calls, so a
    # second scoring pass should hit the memoized term list for every
    # single one of them — the exact re-tokenization the bug report
    # described as "no warm-cache benefit" on a repeat query.
    assert hits_after_second - hits_after_first >= len(cands)
    assert second == first


def test_bm25_second_score_call_is_meaningfully_faster() -> None:
    import time

    relevance._raw_terms.cache_clear()
    relevance._stemmed_terms.cache_clear()
    task = TaskContext(terms=("retry", "backoff", "timeout"))
    cands = _large_candidate_batch(20_000)
    scorer = BM25Scorer()

    start = time.perf_counter()
    scorer.score(task, cands)
    cold = time.perf_counter() - start

    start = time.perf_counter()
    scorer.score(task, cands)
    warm = time.perf_counter() - start

    # Generous margin (not a tight perf assertion) — the point is a
    # real, structural speedup from skipping re-tokenization, not a
    # specific ratio tuned to one machine.
    assert warm < cold * 0.9


# --- pure core: blended_scores ---------------------------------------


def test_blend_w_rel_one_is_pure_relevance() -> None:
    task = TaskContext(terms=("beta",))
    cands = [Candidate("a", "alpha", "a.py"), Candidate("b", "beta", "b.py")]
    central = {"a": 100.0, "b": 0.0}
    blended = blended_scores(task, cands, central, w_rel=1.0)
    assert blended["b"] == 1.0 and blended["a"] == 0.0


def test_blend_w_rel_zero_is_pure_centrality() -> None:
    task = TaskContext(terms=("beta",))
    cands = [Candidate("a", "alpha", "a.py"), Candidate("b", "beta", "b.py")]
    central = {"a": 100.0, "b": 0.0}
    blended = blended_scores(task, cands, central, w_rel=0.0)
    assert blended["a"] == 1.0 and blended["b"] == 0.0


def test_blend_flat_centrality_contributes_nothing() -> None:
    task = TaskContext(terms=("beta",))
    cands = [Candidate("a", "alpha", "a.py"), Candidate("b", "beta", "b.py")]
    blended = blended_scores(task, cands, {"a": 5.0, "b": 5.0}, w_rel=0.0)
    assert blended == {"a": 0.0, "b": 0.0}


def test_task_context_is_empty() -> None:
    assert TaskContext().is_empty is True
    assert TaskContext(terms=("x",)).is_empty is False
    assert TaskContext(diff_paths=frozenset({"a.py"})).is_empty is False


# --- integration fixtures --------------------------------------------

_TWO_LEAVES = {
    "src/auth.py": ('"""Authentication."""\ndef login() -> None:\n    pass\n'),
    "src/db.py": (
        '"""Database access."""\ndef connect() -> None:\n    pass\n'
    ),
}

_CALLERS = {
    "src/core.py": (
        '"""Core."""\n'
        "def target() -> None:\n    pass\n"
        "def alpha() -> None:\n    target()\n"
        "def bravo() -> None:\n    target()\n"
    ),
}


def _index(
    make_mapped_repo: RepoFactory, files: dict[str, str]
) -> tuple[Path, MapIndex]:
    root = make_mapped_repo(files)
    index = load_map(root)
    assert index is not None
    return root, index


# --- lean blend ------------------------------------------------------


def test_lean_relevance_lifts_matching_symbol(
    make_mapped_repo: RepoFactory,
) -> None:
    root, index = _index(make_mapped_repo, _TWO_LEAVES)
    model = render_lean.build_model(index, root)
    task = relevance.task_context("work on login", root)
    scores = render_lean._relevance_scores(model, task)
    login = next(k for k in scores if k.endswith("login"))
    connect = next(k for k in scores if k.endswith("connect"))
    # Equal (zero) centrality leaves; the task term breaks the tie.
    assert scores[login] > scores[connect]


def test_lean_live_atoms_sort_lowest_survival_first(
    make_mapped_repo: RepoFactory,
) -> None:
    root, index = _index(make_mapped_repo, _TWO_LEAVES)
    model = render_lean.build_model(index, root)
    ids = [a.sym_id for atoms in model.atoms_by_path.values() for a in atoms]
    scores = {sid: float(i) for i, sid in enumerate(sorted(ids))}
    ordered = render_lean._live_atoms(model, scores)
    got = [a.sym_id for a in ordered]
    assert got == sorted(got, key=lambda s: (scores[s], s))


def test_lean_without_task_is_unchanged(
    make_mapped_repo: RepoFactory,
) -> None:
    root, index = _index(make_mapped_repo, _TWO_LEAVES)
    a, _ = render_lean.generate(index, root)
    b, _ = render_lean.generate(index, root, task=TaskContext())
    assert a == b


# --- workset blend ---------------------------------------------------


def test_workset_apply_task_reorders_touched_and_files(
    make_mapped_repo: RepoFactory,
) -> None:
    root, index = _index(make_mapped_repo, _TWO_LEAVES)
    login = index.symbols_by_path["src/auth.py"][0]
    connect = index.symbols_by_path["src/db.py"][0]
    seed = workset.Seed(
        mode="rev",
        label="t",
        rev=None,
        symbol=None,
        touched=[connect, login],
        files=["src/db.py", "src/auth.py"],
        impacts=[],
    )
    task = relevance.task_context("fix the login flow", root)
    out = workset._apply_task(seed, index, task)
    assert out.touched[0].id == login.id
    assert out.files[0] == "src/auth.py"


# --- context blend ---------------------------------------------------


def test_context_entry_scores_favor_task_match(
    make_mapped_repo: RepoFactory,
) -> None:
    root, index = _index(make_mapped_repo, _CALLERS)
    target, _ = query.resolve_target(index, "target")
    assert target is not None
    pack = contextpack.build_pack(index, target, 1)
    task = relevance.task_context("touch alpha", root)
    scores = contextpack._entry_scores(index, pack, task)
    alpha = next(k for k in scores if k.endswith("alpha"))
    bravo = next(k for k in scores if k.endswith("bravo"))
    assert scores[alpha] > scores[bravo]


# --- CLI smoke: --task accepted everywhere ---------------------------


def test_cli_task_flag_accepted(make_mapped_repo: RepoFactory) -> None:
    root = make_mapped_repo(_TWO_LEAVES)
    r = str(root)
    assert cli.main(["lean", "--root", r, "--task", "login"]) == 0
    assert cli.main(["context", "login", "--root", r, "--task", "auth"]) == 0
    assert (
        cli.main(
            ["workset", "--symbol", "login", "--root", r, "--task", "auth"]
        )
        == 0
    )
