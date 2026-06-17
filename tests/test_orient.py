"""Proactive orientation (F4): session digest + pre-read advisory."""

import json
from pathlib import Path

import pytest

from dekko import cli, mapfile, orient, server

from conftest import RepoFactory

PY = {
    "a.py": (
        '"""Module A does things."""\n'
        "def helper(x: int) -> int:\n"
        '    """Add one."""\n'
        "    return x + 1\n"
        "\n"
        "\n"
        "class Thing:\n"
        '    """A thing."""\n'
        "    def go(self) -> None:\n"
        "        helper(1)\n"
    ),
    "b.py": "def lone() -> None:\n    pass\n",
}


def test_session_renders_preamble_and_summary(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY)
    code = cli.main(["orient", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    # Steering preamble (the behavior-change payload) is always present.
    assert "dekko orientation" in out
    # The summary digest is included.
    assert "files," in out and "symbols," in out
    # The F1 Meter footer self-meters the cost.
    assert "tokens" in out.splitlines()[-1]


def test_preamble_names_the_core_verbs(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # Guards against the steering block drifting from the real commands.
    for verb in ("outline", "workset", "query", "context", "affected"):
        assert verb in orient._PREAMBLE


def test_session_json_shape(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY)
    code = cli.main(["orient", "--root", str(root), "--json"])
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert set(doc) == {"preamble", "summary", "meta"}
    assert "dekko orientation" in doc["preamble"]
    assert doc["meta"]["total"] >= doc["meta"]["returned"]


def test_session_tight_budget_keeps_preamble_and_reports_omission(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY)
    code = cli.main(["orient", "--root", str(root), "--budget", "120"])
    assert code == 0
    out = capsys.readouterr().out
    # Preamble survives a tight budget (it is the non-droppable prefix).
    assert "dekko orientation" in out
    # The summary tail was trimmed and the footer says so.
    assert "omitted" in out.splitlines()[-1]


def test_session_deterministic(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY)
    cli.main(["orient", "--root", str(root)])
    first = capsys.readouterr().out
    cli.main(["orient", "--root", str(root)])
    second = capsys.readouterr().out
    assert first == second


def test_read_advisory_over_threshold(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY)
    code = cli.main(
        ["orient", "--root", str(root), "--read", "a.py", "--threshold", "1"]
    )
    assert code == 0
    out = capsys.readouterr().out
    # One nudge carrying both token numbers and the exact command.
    assert "dekko: a.py" in out
    assert "outline ≈" in out
    assert "dekko outline a.py" in out


def test_read_advisory_silent_below_threshold(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY)
    # Default threshold (1000 tok) dwarfs this tiny file → no nudge.
    code = cli.main(["orient", "--root", str(root), "--read", "a.py"])
    assert code == 0
    assert capsys.readouterr().out == ""


def test_read_advisory_silent_for_unmapped_path(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY)
    args = ["orient", "--root", str(root), "--read", "nope.py"]
    code = cli.main([*args, "--threshold", "1"])
    assert code == 0
    assert capsys.readouterr().out == ""


def test_read_advisory_silent_with_no_map(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    # No .dekko/ map at all: load returns None → silent, exit 0, no regen.
    (tmp_path / "a.py").write_text("def f():\n    pass\n")
    args = ["orient", "--root", str(tmp_path), "--read", "a.py"]
    code = cli.main([*args, "--threshold", "1"])
    assert code == 0
    assert capsys.readouterr().out == ""


def test_size_estimate_helper(make_mapped_repo: RepoFactory) -> None:
    root = make_mapped_repo(PY)
    index = mapfile.load_map(root)
    assert index is not None
    from dekko import outline

    est = outline.size_estimate(index, root, "a.py")
    assert est is not None
    full, outline_tokens = est
    assert 0 < outline_tokens < full
    assert outline.size_estimate(index, root, "missing.py") is None


def test_orient_registered_as_subcommand() -> None:
    assert "orient" in cli.SUBCOMMANDS


def test_orient_adds_no_mcp_tool() -> None:
    # The push layer is CLI/skill-only — it exposes no MCP tool. (The
    # canonical tool-count assertion lives in test_lean.)
    assert "orient" not in {t["name"] for t in server.TOOLS}
