"""``sanity`` on a symbol that only exists in test code: the default
run excludes tests, so it checks with them included and says so."""

import json
from pathlib import Path

import pytest

from dekko.analysis import query
from dekko.integrations import cli

from conftest import RepoFactory

REPO = {
    "src/app.py": "def helper():\n    return 1\n",
    "src/main.py": (
        "from app import helper\n\n\ndef run():\n    return helper()\n"
    ),
    "tests/test_app.py": (
        "def make_widget():\n"
        "    return 2\n"
        "\n"
        "\n"
        "def test_one():\n"
        "    make_widget()\n"
        "\n"
        "\n"
        "def test_two():\n"
        "    make_widget()\n"
    ),
}
TEST_TARGET = "tests/test_app.py::make_widget"
NOTE = (
    "'tests/test_app.py::make_widget' is test code; "
    "checked with --include-tests"
)


def _json(root: Path, args: list[str], capsys: pytest.CaptureFixture) -> dict:
    code = cli.main(["sanity", *args, "--root", str(root), "--json"])
    assert code == 0
    return json.loads(capsys.readouterr().out)


def test_test_only_target_runs_with_tests(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    doc = _json(make_mapped_repo(REPO), [TEST_TARGET], capsys)
    assert doc["include_tests"] is True
    assert doc["note"] == NOTE
    assert doc["counts"]["matches"] == 2


def test_explicit_include_tests_has_no_note(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    doc = _json(
        make_mapped_repo(REPO), [TEST_TARGET, "--include-tests"], capsys
    )
    assert "note" not in doc
    assert doc["counts"]["matches"] == 2


def test_text_mode_prints_the_note_first(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(REPO)
    assert cli.main(["sanity", TEST_TARGET, "--root", str(root)]) == 0
    first = capsys.readouterr().out.splitlines()[0]
    assert first == f"note: {NOTE}"


def test_prod_target_never_gets_the_note(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    doc = _json(make_mapped_repo(REPO), ["src/app.py::helper"], capsys)
    assert "note" not in doc
    assert doc["include_tests"] is False


def test_unknown_target_still_not_found(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(REPO)
    code = cli.main(["sanity", "nosuch", "--root", str(root), "--json"])
    assert code == query.EXIT_NOT_FOUND
    assert "no symbol matches" in capsys.readouterr().err
