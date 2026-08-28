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
import subprocess
from pathlib import Path
from typing import Any

import pytest

from dekko.analysis import sanity
from dekko.integrations import cli
from dekko.render import mapfile

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


def test_classify_miss_header_comment_far_from_definition() -> None:
    # Round 24 07-sanity-comment-mention-file-header-gap.md: a
    # module-header comment naming the symbol, far outside
    # near_own_definition's proximity window, must still classify as
    # a comment mention via the new independent qualifying path.
    cause = sanity.classify_miss(
        "//      claudeConfigDir, buddyStateDir.",
        "buddyStateDir",
        is_test_file=False,
        unsupported_language=False,
        tests_excluded=True,
        near_own_definition=False,
        looks_like_comment=True,
        in_leading_header_comment=True,
    )
    assert cause == sanity.CAUSE_COMMENT_MENTION


def test_classify_miss_header_flag_alone_not_enough() -> None:
    # Comment-shape stays a hard requirement -- only the proximity
    # side of the AND became an OR, not the comment-shape side.
    cause = sanity.classify_miss(
        "return buddyStateDir(x)",
        "buddyStateDir",
        is_test_file=False,
        unsupported_language=False,
        tests_excluded=True,
        near_own_definition=False,
        looks_like_comment=False,
        in_leading_header_comment=True,
    )
    assert cause != sanity.CAUSE_COMMENT_MENTION


