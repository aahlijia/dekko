"""``dekko query importers``: reverse import-source lookup."""

import json

import pytest

from dekko.integrations import cli
from dekko.analysis.query import _source_matches
from dekko.core.model import Import

from conftest import RepoFactory


def _imp(name: str, source: str) -> Import:
    return Import(path="app.py", name=name, source=source)


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


def test_importers_side_effect_import_displays_without_as_clause(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo({"a.js": 'import "./polyfill";\n'})
    code = cli.main(["query", "importers", "polyfill", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "(side-effect import)" in out
    assert "(as )" not in out


def test_importers_exact_js_matches_bare_module_specifier(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # I2 fix: --exact for JS/TS compares against the bare module
    # specifier, not the raw "module/localName" stored source.
    root = make_mapped_repo(
        {
            "a.tsx": 'import React from "react";\n',
            "b.ts": 'import { useState } from "react";\n',
        }
    )
    code = cli.main(
        ["query", "importers", "react", "--exact", "--root", str(root)]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "a.tsx" in out
    assert "b.ts" in out


def test_importers_exact_js_does_not_substring_match_similar_package(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo({"a.ts": 'import x from "react-dom";\n'})
    code = cli.main(
        ["query", "importers", "react", "--exact", "--root", str(root)]
    )
    assert code == 3


def test_source_matches_substring_default() -> None:
    imp = _imp("join", "os.path.join")
    assert _source_matches(imp, "python", "os.path", exact=False)
    assert not _source_matches(
        _imp("sys", "sys"), "python", "os.path", exact=False
    )


def test_source_matches_exact_requires_literal_equality() -> None:
    imp = _imp("os", "os.path")
    assert _source_matches(imp, "python", "os.path", exact=True)
    join_imp = _imp("join", "os.path.join")
    assert not _source_matches(join_imp, "python", "os.path", exact=True)


def test_source_matches_exact_trailing_slash_normalized() -> None:
    # A relative source ("./utils" vs "./utils/") shouldn't false-
    # negative under --exact purely over a trailing slash.
    assert _source_matches(
        _imp("utils", "./utils/"), "python", "./utils", exact=True
    )
    assert _source_matches(
        _imp("utils", "./utils"), "python", "./utils/", exact=True
    )


def test_source_matches_exact_js_strips_appended_name() -> None:
    # JS/TS named/default imports store "module/localName" — --exact
    # must compare against the bare module specifier, not the raw
    # compound string (I2 fix).
    imp = _imp("React", "react/React")
    assert _source_matches(imp, "javascript", "react", exact=True)
    assert not _source_matches(imp, "javascript", "react/React", exact=True)


def test_source_matches_exact_js_side_effect_import_already_bare() -> None:
    # A side-effect import's source is already bare (I1 fix) —
    # bare_import_source must not try to strip a nonexistent suffix.
    imp = _imp("", "opentui-spinner/react")
    assert _source_matches(
        imp, "javascript", "opentui-spinner/react", exact=True
    )
