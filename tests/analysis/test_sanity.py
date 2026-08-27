"""``dekko sanity``: the ``classify_miss`` pure blind-spot classifier,
the callers/uses cross-check pipeline, and its JSON shape.

Most blind-spot scenarios force the internal dekko-side query to
report zero hits (``monkeypatch``ing ``sanity._dekko_hits_callers``)
rather than trying to construct a repo whose resolver genuinely misses
a call — the plan's own suggested fallback (a real resolver-blind-spot
repro is fragile and language-specific; the classifier's job is to
explain a grep-only line, and it does that the same way regardless of
*why* dekko didn't also find it). ``classify_miss`` itself is also
tested directly as the pure function it is, with no repo/grep
involved at all.
"""

import json
from pathlib import Path

import pytest

from dekko.analysis import sanity
from dekko.integrations import cli

from conftest import RepoFactory

SIMPLE_REPO = {
    "a.py": (
        "def helper():\n    return 1\n\n\ndef caller():\n    return helper()\n"
    ),
}


def _force_no_dekko_hits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every grep hit land in the grep-only bucket, deterministically."""
    monkeypatch.setattr(
        sanity, "_dekko_hits_callers", lambda *_a, **_kw: ([], [])
    )


# --- classify_miss: pure function, no repo/grep I/O --------------------


def test_classify_miss_qualified_call() -> None:
    cause = sanity.classify_miss(
        "pkg.target(x)",
        "target",
        is_test_file=False,
        unsupported_language=False,
        tests_excluded=True,
    )
    assert cause == sanity.CAUSE_QUALIFIED_CALL


def test_classify_miss_unsupported_language() -> None:
    cause = sanity.classify_miss(
        "value = target(x)",
        "target",
        is_test_file=False,
        unsupported_language=True,
        tests_excluded=True,
    )
    assert cause == sanity.CAUSE_UNSUPPORTED_LANGUAGE


def test_classify_miss_test_filter() -> None:
    cause = sanity.classify_miss(
        "assert target(x) == 1",
        "target",
        is_test_file=True,
        unsupported_language=False,
        tests_excluded=True,
    )
    assert cause == sanity.CAUSE_TEST_FILTER


def test_classify_miss_test_filter_not_applied_when_tests_included() -> None:
    # Same test-file hit, but the dekko-side query never excluded
    # tests to begin with -- "likely filtered by --no-tests" would be
    # a wrong explanation here, so it must not fire.
    cause = sanity.classify_miss(
        "assert target(x) == 1",
        "target",
        is_test_file=True,
        unsupported_language=False,
        tests_excluded=False,
    )
    assert cause == sanity.CAUSE_UNEXPLAINED


def test_classify_miss_generic_name() -> None:
    cause = sanity.classify_miss(
        "value = map(x)",
        "map",
        is_test_file=False,
        unsupported_language=False,
        tests_excluded=True,
    )
    assert cause == sanity.CAUSE_GENERIC_NAME


def test_classify_miss_unexplained() -> None:
    cause = sanity.classify_miss(
        "value = totally_unrelated_wrapper(x)",
        "totally_unrelated_wrapper",
        is_test_file=False,
        unsupported_language=False,
        tests_excluded=True,
    )
    assert cause == sanity.CAUSE_UNEXPLAINED


def test_classify_miss_comment_mention_near_definition() -> None:
    cause = sanity.classify_miss(
        "// Helper is a small utility function.",
        "Helper",
        is_test_file=False,
        unsupported_language=False,
        tests_excluded=True,
        near_own_definition=True,
        looks_like_comment=True,
    )
    assert cause == sanity.CAUSE_COMMENT_MENTION


def test_classify_miss_comment_far_from_definition_falls_through() -> None:
    cause = sanity.classify_miss(
        "// Helper is a small utility function.",
        "Helper",
        is_test_file=False,
        unsupported_language=False,
        tests_excluded=True,
        near_own_definition=False,
        looks_like_comment=True,
    )
    assert cause != sanity.CAUSE_COMMENT_MENTION


def test_classify_miss_near_definition_but_not_comment_shaped() -> None:
    # A real recursive self-call sitting right next to the
    # definition -- proximity alone must never be sufficient.
    cause = sanity.classify_miss(
        "return Helper(x - 1)",
        "Helper",
        is_test_file=False,
        unsupported_language=False,
        tests_excluded=True,
        near_own_definition=True,
        looks_like_comment=False,
    )
    assert cause != sanity.CAUSE_COMMENT_MENTION


def test_classify_miss_qualified_call_still_wins_near_definition() -> None:
    cause = sanity.classify_miss(
        "// e.g. pkg.Helper(3)",
        "Helper",
        is_test_file=False,
        unsupported_language=False,
        tests_excluded=True,
        near_own_definition=True,
        looks_like_comment=True,
    )
    assert cause == sanity.CAUSE_QUALIFIED_CALL


def test_classify_miss_python_docstring_opening_line() -> None:
    cause = sanity.classify_miss(
        '"""Helper does X."""',
        "Helper",
        is_test_file=False,
        unsupported_language=False,
        tests_excluded=True,
        near_own_definition=True,
        looks_like_comment=True,
    )
    assert cause == sanity.CAUSE_COMMENT_MENTION


# --- _looks_like_comment_line: pure, grammar-scoped ---------------------


def test_looks_like_comment_line_slash_style() -> None:
    assert sanity._looks_like_comment_line(
        "  // Helper does X", "pkg/helper.go"
    )
    assert sanity._looks_like_comment_line(
        "  /* Helper does X */", "pkg/helper.go"
    )


def test_looks_like_comment_line_hash_style() -> None:
    assert sanity._looks_like_comment_line(
        "  # Helper does X", "src/helper.py"
    )


def test_looks_like_comment_line_python_docstrings() -> None:
    assert sanity._looks_like_comment_line(
        '"""Helper does X."""', "src/helper.py"
    )
    assert sanity._looks_like_comment_line(
        "'''Helper does X.'''", "src/helper.py"
    )


def test_looks_like_comment_line_plain_code_is_false() -> None:
    assert not sanity._looks_like_comment_line(
        "def helper(x):", "src/helper.py"
    )


def test_looks_like_comment_line_wrapped_multiplication_not_comment() -> None:
    # A gofmt-style line-wrapped recursive-call continuation starting
    # with "*" must not be mistaken for a comment -- bare "*" is no
    # longer in any C-family grammar's marker set.
    for path in (
        "pkg/helper.go",
        "src/helper.rs",
        "src/helper.c",
        "src/Helper.java",
    ):
        assert not sanity._looks_like_comment_line("* Helper(x-1)", path)


def test_looks_like_comment_line_pointer_deref_not_comment() -> None:
    assert not sanity._looks_like_comment_line(
        "*Helper = compute(x);", "src/helper.c"
    )


def test_looks_like_comment_line_c_preprocessor_not_comment() -> None:
    assert not sanity._looks_like_comment_line(
        "#define Helper(x) ...", "src/helper.c"
    )
    assert not sanity._looks_like_comment_line(
        "#include <foo.h>", "src/helper.c"
    )


def test_looks_like_comment_line_python_hash_still_fires() -> None:
    assert sanity._looks_like_comment_line("# Helper does X", "src/helper.py")


def test_looks_like_comment_line_dash_scoped_to_its_own_grammars() -> None:
    assert sanity._looks_like_comment_line(
        "-- Helper does X", "src/helper.lua"
    )
    assert sanity._looks_like_comment_line(
        "-- Helper does X", "src/helper.sql"
    )
    for path in ("pkg/helper.go", "src/Helper.java", "src/helper.ts"):
        assert not sanity._looks_like_comment_line("--counter;", path)


def test_looks_like_comment_line_tier2_spot_checks() -> None:
    assert sanity._looks_like_comment_line(
        "; Helper does X", "src/helper.lisp"
    )
    assert sanity._looks_like_comment_line("% Helper does X", "src/helper.erl")
    assert sanity._looks_like_comment_line("! Helper does X", "src/helper.f90")


def test_looks_like_comment_line_vue_deliberately_unmapped() -> None:
    assert not sanity._looks_like_comment_line(
        "// Helper does X", "src/Helper.vue"
    )


def test_looks_like_comment_line_unsupported_language() -> None:
    assert not sanity._looks_like_comment_line(
        "// Helper does X", "src/Helper.astro"
    )


# --- full pipeline: grep sweep + classification -------------------------


def test_dekko_hits_callers_folds_lined_module_level_into_hits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Round 23 §10: query.py's module_level entries now carry a
    # "lines" key when a site line was recorded. _dekko_hits_callers
    # must fold those (path, line) pairs into hits alongside
    # named-caller sites, matching grep like any other hit, rather
    # than stranding a known-line module-level call site in the
    # line-less bucket where it was previously misclassified as an
    # "unexplained miss" (the exact claude-buddy `server/index.ts:709`
    # shape this fix closes). Only entries with no "lines" key remain
    # in module_level_bare.
    doc = {
        "results": [],
        "module_level": [
            {"path": "server/index.ts", "lines": [709]},
            {"path": "server/path.test.ts"},
        ],
    }
    monkeypatch.setattr(sanity, "_run_query_json", lambda *_a, **_kw: doc)
    hits, module_level_bare = sanity._dekko_hits_callers(
        None, "server/index.ts:buddyStateDir:1"
    )
    assert hits == [("server/index.ts", 709)]
    assert module_level_bare == ["server/path.test.ts"]


def test_sanity_all_matches_reports_clean(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SIMPLE_REPO)
    code = cli.main(["sanity", "helper", "--root", str(root), "--json"])
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["counts"]["grep_only"] == 0
    assert doc["counts"]["matches"] >= 1


def test_sanity_detects_qualified_call_miss(
    make_mapped_repo: RepoFactory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    root = make_mapped_repo(
        {
            "a.py": "def target():\n    return 1\n",
            "b.py": (
                "import pkg\n\n\ndef caller():\n    return pkg.target()\n"
            ),
        }
    )
    _force_no_dekko_hits(monkeypatch)
    code = cli.main(["sanity", "target", "--root", str(root), "--json"])
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    causes = {row["cause"] for row in doc["grep_only"]}
    assert sanity.CAUSE_QUALIFIED_CALL in causes


def test_sanity_detects_test_filter_miss(
    make_mapped_repo: RepoFactory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    root = make_mapped_repo(
        {
            "a.py": "def target():\n    return 1\n",
            "tests/test_a.py": (
                "from a import target\n"
                "\n"
                "\n"
                "def test_target():\n"
                "    assert target() == 1\n"
            ),
        }
    )
    _force_no_dekko_hits(monkeypatch)
    code = cli.main(["sanity", "target", "--root", str(root), "--json"])
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["include_tests"] is False
    causes = {row["cause"] for row in doc["grep_only"]}
    assert sanity.CAUSE_TEST_FILTER in causes


def test_sanity_detects_unsupported_language_miss(
    make_mapped_repo: RepoFactory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    root = make_mapped_repo({"a.py": "def target():\n    return 1\n"})
    (root / "widget.astro").write_text("<script>\n  target();\n</script>\n")
    _force_no_dekko_hits(monkeypatch)
    code = cli.main(["sanity", "target", "--root", str(root), "--json"])
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    causes = {row["cause"] for row in doc["grep_only"]}
    assert sanity.CAUSE_UNSUPPORTED_LANGUAGE in causes


def test_sanity_generic_name_caution(
    make_mapped_repo: RepoFactory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    root = make_mapped_repo(
        {
            "a.py": "def run():\n    return 1\n",
            "b.py": "def other():\n    return run()\n",
        }
    )
    _force_no_dekko_hits(monkeypatch)
    code = cli.main(["sanity", "run", "--root", str(root), "--json"])
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    causes = {row["cause"] for row in doc["grep_only"]}
    assert sanity.CAUSE_GENERIC_NAME in causes


def test_sanity_unexplained_miss_says_so(
    make_mapped_repo: RepoFactory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    root = make_mapped_repo(
        {
            "a.py": "def distinctivelyuniquename():\n    return 1\n",
            "b.py": ("def other():\n    return distinctivelyuniquename()\n"),
        }
    )
    _force_no_dekko_hits(monkeypatch)
    code = cli.main(
        ["sanity", "distinctivelyuniquename", "--root", str(root), "--json"]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    causes = {row["cause"] for row in doc["grep_only"]}
    assert sanity.CAUSE_UNEXPLAINED in causes


def test_sanity_detects_comment_mention_miss(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(
        {
            "a.py": (
                "# helper is a small utility.\n"
                "def helper():\n"
                "    return 1\n"
                "\n"
                "\n"
                "def caller():\n"
                "    return helper()\n"
            ),
        }
    )
    code = cli.main(["sanity", "helper", "--root", str(root), "--json"])
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    causes = {row["cause"] for row in doc["grep_only"]}
    assert sanity.CAUSE_COMMENT_MENTION in causes
    # The definition line itself is out of scope entirely -- not
    # merely reclassified, still excluded from every bucket.
    grep_only_lines = {row["line"] for row in doc["grep_only"]}
    assert 2 not in grep_only_lines


def test_sanity_wrapped_recursive_call_not_comment_mention(
    make_mapped_repo: RepoFactory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    root = make_mapped_repo(
        {
            "helper.go": (
                "package pkg\n"
                "\n"
                "func Helper(x int) int {\n"
                "	return x\n"
                "		* Helper(x-1)\n"
                "}\n"
            ),
        }
    )
    _force_no_dekko_hits(monkeypatch)
    code = cli.main(["sanity", "Helper", "--root", str(root), "--json"])
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    wrapped_row = next(row for row in doc["grep_only"] if row["line"] == 5)
    assert wrapped_row["cause"] != sanity.CAUSE_COMMENT_MENTION


def test_sanity_usages_flag_runs_uses_action(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(
        {"a.py": "import os\n\n\ndef caller():\n    return os.getcwd()\n"}
    )
    code = cli.main(
        ["sanity", "getcwd", "--usages", "--root", str(root), "--json"]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["query_action"] == "uses"
    assert doc["counts"]["matches"] >= 1


def test_sanity_json_shape(
    make_mapped_repo: RepoFactory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    root = make_mapped_repo(
        {
            "a.py": "def target():\n    return 1\n",
            "b.py": "def other():\n    return target()\n",
        }
    )
    _force_no_dekko_hits(monkeypatch)
    code = cli.main(["sanity", "target", "--root", str(root), "--json"])
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert {
        "action",
        "query_action",
        "target",
        "bare_name",
        "matches",
        "dekko_only",
        "grep_only",
        "counts",
        "meta",
    } <= set(doc)
    assert doc["grep_only"], "expected at least one grep-only hit"
    for row in doc["grep_only"]:
        assert {"file", "line", "snippet", "cause"} <= set(row)
    for row in doc["matches"]:
        assert {"file", "line"} <= set(row)
    # Round 23 claude-code.md §2.1: every bucket's meta mirrors
    # ``query --json``'s own Meter.as_dict() shape, and under no
    # truncation reports truncated_by as None.
    for bucket in ("matches", "dekko_only", "grep_only"):
        assert {
            "tokens",
            "returned",
            "total",
            "budget",
            "limit",
            "truncated_by",
        } <= set(doc["meta"][bucket])
        assert doc["meta"][bucket]["truncated_by"] is None
        assert doc["meta"][bucket]["total"] == doc["counts"][bucket]


# --- Round 23 claude-code.md §2.1: report-row truncation disclosure ----


def _make_many_grep_only_hits_repo(
    make_mapped_repo: RepoFactory, n: int
) -> Path:
    """A repo whose ``target`` symbol has ``n`` grep-only call sites,
    one per file -- cheap-to-construct stand-in for the round-23
    report's 379-row ``grep_only`` bucket, small enough to drive
    through an explicit ``--limit``/``--budget`` rather than the real
    ``DEFAULT_REPORT_LIMIT`` (200)."""
    files = {"a.py": "def target():\n    return 1\n"}
    for i in range(n):
        files[f"caller{i}.py"] = f"def other{i}():\n    return target()\n"
    return make_mapped_repo(files)


def test_sanity_json_meta_discloses_limit_truncation(
    make_mapped_repo: RepoFactory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    # Round 23 claude-code.md §2.1: ``sanity --json`` used to cap its
    # row arrays with zero disclosure anywhere in the JSON. Drive the
    # same code path DEFAULT_REPORT_LIMIT (200) exercises, via a small
    # explicit --limit on a 5-hit fixture (cheaper than constructing
    # 200+ real hits).
    root = _make_many_grep_only_hits_repo(make_mapped_repo, 5)
    _force_no_dekko_hits(monkeypatch)
    code = cli.main(
        [
            "sanity",
            "target",
            "--root",
            str(root),
            "--json",
            "--limit",
            "2",
        ]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    meta = doc["meta"]["grep_only"]
    assert meta["truncated_by"] == "limit"
    assert meta["total"] == 5
    assert meta["returned"] == 2
    assert meta["returned"] == len(doc["grep_only"])
    # counts stays exactly as-is -- purely additive schema change.
    assert doc["counts"]["grep_only"] == 5


def test_sanity_json_meta_no_truncation_under_every_cap(
    make_mapped_repo: RepoFactory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    root = _make_many_grep_only_hits_repo(make_mapped_repo, 2)
    _force_no_dekko_hits(monkeypatch)
    code = cli.main(["sanity", "target", "--root", str(root), "--json"])
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["meta"]["matches"]["truncated_by"] is None
    assert doc["meta"]["grep_only"]["truncated_by"] is None


def test_sanity_json_meta_discloses_budget_truncation(
    make_mapped_repo: RepoFactory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    # Distinguishes budget-driven truncation from limit-driven: a
    # generous --limit that never binds, but a --budget small enough
    # that only the first row or two fit.
    root = _make_many_grep_only_hits_repo(make_mapped_repo, 10)
    _force_no_dekko_hits(monkeypatch)
    code = cli.main(
        [
            "sanity",
            "target",
            "--root",
            str(root),
            "--json",
            "--limit",
            "1000",
            "--budget",
            "20",
        ]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    meta = doc["meta"]["grep_only"]
    assert meta["truncated_by"] == "budget"
    assert meta["total"] == 10
    assert meta["returned"] < 10
    assert meta["returned"] == len(doc["grep_only"])


# --- Round 21 Track B1: silent 5,000-line grep truncation ---------------


class _FakeCompletedProcess:
    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = ""


def test_run_grep_reports_truncated_when_over_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # One line past the cap is enough to prove the flag fires; the
    # cap itself (not the exact overshoot) is what matters here.
    n = sanity._MAX_GREP_LINES + 1
    stdout = "\n".join(f"a.py:{i}:target()" for i in range(1, n + 1)) + "\n"
    monkeypatch.setattr(
        sanity.subprocess,
        "run",
        lambda *a, **kw: _FakeCompletedProcess(stdout),
    )

    result = sanity._run_grep(Path("/fake"), "target")

    assert result.truncated is True
    assert len(result.hits) == sanity._MAX_GREP_LINES


def test_run_grep_not_truncated_under_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = "a.py:1:target()\nb.py:2:target()\n"
    monkeypatch.setattr(
        sanity.subprocess,
        "run",
        lambda *a, **kw: _FakeCompletedProcess(stdout),
    )

    result = sanity._run_grep(Path("/fake"), "target")

    assert result.truncated is False
    assert len(result.hits) == 2


def test_sanity_json_suppresses_dekko_only_when_grep_truncated(
    make_mapped_repo: RepoFactory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    # A genuine dekko-resolved call site (a.py:6, "return target()")
    # that a truncated grep sweep simply doesn't happen to contain --
    # this must NOT be reported as a confident "dekko-only" finding,
    # since the sweep can't rule out that grep would have matched it
    # past the cutoff.
    root = make_mapped_repo(
        {
            "a.py": (
                "def target():\n"
                "    return 1\n"
                "\n"
                "\n"
                "def caller():\n"
                "    return target()\n"
            ),
        }
    )
    fake_sweep = sanity.GrepSweepResult(
        hits=[],
        command_text="grep -rn -I -w -F -- target .",
        error=None,
        truncated=True,
        skipped_pathological=0,
    )
    monkeypatch.setattr(sanity, "_run_grep", lambda *a, **kw: fake_sweep)

    code = cli.main(["sanity", "target", "--root", str(root), "--json"])

    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["grep_truncated"] is True
    assert doc["dekko_only"] == []
    assert doc["counts"]["dekko_only"] is None
    assert "dekko_only_note" in doc
    # Round 23 claude-code.md §2.1: the suppressed dekko-only bucket
    # carries no Meter either -- a real count for data explicitly
    # being suppressed as inconclusive would be false confidence,
    # mirroring counts.dekko_only's own None.
    assert doc["meta"]["dekko_only"] is None


def test_sanity_text_reports_truncation_and_inconclusive_dekko_only(
    make_mapped_repo: RepoFactory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    root = make_mapped_repo(
        {
            "a.py": (
                "def target():\n"
                "    return 1\n"
                "\n"
                "\n"
                "def caller():\n"
                "    return target()\n"
            ),
        }
    )
    fake_sweep = sanity.GrepSweepResult(
        hits=[],
        command_text="grep -rn -I -w -F -- target .",
        error=None,
        truncated=True,
        skipped_pathological=0,
    )
    monkeypatch.setattr(sanity, "_run_grep", lambda *a, **kw: fake_sweep)

    code = cli.main(["sanity", "target", "--root", str(root)])

    assert code == 0
    out = capsys.readouterr().out
    assert "note:" in out
    assert "safety cap" in out
    assert "dekko-only: inconclusive" in out
    # The whole spot check is compromised under truncation -- "clean"
    # would be a false-confidence claim even if grep-only happens to
    # be empty.
    assert "clean:" not in out


def test_sanity_json_reports_skipped_pathological_lines(
    make_mapped_repo: RepoFactory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    root = make_mapped_repo({"a.py": "def target():\n    return 1\n"})
    fake_sweep = sanity.GrepSweepResult(
        hits=[],
        command_text="grep -rn -I -w -F -- target .",
        error=None,
        truncated=False,
        skipped_pathological=2,
    )
    monkeypatch.setattr(sanity, "_run_grep", lambda *a, **kw: fake_sweep)

    code = cli.main(["sanity", "target", "--root", str(root), "--json"])

    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["grep_skipped_pathological"] == 2
    assert "grep_skipped_pathological_note" in doc
    assert (
        "2 lines skipped as pathological"
        in (doc["grep_skipped_pathological_note"])
    )


# --- Round 21 Track B2: pathological-line guard + snippet cap -----------


def test_run_grep_skips_pathological_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    huge = "x" * (sanity._PATHOLOGICAL_LINE_CHARS + 100)
    stdout = f"a.py:1:target()\ndata.json:2:{huge}\n"
    monkeypatch.setattr(
        sanity.subprocess,
        "run",
        lambda *a, **kw: _FakeCompletedProcess(stdout),
    )

    result = sanity._run_grep(Path("/fake"), "target")

    assert result.skipped_pathological == 1
    assert len(result.hits) == 1
    assert result.hits[0].path == "a.py"


def test_grep_row_caps_snippet_length() -> None:
    long_snippet = "target(" + "x" * 1000 + ")"
    hit = sanity.GrepHit(path="a.py", line=1, snippet=long_snippet)

    row = sanity._grep_row(hit)

    assert len(row["snippet"]) <= sanity._SNIPPET_MAX_CHARS + len(
        "...(truncated)"
    )
    assert row["snippet"].endswith("...(truncated)")


def test_grep_row_short_snippet_unchanged() -> None:
    hit = sanity.GrepHit(path="a.py", line=1, snippet="  target(x)  ")

    row = sanity._grep_row(hit)

    assert row["snippet"] == "target(x)"


def test_classify_miss_still_sees_full_snippet_not_capped() -> None:
    # Classification must run against the hit's real, untruncated
    # text -- a qualified-call marker sitting past
    # ``_SNIPPET_MAX_CHARS`` must still be found.
    padding = "x" * (sanity._SNIPPET_MAX_CHARS + 50)
    snippet = f"# {padding} pkg.target(x)"
    cause = sanity.classify_miss(
        snippet,
        "target",
        is_test_file=False,
        unsupported_language=False,
        tests_excluded=True,
    )
    assert cause == sanity.CAUSE_QUALIFIED_CALL


# --- Round 21 Track B3: import/require-statement classifier -------------


def test_classify_miss_esm_named_import() -> None:
    cause = sanity.classify_miss(
        "import { target } from './a';",
        "target",
        is_test_file=False,
        unsupported_language=False,
        tests_excluded=True,
    )
    assert cause == sanity.CAUSE_IMPORT_STATEMENT


def test_classify_miss_esm_named_import_multiple() -> None:
    cause = sanity.classify_miss(
        "import { other, target, another } from './a';",
        "target",
        is_test_file=False,
        unsupported_language=False,
        tests_excluded=True,
    )
    assert cause == sanity.CAUSE_IMPORT_STATEMENT


def test_classify_miss_esm_type_import() -> None:
    cause = sanity.classify_miss(
        "import type { target } from './a';",
        "target",
        is_test_file=False,
        unsupported_language=False,
        tests_excluded=True,
    )
    assert cause == sanity.CAUSE_IMPORT_STATEMENT


def test_classify_miss_esm_default_import() -> None:
    cause = sanity.classify_miss(
        "import target from './a';",
        "target",
        is_test_file=False,
        unsupported_language=False,
        tests_excluded=True,
    )
    assert cause == sanity.CAUSE_IMPORT_STATEMENT


def test_classify_miss_esm_namespace_import() -> None:
    cause = sanity.classify_miss(
        "import * as target from './a';",
        "target",
        is_test_file=False,
        unsupported_language=False,
        tests_excluded=True,
    )
    assert cause == sanity.CAUSE_IMPORT_STATEMENT


def test_classify_miss_python_from_import() -> None:
    cause = sanity.classify_miss(
        "from a import target",
        "target",
        is_test_file=False,
        unsupported_language=False,
        tests_excluded=True,
    )
    assert cause == sanity.CAUSE_IMPORT_STATEMENT


def test_classify_miss_python_from_import_multiple() -> None:
    cause = sanity.classify_miss(
        "from a import other, target, another",
        "target",
        is_test_file=False,
        unsupported_language=False,
        tests_excluded=True,
    )
    assert cause == sanity.CAUSE_IMPORT_STATEMENT


def test_classify_miss_python_from_import_indented() -> None:
    cause = sanity.classify_miss(
        "    from a import target",
        "target",
        is_test_file=False,
        unsupported_language=False,
        tests_excluded=True,
    )
    assert cause == sanity.CAUSE_IMPORT_STATEMENT


def test_classify_miss_cjs_require_destructure() -> None:
    cause = sanity.classify_miss(
        "const { target } = require('./a');",
        "target",
        is_test_file=False,
        unsupported_language=False,
        tests_excluded=True,
    )
    assert cause == sanity.CAUSE_IMPORT_STATEMENT


def test_classify_miss_cjs_require_plain_assignment() -> None:
    cause = sanity.classify_miss(
        "const target = require('./a');",
        "target",
        is_test_file=False,
        unsupported_language=False,
        tests_excluded=True,
    )
    assert cause == sanity.CAUSE_IMPORT_STATEMENT


def test_classify_miss_require_call_without_name_not_import() -> None:
    # A require(...) call on the line, but the bare name isn't
    # actually mentioned on it -- must not false-positive.
    cause = sanity.classify_miss(
        "const other = require('./target');",
        "target",
        is_test_file=False,
        unsupported_language=False,
        tests_excluded=True,
    )
    assert cause != sanity.CAUSE_IMPORT_STATEMENT


def test_classify_miss_word_import_mid_sentence_not_import_statement() -> None:
    # The word "import" appearing mid-line/mid-sentence (prose, not a
    # real import statement) must never false-positive -- only a line
    # that genuinely opens with the import/require syntax counts.
    cause = sanity.classify_miss(
        "# remember to import the target module before calling this",
        "target",
        is_test_file=False,
        unsupported_language=False,
        tests_excluded=True,
    )
    assert cause != sanity.CAUSE_IMPORT_STATEMENT


def test_classify_miss_import_of_other_name_not_flagged() -> None:
    # An import statement that exists on the line but doesn't actually
    # name the target must not be misclassified.
    cause = sanity.classify_miss(
        "import { other } from './a';",
        "target",
        is_test_file=False,
        unsupported_language=False,
        tests_excluded=True,
    )
    assert cause != sanity.CAUSE_IMPORT_STATEMENT


def test_classify_miss_qualified_call_wins_over_import_shape() -> None:
    # Precedence: a genuine qualified call is checked first, even on a
    # line that also happens to contain import-like text.
    cause = sanity.classify_miss(
        "pkg.target(x)  # not import { target }",
        "target",
        is_test_file=False,
        unsupported_language=False,
        tests_excluded=True,
    )
    assert cause == sanity.CAUSE_QUALIFIED_CALL


def test_sanity_detects_import_statement_miss(
    make_mapped_repo: RepoFactory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    root = make_mapped_repo(
        {
            "a.py": "def target():\n    return 1\n",
            "b.js": (
                "import { target } from './a';\n\nfunction caller() {\n"
                "  return target();\n}\n"
            ),
        }
    )
    _force_no_dekko_hits(monkeypatch)
    code = cli.main(["sanity", "target", "--root", str(root), "--json"])
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    causes = {row["cause"] for row in doc["grep_only"]}
    assert sanity.CAUSE_IMPORT_STATEMENT in causes


# --- multi-line destructured import member (round 22 §8) ---------------


def test_classify_miss_multiline_import_member() -> None:
    # classify_miss itself stays pure/I/O-free -- the caller computes
    # looks_like_import_member and passes it in, same contract as
    # near_own_definition/looks_like_comment.
    cause = sanity.classify_miss(
        "  target,",
        "target",
        is_test_file=False,
        unsupported_language=False,
        tests_excluded=True,
        looks_like_import_member=True,
    )
    assert cause == sanity.CAUSE_IMPORT_STATEMENT


def test_classify_miss_multiline_import_member_false_stays_unexplained() -> (
    None
):
    cause = sanity.classify_miss(
        "  target,",
        "target",
        is_test_file=False,
        unsupported_language=False,
        tests_excluded=True,
        looks_like_import_member=False,
    )
    assert cause == sanity.CAUSE_UNEXPLAINED


def test_looks_like_multiline_import_member_detects_bare_member(
    tmp_path: Path,
) -> None:
    (tmp_path / "buddy.ts").write_text(
        "import {\n"
        "  buddyStateDir,\n"
        "  claudeSettingsPath,\n"
        "} from '../server/path.ts';\n"
    )
    hit = sanity.GrepHit(path="buddy.ts", line=2, snippet="  buddyStateDir,")
    assert sanity._looks_like_multiline_import_member(
        tmp_path, hit, "buddyStateDir"
    )


def test_looks_like_multiline_import_member_second_name_also_detected(
    tmp_path: Path,
) -> None:
    (tmp_path / "buddy.ts").write_text(
        "import {\n"
        "  buddyStateDir,\n"
        "  claudeSettingsPath,\n"
        "} from '../server/path.ts';\n"
    )
    hit = sanity.GrepHit(
        path="buddy.ts", line=3, snippet="  claudeSettingsPath,"
    )
    assert sanity._looks_like_multiline_import_member(
        tmp_path, hit, "claudeSettingsPath"
    )


def test_looks_like_multiline_import_member_false_when_block_closed(
    tmp_path: Path,
) -> None:
    # A bare "name," line appearing *after* an import block has already
    # closed (e.g. one argument of an unrelated multi-line function
    # call) must not be misclassified as an import member.
    (tmp_path / "a.ts").write_text(
        "import {\n"
        "  other,\n"
        "} from './mod';\n"
        "\n"
        "doSomething(\n"
        "  target,\n"
        "  another,\n"
        ");\n"
    )
    hit = sanity.GrepHit(path="a.ts", line=6, snippet="  target,")
    assert not sanity._looks_like_multiline_import_member(
        tmp_path, hit, "target"
    )


def test_looks_like_multiline_import_member_false_without_bare_line(
    tmp_path: Path,
) -> None:
    # Cheap bail-out: a line that isn't a bare "name,"/"name" at all
    # (e.g. it also contains a call) is never treated as an import
    # member, regardless of surrounding context.
    (tmp_path / "a.ts").write_text(
        "import {\n  target,\n} from './mod';\n\ntarget(1);\n"
    )
    hit = sanity.GrepHit(path="a.ts", line=5, snippet="target(1);")
    assert not sanity._looks_like_multiline_import_member(
        tmp_path, hit, "target"
    )


def test_looks_like_multiline_import_member_nearest_opener_wins(
    tmp_path: Path,
) -> None:
    # Round 23 claude-buddy.md §2.1: the prior any()/any() scan let an
    # unrelated, already-closed earlier import's "}" falsely "close" a
    # genuinely still-open block sitting directly above the hit, as
    # soon as the window contained *any* opener and *any* closer
    # anywhere, regardless of which opener each closer actually
    # belonged to. Here an unrelated single-line import (self-closing)
    # and an unrelated multi-line import (opens and closes fully
    # within the window) both sit above the real, still-open block
    # whose member is the hit -- the old code saw an opener (any of
    # the three "import {"s) and a closer (either unrelated close) and
    # returned False; the fix must find the *nearest* opener (the real
    # block's own) has no intervening close and return True.
    (tmp_path / "state.ts").write_text(
        "import { unrelated } from 'os';\n"
        "import {\n"
        "  other,\n"
        "} from 'fs';\n"
        "\n"
        "import {\n"
        "  buddyStateDir,\n"
        "  claudeSettingsPath,\n"
        "} from './path';\n"
    )
    hit = sanity.GrepHit(path="state.ts", line=7, snippet="  buddyStateDir,")
    assert sanity._looks_like_multiline_import_member(
        tmp_path, hit, "buddyStateDir"
    )


def test_looks_like_multiline_import_member_many_unrelated_imports_stacked(
    tmp_path: Path,
) -> None:
    # Same shape as above, but with three unrelated imports stacked
    # above the real block (mixing single-line and already-closed
    # multi-line) -- confirms the fix is genuinely nearest-opener
    # based, not just "handle one extra import".
    (tmp_path / "state.ts").write_text(
        "import { a } from 'os';\n"
        "import {\n"
        "  b,\n"
        "} from 'fs';\n"
        "import { c } from 'path';\n"
        "\n"
        "import {\n"
        "  buddyStateDir,\n"
        "} from './path';\n"
    )
    hit = sanity.GrepHit(path="state.ts", line=8, snippet="  buddyStateDir,")
    assert sanity._looks_like_multiline_import_member(
        tmp_path, hit, "buddyStateDir"
    )


def test_looks_like_multiline_import_member_single_line_not_dangling(
    tmp_path: Path,
) -> None:
    # Check-order case: a complete single-line import immediately
    # above the hit matches both the opener pattern (starts with
    # "import {") and contains "}" -- it must be read as closed, not
    # misread as a dangling opener that would otherwise cause a
    # completely unrelated bare "name," line below it to be
    # misclassified as an import member.
    (tmp_path / "a.ts").write_text(
        "import { unrelated } from 'os';\ntarget,\n"
    )
    hit = sanity.GrepHit(path="a.ts", line=2, snippet="target,")
    assert not sanity._looks_like_multiline_import_member(
        tmp_path, hit, "target"
    )


def test_sanity_detects_multiline_destructured_import_member_miss(
    make_mapped_repo: RepoFactory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    # Round 22 claude-buddy.md §2.4: the residual gap in the
    # single-line import fix -- a multi-line destructured import puts
    # the bare-name hit on a line with no import/{/from token at all.
    root = make_mapped_repo(
        {
            "path.ts": "export function buddyStateDir(): string {\n"
            "  return '/tmp';\n"
            "}\n",
            "index.ts": (
                "import {\n"
                "  buddyStateDir,\n"
                "  claudeSettingsPath,\n"
                "} from './path';\n"
                "\n"
                "function main() {\n"
                "  return buddyStateDir();\n"
                "}\n"
            ),
        }
    )
    _force_no_dekko_hits(monkeypatch)
    code = cli.main(["sanity", "buddyStateDir", "--root", str(root), "--json"])
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    rows_by_line = {row["line"]: row["cause"] for row in doc["grep_only"]}
    # The destructured-import line itself (index.ts:2) must classify
    # as an import statement, not fall through to unexplained.
    assert rows_by_line[2] == sanity.CAUSE_IMPORT_STATEMENT


def test_sanity_multiline_import_member_with_unrelated_import_above(
    make_mapped_repo: RepoFactory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    # Round 23 claude-buddy.md §2.1 end-to-end regression: an unrelated
    # single-line import sits above the real, still-open multi-line
    # import block whose member is the hit -- the exact shape that
    # defeated the flat any()/any() scan on any file with more than
    # one import statement above the target block (the common case on
    # a real codebase, per the round-23 repro).
    root = make_mapped_repo(
        {
            "path.ts": "export function buddyStateDir(): string {\n"
            "  return '/tmp';\n"
            "}\n",
            "index.ts": (
                "import { unrelated } from 'os';\n"
                "\n"
                "import {\n"
                "  buddyStateDir,\n"
                "  claudeSettingsPath,\n"
                "} from './path';\n"
                "\n"
                "function main() {\n"
                "  return buddyStateDir();\n"
                "}\n"
            ),
        }
    )
    _force_no_dekko_hits(monkeypatch)
    code = cli.main(["sanity", "buddyStateDir", "--root", str(root), "--json"])
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    rows_by_line = {row["line"]: row["cause"] for row in doc["grep_only"]}
    assert rows_by_line[4] == sanity.CAUSE_IMPORT_STATEMENT


# --- other same-named symbols' own def lines (round 22 §10) -------------


def test_sanity_excludes_unrelated_same_named_symbols_own_def_line(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # Round 22 zed.md §3.3: dekko's own map already knows a genuinely
    # unrelated MetalRenderer.new_internal's own declaration line is a
    # definition, not a call site -- but only the *query target's own*
    # definition line was excluded from grep-only, so this unrelated
    # same-bare-named symbol's own def line still landed in
    # CAUSE_UNEXPLAINED. Mirrors the zed repro's shape: two
    # `new_internal`s in different files, query the first by qualified
    # path so it resolves unambiguously.
    root = make_mapped_repo(
        {
            "editor.py": (
                "def new_internal():\n"
                "    return 1\n"
                "\n"
                "\n"
                "def entry():\n"
                "    return new_internal()\n"
            ),
            "renderer.py": "def new_internal():\n    return 2\n",
        }
    )
    code = cli.main(
        [
            "sanity",
            "editor.py:new_internal",
            "--root",
            str(root),
            "--json",
        ]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    grep_only_locs = {(row["file"], row["line"]) for row in doc["grep_only"]}
    assert ("renderer.py", 1) not in grep_only_locs
    assert doc["counts"]["grep_only"] == 0


def test_sanity_near_own_definition_checks_every_same_named_symbol(
    make_mapped_repo: RepoFactory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    # The comment-proximity check must also widen: a comment mentioning
    # the bare name near an *unrelated* same-named symbol's own
    # definition is just as much "not a call site" as one near the
    # query target's own definition.
    root = make_mapped_repo(
        {
            "editor.py": "def new_internal():\n    return 1\n",
            "renderer.py": (
                "# new_internal creates the renderer state.\n"
                "def new_internal():\n"
                "    return 2\n"
            ),
        }
    )
    _force_no_dekko_hits(monkeypatch)
    code = cli.main(
        [
            "sanity",
            "editor.py:new_internal",
            "--root",
            str(root),
            "--json",
        ]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    rows_by_line = {
        row["line"]: row["cause"]
        for row in doc["grep_only"]
        if row["file"] == "renderer.py"
    }
    assert rows_by_line.get(1) == sanity.CAUSE_COMMENT_MENTION
