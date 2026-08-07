"""The summary digest and the MCP resource that serves it."""

import json
from pathlib import Path

import pytest

from dekko import cli, summary
from dekko import server
from dekko.mapfile import MapIndex

from conftest import RepoFactory

SRC = {
    "src/app.py": (
        '"""The application core."""\n'
        "\n"
        "\n"
        "def helper():\n"
        "    return 1\n"
        "\n"
        "\n"
        "def main():\n"
        "    helper()\n"
    ),
    "src/util/__init__.py": '"""Utility helpers."""\n',
    "src/util/io.py": "def read():\n    return 2\n",
    "tests/test_app.py": "def test_main():\n    pass\n",
}


def _summary(root: Path, *argv: str) -> int:
    return cli.main(["summary", "--root", str(root), *argv])


def test_text_digest(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SRC)
    assert _summary(root) == 0
    out = capsys.readouterr().out
    assert "files," in out and "symbols," in out and "edges" in out
    assert "directories" in out
    assert "src/app.py — The application core." not in out  # purpose is dir
    assert "src/util/  — Utility helpers." in out
    assert "entrypoints:" in out
    assert "main()" in out


def test_json_shape(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SRC)
    assert _summary(root, "--json") == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["files"] >= 4
    dirs = {d["path"]: d for d in doc["directories"]}
    assert dirs["src/util"]["purpose"] == "Utility helpers."
    assert any(e["id"] == "src/app.py::main" for e in doc["entrypoints"])
    assert "parse_errors" in doc


