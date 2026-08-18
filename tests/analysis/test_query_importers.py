"""``dekko query importers``: reverse import-source lookup."""

import json

import pytest

from dekko.integrations import cli
from dekko.analysis.query import _source_matches

from conftest import RepoFactory

PY_IMPORTERS = {
    "app.py": ("import os.path\nfrom os.path import join\n"),
    "pkg/mod.py": "from .. import contextpack\n",
    "unrelated.py": "import sys\n",
}


def test_importers_default_substring_matches_both_aliases(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # "os.path" is a substring of both the bare-module source
    # ("os.path") and the from-import source ("os.path.join") — both
    # should surface under the default (non-exact) match.
    root = make_mapped_repo(PY_IMPORTERS)
    code = cli.main(["query", "importers", "os.path", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "app.py" in out
    assert "os.path" in out
    assert "os.path.join" in out
    assert "(as os)" in out
    assert "(as join)" in out


def test_importers_exact_narrows_to_literal_source(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY_IMPORTERS)
    code = cli.main(
        ["query", "importers", "os.path", "--exact", "--root", str(root)]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "(as os)" in out
    assert "(as join)" not in out


def test_importers_relative_import_source(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # "from .. import contextpack" -> raw source "..contextpack" (the
    # exact shape confirmed against this repo's own real imports in
    # the design doc).
    root = make_mapped_repo(PY_IMPORTERS)
    code = cli.main(["query", "importers", "contextpack", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "pkg/mod.py" in out
    assert "..contextpack" in out


def test_importers_not_found_suggests_closest_sources(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY_IMPORTERS)
    code = cli.main(
        ["query", "importers", "totally_unknown", "--root", str(root)]
    )
    assert code == 3
    err = capsys.readouterr().err
    assert "no imports match 'totally_unknown'" in err


def test_importers_json_shape(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY_IMPORTERS)
    code = cli.main(
        ["query", "importers", "os.path", "--json", "--root", str(root)]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["action"] == "importers"
    assert doc["source"] == "os.path"
    assert doc["exact"] is False
    by_name = {e["local_name"]: e for e in doc["results"]}
    assert by_name["os"]["path"] == "app.py"
    assert by_name["os"]["source"] == "os.path"
    assert by_name["join"]["source"] == "os.path.join"


def test_importers_not_found_json_success_path_unaffected(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY_IMPORTERS)
    code = cli.main(
        ["query", "importers", "nope_at_all", "--json", "--root", str(root)]
    )
    assert code == 3
    assert capsys.readouterr().out == ""


def test_importers_budget_caps_many_matches(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    pad = "z" * 60
    files = {}
    for i in range(80):
        files[f"user_{i}_{pad}.py"] = "import shared_module\n"
    root = make_mapped_repo(files)
    code = cli.main(
        ["query", "importers", "shared_module", "--root", str(root)]
    )
    assert code == 0
    assert "omitted" in capsys.readouterr().out


def test_importers_json_budget_reports_truncated_by_budget(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    pad = "z" * 60
    files = {}
    for i in range(30):
        files[f"user_{i}_{pad}.py"] = "import shared_module\n"
    root = make_mapped_repo(files)
    code = cli.main(
        [
            "query",
            "importers",
            "shared_module",
            "--json",
            "--budget",
            "20",
            "--root",
            str(root),
        ]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["meta"]["truncated_by"] == "budget"


def test_importers_no_tests_excludes_test_files(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    files = {
        "app.py": "import shared_dep\n",
        "tests/test_app.py": "import shared_dep\n",
    }
    root = make_mapped_repo(files)

    # Default: test-file imports are included.
    code = cli.main(["query", "importers", "shared_dep", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "app.py" in out
    assert "tests/test_app.py" in out

    # --no-tests: test-file imports are excluded.
    code = cli.main(
        ["query", "importers", "shared_dep", "--no-tests", "--root", str(root)]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "app.py" in out
    assert "tests/test_app.py" not in out


def test_source_matches_substring_default() -> None:
    assert _source_matches("os.path.join", "os.path", exact=False)
    assert not _source_matches("sys", "os.path", exact=False)


def test_source_matches_exact_requires_literal_equality() -> None:
    assert _source_matches("os.path", "os.path", exact=True)
    assert not _source_matches("os.path.join", "os.path", exact=True)


def test_source_matches_exact_trailing_slash_normalized() -> None:
    # A relative source ("./utils" vs "./utils/") shouldn't false-
    # negative under --exact purely over a trailing slash.
    assert _source_matches("./utils/", "./utils", exact=True)
    assert _source_matches("./utils", "./utils/", exact=True)
