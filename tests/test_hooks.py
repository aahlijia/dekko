"""Pillar A: the opt-in Claude Code push hooks.

Covers the three entrypoints (session-start / prompt-submit / pre-read),
their fail-silent contract, the relevance ⋈ ledger dedup in prompt-submit,
and the idempotent settings.json install/uninstall merge.
"""

import json
from pathlib import Path

from dekko import cli, hooks, ledger, relevance
from dekko.mapfile import MapIndex, load_map

from conftest import RepoFactory

_FILES = {
    "src/auth.py": (
        '"""User login and authentication."""\n'
        "def login() -> None:\n    pass\n"
    ),
    "src/db.py": (
        '"""Database connection pool."""\n'
        "def connect() -> None:\n    pass\n"
    ),
}


def _index(make_mapped_repo: RepoFactory) -> tuple[Path, MapIndex]:
    root = make_mapped_repo(_FILES)
    index = load_map(root)
    assert index is not None
    return root, index


# --- SessionStart ----------------------------------------------------


def test_session_start_injects_lean_map(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(_FILES)
    out = hooks.session_start({"cwd": str(root)})
    assert out is not None
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "SessionStart"
    ctx = hso["additionalContext"]
    assert "dekko orientation" in ctx
    assert "src/" in ctx and "auth.py" in ctx    # the lean map body


def test_session_start_empty_repo_is_silent(tmp_path: Path) -> None:
    assert hooks.session_start({"cwd": str(tmp_path)}) is None


# --- UserPromptSubmit ------------------------------------------------


def test_prompt_submit_points_at_relevant_files(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(_FILES)
    out = hooks.prompt_submit(
        {"cwd": str(root), "prompt": "fix the login bug"}
    )
    assert out is not None
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "src/auth.py" in ctx          # matched the task
    assert "src/db.py" not in ctx        # unrelated, not listed


def test_prompt_submit_unmatched_prompt_is_silent(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(_FILES)
    out = hooks.prompt_submit(
        {"cwd": str(root), "prompt": "something about kubernetes yaml"}
    )
    assert out is None


def test_prompt_submit_empty_prompt_is_silent(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(_FILES)
    assert hooks.prompt_submit({"cwd": str(root), "prompt": "   "}) is None


def test_prompt_submit_dedups_files_already_read(
    make_mapped_repo: RepoFactory,
) -> None:
    _, index = _index(make_mapped_repo)
    task = relevance.TaskContext(terms=("login",))
    view = ledger.LedgerView()
    view.files["src/auth.py"] = ledger.FileState(
        "src/auth.py", fully_read=True
    )
    # auth matched the task but is already fully in context -> excluded.
    assert hooks._relevant_files(index, task, view) == []


def test_adaptive_top_shrinks_as_budget_fills() -> None:
    fresh = ledger.LedgerView(consumed_tokens=0)
    full = ledger.LedgerView(consumed_tokens=hooks.SESSION_TOKEN_BUDGET)
    assert hooks._adaptive_top(fresh) == hooks.PROMPT_TOP_FILES
    assert hooks._adaptive_top(full) == 1


# --- PreToolUse / Read -----------------------------------------------


def test_pre_read_advises_on_large_file(
    make_mapped_repo: RepoFactory,
) -> None:
    big = {"src/big.py": "x = 1\n" * 4000}      # well over the threshold
    root = make_mapped_repo(big)
    out = hooks.pre_read(
        {"cwd": str(root),
         "tool_input": {"file_path": str(root / "src/big.py")}}
    )
    assert out is not None
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "defer"   # never denies (Q5)
    assert "outline" in hso["permissionDecisionReason"]


def test_pre_read_silent_on_small_file(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(_FILES)
    out = hooks.pre_read(
        {"cwd": str(root),
         "tool_input": {"file_path": str(root / "src/auth.py")}}
    )
    assert out is None


def test_pre_read_silent_without_file_path(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(_FILES)
    assert hooks.pre_read({"cwd": str(root), "tool_input": {}}) is None


# --- dispatch (fail-silent contract) ---------------------------------


def test_dispatch_bad_json_is_silent_and_ok(capsys: object) -> None:
    assert hooks.dispatch("session-start", "{not json") == 0
    assert capsys.readouterr().out == ""


def test_dispatch_unknown_event_is_silent(capsys: object) -> None:
    assert hooks.dispatch("nonsense", "{}") == 0
    assert capsys.readouterr().out == ""


def test_dispatch_routes_and_prints(
    make_mapped_repo: RepoFactory, capsys: object
) -> None:
    root = make_mapped_repo(_FILES)
    payload = json.dumps({"cwd": str(root)})
    assert hooks.dispatch("session-start", payload) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["hookSpecificOutput"]["hookEventName"] == "SessionStart"


# --- install / uninstall ---------------------------------------------


def _settings(root: Path) -> dict:
    return json.loads((root / ".claude" / "settings.json").read_text())


def test_install_default_enables_session_start(tmp_path: Path) -> None:
    assert hooks.install(tmp_path, ["session-start"]) == 0
    hooks_cfg = _settings(tmp_path)["hooks"]
    assert "SessionStart" in hooks_cfg
    cmd = hooks_cfg["SessionStart"][0]["hooks"][0]["command"]
    assert cmd == "dekko hooks run session-start"


def test_install_is_idempotent(tmp_path: Path) -> None:
    hooks.install(tmp_path, ["session-start"])
    hooks.install(tmp_path, ["session-start"])
    assert len(_settings(tmp_path)["hooks"]["SessionStart"]) == 1


def test_install_preserves_existing_hooks(tmp_path: Path) -> None:
    settings_file = tmp_path / ".claude" / "settings.json"
    settings_file.parent.mkdir(parents=True)
    settings_file.write_text(
        json.dumps(
            {"hooks": {"SessionStart": [
                {"hooks": [{"type": "command", "command": "echo hi"}]}
            ]}}
        )
    )
    hooks.install(tmp_path, ["session-start"])
    entries = _settings(tmp_path)["hooks"]["SessionStart"]
    commands = [e["hooks"][0]["command"] for e in entries]
    assert "echo hi" in commands
    assert "dekko hooks run session-start" in commands


def test_install_pre_read_uses_read_matcher(tmp_path: Path) -> None:
    hooks.install(tmp_path, ["pre-read"])
    entry = _settings(tmp_path)["hooks"]["PreToolUse"][0]
    assert entry["matcher"] == "Read"


def test_install_unknown_event_errors(tmp_path: Path) -> None:
    assert hooks.install(tmp_path, ["bogus"]) == 2


def test_uninstall_removes_only_dekko(tmp_path: Path) -> None:
    settings_file = tmp_path / ".claude" / "settings.json"
    settings_file.parent.mkdir(parents=True)
    settings_file.write_text(
        json.dumps(
            {"hooks": {"SessionStart": [
                {"hooks": [{"type": "command", "command": "echo hi"}]}
            ]}}
        )
    )
    hooks.install(tmp_path, ["session-start"])
    hooks.uninstall(tmp_path)
    hooks_cfg = _settings(tmp_path)["hooks"]
    commands = [e["hooks"][0]["command"] for e in hooks_cfg["SessionStart"]]
    assert commands == ["echo hi"]       # ours gone, theirs kept


def test_cli_hooks_install_smoke(tmp_path: Path) -> None:
    assert cli.main(["hooks", "install", "--root", str(tmp_path)]) == 0
    assert (tmp_path / ".claude" / "settings.json").is_file()
