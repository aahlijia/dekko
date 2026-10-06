"""``sanity`` on a JVM type a line names through another type path: the
grep hit is a different type of the same name."""

import json
from pathlib import Path

import pytest

from dekko.analysis import sanity
from dekko.integrations import cli

from conftest import RepoFactory

_SRC = "src/main/java"
REPO = {
    f"{_SRC}/org/mine/ErrorPage.java": (
        "package org.mine;\npublic class ErrorPage { }\n"
    ),
    f"{_SRC}/app/Outer.java": (
        "package app;\n"
        "public class Outer {\n"
        "    public static class Inner { }\n"
        "}\n"
    ),
    f"{_SRC}/app/User.java": (
        "package app;\n"
        "public class User {\n"
        "    Object page() {\n"
        "        return new org.apache.catalina.ErrorPage();\n"
        "    }\n"
        "    Object inner() { return new Other.Inner(); }\n"
        "    Object mine() { return new Outer.Inner(); }\n"
        "}\n"
    ),
}


def _rows(
    root: Path, target: str, capsys: pytest.CaptureFixture
) -> dict[tuple[str, int], str]:
    assert cli.main(["sanity", target, "--root", str(root), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    return {(r["file"], r["line"]): r["cause"] for r in doc["grep_only"]}


def test_a_package_path_to_another_type_is_not_a_miss(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(REPO)
    rows = _rows(root, f"{_SRC}/org/mine/ErrorPage.java::ErrorPage", capsys)
    assert rows == {(f"{_SRC}/app/User.java", 4): sanity.CAUSE_OTHER_PACKAGE}


def test_an_outer_type_path_to_another_nesting_is_not_a_miss(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(REPO)
    rows = _rows(root, f"{_SRC}/app/Outer.java::Outer.Inner", capsys)
    assert rows == {(f"{_SRC}/app/User.java", 6): sanity.CAUSE_OTHER_PACKAGE}
