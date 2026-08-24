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
    } <= set(doc)
    assert doc["grep_only"], "expected at least one grep-only hit"
    for row in doc["grep_only"]:
        assert {"file", "line", "snippet", "cause"} <= set(row)
    for row in doc["matches"]:
        assert {"file", "line"} <= set(row)


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
