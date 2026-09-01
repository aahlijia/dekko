"""Pillar A: the opt-in Claude Code push hooks.

Covers the four entrypoints (session-start / prompt-submit / pre-read /
pre-bash), their fail-silent contract, the relevance ⋈ ledger dedup in
prompt-submit, pre-bash's grep/find/cat matching and --strict escalation,
and the idempotent settings.json install/uninstall merge.
"""

import io
import json
from pathlib import Path

import pytest

from dekko.integrations import cli, hooks
from dekko.storage import ledger
from dekko.analysis import relevance
from dekko.render.mapfile import MapIndex, load_map

from conftest import RepoFactory

_FILES = {
    "src/auth.py": (
        '"""User login and authentication."""\n'
        "def login() -> None:\n    pass\n"
    ),
    "src/db.py": (
        '"""Database connection pool."""\ndef connect() -> None:\n    pass\n'
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
    assert "src/" in ctx and "auth.py" in ctx  # the lean map body


def test_session_start_empty_repo_is_silent(tmp_path: Path) -> None:
    assert hooks.session_start({"cwd": str(tmp_path)}) is None


def test_session_start_discloses_when_floor_exceeds_budget(
    make_mapped_repo: RepoFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    # round-13 tensorflow.md: on a large monorepo, the path-only floor
    # (~80K tok there) silently overrode SESSION_MAP_BUDGET (2000 tok)
    # by ~40x with no note anywhere -- `dekko lean --budget` has
    # disclosed this same override on stderr since round 09, but
    # `session_start` called `render_lean.generate()` directly and
    # skipped that check entirely. Shrinking the budget constant below
    # any repo's real floor is the same technique
    # `test_cli_lean_tiny_budget_discloses_floor_override` uses for the
    # CLI path.
    monkeypatch.setattr(hooks, "SESSION_MAP_BUDGET", 1)
    root = make_mapped_repo(_FILES)
    out = hooks.session_start({"cwd": str(root)})
    assert out is not None
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "path-only floor" in ctx
    assert "exceeds dekko's 1-token session-start budget" in ctx
    assert "src/" in ctx and "auth.py" in ctx  # the lean map still renders
    # under the (default, much larger) hard ceiling -- only the round-13
    # soft-overage note fires, not the hard-ceiling disclosure-only note.
    assert "far larger than dekko's" not in ctx


def test_session_start_ample_budget_has_no_floor_note(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(_FILES)
    out = hooks.session_start({"cwd": str(root)})
    assert out is not None
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "path-only floor" not in ctx


def test_session_start_hard_ceiling_omits_map_body(
    make_mapped_repo: RepoFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    # round-25 spring-boot.md finding 1: on a repo whose path-only floor
    # exceeds even SESSION_MAP_HARD_CEILING (spring-boot: ~113K tok,
    # ~56x SESSION_MAP_BUDGET), rendering the floor anyway means a hook
    # that fires automatically, with zero user choice, on every new
    # session can cost more than the whole session's context is worth.
    # Shrinking the constant below any repo's real floor is the same
    # technique the round-13 test above uses for the softer case.
    monkeypatch.setattr(hooks, "SESSION_MAP_HARD_CEILING", 1)
    root = make_mapped_repo(_FILES)
    out = hooks.session_start({"cwd": str(root)})
    assert out is not None
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "far larger than dekko's 2000-token session-start budget" in ctx
    assert "even the narrowest available map" in ctx
    assert "dekko lean --budget N" in ctx
    # no map body at all -- the whole point of the disclosure-only path.
    assert "src/" not in ctx and "auth.py" not in ctx
    # and the softer round-13 note must not also fire alongside it.
    assert "uses the floor instead" not in ctx


def test_session_start_hard_ceiling_note_precedes_ambiguous_note(
    make_mapped_repo: RepoFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Mirrors test_session_start_ambiguous_note_precedes_floor_note: the
    # ambiguous-rate caveat must still appear before the hard-ceiling
    # disclosure, matching the documented ordering
    # (`parts = [_PREAMBLE]`, then the ambiguous note, then the
    # floor/ceiling note).
    monkeypatch.setattr(hooks, "SESSION_MAP_HARD_CEILING", 1)
    root = make_mapped_repo(_HIGH_AMBIGUOUS_FILES)
    out = hooks.session_start({"cwd": str(root)})
    assert out is not None
    ctx = out["hookSpecificOutput"]["additionalContext"]
    ambiguous_pos = ctx.index("note: this repo's call resolution is")
    ceiling_pos = ctx.index("far larger than dekko's")
    assert ambiguous_pos < ceiling_pos


# A single unresolved bare-name collision with no other resolved calls
# at all -- 100% ambiguous, comfortably above HIGH_AMBIGUOUS_RATE.
_HIGH_AMBIGUOUS_FILES = {
    "a.py": "def target() -> int:\n    return 1\n",
    "b.py": "def target() -> int:\n    return 2\n",
    "c.py": "def caller() -> int:\n    return target()\n",
}


def test_session_start_omits_ambiguous_note_below_threshold(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(_FILES)
    out = hooks.session_start({"cwd": str(root)})
    assert out is not None
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "call resolution is" not in ctx


def test_session_start_injects_ambiguous_note_above_threshold(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(_HIGH_AMBIGUOUS_FILES)
    out = hooks.session_start({"cwd": str(root)})
    assert out is not None
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "note: this repo's call resolution is 100% ambiguous" in ctx
    assert "dekko ambiguous --by name" in ctx


def test_session_start_ambiguous_note_precedes_floor_note(
    make_mapped_repo: RepoFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    # When both notes fire, the ambiguous-rate caveat must appear
    # before the path-only-floor disclosure -- matches the doc's
    # documented ordering (`parts = [_PREAMBLE]`, then the ambiguous
    # note, then the floor note).
    monkeypatch.setattr(hooks, "SESSION_MAP_BUDGET", 1)
    root = make_mapped_repo(_HIGH_AMBIGUOUS_FILES)
    out = hooks.session_start({"cwd": str(root)})
    assert out is not None
    ctx = out["hookSpecificOutput"]["additionalContext"]
    ambiguous_pos = ctx.index("note: this repo's call resolution is")
    floor_pos = ctx.index("path-only floor")
    assert ambiguous_pos < floor_pos


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
    assert "src/auth.py" in ctx  # matched the task
    assert "src/db.py" not in ctx  # unrelated, not listed


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
    big = {"src/big.py": "x = 1\n" * 4000}  # well over the threshold
    root = make_mapped_repo(big)
    out = hooks.pre_read(
        {
            "cwd": str(root),
            "tool_input": {"file_path": str(root / "src/big.py")},
        }
    )
    assert out is not None
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "defer"  # never denies (Q5)
    assert "outline" in hso["permissionDecisionReason"]


def test_pre_read_silent_on_small_file(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(_FILES)
    out = hooks.pre_read(
        {
            "cwd": str(root),
            "tool_input": {"file_path": str(root / "src/auth.py")},
        }
    )
    assert out is None


def test_pre_read_silent_without_file_path(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(_FILES)
    assert hooks.pre_read({"cwd": str(root), "tool_input": {}}) is None


# --- PreToolUse / Bash -------------------------------------------------


def test_pre_bash_asks_on_recursive_grep(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(_FILES)
    out = hooks.pre_bash(
        {
            "cwd": str(root),
            "tool_input": {"command": 'grep -rn "login" .'},
        }
    )
    assert out is not None
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "ask"
    assert "search_code" in hso["permissionDecisionReason"]


def test_pre_bash_rg_always_counts_as_recursive(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(_FILES)
    out = hooks.pre_bash(
        {"cwd": str(root), "tool_input": {"command": "rg login"}}
    )
    assert out is not None
    assert (
        "search_code" in out["hookSpecificOutput"]["permissionDecisionReason"]
    )


def test_pre_bash_silent_on_targeted_grep(
    make_mapped_repo: RepoFactory,
) -> None:
    # A single-file, non-recursive grep is a targeted read, not a blind
    # search -- deliberately not matched (favor false negatives).
    root = make_mapped_repo(_FILES)
    out = hooks.pre_bash(
        {
            "cwd": str(root),
            "tool_input": {"command": "grep login src/auth.py"},
        }
    )
    assert out is None


def test_pre_bash_asks_on_find_name(make_mapped_repo: RepoFactory) -> None:
    root = make_mapped_repo(_FILES)
    out = hooks.pre_bash(
        {
            "cwd": str(root),
            "tool_input": {"command": 'find . -name "*auth*"'},
        }
    )
    assert out is not None
    assert "outline" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_pre_bash_asks_on_cat_large_mapped_file(
    make_mapped_repo: RepoFactory,
) -> None:
    big = {"src/big.py": "x = 1\n" * 4000}
    root = make_mapped_repo(big)
    out = hooks.pre_bash(
        {"cwd": str(root), "tool_input": {"command": "cat src/big.py"}}
    )
    assert out is not None
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "ask"
    assert "dekko outline" in hso["permissionDecisionReason"]


def test_pre_bash_silent_on_cat_small_mapped_file(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(_FILES)
    out = hooks.pre_bash(
        {"cwd": str(root), "tool_input": {"command": "cat src/auth.py"}}
    )
    assert out is None


def test_pre_bash_silent_on_cat_unmapped_file(
    make_mapped_repo: RepoFactory,
) -> None:
    # e.g. `cat package.json` -- not a file dekko's map indexes at all.
    root = make_mapped_repo(_FILES)
    out = hooks.pre_bash(
        {"cwd": str(root), "tool_input": {"command": "cat package.json"}}
    )
    assert out is None


def test_pre_bash_strict_denies_instead_of_asks(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(_FILES)
    out = hooks.pre_bash(
        {"cwd": str(root), "tool_input": {"command": "rg login"}},
        strict=True,
    )
    assert out is not None
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_pre_bash_silent_without_command(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(_FILES)
    assert hooks.pre_bash({"cwd": str(root), "tool_input": {}}) is None


def test_pre_bash_silent_on_unparseable_command(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(_FILES)
    out = hooks.pre_bash(
        {"cwd": str(root), "tool_input": {"command": "grep 'unterminated"}}
    )
    assert out is None


def test_pre_bash_checks_each_piped_statement(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(_FILES)
    out = hooks.pre_bash(
        {
            "cwd": str(root),
            "tool_input": {"command": "echo start && rg login"},
        }
    )
    assert out is not None


def test_dispatch_pre_bash_strict_flag(
    make_mapped_repo: RepoFactory, capsys: object
) -> None:
    root = make_mapped_repo(_FILES)
    payload = json.dumps(
        {"cwd": str(root), "tool_input": {"command": "rg login"}}
    )
    assert hooks.dispatch("pre-bash", payload, strict=True) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["hookSpecificOutput"]["permissionDecision"] == "deny"


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
            {
                "hooks": {
                    "SessionStart": [
                        {"hooks": [{"type": "command", "command": "echo hi"}]}
                    ]
                }
            }
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


def test_install_pre_bash_uses_bash_matcher(tmp_path: Path) -> None:
    hooks.install(tmp_path, ["pre-bash"])
    entry = _settings(tmp_path)["hooks"]["PreToolUse"][0]
    assert entry["matcher"] == "Bash"
    cmd = entry["hooks"][0]["command"]
    assert cmd == "dekko hooks run pre-bash"


def test_install_pre_bash_strict_appends_flag(tmp_path: Path) -> None:
    hooks.install(tmp_path, ["pre-bash"], strict=True)
    entry = _settings(tmp_path)["hooks"]["PreToolUse"][0]
    cmd = entry["hooks"][0]["command"]
    assert cmd == "dekko hooks run pre-bash --strict"


def test_install_pre_bash_strict_is_idempotent(tmp_path: Path) -> None:
    hooks.install(tmp_path, ["pre-bash"], strict=True)
    hooks.install(tmp_path, ["pre-bash"], strict=True)
    assert len(_settings(tmp_path)["hooks"]["PreToolUse"]) == 1


def test_install_pre_bash_plain_then_strict_does_not_duplicate(
    tmp_path: Path,
) -> None:
    # Re-running with a different --strict setting merges (skips), it
    # doesn't reconfigure -- install() docs this; uninstall+reinstall
    # is the documented path to change it.
    hooks.install(tmp_path, ["pre-bash"], strict=False)
    hooks.install(tmp_path, ["pre-bash"], strict=True)
    assert len(_settings(tmp_path)["hooks"]["PreToolUse"]) == 1


def test_uninstall_removes_only_dekko(tmp_path: Path) -> None:
    settings_file = tmp_path / ".claude" / "settings.json"
    settings_file.parent.mkdir(parents=True)
    settings_file.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {"hooks": [{"type": "command", "command": "echo hi"}]}
                    ]
                }
            }
        )
    )
    hooks.install(tmp_path, ["session-start"])
    hooks.uninstall(tmp_path)
    hooks_cfg = _settings(tmp_path)["hooks"]
    commands = [e["hooks"][0]["command"] for e in hooks_cfg["SessionStart"]]
    assert commands == ["echo hi"]  # ours gone, theirs kept


def test_uninstall_removes_settings_file_when_nothing_left(
    tmp_path: Path,
) -> None:
    # Round 25 finding #16: when dekko's own hook entries were the
    # only content, uninstall must remove the now-empty settings.json
    # (and the now-empty .claude/ dir it lived in) rather than leaving
    # a stray `{}` and an empty directory behind.
    hooks.install(tmp_path, ["session-start"])
    settings_file = tmp_path / ".claude" / "settings.json"
    assert settings_file.is_file()
    hooks.uninstall(tmp_path)
    assert not settings_file.exists()
    assert not settings_file.parent.exists()


def test_uninstall_keeps_file_with_other_top_level_keys(
    tmp_path: Path,
) -> None:
    # Settings holding an unrelated top-level key (not just other
    # hooks) must survive removal, not be deleted just because the
    # hooks key emptied out.
    settings_file = tmp_path / ".claude" / "settings.json"
    settings_file.parent.mkdir(parents=True)
    settings_file.write_text(json.dumps({"someOtherSetting": True}))
    hooks.install(tmp_path, ["session-start"])
    hooks.uninstall(tmp_path)
    assert settings_file.is_file()
    assert _settings(tmp_path) == {"someOtherSetting": True}


def test_uninstall_keeps_dir_with_other_files(tmp_path: Path) -> None:
    # .claude/ holding another file besides settings.json must survive
    # even when settings.json itself is removed.
    dekko_dir = tmp_path / ".claude"
    dekko_dir.mkdir()
    (dekko_dir / "CLAUDE.md").write_text("# notes\n")
    hooks.install(tmp_path, ["session-start"])
    settings_file = dekko_dir / "settings.json"
    hooks.uninstall(tmp_path)
    assert not settings_file.exists()
    assert dekko_dir.is_dir()
    assert (dekko_dir / "CLAUDE.md").exists()


def test_install_preserves_existing_indent_style(tmp_path: Path) -> None:
    # Round 25 finding #17: install must not force 2-space indent onto
    # a file that already used a different width, avoiding unnecessary
    # whitespace-only diff noise.
    settings_file = tmp_path / ".claude" / "settings.json"
    settings_file.parent.mkdir(parents=True)
    settings_file.write_text(
        json.dumps({"someOtherSetting": True}, indent=4) + "\n"
    )
    hooks.install(tmp_path, ["session-start"])
    lines = settings_file.read_text().splitlines()
    setting_line = next(ln for ln in lines if '"someOtherSetting"' in ln)
    assert setting_line.startswith("    ")  # 4 spaces, not 2
    assert not setting_line.startswith("      ")  # not 6 either


def test_uninstall_preserves_existing_indent_style(tmp_path: Path) -> None:
    settings_file = tmp_path / ".claude" / "settings.json"
    settings_file.parent.mkdir(parents=True)
    settings_file.write_text(
        json.dumps({"someOtherSetting": True}, indent=4) + "\n"
    )
    hooks.install(tmp_path, ["session-start"])
    hooks.uninstall(tmp_path)
    lines = settings_file.read_text().splitlines()
    setting_line = next(ln for ln in lines if '"someOtherSetting"' in ln)
    assert setting_line.startswith("    ")  # 4 spaces, not 2


def test_cli_hooks_install_smoke(tmp_path: Path) -> None:
    assert cli.main(["hooks", "install", "--root", str(tmp_path)]) == 0
    assert (tmp_path / ".claude" / "settings.json").is_file()


def test_cli_hooks_install_pre_bash_strict_smoke(tmp_path: Path) -> None:
    code = cli.main(
        [
            "hooks",
            "install",
            "--enable",
            "pre-bash",
            "--strict",
            "--root",
            str(tmp_path),
        ]
    )
    assert code == 0
    entry = _settings(tmp_path)["hooks"]["PreToolUse"][0]
    assert entry["hooks"][0]["command"] == "dekko hooks run pre-bash --strict"


def test_cli_hooks_run_pre_bash_strict_smoke(
    make_mapped_repo: RepoFactory,
    capsys: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = make_mapped_repo(_FILES)
    payload = json.dumps(
        {"cwd": str(root), "tool_input": {"command": "rg login"}}
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    code = cli.main(["hooks", "run", "pre-bash", "--strict"])
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["hookSpecificOutput"]["permissionDecision"] == "deny"