def test_digest_reports_unsupported_coverage(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(
        dict(SRC, **{"src/Card.astro": "---\nconst x = 1;\n---\n"})
    )
    assert _summary(root) == 0
    out = capsys.readouterr().out
    assert "coverage:" in out
    assert "no parser for: astro (1)" in out
    assert "may be incomplete" in out

    assert _summary(root, "--json") == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["unsupported"] == {"count": 1, "languages": {"astro": 1}}


def test_digest_omits_coverage_when_fully_covered(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SRC)
    assert _summary(root) == 0
    assert "coverage:" not in capsys.readouterr().out


def test_directory_purpose_prefers_index_file(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SRC)
    assert _summary(root, "--json") == 0
    doc = json.loads(capsys.readouterr().out)
    src = next(d for d in doc["directories"] if d["path"] == "src")
    # src/app.py has a module doc; src/ has no __init__, so the first
    # doc'd file supplies the purpose.
    assert src["purpose"] == "The application core."


def test_cross_dir_edges_counted(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(
        {
            "a/one.py": "def f():\n    return 1\n",
            "b/two.py": "from a.one import f\n\n\ndef g():\n    return f()\n",
        }
    )
    assert _summary(root, "--json") == 0
    doc = json.loads(capsys.readouterr().out)
    dirs = {d["path"]: d for d in doc["directories"]}
    assert dirs["a"]["cross_edges"] == 1
    assert dirs["b"]["cross_edges"] == 1


def test_entrypoints_exclude_test_methods_and_noncallable_exports(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # 3.5: a decorated, uncalled pytest-style fixture under tests/ and
    # an uncalled exported (non-callable) constant used to both pass
    # the old, too-permissive entrypoints heuristic.
    root = make_mapped_repo(
        dict(
            SRC,
            **{
                "tests/test_app.py": (
                    "import pytest\n\n\n"
                    "@pytest.fixture\n"
                    "def helper_fixture():\n"
                    "    pass\n"
                ),
                "src/consts.ts": "export const CONFIG = 1;\n",
            },
        )
    )
    assert _summary(root, "--json") == 0
    doc = json.loads(capsys.readouterr().out)
    ids = {e["id"] for e in doc["entrypoints"]}
    assert not any("helper_fixture" in i for i in ids)
    assert not any("CONFIG" in i for i in ids)
    # The real entry point is untouched by the tightened filter.
    assert any(i == "src/app.py::main" for i in ids)


def test_parse_errors_capped_with_language_breakdown() -> None:
    # 2.3: an uncapped grammar gap used to make one repeated message
    # dominate the whole digest (spring-boot/tensorflow/zed: 97%+ of
    # `summary`'s output). Built directly against a MapIndex, since
    # reproducing a real Tier-1-grammar-missing error needs no actual
    # source files, just the resulting error/language records.
    index = MapIndex(root_label="repo")
    for i in range(20):
        path = f"gen/file{i}.kt"
        index.errors_by_path[path] = (
            "grammar 'kotlin' is not in the offline Tier-1 set"
        )
        index.languages_by_path[path] = "kotlin"

    doc = summary.compute(index)
    assert len(doc["parse_errors"]) == summary._MAX_PARSE_ERRORS
    assert doc["parse_errors_total"] == 20

    text = summary.render_text(index)
    assert "parse errors:" in text
    hidden = 20 - summary._MAX_PARSE_ERRORS
    assert f"... and {hidden} more" in text
    assert "kotlin (20)" in text


def test_no_tests_filter(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SRC)
    assert _summary(root, "--no-tests", "--json") == 0
    doc = json.loads(capsys.readouterr().out)
    assert all(d["path"] != "tests" for d in doc["directories"])


def _request(root: Path, method: str, params: dict) -> dict:
    ctx = server.Context(default_root=root, no_regen=False)
    msg = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    return server.handle(ctx, msg)


def test_mcp_resources_list_and_read(make_mapped_repo: RepoFactory) -> None:
    root = make_mapped_repo(SRC)
    listed = _request(root, "resources/list", {})["result"]
    uris = {r["uri"] for r in listed["resources"]}
    assert "dekko://summary" in uris

    read = _request(root, "resources/read", {"uri": "dekko://summary"})[
        "result"
    ]
    text = read["contents"][0]["text"]
    assert "symbols," in text
    assert read["contents"][0]["uri"] == "dekko://summary"


def test_mcp_resources_read_unknown_uri(make_mapped_repo: RepoFactory) -> None:
    root = make_mapped_repo(SRC)
    resp = _request(root, "resources/read", {"uri": "dekko://nope"})
    assert "error" in resp


def test_mcp_summary_tool(make_mapped_repo: RepoFactory) -> None:
    root = make_mapped_repo(SRC)
    resp = _request(
        root,
        "tools/call",
        {"name": "summary", "arguments": {}},
    )
    result = resp["result"]
    assert not result["isError"]
    assert "directories" in result["content"][0]["text"]


def test_initialize_advertises_resources() -> None:
    resp = server.handle(
        server.Context(default_root=Path("."), no_regen=False),
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )
    assert resp["result"]["capabilities"]["resources"] == {}


def test_summary_tool_registered() -> None:
    assert "summary" in {t["name"] for t in server.TOOLS}


def test_cli_budget_caps_text_and_footers(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SRC)
    assert _summary(root, "--budget", "30") == 0
    out = capsys.readouterr().out
    assert "omitted" in out.splitlines()[-1]
    assert "entrypoints:" not in out  # trailing sections shed first


def test_mcp_summary_tool_applies_default_budget(
    make_mapped_repo: RepoFactory,
) -> None:
    # The tool defaults its budget (the digest scales with repo size and
    # is re-read as cache every turn); the footer proves a cap was live.
    root = make_mapped_repo(SRC)
    resp = _request(root, "tools/call", {"name": "summary", "arguments": {}})
    text = resp["result"]["content"][0]["text"]
    assert not resp["result"]["isError"]
    assert "tokens" in text.splitlines()[-1]


def test_mcp_summary_tool_honors_explicit_budget(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(SRC)
    resp = _request(
        root, "tools/call", {"name": "summary", "arguments": {"budget": 30}}
    )
    text = resp["result"]["content"][0]["text"]
    assert not resp["result"]["isError"]
    assert "omitted" in text.splitlines()[-1]


def test_mcp_summary_resource_stays_unbudgeted(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(SRC)
    read = _request(root, "resources/read", {"uri": "dekko://summary"})[
        "result"
    ]
    text = read["contents"][0]["text"]
    assert "entrypoints:" in text
    assert "omitted" not in text.splitlines()[-1]
