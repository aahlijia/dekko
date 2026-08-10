"""The search subcommand: BM25 free-text relevance ranking and budget."""

import json

import pytest

from dekko import cli, search
from dekko.relevance import Candidate, TaskContext
from dekko.textutil import Meter

from conftest import RepoFactory

SRC = {
    "src/httpclient.py": (
        '"""HTTP client with retry support."""\n'
        "\n"
        "\n"
        "def retry_request(fn, max_attempts=3):\n"
        '    """Retry a failed HTTP request with exponential backoff."""\n'
        "    pass\n"
        "\n"
        "\n"
        "class RetryPolicy:\n"
        '    """Decide whether a failed response should be retried."""\n'
        "\n"
        "    def should_retry(self, response):\n"
        '        """Whether a failed response is eligible for '
        'another attempt."""\n'
        "        pass\n"
    ),
    "src/storage.py": (
        '"""File storage helpers."""\n'
        "\n"
        "\n"
        "def read_file(path):\n"
        '    """Read a file from disk."""\n'
        "    pass\n"
    ),
    "src/auth.py": (
        '"""Authentication."""\n'
        "\n"
        "\n"
        "def login(username, password):\n"
        '    """Authenticate a user with a username and password."""\n'
        "    pass\n"
    ),
    "tests/test_httpclient.py": (
        "from src.httpclient import retry_request\n"
        "\n"
        "\n"
        "def test_retries_on_500():\n"
        "    assert retry_request(lambda: None) is None\n"
    ),
}


def _json_out(capsys: pytest.CaptureFixture) -> dict:
    return json.loads(capsys.readouterr().out)


# --- parse_kinds --------------------------------------------------------


def test_parse_kinds_none_for_empty() -> None:
    assert search.parse_kinds(None) is None
    assert search.parse_kinds("") is None


def test_parse_kinds_splits_and_strips() -> None:
    assert search.parse_kinds("function, class,method") == {
        "function",
        "class",
        "method",
    }


# --- ranking / CLI composition ------------------------------------------


def test_search_ranks_matching_symbols_first(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SRC)
    assert (
        cli.main(
            [
                "search",
                "retries failed http request",
                "--root",
                str(root),
                "--json",
            ]
        )
        == 0
    )
    doc = _json_out(capsys)
    quals = {h["qualname"] for h in doc["hits"]}
    assert "retry_request" in quals
    assert quals <= {
        "retry_request",
        "RetryPolicy",
        "RetryPolicy.should_retry",
    }
    assert "login" not in quals
    assert "read_file" not in quals
    assert doc["hits"][0]["qualname"] in {
        "retry_request",
        "RetryPolicy.should_retry",
    }


def test_search_suffix_broadening_matches_inflection(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SRC)
    assert cli.main(["search", "retrying", "--root", str(root), "--json"]) == 0
    doc = _json_out(capsys)
    quals = {h["qualname"] for h in doc["hits"]}
    assert "retry_request" in quals


def test_search_excludes_tests_by_default(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SRC)
    assert (
        cli.main(["search", "retries on 500", "--root", str(root), "--json"])
        == 0
    )
    doc = _json_out(capsys)
    quals = {h["qualname"] for h in doc["hits"]}
    assert "test_retries_on_500" not in quals

    assert (
        cli.main(
            [
                "search",
                "retries on 500",
                "--root",
                str(root),
                "--json",
                "--include-tests",
            ]
        )
        == 0
    )
    doc2 = _json_out(capsys)
    quals2 = {h["qualname"] for h in doc2["hits"]}
    assert "test_retries_on_500" in quals2


# --- round-08 §2.2: exclusion hint when test filtering may have hidden
# the real match --------------------------------------------------------


def test_search_exclusion_note_fires_on_weak_top_hit(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SRC)
    assert (
        cli.main(
            [
                "search",
                "500 status code retry",
                "--root",
                str(root),
                "--json",
            ]
        )
        == 0
    )
    doc = _json_out(capsys)
    assert doc["hits"][0]["score"] < search.LOW_CONFIDENCE_THRESHOLD
    assert "note" in doc
    assert "1 test-file symbol excluded" in doc["note"]
    assert "--include-tests" in doc["note"]


