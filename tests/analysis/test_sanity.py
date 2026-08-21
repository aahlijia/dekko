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