def test_classify_miss_near_definition_unchanged_without_header_flag() -> None:
    # Existing adjacent-doc-comment path stays correct and unaffected
    # when the new flag simply isn't set.
    cause = sanity.classify_miss(
        "// Helper is a small utility function.",
        "Helper",
        is_test_file=False,
        unsupported_language=False,
        tests_excluded=True,
        near_own_definition=True,
        looks_like_comment=True,
        in_leading_header_comment=False,
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


def test_sanity_detects_header_comment_mention_far_from_definition(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # Mirrors the real gap confirmed against claude-buddy's
    # server/path.ts (round 24
    # 07-sanity-comment-mention-file-header-gap.md): a module-header
    # comment block naming several exports sits well outside
    # _COMMENT_PROXIMITY_LINES of any one definition, so only the new
    # in_leading_header_comment path -- not near_own_definition --
    # can explain the miss.
    root = make_mapped_repo(
        {
            "path.ts": (
                "// Path utilities and helpers\n"
                "//\n"
                "// Two related concerns live here:\n"
                "//   1. Path normalization (Windows compat).\n"
                "//   2. Resolution of Claude Code config / state "
                "paths --\n"
                "//      claudeConfigDir, buddyStateDir.\n"
                "//      These honor CLAUDE_CONFIG_DIR.\n"
                "//\n"
                "// The shell counterpart lives in scripts/paths.sh.\n"
                "\n"
                "import { join } from 'path';\n"
                "\n"
                "export function toUnixPath(p: string): string {\n"
                "  return p;\n"
                "}\n"
                "\n"
                "export function buddyStateDir(): string {\n"
                "  return join('state');\n"
                "}\n"
            ),
        }
    )
    code = cli.main(["sanity", "buddyStateDir", "--root", str(root), "--json"])
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    rows_by_line = {row["line"]: row["cause"] for row in doc["grep_only"]}
    # Line 6 is 11 lines from buddyStateDir's own definition (line
    # 17) -- well past _COMMENT_PROXIMITY_LINES -- yet must still
    # classify as a comment mention, not fall through to unexplained.
    assert rows_by_line.get(6) == sanity.CAUSE_COMMENT_MENTION


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


# --- leading-header-comment mention (round 24 plan 07) -----------------


def test_in_leading_header_comment_true_for_module_header_shape(
    tmp_path: Path,
) -> None:
    # Mirrors claude-buddy's server/path.ts: an uninterrupted comment
    # run from line 1 through the hit line, well before the file's
    # first real code.
    (tmp_path / "path.ts").write_text(
        "// Path utilities and helpers\n"
        "//\n"
        "// Two related concerns live here:\n"
        "//   1. Path normalization (Windows compat) -- toUnixPath().\n"
        "//   2. Resolution of Claude Code config / state paths --\n"
        "//      claudeConfigDir, buddyStateDir.\n"
        "//      These honor CLAUDE_CONFIG_DIR.\n"
        "//\n"
        "// The shell counterpart lives in scripts/paths.sh.\n"
        "\n"
        "import { join } from 'path';\n"
    )
    hit = sanity.GrepHit(
        path="path.ts",
        line=6,
        snippet="//      claudeConfigDir, buddyStateDir.",
    )
    assert sanity._in_leading_header_comment(tmp_path, hit)


def test_in_leading_header_comment_false_past_scan_cap(
    tmp_path: Path,
) -> None:
    # Even an uninterrupted comment run for its first
    # _HEADER_SCAN_LINES + 1 lines is deliberately not read past the
    # cap -- a hit this far into the file is never treated as part of
    # a "module header" mention.
    lines = ["// header line naming target\n"] * (
        sanity._HEADER_SCAN_LINES + 1
    )
    (tmp_path / "big.ts").write_text("".join(lines))
    hit = sanity.GrepHit(
        path="big.ts",
        line=sanity._HEADER_SCAN_LINES + 1,
        snippet="// header line naming target",
    )
    assert not sanity._in_leading_header_comment(tmp_path, hit)


def test_in_leading_header_comment_false_when_no_header_at_all(
    tmp_path: Path,
) -> None:
    # Line 1 is real code -- this shape stays correctly gated by
    # near_own_definition alone, unchanged.
    (tmp_path / "code.ts").write_text(
        "import { join } from 'path';\n"
        "\n"
        "// mentions target here, but not from line 1\n"
        "export function other() {}\n"
    )
    hit = sanity.GrepHit(
        path="code.ts",
        line=3,
        snippet="// mentions target here, but not from line 1",
    )
    assert not sanity._in_leading_header_comment(tmp_path, hit)


def test_in_leading_header_comment_false_when_interrupted_by_code(
    tmp_path: Path,
) -> None:
    # A header block interrupted by one blank-then-code line before
    # the hit -- confirms the all-comment-or-blank requirement is
    # enforced line-by-line, not just at the hit line.
    (tmp_path / "mixed.ts").write_text(
        "// header line 1\n"
        "// header line 2\n"
        "\n"
        "const x = 1;\n"
        "// target mentioned here, after real code broke the run\n"
    )
    hit = sanity.GrepHit(
        path="mixed.ts",
        line=5,
        snippet="// target mentioned here, after real code broke the run",
    )
    assert not sanity._in_leading_header_comment(tmp_path, hit)


def test_in_leading_header_comment_unreadable_file_is_false(
    tmp_path: Path,
) -> None:
    hit = sanity.GrepHit(path="missing.ts", line=1, snippet="// x")
    assert not sanity._in_leading_header_comment(tmp_path, hit)


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


# --- ``sanity --unused``: classify_unused_reference (pure) -------------


def test_classify_unused_reference_spread_object() -> None:
    bucket, detail = sanity.classify_unused_reference(
        "return { ...TARGET, ...def };", "TARGET", path="a.ts"
    )
    assert (bucket, detail) == ("reference", sanity.SHAPE_SPREAD)


def test_classify_unused_reference_spread_of_bare_name_in_call_arg() -> None:
    bucket, detail = sanity.classify_unused_reference(
        "f(...TARGET);", "TARGET", path="a.ts"
    )
    assert (bucket, detail) == ("reference", sanity.SHAPE_SPREAD)


def test_classify_unused_reference_typeof() -> None:
    bucket, detail = sanity.classify_unused_reference(
        "type ToolDefaultsType = typeof TARGET;", "TARGET", path="a.ts"
    )
    assert (bucket, detail) == ("reference", sanity.SHAPE_TYPEOF)


def test_classify_unused_reference_subscript() -> None:
    bucket, detail = sanity.classify_unused_reference(
        "const v = TARGET[key];", "TARGET", path="a.ts"
    )
    assert (bucket, detail) == ("reference", sanity.SHAPE_SUBSCRIPT)


def test_classify_unused_reference_bare_call() -> None:
    bucket, detail = sanity.classify_unused_reference(
        "TARGET();", "TARGET", path="a.ts"
    )
    assert (bucket, detail) == ("reference", sanity.SHAPE_CALL)


def test_classify_unused_reference_qualified_call() -> None:
    bucket, detail = sanity.classify_unused_reference(
        "pkg.TARGET();", "TARGET", path="a.py"
    )
    assert (bucket, detail) == ("reference", sanity.SHAPE_CALL)


def test_classify_unused_reference_other_catch_all() -> None:
    # A plain assignment RHS -- not one of the three named shapes, but
    # still a real reference, so it must surface as SHAPE_OTHER rather
    # than being dropped for not matching a known template.
    bucket, detail = sanity.classify_unused_reference(
        "const x = TARGET;", "TARGET", path="a.ts"
    )
    assert (bucket, detail) == ("reference", sanity.SHAPE_OTHER)


def test_classify_unused_reference_import_is_noise() -> None:
    bucket, detail = sanity.classify_unused_reference(
        "import { TARGET } from './a';", "TARGET", path="a.ts"
    )
    assert (bucket, detail) == ("noise", sanity.CAUSE_IMPORT_STATEMENT)


def test_classify_unused_reference_comment_is_noise() -> None:
    bucket, detail = sanity.classify_unused_reference(
        "# mentions TARGET here", "TARGET", path="a.py"
    )
    assert (bucket, detail) == ("noise", sanity.CAUSE_COMMENT_MENTION)


def test_classify_unused_reference_comment_noise_unconditional() -> None:
    # Unlike classify_miss, comment detection here has no
    # near_own_definition gate -- a comment anywhere in the repo is
    # still just a comment, not usage evidence.
    bucket, detail = sanity.classify_unused_reference(
        "// TARGET is mentioned far from its own definition",
        "TARGET",
        path="far/away.ts",
    )
    assert (bucket, detail) == ("noise", sanity.CAUSE_COMMENT_MENTION)


def test_classify_unused_reference_call_wins_over_spread_check_order() -> None:
    # Check-order regression: "...TARGET()" spreads a call's *result*,
    # not TARGET itself -- SHAPE_CALL must win over SHAPE_SPREAD.
    bucket, detail = sanity.classify_unused_reference(
        "f(...TARGET());", "TARGET", path="a.ts"
    )
    assert (bucket, detail) == ("reference", sanity.SHAPE_CALL)


# --- ``sanity --unused``: end-to-end ------------------------------------


def test_sanity_unused_excludes_own_definition_line(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo({"a.py": "def TARGET_NAME():\n    return 1\n"})
    (root / "widget.dat").write_text("noise\n  ...TARGET_NAME\n")
    code = cli.main(
        ["sanity", "--unused", "TARGET_NAME", "--root", str(root), "--json"]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    locs = {(row["file"], row["line"]) for row in doc["reference_hits"]}
    assert ("a.py", 1) not in locs
    assert ("widget.dat", 2) in locs


def test_sanity_unused_catches_reference_shapes_no_reference_query_covers(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # Proves the "general safety net" claim independently of any one
    # language's reference_query fix: a reference living in a file
    # dekko can't parse at all is invisible to calls_in/referenced_in
    # by construction (the TS/JS spread/typeof/subscript gap this doc
    # was originally filed against is already closed as of 0.43.19,
    # so this stands in for "a shape no reference_query covers yet").
    root = make_mapped_repo({"a.py": "def TARGET_NAME():\n    return 1\n"})
    (root / "widget.dat").write_text(
        "noise line\n"
        "  ...TARGET_NAME\n"
        "  typeof TARGET_NAME\n"
        "  TARGET_NAME[0]\n"
    )
    code = cli.main(
        ["sanity", "--unused", "TARGET_NAME", "--root", str(root), "--json"]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["has_dekko_evidence"] is False
    shapes = {row["shape"] for row in doc["reference_hits"]}
    assert shapes == {
        sanity.SHAPE_SPREAD,
        sanity.SHAPE_TYPEOF,
        sanity.SHAPE_SUBSCRIPT,
    }


def test_sanity_unused_json_reports_counts_and_meta(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo({"a.py": "def TARGET_NAME():\n    return 1\n"})
    (root / "widget.dat").write_text(
        "  ...TARGET_NAME\n  typeof TARGET_NAME\n"
    )
    code = cli.main(
        ["sanity", "--unused", "TARGET_NAME", "--root", str(root), "--json"]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["counts"]["reference_hits"] == 2
    meta = doc["meta"]["reference_hits"]
    assert meta["total"] == 2
    assert meta["returned"] == 2
    shapes = {row["shape"] for row in doc["reference_hits"]}
    assert shapes == {sanity.SHAPE_SPREAD, sanity.SHAPE_TYPEOF}


def test_sanity_unused_reports_evidence_present_when_dekko_has_callers(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # sanity --unused doesn't re-run _is_root -- it just reports
    # calls_in/referenced_in evidence directly, so running it against
    # a symbol dekko would never actually flag (it has a real caller)
    # still answers correctly: has_dekko_evidence: true.
    root = make_mapped_repo(
        {
            "a.py": "def target():\n    return 1\n",
            "b.py": "def other():\n    return target()\n",
        }
    )
    code = cli.main(
        ["sanity", "--unused", "target", "--root", str(root), "--json"]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["has_dekko_evidence"] is True


def test_sanity_unused_clean_case_text(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo({"a.py": "def trulyDeadHelper():\n    return 1\n"})
    code = cli.main(
        ["sanity", "--unused", "trulyDeadHelper", "--root", str(root)]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "reference hits found outside definition/import/comment: 0" in out
    assert (
        "clean: no reference evidence found outside definition "
        "-- flagged-unused looks correct" in out
    )


def test_sanity_unused_clean_case_json(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo({"a.py": "def trulyDeadHelper():\n    return 1\n"})
    code = cli.main(
        [
            "sanity",
            "--unused",
            "trulyDeadHelper",
            "--root",
            str(root),
            "--json",
        ]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["reference_hits"] == []
    assert doc["has_dekko_evidence"] is False


def test_sanity_unused_text_call_shaped_summary_wording(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo({"a.py": "def TARGET_NAME():\n    return 1\n"})
    (root / "widget.dat").write_text("  TARGET_NAME()\n  typeof TARGET_NAME\n")
    code = cli.main(["sanity", "--unused", "TARGET_NAME", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "1 call-shaped reference" in out
    assert "possible resolver miss" in out
    assert "1 non-call reference" in out
    assert "verify before deleting" in out


def test_sanity_usages_unused_mutex(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo({"a.py": "def target():\n    return 1\n"})
    code = cli.main(
        [
            "sanity",
            "target",
            "--usages",
            "--unused",
            "target",
            "--root",
            str(root),
        ]
    )
    assert code == 2
    assert "not both" in capsys.readouterr().err


def test_sanity_no_target_and_no_unused_is_usage_error(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo({"a.py": "def target():\n    return 1\n"})
    code = cli.main(["sanity", "--root", str(root)])
    assert code == 2
    assert "requires a target" in capsys.readouterr().err


def test_sanity_unused_generic_name_caution(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo({"a.py": "def run():\n    return 1\n"})
    (root / "widget.dat").write_text("  typeof run\n")
    code = cli.main(
        ["sanity", "--unused", "run", "--root", str(root), "--json"]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["generic_name_caution"] is True
    # Still reported, not suppressed.
    assert doc["counts"]["reference_hits"] == 1


def test_sanity_unused_generic_name_caution_text_note(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo({"a.py": "def run():\n    return 1\n"})
    (root / "widget.dat").write_text("  typeof run\n")
    code = cli.main(["sanity", "--unused", "run", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert sanity.CAUSE_GENERIC_NAME in out


def test_sanity_unused_json_discloses_truncation_and_pathological_skips(
    make_mapped_repo: RepoFactory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    root = make_mapped_repo({"a.py": "def TARGET_NAME():\n    return 1\n"})
    fake_sweep = sanity.GrepSweepResult(
        hits=[
            sanity.GrepHit(
                path="widget.dat", line=2, snippet="  ...TARGET_NAME"
            )
        ],
        command_text="grep -rn -I -w -F -- TARGET_NAME .",
        error=None,
        truncated=True,
        skipped_pathological=3,
    )
    monkeypatch.setattr(sanity, "_run_grep", lambda *a, **kw: fake_sweep)

    code = cli.main(
        ["sanity", "--unused", "TARGET_NAME", "--root", str(root), "--json"]
    )

    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["grep_truncated"] is True
    assert doc["grep_skipped_pathological"] == 3
    assert "reference_hits_note" in doc
    assert "grep_skipped_pathological_note" in doc


def test_sanity_unused_text_discloses_truncation_and_pathological_skips(
    make_mapped_repo: RepoFactory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    root = make_mapped_repo({"a.py": "def TARGET_NAME():\n    return 1\n"})
    fake_sweep = sanity.GrepSweepResult(
        hits=[],
        command_text="grep -rn -I -w -F -- TARGET_NAME .",
        error=None,
        truncated=True,
        skipped_pathological=2,
    )
    monkeypatch.setattr(sanity, "_run_grep", lambda *a, **kw: fake_sweep)

    code = cli.main(["sanity", "--unused", "TARGET_NAME", "--root", str(root)])

    assert code == 0
    out = capsys.readouterr().out
    assert "safety cap" in out
    assert "pathological" in out


# --- ``sanity --all``: repo-wide sweep -----------------------------
#
# .features/plans/round23/24-sanity-all-sweep.md


def test_group_fan_in_symbols_excludes_zero_fan_in(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(
        {
            "a.py": (
                "def called():\n"
                "    return 1\n"
                "\n"
                "\n"
                "def uncalled():\n"
                "    return 2\n"
                "\n"
                "\n"
                "def caller():\n"
                "    return called()\n"
            ),
        }
    )
    index = mapfile.load_map(root)
    assert index is not None
    groups = sanity._group_fan_in_symbols(index)
    assert "called" in groups
    assert "uncalled" not in groups


def test_group_fan_in_symbols_respects_include_tests(
    make_mapped_repo: RepoFactory,
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
    index = mapfile.load_map(root)
    assert index is not None
    # Excluded by default -- target's only caller is a test file.
    assert "target" not in sanity._group_fan_in_symbols(index.without_tests())
    # Included with the full (unfiltered) index, mirroring
    # sanity <target>'s own --include-tests default handling.
    assert "target" in sanity._group_fan_in_symbols(index)


def test_classify_grep_hits_matches_single_target_path(
    make_mapped_repo: RepoFactory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    # The extracted helper and run()'s own classification must agree
    # byte-for-byte -- this is the invariant the whole --all feature
    # depends on (see module docstring's --all paragraph).
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
    causes_from_run = {
        (row["file"], row["line"]): row["cause"] for row in doc["grep_only"]
    }
    assert causes_from_run, "expected at least one grep-only hit"

    index = mapfile.load_map(root)
    assert index is not None
    own_def_locs = frozenset(
        (s.path, s.start_line) for s in index.symbols_by_name.get("target", [])
    )
    sweep = sanity._run_grep(root, "target")
    causes_from_helper = sanity._classify_grep_hits(
        sweep.hits,
        "target",
        root,
        own_def_locs=own_def_locs,
        tests_excluded=True,
    )
    # _force_no_dekko_hits means every non-own-def hit landed in
    # grep_only above, so the two maps cover exactly the same set.
    assert causes_from_helper == causes_from_run


def test_sanity_all_dedupes_grep_by_bare_name(
    make_mapped_repo: RepoFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_mapped_repo(
        {
            "a.py": (
                "def helper():\n"
                "    return 1\n"
                "\n"
                "\n"
                "def caller():\n"
                "    return helper()\n"
            ),
            "b.py": (
                "def helper():\n"
                "    return 2\n"
                "\n"
                "\n"
                "def other():\n"
                "    return helper()\n"
            ),
        }
    )
    index = mapfile.load_map(root)
    assert index is not None
    groups = sanity._group_fan_in_symbols(index.without_tests())
    # Two distinct symbols sharing the bare name "helper", both with
    # their own fan-in -- the overload-set shape the dedup targets.
    assert len(groups["helper"]) == 2

    call_count = 0
    real_run = subprocess.run

    def counting_run(*args: Any, **kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        return real_run(*args, **kwargs)

    monkeypatch.setattr(sanity.subprocess, "run", counting_run)

    code = sanity.run_all(index, root, jobs=1)

    assert code == sanity.EXIT_OK
    assert call_count == 1


def test_sanity_all_aggregates_across_symbols(
    make_mapped_repo: RepoFactory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    root = make_mapped_repo(
        {
            "a.py": (
                "def cleanhelper():\n"
                "    return 1\n"
                "\n"
                "\n"
                "def caller():\n"
                "    return cleanhelper()\n"
            ),
            "b.py": (
                "def distinctivelyuniquename():\n"
                "    return 1\n"
                "\n"
                "\n"
                "def other():\n"
                "    return distinctivelyuniquename()\n"
            ),
        }
    )
    original = sanity._dekko_hits_callers

    def patched(
        index_arg: mapfile.MapIndex, sym_target: str
    ) -> tuple[list[tuple[str, int]], list[str]]:
        # Force only distinctivelyuniquename's own dekko-side query to
        # report zero hits -- its one real call site becomes an
        # unexplained grep-only miss; cleanhelper's stays a real match.
        if "distinctivelyuniquename" in sym_target:
            return [], []
        return original(index_arg, sym_target)

    monkeypatch.setattr(sanity, "_dekko_hits_callers", patched)

    code = cli.main(["sanity", "--all", "--root", str(root), "--json"])
    assert code == 0
    doc = json.loads(capsys.readouterr().out)

    assert doc["aggregate_causes"].get(sanity.CAUSE_UNEXPLAINED) == 1
    flagged_targets = {f["target"] for f in doc["flagged"]}
    assert any("distinctivelyuniquename" in t for t in flagged_targets)
    assert not any("cleanhelper" in t for t in flagged_targets)


def test_sanity_all_json_shape(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(
        {
            "a.py": (
                "def helper():\n"
                "    return 1\n"
                "\n"
                "\n"
                "def caller():\n"
                "    return helper()\n"
            ),
        }
    )
    code = cli.main(["sanity", "--all", "--root", str(root), "--json"])
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert {
        "action",
        "symbols_swept",
        "unique_names_swept",
        "names_truncated",
        "jobs",
        "aggregate_causes",
        "flagged",
        "symbols",
    } <= set(doc)
    assert doc["action"] == "sanity_all"
    assert doc["symbols_swept"] == 1
    assert doc["unique_names_swept"] == 1
    assert doc["names_truncated"] is False
    for row in doc["symbols"]:
        assert {"target", "bare_name", "counts", "causes"} <= set(row)
        assert {"matches", "dekko_only", "grep_only"} <= set(row["counts"])


def test_sanity_all_names_truncated_disclosed(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    files = {}
    for i in range(3):
        files[f"m{i}.py"] = (
            f"def helper{i}():\n"
            f"    return {i}\n"
            "\n"
            "\n"
            f"def caller{i}():\n"
            f"    return helper{i}()\n"
        )
    root = make_mapped_repo(files)
    code = cli.main(
        [
            "sanity",
            "--all",
            "--root",
            str(root),
            "--json",
            "--max-names",
            "1",
        ]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["names_truncated"] is True
    assert doc["unique_names_swept"] == 1
    assert "names_truncated_note" in doc


def test_sanity_all_fail_on_unexplained_exit_code(
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

    code = cli.main(["sanity", "--all", "--root", str(root), "--json"])
    assert code == sanity.EXIT_OK
    capsys.readouterr()

    code = cli.main(
        [
            "sanity",
            "--all",
            "--root",
            str(root),
            "--json",
            "--fail-on-unexplained",
        ]
    )
    assert code == sanity.EXIT_UNEXPLAINED_FOUND


def test_sanity_all_target_and_all_mutually_exclusive(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo({"a.py": "def target():\n    return 1\n"})
    code = cli.main(["sanity", "target", "--all", "--root", str(root)])
    assert code == 2
    assert "not both" in capsys.readouterr().err


def test_sanity_all_usages_incompatible(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo({"a.py": "def target():\n    return 1\n"})
    code = cli.main(["sanity", "--all", "--usages", "--root", str(root)])
    assert code == 2
    assert "--usages" in capsys.readouterr().err


def test_sanity_all_unused_incompatible(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo({"a.py": "def target():\n    return 1\n"})
    code = cli.main(
        ["sanity", "--all", "--unused", "target", "--root", str(root)]
    )
    assert code == 2
    assert "--unused" in capsys.readouterr().err


def test_sanity_all_cli_smoke(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SIMPLE_REPO)
    code = cli.main(["sanity", "--all", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "dekko sanity --all" in out


def _buggy_looks_like_multiline_import_member(
    root: Path, hit: sanity.GrepHit, bare_name: str
) -> bool:
    """The pre-0.43.18 flat ``any()``/``any()`` implementation this
    module's round-23 regression fixed (commit ``b5f692f``) -- a
    still-open multi-line destructured import block is falsely read as
    "closed" as soon as *any* line in the lookback window contains
    ``}``, regardless of which opener it actually belongs to."""
    stripped = hit.snippet.strip().rstrip(",")
    if stripped != bare_name:
        return False
    try:
        lines = (
            (root / hit.path)
            .read_text(encoding="utf-8", errors="replace")
            .splitlines()
        )
    except OSError:
        return False
    start = max(0, hit.line - 1 - sanity._IMPORT_WINDOW_LINES)
    window = lines[start : hit.line - 1]
    opened = any(sanity._IMPORT_OPEN_BRACE.match(ln) for ln in window)
    if not opened:
        return False
    closed_before_hit = any("}" in ln for ln in window)
    return not closed_before_hit


def test_sanity_all_regression_would_have_caught_multiline_import_bug(
    make_mapped_repo: RepoFactory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    # Round 23's own thesis, made concrete: sweeping every fan-in
    # symbol with the *pre-fix* buggy classifier flags a nonzero
    # unexplained count on the exact shape that defeated it (an
    # unrelated single-line import sitting above the real, still-open
    # multi-line destructured import block) -- proof `sanity --all`
    # would have surfaced this without anyone hand-picking
    # `buddyStateDir`. The fixed implementation (no monkeypatch) must
    # NOT flag it.
    root = make_mapped_repo(
        {
            "path.ts": (
                "export function buddyStateDir(): string {\n"
                "  return '/tmp';\n"
                "}\n"
            ),
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

    code = cli.main(["sanity", "--all", "--root", str(root), "--json"])
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["aggregate_causes"].get(sanity.CAUSE_UNEXPLAINED, 0) == 0

    monkeypatch.setattr(
        sanity,
        "_looks_like_multiline_import_member",
        _buggy_looks_like_multiline_import_member,
    )
    code = cli.main(["sanity", "--all", "--root", str(root), "--json"])
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["aggregate_causes"].get(sanity.CAUSE_UNEXPLAINED, 0) > 0


# --- receiver-mismatch cue (round 23 plan 25) --------------------------
#
# See .features/plans/round23/25-sanity-receiver-mismatch-cue.md. A
# grep-only hit for a single-repo-candidate *method* target can be
# flagged CAUSE_LIKELY_EXTERNAL_COLLISION when neither the hit's own
# line nor its file's top-of-file imports mention the target's
# declaring type -- the cheap textual proxy for "this is almost
# certainly an unrelated external-library method sharing the bare
# name" (the spring-boot ``isTrue``/AssertJ repro this design closes).


def test_receiver_mismatch_type_on_own_line_is_false(tmp_path: Path) -> None:
    hit = sanity.GrepHit(
        path="b.py", line=3, snippet="    return Widget.isTrue()"
    )
    assert sanity._receiver_mismatch(tmp_path, hit, "Widget") is False


def test_receiver_mismatch_type_in_file_imports_is_false(
    tmp_path: Path,
) -> None:
    (tmp_path / "b.py").write_text(
        "from a import Widget\n\n\ndef unrelated():\n    return isTrue()\n"
    )
    hit = sanity.GrepHit(path="b.py", line=5, snippet="    return isTrue()")
    assert sanity._receiver_mismatch(tmp_path, hit, "Widget") is False


def test_receiver_mismatch_type_absent_is_true(tmp_path: Path) -> None:
    (tmp_path / "b.py").write_text("def unrelated():\n    return isTrue()\n")
    hit = sanity.GrepHit(path="b.py", line=2, snippet="    return isTrue()")
    assert sanity._receiver_mismatch(tmp_path, hit, "Widget") is True


def test_receiver_mismatch_unreadable_file_is_false(tmp_path: Path) -> None:
    hit = sanity.GrepHit(
        path="missing.py", line=1, snippet="    return isTrue()"
    )
    assert sanity._receiver_mismatch(tmp_path, hit, "Widget") is False


def test_classify_miss_likely_unrelated_external() -> None:
    cause = sanity.classify_miss(
        "    return isTrue()",
        "isTrue",
        is_test_file=False,
        unsupported_language=False,
        tests_excluded=True,
        likely_unrelated_external=True,
    )
    assert cause == sanity.CAUSE_LIKELY_EXTERNAL_COLLISION


def test_classify_miss_likely_unrelated_external_preempts_test_filter() -> (
    None
):
    # Without the new flag this line would land on CAUSE_TEST_FILTER --
    # the exact regression this design targets (round 23
    # spring-boot.md §4: a grep-only AssertJ-style hit in a test file
    # read as "re-run with --include-tests" when it was never a real
    # caller to begin with).
    cause = sanity.classify_miss(
        "    return isTrue()",
        "isTrue",
        is_test_file=True,
        unsupported_language=False,
        tests_excluded=True,
        likely_unrelated_external=True,
    )
    assert cause == sanity.CAUSE_LIKELY_EXTERNAL_COLLISION


def test_classify_miss_likely_unrelated_external_preempts_generic_name() -> (
    None
):
    cause = sanity.classify_miss(
        "    return map()",
        "map",
        is_test_file=False,
        unsupported_language=False,
        tests_excluded=True,
        likely_unrelated_external=True,
    )
    assert cause == sanity.CAUSE_LIKELY_EXTERNAL_COLLISION


def test_classify_miss_unsupported_language_wins_over_receiver_cue() -> None:
    # Precedence unchanged: a hard "dekko can't parse this file at
    # all" fact still outranks a heuristic guess.
    cause = sanity.classify_miss(
        "    return isTrue()",
        "isTrue",
        is_test_file=False,
        unsupported_language=True,
        tests_excluded=True,
        likely_unrelated_external=True,
    )
    assert cause == sanity.CAUSE_UNSUPPORTED_LANGUAGE


def test_resolve_declaring_type_method_single_candidate(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(
        {
            "a.py": (
                "class Widget:\n    def isTrue(self):\n        return True\n"
            ),
        }
    )
    index = mapfile.load_map(root)
    assert index is not None
    sym = next(
        s for s in index.symbols_by_name["isTrue"] if s.kind == "method"
    )
    assert sanity._resolve_declaring_type(index, sym) == "Widget"


def test_resolve_declaring_type_none_for_free_function(
    make_mapped_repo: RepoFactory,
) -> None:
    # Gating condition #2/#1: a free function has no "." in its
    # qualname (no container) -- layer 1's denylist domain, not this
    # heuristic's.
    root = make_mapped_repo(
        {"a.py": "def isTrue():\n    return True\n"},
    )
    index = mapfile.load_map(root)
    assert index is not None
    sym = index.symbols_by_name["isTrue"][0]
    assert sym.kind == "function"
    assert sanity._resolve_declaring_type(index, sym) is None


def test_resolve_declaring_type_none_when_multiple_candidates(
    make_mapped_repo: RepoFactory,
) -> None:
    # Gating condition #4: two repo-defined symbols share the bare
    # name (the multi-candidate case `dekko ambiguous` already
    # handles) -- don't guess.
    root = make_mapped_repo(
        {
            "a.py": (
                "class Widget:\n    def isTrue(self):\n        return True\n"
            ),
            "b.py": (
                "class Gadget:\n    def isTrue(self):\n        return False\n"
            ),
        }
    )
    index = mapfile.load_map(root)
    assert index is not None
    sym = next(
        s
        for s in index.symbols_by_name["isTrue"]
        if s.path == "a.py" and s.kind == "method"
    )
    assert sanity._resolve_declaring_type(index, sym) is None


def test_sanity_receiver_mismatch_flags_unrelated_collision(
    make_mapped_repo: RepoFactory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    # The isTrue/AssertJ shape as a synthetic, language-agnostic
    # fixture: a class defining the target method, plus an unrelated
    # same-named zero-arg call in a different file with no import of
    # the defining class and no receiver identifier on the same line
    # (so it doesn't already match the higher-precedence
    # CAUSE_QUALIFIED_CALL check -- see _QUALIFIED_CALL_TEMPLATE).
    root = make_mapped_repo(
        {
            "a.py": (
                "class Widget:\n"
                "    def isTrue(self):\n"
                "        return True\n"
                "\n\n"
                "def caller():\n"
                "    w = Widget()\n"
                "    return w.isTrue()\n"
            ),
            "b.py": ("def unrelated():\n    return isTrue()\n"),
        }
    )
    _force_no_dekko_hits(monkeypatch)
    code = cli.main(["sanity", "isTrue", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert sanity.CAUSE_LIKELY_EXTERNAL_COLLISION in out
    assert "declaring type ('Widget')" in out

    code = cli.main(["sanity", "isTrue", "--root", str(root), "--json"])
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    causes = {row["file"]: row["cause"] for row in doc["grep_only"]}
    assert causes["b.py"] == sanity.CAUSE_LIKELY_EXTERNAL_COLLISION
    assert doc["receiver_mismatch_declaring_type"] == "Widget"
    assert doc["receiver_mismatch_count"] >= 1
    assert "Widget" in doc["receiver_mismatch_note"]


def test_sanity_receiver_mismatch_absent_when_gate_unheld(
    make_mapped_repo: RepoFactory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    # A free function shares a bare name with something external, but
    # kind != "method" -- gating condition #1 fails, falls through
    # unchanged (layer 1's domain, not this design's).
    root = make_mapped_repo(
        {
            "a.py": "def isTrue():\n    return True\n",
            "b.py": ("def unrelated():\n    return isTrue()\n"),
        }
    )
    _force_no_dekko_hits(monkeypatch)
    code = cli.main(["sanity", "isTrue", "--root", str(root), "--json"])
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    causes = {row["cause"] for row in doc["grep_only"]}
    assert sanity.CAUSE_LIKELY_EXTERNAL_COLLISION not in causes
    assert "receiver_mismatch_note" not in doc


# --- C.1/round-24 11-output-self-disclosure-hints.md: C.3
# --group-by-file --------------------------------------------------


def test_sanity_group_by_file_rolls_up_grep_only(
    make_mapped_repo: RepoFactory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    # C.3: two unexplained-cause hits clustered in b.py and one
    # qualified-call hit in c.py must roll up into a per-file count
    # with a per-cause breakdown, largest cluster first.
    root = make_mapped_repo(
        {
            "a.py": ("def totally_unrelated_wrapper():\n    return 1\n"),
            "b.py": (
                "value = totally_unrelated_wrapper(x)\n"
                "value = totally_unrelated_wrapper(y)\n"
            ),
            "c.py": (
                "import pkg\n\n\n"
                "def caller():\n"
                "    return pkg.totally_unrelated_wrapper()\n"
            ),
        }
    )
    _force_no_dekko_hits(monkeypatch)
    code = cli.main(
        [
            "sanity",
            "totally_unrelated_wrapper",
            "--root",
            str(root),
            "--group-by-file",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "grep-only: 3 (grouped by file)" in out
    assert "b.py: 2" in out
    assert "c.py: 1" in out
    # b.py (2 hits) must sort before c.py (1 hit).
    assert out.index("b.py: 2") < out.index("c.py: 1")
    assert f"{sanity.CAUSE_UNEXPLAINED}   <-- look here" in out
    assert sanity.CAUSE_QUALIFIED_CALL in out
    assert f"{sanity.CAUSE_QUALIFIED_CALL}   <-- look here" not in out


def test_sanity_group_by_file_omitted_keeps_flat_listing(
    make_mapped_repo: RepoFactory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    # C.3: default behavior (--group-by-file omitted) is unchanged —
    # the existing flat _print_bucket_text rendering still applies.
    root = make_mapped_repo(
        {
            "a.py": ("def totally_unrelated_wrapper():\n    return 1\n"),
            "b.py": (
                "value = totally_unrelated_wrapper(x)\n"
                "value = totally_unrelated_wrapper(y)\n"
            ),
        }
    )
    _force_no_dekko_hits(monkeypatch)
    code = cli.main(
        ["sanity", "totally_unrelated_wrapper", "--root", str(root)]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "grep-only: 2" in out
    assert "(grouped by file)" not in out
    assert f"[{sanity.CAUSE_UNEXPLAINED}]" in out


def test_sanity_group_by_file_respects_limit_truncation(
    make_mapped_repo: RepoFactory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    # C.3: grouping happens over whatever rows survived --limit
    # fitting, not the pre-truncation total — the trailer must report
    # against the already-truncated row count.
    root = make_mapped_repo(
        {
            "a.py": ("def totally_unrelated_wrapper():\n    return 1\n"),
            "b.py": "value = totally_unrelated_wrapper(x)\n",
            "c.py": "value = totally_unrelated_wrapper(x)\n",
            "d.py": "value = totally_unrelated_wrapper(x)\n",
        }
    )
    _force_no_dekko_hits(monkeypatch)
    code = cli.main(
        [
            "sanity",
            "totally_unrelated_wrapper",
            "--root",
            str(root),
            "--group-by-file",
            "--limit",
            "1",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "grep-only: 3 (grouped by file)" in out
    assert "... +2 more (outside --limit/budget)" in out