def test_search_exclusion_note_text_rendering(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SRC)
    assert (
        cli.main(["search", "500 status code retry", "--root", str(root)]) == 0
    )
    out = capsys.readouterr().out
    assert "note: 1 test-file symbol excluded" in out


def test_search_exclusion_note_absent_on_confident_top_hit(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SRC)
    assert (
        cli.main(["search", "http retry", "--root", str(root), "--json"]) == 0
    )
    doc = _json_out(capsys)
    assert doc["hits"][0]["score"] >= search.LOW_CONFIDENCE_THRESHOLD
    assert "note" not in doc


def test_search_exclusion_note_absent_with_include_tests(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SRC)
    assert (
        cli.main(
            [
                "search",
                "500 status code retry",
                "--root",
                str(root),
                "--json",
                "--include-tests",
            ]
        )
        == 0
    )
    doc = _json_out(capsys)
    assert "note" not in doc


def test_search_kind_filter(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SRC)
    assert (
        cli.main(
            [
                "search",
                "retry",
                "--root",
                str(root),
                "--json",
                "--kind",
                "class",
            ]
        )
        == 0
    )
    doc = _json_out(capsys)
    assert doc["hits"]
    assert {h["kind"] for h in doc["hits"]} == {"class"}
    assert any(h["qualname"] == "RetryPolicy" for h in doc["hits"])


def test_search_budget_trims_and_reports_total(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SRC)
    assert (
        cli.main(
            [
                "search",
                "retry retried retries retrying",
                "--root",
                str(root),
                "--json",
                "--budget",
                "1",
            ]
        )
        == 0
    )
    doc = _json_out(capsys)
    assert doc["meta"]["total"] > doc["meta"]["returned"]
    assert len(doc["hits"]) == doc["meta"]["returned"]


def test_search_zero_hits_is_exit_ok_with_no_matches(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SRC)
    assert (
        cli.main(
            [
                "search",
                "xyzxyzxyz completely unrelated gibberish term",
                "--root",
                str(root),
            ]
        )
        == 0
    )
    assert "(no matches)" in capsys.readouterr().out


def test_search_zero_hits_json_has_empty_hits(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SRC)
    assert (
        cli.main(
            [
                "search",
                "xyzxyzxyz completely unrelated gibberish term",
                "--root",
                str(root),
                "--json",
            ]
        )
        == 0
    )
    doc = _json_out(capsys)
    assert doc["hits"] == []
    assert doc["meta"]["total"] == 0


def test_search_no_regen_fails_on_stale(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SRC)
    (root / "src/auth.py").write_text(SRC["src/auth.py"] + "\nX = 1\n")
    code = cli.main(["search", "retry", "--root", str(root), "--no-regen"])
    assert code == 5
    assert "missing or stale" in capsys.readouterr().err


def test_search_json_shape_is_stable(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SRC)
    assert (
        cli.main(["search", "retry request", "--root", str(root), "--json"])
        == 0
    )
    doc = _json_out(capsys)
    assert set(doc) == {"query", "hits", "meta"}
    assert doc["query"] == "retry request"
    for hit in doc["hits"]:
        assert set(hit) == {
            "id",
            "path",
            "line",
            "qualname",
            "kind",
            "signature",
            "doc",
            "score",
        }
    assert set(doc["meta"]) == set(Meter(0, 0, 0).as_dict())


def test_search_unquoted_multiword_query_is_joined(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SRC)
    assert (
        cli.main(
            [
                "search",
                "retry",
                "failed",
                "request",
                "--root",
                str(root),
                "--json",
            ]
        )
        == 0
    )
    doc = _json_out(capsys)
    assert doc["query"] == "retry failed request"


def test_search_is_deterministic(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SRC)
    assert (
        cli.main(["search", "retry request", "--root", str(root), "--json"])
        == 0
    )
    first = capsys.readouterr().out
    assert (
        cli.main(["search", "retry request", "--root", str(root), "--json"])
        == 0
    )
    second = capsys.readouterr().out
    assert first == second


