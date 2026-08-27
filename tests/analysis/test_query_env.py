"""``dekko query env``: statically-known environment-variable reads.

Design doc: config-constant-value-tracing-design.md. CLI-level
end-to-end tests through ``cli.main`` + a real ``dekko map`` run
(``make_mapped_repo``), mirroring ``test_query_throws.py``'s pattern.
Covers: exact-key lookup across languages, ``--list`` aggregate
ranking, JSON output, the default-value-argument-not-shown scope
boundary, case-sensitivity, not-found, and the "TARGET required
unless --list" CLI-level validation.
"""

import json
import re

import pytest

from dekko.integrations import cli

from conftest import RepoFactory

ENV_SRC = {
    "src/config.py": (
        "import os\n\n"
        "DATABASE_URL = os.getenv('DATABASE_URL')\n\n\n"
        "def load():\n"
        "    port = os.environ.get('PORT', '8080')\n"
        "    log = os.environ['LOG_LEVEL']\n"
        "    return port, log\n"
    ),
    "src/app.js": (
        "const port = process.env.PORT;\n\n"
        "function main() {\n"
        "  const url = process.env['DATABASE_URL'];\n"
        "}\n"
    ),
}


def test_env_exact_key_lookup(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(ENV_SRC)
    code = cli.main(["query", "env", "DATABASE_URL", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "src/config.py:3" in out
    assert 'os.getenv("DATABASE_URL")' in out
    assert "src/app.js:4" in out
    assert 'process.env["DATABASE_URL"]' in out
    # A different key must not leak into this result.
    assert "PORT" not in out.replace("DATABASE_URL", "")


def test_env_default_value_argument_not_shown(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # os.environ.get('PORT', '8080') only has its key captured — the
    # default-value second argument is out of scope (design doc).
    root = make_mapped_repo(ENV_SRC)
    code = cli.main(["query", "env", "PORT", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert 'os.environ.get("PORT")' in out
    assert "8080" not in out


def test_env_case_sensitive(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(ENV_SRC)
    code = cli.main(["query", "env", "database_url", "--root", str(root)])
    assert code == 3
    err = capsys.readouterr().err
    assert "no env-var reads found for 'database_url'" in err


def test_env_not_found(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(ENV_SRC)
    code = cli.main(["query", "env", "NOPE", "--root", str(root)])
    assert code == 3
    err = capsys.readouterr().err
    assert "no env-var reads found for 'NOPE'" in err


def test_env_list_ranking(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(ENV_SRC)
    code = cli.main(["query", "env", "--list", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "3 distinct env vars read across 2 files" in out
    # DATABASE_URL (2 sites) must rank above PORT/LOG_LEVEL (1 each).
    lines = [
        line
        for line in out.splitlines()
        if "DATABASE_URL" in line or "PORT" in line or "LOG_LEVEL" in line
    ]
    assert lines[0].strip().endswith("DATABASE_URL")


def test_env_list_footer_total_matches_distinct_key_count(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # Round 23 §17: the text footer's TOTAL previously folded the
    # summary header line into the counted/droppable row list, so with
    # N distinct keys and truncation active it reported N+1. TOTAL must
    # equal the real distinct-key count, and shown + omitted must equal
    # that same TOTAL exactly.
    files = {
        f"src/config_{i}.py": (
            f"import os\n\nVAR_{i:02d} = os.getenv('VAR_{i:02d}')\n"
        )
        for i in range(20)
    }
    root = make_mapped_repo(files)
    code = cli.main(
        ["query", "env", "--list", "--limit", "5", "--root", str(root)]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "20 distinct env vars read across 20 files" in out
    match = re.search(r"(\d+) of (\d+) omitted", out)
    assert match is not None
    omitted, total = int(match.group(1)), int(match.group(2))
    assert total == 20
    data_rows = [
        line for line in out.splitlines() if re.match(r"^\s*\d+\s+VAR_", line)
    ]
    assert len(data_rows) + omitted == total


def test_env_list_footer_no_omitted_clause_when_untruncated(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # Regression check for the untruncated case: with few enough keys
    # that nothing is omitted, no "N of TOTAL omitted" clause should
    # appear at all, and the header count is unaffected by the fix.
    root = make_mapped_repo(ENV_SRC)
    code = cli.main(["query", "env", "--list", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "3 distinct env vars read across 2 files" in out
    assert "omitted" not in out


def test_env_json_output(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(ENV_SRC)
    code = cli.main(
        ["query", "env", "LOG_LEVEL", "--json", "--root", str(root)]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["action"] == "env"
    assert doc["key"] == "LOG_LEVEL"
    assert len(doc["results"]) == 1
    entry = doc["results"][0]
    assert entry["path"] == "src/config.py"
    assert entry["key"] == "LOG_LEVEL"
    assert entry["call"] == "os.environ[]"


def test_env_list_json_output(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(ENV_SRC)
    code = cli.main(["query", "env", "--list", "--json", "--root", str(root)])
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["action"] == "env"
    assert doc["list"] is True
    assert doc["distinct_keys"] == 3
    keys = {r["key"]: r["read_sites"] for r in doc["results"]}
    assert keys == {"DATABASE_URL": 2, "PORT": 2, "LOG_LEVEL": 1}


def test_env_missing_target_without_list_is_usage_error(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(ENV_SRC)
    code = cli.main(["query", "env", "--root", str(root)])
    assert code == 2
    err = capsys.readouterr().err
    assert "requires TARGET" in err


def test_env_list_needs_no_target(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # Sanity check that --list alone (no TARGET) is accepted, not
    # rejected by the same validation the previous test exercises.
    root = make_mapped_repo(ENV_SRC)
    code = cli.main(["query", "env", "--list", "--root", str(root)])
    assert code == 0


# ---------------------------------------------------------------------
# Write/delete exclusion (E1)

WRITE_DELETE_REPO = {
    # Mirrors the report's real-world repro shape (a read, then a
    # write, then a delete, then another write of the same key) --
    # only the genuine read should surface.
    "src/env.js": (
        "function setup() {\n"
        "  const dir = process.env.CLINE_DIR;\n"
        "  process.env.CLINE_DIR = '/tmp/a';\n"
        "  delete process.env.CLINE_DIR;\n"
        "  process.env.CLINE_DIR = '/tmp/b';\n"
        "  return dir;\n"
        "}\n"
    ),
}


def test_env_write_delete_sites_not_reported_as_reads(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(WRITE_DELETE_REPO)
    code = cli.main(
        ["query", "env", "CLINE_DIR", "--root", str(root), "--json"]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert len(doc["results"]) == 1
    assert doc["results"][0]["path"] == "src/env.js"
