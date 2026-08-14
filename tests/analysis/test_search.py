"""The search subcommand: BM25 free-text relevance ranking and budget."""

import json

import pytest

from dekko.integrations import cli
from dekko.analysis import search
from dekko.analysis.relevance import (
    BM25Scorer,
    Candidate,
    TaskContext,
    blended_scores,
)
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


def test_search_header_uses_scored_candidate_wording(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    """9.1: the header used to read "N hits", which sounds alarming
    even when the shown results are excellent -- it implied every one
    of them matched strongly. It now reads "N candidates scored"."""
    root = make_mapped_repo(SRC)
    assert cli.main(["search", "retry", "--root", str(root)]) == 0
    out = capsys.readouterr().out
    assert "candidates scored" in out or "candidate scored" in out
    assert " hits" not in out.splitlines()[0]
    assert " hit\n" not in out


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
    assert search.SCORER_CHOICES == ("lexical", "embedding", "both")


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
    from dekko.storage import embedding

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
    from dekko.render.mapfile import load_map

    root = make_mapped_repo(SRC)
    index = load_map(root)
    assert index is not None

    first = search._build_candidates(index, None)
    second = search._build_candidates(index, None)
    assert second is first  # cache hit, not rebuilt


def test_build_candidates_cache_is_keyed_by_kinds(
    make_mapped_repo: RepoFactory,
) -> None:
    from dekko.render.mapfile import load_map

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
    from dekko.render.mapfile import load_map

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
    from dekko.render.mapfile import load_map

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
    from dekko.analysis import relevance as relevance_mod
    from dekko.render.mapfile import load_map

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


# --- round-12 §3.13: golden-query regression -- a lexically-common
# term shouldn't let a generic match outrank a lexically-narrower but
# semantically-correct one. Reproduces spring-boot's reported shape
# ("parse yaml configuration properties" ranking a generic ``*Parser.
# parse()`` ahead of ``YamlPropertySourceLoader``): "parse" is common
# across the corpus, "yaml" is rare -- a candidate missing the rare
# term should no longer tie-and-then-lose to one missing the common
# term on raw magnitude alone. Uses a relative-ordering assertion
# between two known symbols (not "must be #1"), per the design doc's
# own framing -- more robust across future retuning than an exact-rank
# pin.
# --------------------------------------------------------------------


def test_search_specific_match_not_buried_by_generic_common_term_match(
    make_mapped_repo: RepoFactory,
) -> None:
    """ "yaml" (rare) missing should cost the ranking more than "parse"
    (common) missing, once both candidates cover the same number of
    the query's distinct terms. Comparing against the same ranking
    with the coverage discount forced flat (round-08's original,
    unweighted behavior) proves the IDF-weighting actually engages --
    not just that the specific match already happened to win here for
    unrelated reasons -- by showing its margin over the generic match
    widens once the discount is weighted.
    """
    from dekko.analysis import relevance as relevance_mod
    from dekko.render.mapfile import load_map

    files = {}
    for i in range(8):
        files[f"src/parser_{i}.py"] = (
            f'"""Parsing helper {i}."""\n\n\n'
            f"def parse_thing_{i}(s):\n"
            f'    """Parse parse parse the input, parse it well."""\n'
            f"    pass\n"
        )
    files["src/elements_parser.py"] = (
        '"""Configuration property element parsing."""\n\n\n'
        "class ElementsParser:\n"
        '    """Parses configuration properties from a source."""\n\n'
        "    def parse(self, name):\n"
        '        """Parse one configuration properties element."""\n'
        "        pass\n"
    )
    files["src/yaml_loader.py"] = (
        '"""YAML property source loading."""\n\n\n'
        "class YamlPropertySourceLoader:\n"
        '    """Loads YAML configuration properties from a source."""\n\n'
        "    def load(self, name):\n"
        '        """Load a YAML configuration properties file."""\n'
        "        pass\n"
    )
    root = make_mapped_repo(files)
    index = load_map(root)
    assert index is not None

    def _margin(hits: list[search.SearchHit]) -> float:
        by_qualname = {h.symbol.qualname: h for h in hits}
        generic = by_qualname["ElementsParser.parse"]
        specific = by_qualname["YamlPropertySourceLoader"]
        return specific.score - generic.score

    hits = search.rank(index, "parse yaml configuration properties")
    margin_with_fix = _margin(hits)
    assert margin_with_fix > 0  # the specific match must not be buried

    def _flat_weighted_coverage(
        terms: tuple[str, ...],
        text: str,
        term_weights: dict[str, float] | None = None,
    ) -> float:
        if not terms:
            return 1.0
        present = set(relevance_mod.normalize_terms(text))
        stemmed_present = {relevance_mod._stem(t) for t in present}
        hits_ = sum(
            1
            for t in terms
            if t in present or relevance_mod._stem(t) in stemmed_present
        )
        return hits_ / len(terms)

    original = relevance_mod.weighted_term_coverage
    relevance_mod.weighted_term_coverage = _flat_weighted_coverage
    try:
        hits_flat = search.rank(index, "parse yaml configuration properties")
    finally:
        relevance_mod.weighted_term_coverage = original
    margin_without_fix = _margin(hits_flat)

    assert margin_with_fix > margin_without_fix


# --- round-13 §3: held-out multi-query golden corpus. Items 1-2 above
# (yaml/sandbox-escape) already cover coverage-tie / common-term-
# dominance and weak-field renormalization. Items 3-6 below add the
# four new shapes the round-13 relevance-tuning plan asked for --
# corpus-size batch consistency (§1's own bug class), rare-term IDF
# sanity on a non-Python identifier shape, length-normalization bias,
# and the sparse-candidate/no-lexical-connection shape (§2, documented
# but not asserted). See
# .features/plans/round13/search-relevance-tuning-plan.md §3.
# --------------------------------------------------------------------


def test_search_batch_size_consistency_cline_shaped(
    make_mapped_repo: RepoFactory,
) -> None:
    """Round-13 §1's own bug, reproduced at fixture scale (TS/camelCase,
    modeled on cline's reported "cancel task execution" miss).

    ``cancelTask`` is a short, no-doc candidate matching two of the
    three query terms ("cancel", "task") by name alone.
    ``captureHookExecution`` is a doc/signature-heavy candidate
    matching only the third term ("execution") but repeating it many
    times across a big union-typed signature and a doc paragraph. A
    large pool of "task"-matching (but otherwise unrelated) survivor
    candidates, plus a larger pool of candidates matching neither term
    at all, makes ``len(candidates)`` and ``len(survivors)`` differ
    enough that BM25's batch-relative IDF/avgdl diverge between the
    full-corpus pass and a re-scored survivor-only pass -- exactly the
    mechanism §1 fixed. Pre-fix, this flips the ranking
    (``captureHookExecution`` outranks ``cancelTask``); post-fix,
    ``cancelTask`` stays on top, matching the raw full-corpus number
    that was correct all along. The distractor counts here (600
    unrelated, 150 "task"-matching) were tuned empirically per the
    plan's own note that this number "needs empirical tuning during
    implementation" -- confirmed via ``git stash``/``stash pop`` against
    pre-fix ``HEAD`` to actually fail before the fix and pass after.
    """
    from dekko.render.mapfile import load_map

    files = {
        "src/task_control.ts": (
            "class SdkTaskControlCoordinator {\n"
            "  cancelTask(): void {\n"
            "  }\n"
            "}\n"
        ),
        "src/telemetry.ts": (
            "/**\n"
            " * Records hook execution events with a unified "
            "status-based\n"
            " * approach for downstream execution analytics "
            "pipelines.\n"
            " * Handles execution lifecycle, execution retries, "
            "execution\n"
            " * timeouts, and execution completion callbacks across "
            "every\n"
            " * execution stage, execution phase, and execution "
            "boundary.\n"
            " */\n"
            "function captureHookExecution(\n"
            "  ulid: string,\n"
            "  hookName: string,\n"
            "  status: string,\n"
            "  executionContext: Record<string, unknown>,\n"
            "  executionTimingMs: number,\n"
            "  executionMetadata: Record<string, unknown> | null,\n"
            "  executionPhase: string,\n"
            "): void {\n"
            "}\n"
        ),
    }
    for i in range(600):
        files[f"src/noise_{i}.ts"] = (
            f"function formatCurrency{i}(amount: number): string {{\n"
            f'  return "";\n'
            f"}}\n"
        )
    for i in range(150):
        files[f"src/task_{i}.ts"] = (
            "/**\n"
            " * Schedules a background task in the worker task "
            "queue.\n"
            " */\n"
            f"function scheduleTask{i}(taskId: string, "
            "delayMs: number): void {\n"
            "}\n"
        )
    root = make_mapped_repo(files)
    index = load_map(root)
    assert index is not None

    hits = search.rank(index, "cancel task execution")
    by_qualname = {h.symbol.qualname: h for h in hits}
    target = by_qualname["SdkTaskControlCoordinator.cancelTask"]
    distractor = by_qualname["captureHookExecution"]
    assert target.score > distractor.score
    assert hits[0] is target


def test_search_rare_term_beats_common_term_repetition_go_naming(
    make_mapped_repo: RepoFactory,
) -> None:
    """Rare-term IDF sanity on a non-Python, non-camelCase identifier
    shape.

    Same shape as the round-12 yaml fixture above (a rare, distinctive
    term should out-discriminate a common term repeated across many
    generic candidates), but on Go-style package-qualified
    identifiers, so ``idf_term_weights``/``weighted_term_coverage`` are
    verified outside Python/camelCase naming too.
    """
    from dekko.analysis import relevance as relevance_mod
    from dekko.render.mapfile import load_map

    files = {}
    for i in range(8):
        files[f"internal/parse/parse_{i}.go"] = (
            f"package parse\n\n"
            f"// parse_thing_{i} parses parses parses generic "
            f"input, parses it well.\n"
            f"func parse_thing_{i}(s string) error {{\n"
            f"\treturn nil\n"
            f"}}\n"
        )
    files["internal/quota/quota_limit.go"] = (
        "package quota\n\n"
        "// parse_quota_limit parses a quota limit configuration "
        "value from a byte string.\n"
        "func parse_quota_limit(data []byte) (int, error) {\n"
        "\treturn 0, nil\n"
        "}\n"
    )
    root = make_mapped_repo(files)
    index = load_map(root)
    assert index is not None

    def _margin(hits: list[search.SearchHit]) -> float:
        by_qualname = {h.symbol.qualname: h for h in hits}
        generic = max(
            h.score
            for name, h in by_qualname.items()
            if name != "parse_quota_limit"
        )
        specific = by_qualname["parse_quota_limit"].score
        return specific - generic

    hits = search.rank(index, "parse quota limit")
    margin_with_fix = _margin(hits)
    assert margin_with_fix > 0

    def _flat_weighted_coverage(
        terms: tuple[str, ...],
        text: str,
        term_weights: dict[str, float] | None = None,
    ) -> float:
        if not terms:
            return 1.0
        present = set(relevance_mod.normalize_terms(text))
        stemmed_present = {relevance_mod._stem(t) for t in present}
        hits_ = sum(
            1
            for t in terms
            if t in present or relevance_mod._stem(t) in stemmed_present
        )
        return hits_ / len(terms)

    original = relevance_mod.weighted_term_coverage
    relevance_mod.weighted_term_coverage = _flat_weighted_coverage
    try:
        hits_flat = search.rank(index, "parse quota limit")
    finally:
        relevance_mod.weighted_term_coverage = original
    margin_without_fix = _margin(hits_flat)

    assert margin_with_fix > margin_without_fix


def test_search_length_normalization_favors_short_precise_match_rust(
    make_mapped_repo: RepoFactory,
) -> None:
    """BM25 length normalization should still favor a short, precise
    match over a long candidate that mentions every query term many
    times incidentally, once ``avgdl`` is computed over a batch with
    real size variance (Rust/snake_case naming, loosely modeled on the
    zed corpus's shape without reproducing its actual source).

    Filler candidates of both short and long lengths are included so
    the batch's average document length reflects real variance, not
    the near-uniform lengths the existing fixtures happen to use.
    """
    from dekko.render.mapfile import load_map

    files = {
        "src/db/pool.rs": (
            "/// Connect to the database pool.\n"
            "pub fn connect_database_pool(cfg: &Config) -> "
            "Result<Pool> {\n"
            "    todo!()\n"
            "}\n"
        ),
        "src/db/generic_helper.rs": (
            "/// This module manages database connections. When you "
            "connect to\n"
            "/// the database, a database pool of database "
            "connections is\n"
            "/// created; the pool tracks each connect and disconnect "
            "and pool\n"
            "/// exhaustion event, and the database pool grows or "
            "shrinks as\n"
            "/// connect load changes across the database pool "
            "lifecycle.\n"
            "pub fn generic_helper(\n"
            "    connect_flag: bool,\n"
            "    database_name: String,\n"
            "    pool_size: usize,\n"
            "    connect_retry_count: usize,\n"
            "    database_timeout_ms: u64,\n"
            "    pool_max_idle: usize,\n"
            ") -> Result<()> {\n"
            "    todo!()\n"
            "}\n"
        ),
    }
    for i in range(6):
        files[f"src/util/short_{i}.rs"] = (
            f"/// Utility {i}.\npub fn util_{i}() -> u32 {{ {i} }}\n"
        )
    for i in range(6):
        files[f"src/util/long_{i}.rs"] = (
            "/// A longer helper with several unrelated parameters "
            "for\n"
            f"/// formatting output {i}, including width, precision, "
            "and\n"
            "/// alignment settings used across the rendering "
            "pipeline.\n"
            f"pub fn format_output_{i}(width: usize, precision: "
            "usize, align: Align, pad: char, upper: bool, "
            "trim: bool) -> String {\n"
            "    String::new()\n"
            "}\n"
        )
    root = make_mapped_repo(files)
    index = load_map(root)
    assert index is not None

    hits = search.rank(index, "connect database pool")
    by_qualname = {h.symbol.qualname: h for h in hits}
    assert (
        by_qualname["connect_database_pool"].score
        > by_qualname["generic_helper"].score
    )
    assert hits[0].symbol.qualname == "connect_database_pool"


def test_search_sparse_candidate_no_lexical_connection_documented_zed(
    make_mapped_repo: RepoFactory,
) -> None:
    """Round-13 §2's shape (Rust trait/impl naming modeled on zed),
    documented as a known limitation and NOT asserted on ordering.

    ``Item.save``'s indexed text (name + short trait-default
    signature, no doc) genuinely lacks 2 of the 3 query terms ("file",
    "disk") -- the coverage/IDF machinery correctly discounts it hard,
    and a partial-coverage distractor (``DiskState``, whose qualname's
    "Disk" prefix covers "disk" and whose method covers "file"'s
    stem-adjacent tokens) wins instead. This is not a bug this round
    fixed (§2 was deferred -- see the plan's §2 for why: no lexical
    scorer change can manufacture a signal that isn't in the indexed
    text). A hard ordering assertion here would either fail immediately
    as a "known failure" (noisy, easy to ignore) or silently pin
    today's arbitrary distractor-wins ordering as if it were intended
    behavior. So this fixture only pins the shape (it doesn't crash,
    it returns a non-empty ranked result) for whoever eventually builds
    §2's candidate-text-enrichment fix.
    """
    from dekko.render.mapfile import load_map

    files = {
        "src/item.rs": (
            "pub trait Item {\n    fn save(&self) -> Result<()>;\n}\n"
        ),
        "src/workspace.rs": (
            "pub struct Workspace;\n\n"
            "impl Item for Workspace {\n"
            "    fn save(&self) -> Result<()> {\n"
            "        todo!()\n"
            "    }\n"
            "}\n"
        ),
        "src/disk.rs": (
            "/// Tracks on-disk state for a file, including mtime "
            "and size.\n"
            "pub struct DiskState;\n\n"
            "impl DiskState {\n"
            "    /// The file's last modified time on disk.\n"
            "    pub fn mtime(&self) -> u64 {\n"
            "        0\n"
            "    }\n"
            "}\n"
        ),
    }
    root = make_mapped_repo(files)
    index = load_map(root)
    assert index is not None

    hits = search.rank(index, "save file to disk")
    assert hits  # doesn't crash, returns a ranked (non-empty) result


# --- round-13 §4: --scorer both (reciprocal rank fusion) ---------------
#
# §2's fix, designed and implemented: run BM25Scorer and
# EmbeddingScorer independently and fuse their rankings by rank
# position (not raw score -- the two scorers' scores aren't
# scale-comparable). Opt-in only; --scorer lexical/--scorer embedding
# are unaffected (covered by the tests above, unchanged).


def test_search_scorer_both_fuses_lexical_and_embedding_picks(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    """Plumbing test: --scorer both must genuinely call both scorers,
    not silently degrade to one.

    ``connect_database_pool`` is an exact literal match for the query
    (thin surrounding text, no doc) -- BM25's top pick.
    ``qonect_datbase_pol`` is a misspelled variant: it shares no
    literal or stemmed token with the query at all, so BM25 gives it
    zero raw relevance and ``rank()`` drops it before it ever reaches
    the lexical survivor set -- but its character trigrams still
    overlap heavily with "connect database pool", which is exactly
    what the hashing-trick embedding scorer picks up. Both must appear
    in the fused --scorer both result for the composition to be
    genuine.
    """
    pytest.importorskip("numpy")
    files = {
        "src/pool.py": "def connect_database_pool():\n    pass\n",
        "src/typo.py": "def qonect_datbase_pol():\n    pass\n",
        "src/unrelated.py": (
            'def render_widget():\n    """Render a UI widget."""\n    pass\n'
        ),
    }
    root = make_mapped_repo(files)

    from dekko.render.mapfile import load_map

    index = load_map(root)
    assert index is not None
    lexical_hits = search.rank(index, "connect database pool")
    lexical_quals = {h.symbol.qualname for h in lexical_hits}
    assert "connect_database_pool" in lexical_quals
    assert "qonect_datbase_pol" not in lexical_quals  # zero raw overlap

    code = cli.main(
        [
            "search",
            "connect database pool",
            "--root",
            str(root),
            "--scorer",
            "both",
            "--json",
        ]
    )
    assert code == 0
    doc = _json_out(capsys)
    fused_quals = {h["qualname"] for h in doc["hits"]}
    assert "connect_database_pool" in fused_quals  # lexical's own pick
    assert "qonect_datbase_pol" in fused_quals  # embedding-only pick


def test_search_scorer_both_surfaces_item_save_zed_shaped(
    make_mapped_repo: RepoFactory,
) -> None:
    """Round-13 §4's motivating case: reuses item 6's exact fixture
    (``test_search_sparse_candidate_no_lexical_connection_documented_
    zed``, above) with ``--scorer both`` instead of the default
    lexical scorer.

    Under the default lexical scorer, ``Item.save`` and
    ``Workspace.save`` tie (both cover only "save" of the three query
    terms, identical candidate-text shape) -- item 6's own test
    deliberately leaves that tie unordered. Under ``--scorer both``,
    the embedding scorer breaks the tie in ``Item.save``'s favor,
    which is a real, assertable improvement RRF fusion earns (unlike
    item 6's own necessarily-unordered assertion for the unchanged
    default path). The dramatic real-world version of this
    improvement -- ``Item.save`` buried at rank 133 of 1,548 survivors
    under the default scorer on the actual zed repo, promoted to rank
    35 of 23,667 under the embedding scorer -- is validated live
    against ``test-repos/zed``, not by this small synthetic fixture;
    see the plan doc's §4 Implementation notes for the exact numbers.
    """
    pytest.importorskip("numpy")
    files = {
        "src/item.rs": (
            "pub trait Item {\n    fn save(&self) -> Result<()>;\n}\n"
        ),
        "src/workspace.rs": (
            "pub struct Workspace;\n\n"
            "impl Item for Workspace {\n"
            "    fn save(&self) -> Result<()> {\n"
            "        todo!()\n"
            "    }\n"
            "}\n"
        ),
        "src/disk.rs": (
            "/// Tracks on-disk state for a file, including mtime "
            "and size.\n"
            "pub struct DiskState;\n\n"
            "impl DiskState {\n"
            "    /// The file's last modified time on disk.\n"
            "    pub fn mtime(&self) -> u64 {\n"
            "        0\n"
            "    }\n"
            "}\n"
        ),
    }
    root = make_mapped_repo(files)

    from dekko.render.mapfile import load_map

    index = load_map(root)
    assert index is not None
    lexical_hits = search.rank(index, "save file to disk")
    by_qualname = {h.symbol.qualname: h for h in lexical_hits}
    assert (
        by_qualname["Item.save"].score == by_qualname["Workspace.save"].score
    )  # the tie item 6 leaves unbroken


def test_search_scorer_both_does_not_demote_correct_lexical_top_hit(
    make_mapped_repo: RepoFactory,
) -> None:
    """No-regression check: for a query already correctly ranked by
    the default lexical scorer, ``--scorer both`` must not demote that
    top hit while promoting a previously-missed one elsewhere.

    Two shapes, both modeled on round-13 §1's already-validated
    controls (cline's "cancel task execution" and zed's "resolve
    diagnostics", both confirmed correctly ranked post-§1-fix): a
    short, precise, no-doc match competing against a doc/signature-
    heavy distractor that repeats one query term many times. RRF
    fusion's fused top-1 must land on the same on-target symbol the
    plain lexical result already gets right in both cases.
    """
    pytest.importorskip("numpy")

    from dekko.render.mapfile import load_map

    cline_files = {
        "src/task_control.ts": (
            "class SdkTaskControlCoordinator {\n"
            "  cancelTask(): void {\n"
            "  }\n"
            "}\n"
        ),
        "src/telemetry.ts": (
            "/**\n"
            " * Records hook execution events with a unified "
            "status-based\n"
            " * approach for downstream execution analytics "
            "pipelines.\n"
            " * Handles execution lifecycle, execution retries, "
            "execution\n"
            " * timeouts, and execution completion callbacks across "
            "every\n"
            " * execution stage, execution phase, and execution "
            "boundary.\n"
            " */\n"
            "function captureHookExecution(\n"
            "  ulid: string,\n"
            "  hookName: string,\n"
            "  status: string,\n"
            "  executionContext: Record<string, unknown>,\n"
            "  executionTimingMs: number,\n"
            "  executionMetadata: Record<string, unknown> | null,\n"
            "  executionPhase: string,\n"
            "): void {\n"
            "}\n"
        ),
    }
    root = make_mapped_repo(cline_files)
    index = load_map(root)
    assert index is not None
    lexical_hits = search.rank(index, "cancel task execution")
    assert lexical_hits[0].symbol.qualname == (
        "SdkTaskControlCoordinator.cancelTask"
    )

    from dekko.storage import embedding as embedding_mod

    embedding_hits = search.rank(
        index, "cancel task execution", scorer=embedding_mod.EmbeddingScorer()
    )
    fused = search._fuse_both(lexical_hits, embedding_hits)
    assert fused[0].symbol.qualname == "SdkTaskControlCoordinator.cancelTask"

    zed_files = {
        "src/diagnostics.rs": (
            "pub struct DiagnosticEntry;\n\n"
            "impl DiagnosticEntry {\n"
            "    /// Resolve this diagnostic entry against the "
            "current buffer snapshot.\n"
            "    pub fn resolve(&self) -> Resolved {\n"
            "        todo!()\n"
            "    }\n"
            "}\n\n"
            "pub struct DiagnosticGroup;\n\n"
            "impl DiagnosticGroup {\n"
            "    /// Resolve every diagnostic in this group.\n"
            "    pub fn resolve(&self) -> Vec<Resolved> {\n"
            "        todo!()\n"
            "    }\n"
            "}\n"
        ),
        "src/task.rs": (
            "pub struct Task;\n\n"
            "impl Task {\n"
            "    /// Whether this task is ready to run.\n"
            "    pub fn ready(&self) -> bool {\n"
            "        true\n"
            "    }\n"
            "}\n"
        ),
    }
    root2 = make_mapped_repo(zed_files)
    index2 = load_map(root2)
    assert index2 is not None
    lexical_hits2 = search.rank(index2, "resolve diagnostics")
    assert lexical_hits2[0].symbol.qualname.endswith(".resolve")

    embedding_hits2 = search.rank(
        index2, "resolve diagnostics", scorer=embedding_mod.EmbeddingScorer()
    )
    fused2 = search._fuse_both(lexical_hits2, embedding_hits2)
    assert fused2[0].symbol.qualname.endswith(".resolve")


def test_search_scorer_both_scale_note_fires_unconditionally(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    """Round-14 master report (cline §4.3, claude-code §2.2, LOW):
    ``--scorer both``'s fused score is a reciprocal-rank-fusion value
    on a different scale than ``lexical``/``embedding`` scores, with
    no in-band explanation. ``_scale_note`` fixes this: it must fire
    on every ``--scorer both`` call, JSON and text alike, regardless
    of whether the round-08 §2.2 exclusion note also fires.
    """
    pytest.importorskip("numpy")
    root = make_mapped_repo(SRC)
    assert (
        cli.main(
            [
                "search",
                "http retry",
                "--root",
                str(root),
                "--json",
                "--scorer",
                "both",
            ]
        )
        == 0
    )
    doc = _json_out(capsys)
    assert "note" in doc
    assert "reciprocal-rank-fusion" in doc["note"]

    assert (
        cli.main(
            [
                "search",
                "http retry",
                "--root",
                str(root),
                "--scorer",
                "both",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "note: --scorer both's score is a reciprocal-rank-fusion" in out


def test_search_scorer_lexical_and_embedding_have_no_scale_note(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    """``_scale_note`` is specific to ``--scorer both`` -- the default
    lexical scorer and plain ``--scorer embedding`` must stay exactly
    as before (no note absent an exclusion hint)."""
    pytest.importorskip("numpy")
    root = make_mapped_repo(SRC)
    for scorer_name in ("lexical", "embedding"):
        assert (
            cli.main(
                [
                    "search",
                    "http retry",
                    "--root",
                    str(root),
                    "--json",
                    "--scorer",
                    scorer_name,
                ]
            )
            == 0
        )
        doc = _json_out(capsys)
        assert "note" not in doc


def test_search_scorer_both_combines_scale_note_with_exclusion_note(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    """When both notes fire (a weak top hit *and* ``--scorer both``),
    they're joined into the same single ``note`` field, not just the
    last one to run clobbering the other."""
    pytest.importorskip("numpy")
    root = make_mapped_repo(SRC)
    assert (
        cli.main(
            [
                "search",
                "500 status code retry",
                "--root",
                str(root),
                "--json",
                "--scorer",
                "both",
            ]
        )
        == 0
    )
    doc = _json_out(capsys)
    assert "note" in doc
    assert "test-file" in doc["note"]
    assert "reciprocal-rank-fusion" in doc["note"]


def test_search_scorer_both_exclusion_note_uses_underlying_top_score(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    """Implementation divergence from §4's design, not in the plan's own
    spec: ``SearchHit.score`` under ``--scorer both`` is a reciprocal
    rank fusion value (max ``~1/k`` with ``k=60``, i.e. well under
    0.02) rather than a blended ``[0, 1]`` score --
    ``LOW_CONFIDENCE_THRESHOLD`` (0.4) was calibrated against the
    latter, so comparing a fused score against it directly would make
    the round-08 §2.2 "low-confidence" note fire on *every* --scorer
    both call with any excluded test symbols, confident result or not.
    ``run()`` instead passes ``_exclusion_note`` the better of the two
    underlying scorers' own top blended score. This fixture's top hit
    is confidently on-target under plain lexical search (well above
    0.4) -- the note must stay silent under --scorer both too, not
    fire just because the fused score itself reads low.
    """
    pytest.importorskip("numpy")
    root = make_mapped_repo(SRC)

    # Confirm the plain lexical top hit is confident (this is the
    # existing control test_search_exclusion_note_absent_on_confident_
    # top_hit's own query/assertion, re-derived here to justify why
    # --scorer both should also stay silent).
    assert (
        cli.main(["search", "http retry", "--root", str(root), "--json"]) == 0
    )
    lexical_doc = _json_out(capsys)
    assert lexical_doc["hits"][0]["score"] >= search.LOW_CONFIDENCE_THRESHOLD
    assert "note" not in lexical_doc

    assert (
        cli.main(
            [
                "search",
                "http retry",
                "--root",
                str(root),
                "--json",
                "--scorer",
                "both",
            ]
        )
        == 0
    )
    both_doc = _json_out(capsys)
    # The fused score itself is well under LOW_CONFIDENCE_THRESHOLD --
    # proves this isn't passing by accident of the fused scale being
    # high enough on its own.
    assert both_doc["hits"][0]["score"] < search.LOW_CONFIDENCE_THRESHOLD
    # The exclusion note itself must still stay silent (the underlying
    # top score is confident) -- but --scorer both's own scale note
    # fires unconditionally, so "note" is present with only that text,
    # not the exclusion hint.
    assert "note" in both_doc
    assert "test-file" not in both_doc["note"]
    assert "reciprocal-rank-fusion" in both_doc["note"]


def test_blended_scores_precomputed_relevance_matches_subset_call() -> None:
    """``precomputed_relevance`` must make ``blended_scores`` agree with
    itself when the same candidates are scored via two different-sized
    batches -- the exact inconsistency round-13 found in
    ``search.rank`` (a candidate's relevance changing depending on
    which other candidates happen to be in the batch it's scored
    against). Corpus-size-independent unit test of the property §1's
    fix restores, complementing the four end-to-end shape fixtures
    above.
    """
    task = TaskContext(terms=("cancel", "task", "execution"))
    target = Candidate(
        "target",
        "cancelTask cancelTask cancelTask cancelTask(): void",
        "a.ts",
    )
    other = Candidate(
        "other",
        "captureHookExecution captureHookExecution captureHookExecution "
        "Records hook execution events for downstream execution "
        "analytics across execution stages and execution phases. "
        "captureHookExecution(ulid, hookName, status, "
        "executionContext, executionTimingMs): void",
        "b.ts",
    )
    distractors = [
        Candidate(
            f"task_{i}",
            f"scheduleTask{i} schedules a background task in the "
            "worker task queue",
            f"d{i}.ts",
        )
        for i in range(30)
    ]
    all_cands = [target, other, *distractors]
    subset = [target, other]
    scorer = BM25Scorer()
    full_relevance = scorer.score(task, all_cands)
    # Old, buggy path: re-score the subset from scratch.
    subset_relevance = scorer.score(task, subset)
    assert full_relevance["target"] != subset_relevance["target"]

    # Fixed path: reuse the full-batch number via precomputed_relevance.
    blended = blended_scores(
        task,
        subset,
        {c.id: 0.0 for c in subset},
        precomputed_relevance=full_relevance,
        w_rel=1.0,
    )
    assert blended["target"] == full_relevance["target"]
