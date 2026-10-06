"""``sanity`` on a JVM method another file or package can't reach: a
grep hit there names some other type's same-named method."""

import json
from pathlib import Path

import pytest

from dekko.analysis import sanity
from dekko.integrations import cli

from conftest import RepoFactory

_MAIN = "src/main/java/app"
HELPER_JAVA = (
    "package app;\n"
    "public class Helper {\n"
    "    private Object extracting(String a) { return a; }\n"
    "    void tidy() { }\n"
    '    void self() { extracting("x"); tidy(); }\n'
    "}\n"
)
USER_JAVA = (
    "package app;\n"
    "public class User {\n"
    "    void go(Object r) {\n"
    '        assertThat(r).extracting("x");\n'
    '        r.extracting("y");\n'
    "    }\n"
    "}\n"
)
FAR_JAVA = (
    "package other;\n"
    "public class Far {\n"
    "    void go(Object h) { h.tidy(); }\n"
    "}\n"
)
REPO = {
    f"{_MAIN}/Helper.java": HELPER_JAVA,
    f"{_MAIN}/User.java": USER_JAVA,
    "src/main/java/other/Far.java": FAR_JAVA,
}


def _rows(
    root: Path, target: str, capsys: pytest.CaptureFixture
) -> dict[tuple[str, int], str]:
    assert cli.main(["sanity", target, "--root", str(root), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    return {(r["file"], r["line"]): r["cause"] for r in doc["grep_only"]}


def test_a_private_targets_other_file_hits_are_not_reachable(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(REPO)
    rows = _rows(root, f"{_MAIN}/Helper.java::Helper.extracting", capsys)
    user = f"{_MAIN}/User.java"
    assert rows == {
        (user, 4): sanity.CAUSE_NOT_REACHABLE,
        (user, 5): sanity.CAUSE_NOT_REACHABLE,
    }


def test_a_package_private_targets_other_package_hit_is_not_reachable(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(REPO)
    rows = _rows(root, f"{_MAIN}/Helper.java::Helper.tidy", capsys)
    assert rows == {
        ("src/main/java/other/Far.java", 3): sanity.CAUSE_NOT_REACHABLE
    }


def test_all_mode_aggregates_not_reachable(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(REPO)
    assert cli.main(["sanity", "--all", "--root", str(root), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    agg = doc["aggregate_causes"]
    assert agg.get(sanity.CAUSE_NOT_REACHABLE) == 3
    assert agg.get(sanity.CAUSE_UNEXPLAINED) is None
