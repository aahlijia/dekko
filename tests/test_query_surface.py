"""A2 query surface: --sites, uses, --no-tests, ranking, footers."""

import json
import re
from pathlib import Path

import pytest

from dekko import cli
from dekko import server

from conftest import RepoFactory

SRC = {
    "src/app.py": (
        "def helper():\n"
        "    return 1\n"
        "\n"
        "\n"
        "def fetch():\n"
        "    return 2\n"
        "\n"
        "\n"
        "def main():\n"
        "    helper()\n"
        "    helper()\n"
        "    external_thing()\n"
    ),
    "src/other.py": ("def go():\n    fetch()\n"),
    "tests/test_app.py": (
        "def fetch():\n    return 3\n\n\ndef test_main():\n    helper()\n"
    ),
}

_FOOTER = re.compile(r"\(~\d+ tokens\)")


def _query(root: Path, *argv: str) -> int:
    return cli.main(["query", *argv, "--root", str(root)])


def test_callers_sites_rows(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SRC)
    assert _query(root, "callers", "helper", "--sites") == 0
    out = capsys.readouterr().out
    assert "src/app.py:10  main()" in out
    assert "src/app.py:11  main()" in out
    assert "tests/test_app.py:6  test_main()" in out


def test_callees_sites_locate_in_callers_file(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SRC)
    assert _query(root, "callees", "main", "--sites") == 0
    out = capsys.readouterr().out
    assert "src/app.py:10  helper()" in out
    assert "src/app.py:11  helper()" in out


def test_no_tests_filters_callers(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SRC)
    assert _query(root, "callers", "helper", "--no-tests") == 0
    out = capsys.readouterr().out
    assert "main" in out
    assert "tests/" not in out


def test_no_tests_disambiguates(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SRC)
    assert _query(root, "symbol", "fetch") == 4
    err = capsys.readouterr().err
    assert err.index("src/app.py") < err.index("tests/test_app.py")

    assert _query(root, "symbol", "fetch", "--no-tests") == 0
    assert "src/app.py:5" in capsys.readouterr().out


def test_uses_text_and_json(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SRC)
    assert _query(root, "uses", "external_thing") == 0
    out = capsys.readouterr().out
    assert "src/app.py:12" in out
    assert "main()" in out
    assert "[external_thing]" in out

    assert _query(root, "uses", "external_thing", "--json") == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["results"] == [
        {
            "caller": "src/app.py::main",
            "callee": "external_thing",
            "lines": [12],
        }
    ]


def test_uses_unknown_name_not_found(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SRC)
    assert _query(root, "uses", "nope_never") == 3
    assert "no external reference" in capsys.readouterr().err


def test_ambiguous_candidates_rank_prod_first(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(SRC)
    doc = json.loads((root / ".dekko" / "map.json").read_text())
    entry = next(
        a for a in doc["ambiguous"] if a["caller"] == "src/other.py::go"
    )
    assert entry["candidates"][0] == "src/app.py::fetch"
    assert entry["candidates"][-1] == "tests/test_app.py::fetch"


def test_token_footer_in_text_not_json(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SRC)
    assert _query(root, "callers", "helper") == 0
    assert _FOOTER.search(capsys.readouterr().out)

    assert _query(root, "callers", "helper", "--json") == 0
    json.loads(capsys.readouterr().out)  # pure JSON, no footer

    assert cli.main(["context", "helper", "--root", str(root)]) == 0
    assert _FOOTER.search(capsys.readouterr().out)

    assert cli.main(["context", "helper", "--root", str(root), "--json"]) == 0
    json.loads(capsys.readouterr().out)


def _call_tool(root: Path, name: str, arguments: dict) -> dict:
    ctx = server.Context(default_root=root, no_regen=False)
    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    return server.handle(ctx, msg)["result"]


def test_mcp_callers_sites(make_mapped_repo: RepoFactory) -> None:
    root = make_mapped_repo(SRC)
    result = _call_tool(
        root, "get_callers", {"symbol": "helper", "sites": True}
    )
    assert not result["isError"]
    assert "src/app.py:10" in result["content"][0]["text"]


def test_mcp_find_usages(make_mapped_repo: RepoFactory) -> None:
    root = make_mapped_repo(SRC)
    result = _call_tool(root, "find_usages", {"name": "external_thing"})
    assert not result["isError"]
    assert "src/app.py:12" in result["content"][0]["text"]

    missing = _call_tool(root, "find_usages", {"name": "nope_never"})
    assert missing["isError"]


def test_find_usages_listed_in_tools() -> None:
    names = {t["name"] for t in server.TOOLS}
    assert "find_usages" in names