def test_search_text_output_includes_score_and_location(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SRC)
    assert cli.main(["search", "retry request", "--root", str(root)]) == 0
    out = capsys.readouterr().out
    assert 'search: "retry request"' in out
    assert "src/httpclient.py:" in out


def test_search_cli_dispatches(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # Smoke test that ``dekko search`` parses and dispatches (test_cli.py
    # holds no per-subcommand parse-smoke pattern for the other read
    # commands to mirror — they're each covered end-to-end in their own
    # test_<command>.py instead, as this file does for search).
    root = make_mapped_repo(SRC)
    assert cli.main(["search", "retry", "--root", str(root)]) == 0


# --- --scorer selection (Phase 2: optional embedding scorer) -----------


def test_scorer_default_is_lexical() -> None:
    assert search.DEFAULT_SCORER == "lexical"
    assert search.SCORER_CHOICES == ("lexical", "embedding")


def test_search_unrecognized_scorer_name_is_a_clear_error(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # Not reachable through the CLI (argparse's `choices=` rejects a bad
    # --scorer before dispatch), but search.run itself must still fail
    # closed for any other caller (e.g. a future MCP arg without the
    # same enum guard).
    root = make_mapped_repo(SRC)
    index, code = cli._load_or_regen(root, no_regen=False)
    assert code == 0
    code = search.run(index, "retry", root=root, scorer_name="bogus")
    assert code == search.EXIT_ERROR
    assert "unknown --scorer" in capsys.readouterr().err


def test_search_embedding_scorer_unavailable_is_a_clear_error(
    monkeypatch: pytest.MonkeyPatch,
    make_mapped_repo: RepoFactory,
    capsys: pytest.CaptureFixture,
) -> None:
    # Asserted regardless of whether numpy happens to be installed in
    # the env running this suite — an explicit --scorer embedding
    # request must fail clearly, never silently fall back to lexical,
    # whenever the dependency probe says "not available".
    monkeypatch.setattr(search.embedding, "available", lambda: False)
    root = make_mapped_repo(SRC)
    code = cli.main(
        ["search", "retry", "--root", str(root), "--scorer", "embedding"]
    )
    assert code == search.EXIT_ERROR
    err = capsys.readouterr().err
    assert "dekko[search]" in err


def test_search_default_scorer_unaffected_when_embedding_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    make_mapped_repo: RepoFactory,
    capsys: pytest.CaptureFixture,
) -> None:
    # The base (lexical) path must be completely untouched by Phase 2:
    # simulate "extra not installed" and confirm a plain `dekko search`
    # (no --scorer flag) still works exactly as before.
    monkeypatch.setattr(search.embedding, "available", lambda: False)
    root = make_mapped_repo(SRC)
    assert cli.main(["search", "retry", "--root", str(root), "--json"]) == 0
    doc = _json_out(capsys)
    assert doc["hits"]


def test_search_embedding_scorer_ranks_related_symbols(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    pytest.importorskip("numpy")
    root = make_mapped_repo(SRC)
    assert (
        cli.main(
            [
                "search",
                "retry failed http request",
                "--root",
                str(root),
                "--json",
                "--scorer",
                "embedding",
            ]
        )
        == 0
    )
    doc = _json_out(capsys)
    quals = {h["qualname"] for h in doc["hits"]}
    assert "retry_request" in quals
    # A hashing-trick embedding is closer to fuzzy character/subword
    # overlap than true semantic matching (see embedding.py's module
    # docstring on the model-choice tradeoff), so unrelated symbols
    # can still surface with some nonzero similarity — the bar here is
    # that the actually-relevant symbol ranks first, not that
    # everything else is excluded the way BM25's exact term overlap
    # filter would.
    assert doc["hits"][0]["qualname"] == "retry_request"


def test_search_embedding_scorer_persists_cache(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    pytest.importorskip("numpy")
    root = make_mapped_repo(SRC)
    assert (
        cli.main(
            [
                "search",
                "retry request",
                "--root",
                str(root),
                "--scorer",
                "embedding",
            ]
        )
        == 0
    )
    cache_path = root / ".dekko" / "embeddings.json"
    assert cache_path.exists()
    doc = json.loads(cache_path.read_text())
    assert doc["symbols"]


def test_search_embedding_scorer_reuses_cache_on_second_run(
    monkeypatch: pytest.MonkeyPatch,
    make_mapped_repo: RepoFactory,
    capsys: pytest.CaptureFixture,
) -> None:
    from dekko import embedding

    pytest.importorskip("numpy")
    root = make_mapped_repo(SRC)
    args = [
        "search",
        "retry request",
        "--root",
        str(root),
        "--scorer",
        "embedding",
    ]
    assert cli.main(args) == 0
    capsys.readouterr()

    seen: dict = {}
    real_get = embedding.EmbeddingCache.get

    def spying_get(self, candidate_id, text):  # noqa: ANN001, ANN202
        result = real_get(self, candidate_id, text)
        if result is not None:
            seen["hit"] = True
        return result

    monkeypatch.setattr(embedding.EmbeddingCache, "get", spying_get)
    assert cli.main(args) == 0
    assert seen.get("hit") is True


# --- 2.5: per-index candidate-list cache (search/BM25 performance) ---
#
# Track F: ``_build_candidates`` used to rebuild the full candidate
# list (one entry per symbol) from scratch on every ``rank()`` call.
# Attaching the cache to the ``MapIndex`` instance means a repeated
# ``rank()`` against the *same* already-loaded index — e.g. multiple
# ``dekko search`` calls served by one long-lived MCP session — skips
# rebuilding it.


def test_build_candidates_reuses_cache_for_same_index_and_kinds(
    make_mapped_repo: RepoFactory,
) -> None:
    from dekko.mapfile import load_map

    root = make_mapped_repo(SRC)
    index = load_map(root)
    assert index is not None

    first = search._build_candidates(index, None)
    second = search._build_candidates(index, None)
    assert second is first  # cache hit, not rebuilt


def test_build_candidates_cache_is_keyed_by_kinds(
    make_mapped_repo: RepoFactory,
) -> None:
    from dekko.mapfile import load_map

    root = make_mapped_repo(SRC)
    index = load_map(root)
    assert index is not None

    unfiltered = search._build_candidates(index, None)
    filtered = search._build_candidates(index, frozenset({"function"}))
    assert filtered is not unfiltered
    assert len(filtered) <= len(unfiltered)
    # Re-fetching each kind still hits its own cache entry, not the
    # other kind's.
    assert search._build_candidates(index, None) is unfiltered
    assert search._build_candidates(index, frozenset({"function"})) is filtered


def test_build_candidates_does_not_leak_across_index_instances(
    make_mapped_repo: RepoFactory,
) -> None:
    from dekko.mapfile import load_map

    root = make_mapped_repo(SRC)
    index_a = load_map(root)
    index_b = load_map(root)
    assert index_a is not None and index_b is not None
    assert index_a is not index_b

    cands_a = search._build_candidates(index_a, None)
    cands_b = search._build_candidates(index_b, None)
    assert cands_a is not cands_b
    assert {c.id for c in cands_a} == {c.id for c in cands_b}


def test_search_rank_still_deterministic_across_repeated_calls(
    make_mapped_repo: RepoFactory,
) -> None:
    """The candidate cache must not change ranking output, only speed."""
    from dekko.mapfile import load_map

    root = make_mapped_repo(SRC)
    index = load_map(root)
    assert index is not None

    first = search.rank(index, "retries failed http request")
    second = search.rank(index, "retries failed http request")
    assert [h.symbol.id for h in first] == [h.symbol.id for h in second]


# --- round-08 §2.3: a common term shouldn't crowd out a distinctive
# one ------------------------------------------------------------------


def test_coverage_adjusted_scorer_flips_common_term_domination() -> None:
    """Unit-level check on the exact mechanism ``rank()`` wraps in.

    A candidate that only covers the common query term ("integration")
    scores higher than a candidate covering both distinctive terms
    under the raw (unadjusted) scorer -- the shape reported for
    claude-code's "sandbox escape" and cline's "slack integration"
    (round-08 §2.3). Wrapping the scorer in
    :class:`search._CoverageAdjustedScorer` should flip the order.
    """
    task = TaskContext(terms=("slack", "integration"))
    cands = [
        Candidate(
            "shell_integration",
            "shell_integration shell integration helper",
            "src/shell.py",
        ),
        Candidate(
            "slack_connector",
            "slack_connector post a message via a slack integration webhook",
            "src/slack.py",
        ),
    ]

    class _FakeScorer:
        """Returns fixed scores, standing in for a real scorer's raw
        output (BM25 or embedding) so the wrapper is tested in
        isolation from either scorer's own internals."""

        def score(
            self, task: TaskContext, candidates: list[Candidate]
        ) -> dict[str, float]:
            return {"shell_integration": 0.7, "slack_connector": 0.6}

    # Pre-adjustment: the common-term-only candidate already outranks
    # the full-coverage one -- the bug being fixed.
    raw = _FakeScorer().score(task, cands)
    assert raw["shell_integration"] > raw["slack_connector"]

    adjusted = search._CoverageAdjustedScorer(_FakeScorer()).score(task, cands)
    assert adjusted["slack_connector"] > adjusted["shell_integration"]


def test_coverage_adjusted_scorer_is_noop_for_single_term_query() -> None:
    """No "crowded out by a different term" failure mode for one term."""
    task = TaskContext(terms=("integration",))
    cands = [Candidate("a", "integration integration", "a.py")]

    class _FakeScorer:
        def score(
            self, task: TaskContext, candidates: list[Candidate]
        ) -> dict[str, float]:
            return {"a": 0.42}

    inner = _FakeScorer()
    assert search._CoverageAdjustedScorer(inner).score(
        task, cands
    ) == inner.score(task, cands)


def test_search_coverage_multiplier_discounts_common_term_only_matches(
    make_mapped_repo: RepoFactory,
) -> None:
    """End-to-end: single-term-only matches score lower with the fix.

    Six symbols repeat "escape" heavily but never mention "sandbox";
    one symbol covers both distinctive query terms. Comparing against
    the same ranking with the coverage multiplier neutralized proves
    the mechanism actually engages (not just that the target already
    wins on IDF alone) -- the common-term-only ceiling drops once the
    multiplier is active.
    """
    from dekko import relevance as relevance_mod
    from dekko.mapfile import load_map

    files = {}
    for i in range(6):
        files[f"src/escape_{i}.py"] = (
            f'"""Escaping helper {i}."""\n\n\n'
            f"def escape_thing_{i}(s):\n"
            f'    """Escape escape escape a string, escape it well, '
            f'escape."""\n'
            f"    pass\n"
        )
    for i in range(4):
        files[f"src/sandbox_{i}.py"] = (
            f'"""Sandbox config {i}."""\n\n\n'
            f"def sandbox_setting_{i}(x):\n"
            f'    """Configure sandbox option {i}."""\n'
            f"    pass\n"
        )
    files["src/sandbox_escape.py"] = (
        '"""Sandbox boundary enforcement."""\n\n\n'
        "def check(proc):\n"
        '    """Detect a sandbox escape attempt."""\n'
        "    pass\n"
    )
    root = make_mapped_repo(files)
    index = load_map(root)
    assert index is not None

    def _target_and_ceiling(
        hits: list[search.SearchHit],
    ) -> tuple[float, float]:
        target = next(h for h in hits if h.symbol.qualname == "check")
        ceiling = max(h.score for h in hits if h.symbol.qualname != "check")
        return target.score, ceiling

    hits = search.rank(index, "sandbox escape")
    target_score, ceiling_with_fix = _target_and_ceiling(hits)
    assert target_score > ceiling_with_fix

    original = relevance_mod.coverage_factor
    relevance_mod.coverage_factor = lambda coverage: 1.0
    try:
        hits_unadjusted = search.rank(index, "sandbox escape")
    finally:
        relevance_mod.coverage_factor = original
    _, ceiling_without_fix = _target_and_ceiling(hits_unadjusted)

    assert ceiling_with_fix < ceiling_without_fix
