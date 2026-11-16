"""Tests for dekko's Codex hooks (``--agent codex``) and ranged reads.

Covers the Codex hook file (``.codex/hooks.json``) install/uninstall,
the pinned shape of every entry dekko writes there, the refusals, the
``pre-bash`` advisory/deny adapter, schema conformance of every output
Codex receives, and ``pre-bash``'s handling of line-ranged shell reads
(shared by both agents).
"""

import io
import json
from pathlib import Path

import pytest

from dekko.integrations import cli, hooks

from conftest import RepoFactory

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "codex"

_FILES = {
    "src/auth.py": (
        '"""User login and authentication."""\n'
        "def login() -> None:\n    pass\n"
    ),
    "src/db.py": (
        '"""Database connection pool."""\ndef connect() -> None:\n    pass\n'
    ),
}
# 4,000 lines, several thousand tokens: large enough for every read nudge.
_BIG = {**_FILES, "src/big.py": "x = 1\n" * 4000}

_ALL_CODEX_EVENTS = ["session-start", "prompt-submit", "pre-bash"]

# What dekko writes into .codex/hooks.json for every Codex event. Codex's
# trust hash covers the event, the matcher and the whole handler, so any
# change here makes every existing install read as modified and stop
# running until the user reviews it again. Change it only on purpose.
_PINNED_CODEX_HOOKS = {
    "hooks": {
        "SessionStart": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": "dekko hooks run session-start "
                        "--agent codex",
                        "additionalContextLimit": 20000,
                    }
                ]
            }
        ],
        "UserPromptSubmit": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": "dekko hooks run prompt-submit "
                        "--agent codex",
                    }
                ]
            }
        ],
        "PreToolUse": [
            {
                "matcher": "Bash",
                "hooks": [
                    {
                        "type": "command",
                        "command": "dekko hooks run pre-bash --agent codex",
                    }
                ],
            }
        ],
    }
}


def _codex_file(root: Path) -> Path:
    return root / ".codex" / "hooks.json"


def _codex_hooks(root: Path) -> dict:
    return json.loads(_codex_file(root).read_text())


def _bash(root: Path, command: str) -> dict:
    return {"cwd": str(root), "tool_input": {"command": command}}


# --- install / uninstall ---------------------------------------------


def test_codex_install_writes_the_pinned_entries(tmp_path: Path) -> None:
    code = hooks.install(tmp_path, _ALL_CODEX_EVENTS, agent="codex")
    assert code == 0
    assert _codex_hooks(tmp_path) == _PINNED_CODEX_HOOKS
    assert not (tmp_path / ".claude").exists()


def test_codex_strict_pre_bash_entry_is_pinned(tmp_path: Path) -> None:
    hooks.install(tmp_path, ["pre-bash"], strict=True, agent="codex")
    assert _codex_hooks(tmp_path)["hooks"]["PreToolUse"] == [
        {
            "matcher": "Bash",
            "hooks": [
                {
                    "type": "command",
                    "command": "dekko hooks run pre-bash --strict "
                    "--agent codex",
                }
            ],
        }
    ]


def test_codex_install_is_idempotent(tmp_path: Path) -> None:
    hooks.install(tmp_path, _ALL_CODEX_EVENTS, agent="codex")
    hooks.install(tmp_path, _ALL_CODEX_EVENTS, agent="codex")
    assert _codex_hooks(tmp_path) == _PINNED_CODEX_HOOKS


def test_codex_install_appends_after_existing_entries(
    tmp_path: Path,
) -> None:
    # Codex keys hook trust by position, so the user's own entries must
    # keep their place and ours go after them.
    path = _codex_file(tmp_path)
    path.parent.mkdir()
    user_entry = {"hooks": [{"type": "command", "command": "echo hi"}]}
    path.write_text(
        json.dumps(
            {
                "description": "team hooks",
                "hooks": {"SessionStart": [user_entry]},
            }
        )
    )
    hooks.install(tmp_path, ["session-start"], agent="codex")
    doc = _codex_hooks(tmp_path)
    assert doc["description"] == "team hooks"
    assert set(doc) == {"description", "hooks"}
    entries = doc["hooks"]["SessionStart"]
    assert entries[0] == user_entry
    assert entries[1] == _PINNED_CODEX_HOOKS["hooks"]["SessionStart"][0]


def test_codex_install_prints_the_trust_note(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    hooks.install(tmp_path, ["session-start"], agent="codex")
    out = capsys.readouterr().out
    assert "Hooks need review" in out
    assert "codex exec" in out
    assert "--strict" in out


def test_codex_install_refuses_pre_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = hooks.install(tmp_path, ["pre-read"], agent="codex")
    assert code == 2
    assert "no Read tool" in capsys.readouterr().err
    assert not _codex_file(tmp_path).exists()


@pytest.mark.parametrize(
    "toml",
    [
        "[hooks]\n",
        '[[hooks.PreToolUse]]\nmatcher = "Bash"\n',
        "model = 'x'\n\n[hooks.SessionStart]\n",
    ],
)
def test_codex_install_refuses_a_config_toml_hooks_table(
    tmp_path: Path, toml: str, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text(toml)
    code = hooks.install(tmp_path, ["session-start"], agent="codex")
    assert code == 2
    assert "[hooks]" in capsys.readouterr().err
    assert not _codex_file(tmp_path).exists()


def test_codex_install_allows_a_trust_state_table(tmp_path: Path) -> None:
    # `[hooks.state."..."]` only records trust; it defines no hooks.
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text('[hooks.state."x:SessionStart:0:0"]\nenabled = true\n')
    assert hooks.install(tmp_path, ["session-start"], agent="codex") == 0
    assert _codex_file(tmp_path).is_file()


def test_codex_uninstall_removes_file_and_empty_dir(tmp_path: Path) -> None:
    hooks.install(tmp_path, _ALL_CODEX_EVENTS, agent="codex")
    assert hooks.uninstall(tmp_path, agent="codex") == 0
    assert not (tmp_path / ".codex").exists()


def test_codex_uninstall_keeps_dir_with_config(tmp_path: Path) -> None:
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text("model = 'x'\n")
    hooks.install(tmp_path, ["session-start"], agent="codex")
    hooks.uninstall(tmp_path, agent="codex")
    assert not _codex_file(tmp_path).exists()
    assert config.read_text() == "model = 'x'\n"


def test_codex_uninstall_keeps_user_entries(tmp_path: Path) -> None:
    path = _codex_file(tmp_path)
    path.parent.mkdir()
    user_entry = {"hooks": [{"type": "command", "command": "echo hi"}]}
    path.write_text(json.dumps({"hooks": {"SessionStart": [user_entry]}}))
    hooks.install(tmp_path, _ALL_CODEX_EVENTS, agent="codex")
    hooks.uninstall(tmp_path, agent="codex")
    assert _codex_hooks(tmp_path) == {"hooks": {"SessionStart": [user_entry]}}


def test_claude_uninstall_leaves_codex_hooks_alone(tmp_path: Path) -> None:
    hooks.install(tmp_path, ["session-start"], agent="codex")
    hooks.install(tmp_path, ["session-start"])
    hooks.uninstall(tmp_path)
    assert _codex_file(tmp_path).is_file()
    assert not (tmp_path / ".claude").exists()


def test_cli_codex_install_and_uninstall(tmp_path: Path) -> None:
    argv = ["hooks", "install", "--agent", "codex", "--root", str(tmp_path)]
    for event in _ALL_CODEX_EVENTS:
        argv += ["--enable", event]
    assert cli.main(argv) == 0
    assert _codex_hooks(tmp_path) == _PINNED_CODEX_HOOKS
    code = cli.main(
        ["hooks", "uninstall", "--agent", "codex", "--root", str(tmp_path)]
    )
    assert code == 0
    assert not _codex_file(tmp_path).exists()


def test_cli_codex_install_pre_read_exits_2(tmp_path: Path) -> None:
    argv = [
        "hooks",
        "install",
        "--agent",
        "codex",
        "--enable",
        "pre-read",
        "--root",
        str(tmp_path),
    ]
    assert cli.main(argv) == 2


# --- pre-bash adapter ------------------------------------------------


def test_codex_pre_bash_is_advisory_by_default(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(_FILES)
    out = hooks.pre_bash(_bash(root, "rg login"), agent="codex")
    assert out is not None
    hso = out["hookSpecificOutput"]
    assert set(hso) == {"hookEventName", "additionalContext"}
    assert hso["hookEventName"] == "PreToolUse"
    assert "search_code" in hso["additionalContext"]


def test_codex_pre_bash_strict_denies_with_a_reason(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(_FILES)
    out = hooks.pre_bash(_bash(root, "rg login"), strict=True, agent="codex")
    assert out is not None
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert hso["permissionDecisionReason"]


def test_claude_pre_bash_output_is_unchanged(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(_FILES)
    out = hooks.pre_bash(_bash(root, "rg login"))
    assert out is not None
    assert set(out["hookSpecificOutput"]) == {
        "hookEventName",
        "permissionDecision",
        "permissionDecisionReason",
    }
    assert out["hookSpecificOutput"]["permissionDecision"] == "ask"


@pytest.mark.parametrize(
    ("strict", "key"),
    [(False, "additionalContext"), (True, "permissionDecision")],
)
def test_dispatch_threads_agent_to_pre_bash(
    make_mapped_repo: RepoFactory,
    capsys: pytest.CaptureFixture[str],
    strict: bool,
    key: str,
) -> None:
    root = make_mapped_repo(_FILES)
    payload = json.dumps(_bash(root, "rg login"))
    code = hooks.dispatch("pre-bash", payload, strict=strict, agent="codex")
    assert code == 0
    hso = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert key in hso
    assert hso.get("permissionDecision") in (None, "deny")


def test_cli_hooks_run_codex_pre_bash(
    make_mapped_repo: RepoFactory,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = make_mapped_repo(_FILES)
    payload = json.dumps(_bash(root, "rg login"))
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    assert cli.main(["hooks", "run", "pre-bash", "--agent", "codex"]) == 0
    hso = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert "permissionDecision" not in hso
    assert "additionalContext" in hso


# --- schema conformance ----------------------------------------------


def _schema(event: str) -> dict:
    path = _FIXTURES / f"{event}.command.output.schema.json"
    return json.loads(path.read_text())


def _resolve(node: dict, schema: dict) -> dict:
    """Follow a ``$ref`` or a one-item ``allOf`` to the node it names."""
    if "$ref" in node:
        name = node["$ref"].rsplit("/", 1)[-1]
        return _resolve(schema["definitions"][name], schema)
    if len(node.get("allOf", [])) == 1:
        return _resolve(node["allOf"][0], schema)

    return node


def _violations(doc: dict, node: dict, schema: dict) -> list[str]:
    """Where ``doc`` breaks the keys, consts and enums ``node`` allows."""
    node = _resolve(node, schema)
    props = node.get("properties", {})
    problems = [f"{k}: not allowed" for k in doc if k not in props]
    problems += [
        f"{k}: missing" for k in node.get("required", []) if k not in doc
    ]
    for key, value in doc.items():
        if key not in props:
            continue
        sub = _resolve(props[key], schema)
        if isinstance(value, dict):
            problems += [f"{key}.{p}" for p in _violations(value, sub, schema)]
        elif "const" in sub and value != sub["const"]:
            problems.append(f"{key}: not {sub['const']!r}")
        elif "enum" in sub and value not in sub["enum"]:
            problems.append(f"{key}: not one of {sub['enum']}")

    return problems


def test_vendored_schemas_forbid_unknown_keys() -> None:
    for event in ("session-start", "user-prompt-submit", "pre-tool-use"):
        schema = _schema(event)
        assert schema["additionalProperties"] is False
        nested = _resolve(schema["properties"]["hookSpecificOutput"], schema)
        assert nested["additionalProperties"] is False


def _codex_outputs(root: Path) -> list[tuple[str, dict | None]]:
    """Every output dekko's hooks hand Codex, paired with its schema."""
    return [
        ("session-start", hooks.session_start({"cwd": str(root)})),
        (
            "user-prompt-submit",
            hooks.prompt_submit(
                {"cwd": str(root), "prompt": "fix the login bug"}
            ),
        ),
        (
            "pre-tool-use",
            hooks.pre_bash(_bash(root, "rg login"), agent="codex"),
        ),
        (
            "pre-tool-use",
            hooks.pre_bash(
                _bash(root, "rg login"), strict=True, agent="codex"
            ),
        ),
        (
            "pre-tool-use",
            hooks.pre_bash(_bash(root, "cat src/big.py"), agent="codex"),
        ),
    ]


def test_every_codex_output_conforms_to_its_schema(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(_BIG)
    for event, out in _codex_outputs(root):
        assert out is not None, event
        schema = _schema(event)
        assert _violations(out, schema, schema) == [], event
        # Codex's schema lists "ask", but its runtime rejects it.
        hso = out["hookSpecificOutput"]
        assert hso.get("permissionDecision") != "ask", event


def test_a_stray_key_fails_the_schema_check() -> None:
    schema = _schema("pre-tool-use")
    nested = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": "x",
            "decisionReason": "x",
        }
    }
    top = {"hookSpecificOutput": {"hookEventName": "PreToolUse"}, "x": 1}
    assert _violations(nested, schema, schema) == [
        "hookSpecificOutput.decisionReason: not allowed"
    ]
    assert _violations(top, schema, schema) == ["x: not allowed"]


# --- ranged reads (both agents) --------------------------------------


@pytest.mark.parametrize("agent", ["claude", "codex"])
@pytest.mark.parametrize(
    "command",
    [
        "sed -n '1,120p' src/big.py",
        "sed -n '1,120 p' src/big.py",
        "sed -n '7p' src/big.py",
        "head src/big.py",
        "head -50 src/big.py",
        "head -n 200 src/big.py",
        "head --lines=80 src/big.py",
        "nl -ba src/big.py | sed -n '600,660p'",
        "cat src/big.py | head -n 100",
        "nl -ba src/big.py | sed -n '1,300p'",
        "sed -i 's/x/y/' src/big.py",
        "sed -e '1,9999p' src/big.py",
        "head -c 100 src/big.py",
    ],
)
def test_short_or_unclear_reads_are_left_alone(
    make_mapped_repo: RepoFactory, command: str, agent: str
) -> None:
    root = make_mapped_repo(_BIG)
    out = hooks.pre_bash(_bash(root, command), strict=True, agent=agent)
    assert out is None, command


@pytest.mark.parametrize(
    "command",
    [
        "cat src/big.py",
        "nl -ba src/big.py",
        "sed 's/x/y/' src/big.py",
        "sed -n '1,4000p' src/big.py",
        "head -n 9000 src/big.py",
    ],
)
def test_whole_file_reads_match(
    make_mapped_repo: RepoFactory, command: str
) -> None:
    root = make_mapped_repo(_BIG)
    out = hooks.pre_bash(_bash(root, command))
    assert out is not None, command
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert reason.startswith("dekko: src/big.py ≈ ")
    assert f"via {command.split()[0]}" in reason


@pytest.mark.parametrize(
    ("command", "span"),
    [
        ("sed -n '100,$p' src/big.py", "lines 100-4000"),
        ("head -n 3000 src/big.py", "lines 1-3000"),
        ("nl -ba src/big.py | sed -n '1,900p'", "lines 1-900"),
        ("cat src/big.py | sed -n '2001,$p'", "lines 2001-4000"),
    ],
)
def test_long_partial_reads_are_costed_by_their_lines(
    make_mapped_repo: RepoFactory, command: str, span: str
) -> None:
    root = make_mapped_repo(_BIG)
    out = hooks.pre_bash(_bash(root, command), agent="codex")
    assert out is not None, command
    reason = out["hookSpecificOutput"]["additionalContext"]
    assert f"src/big.py {span} ≈ " in reason
    read = int(reason.split(" ≈ ")[1].split(" tok")[0])
    whole = int(reason.split("whole file ≈ ")[1].split(" tok")[0])
    assert 0 < read < whole


def test_a_partial_read_below_the_token_threshold_is_left_alone(
    make_mapped_repo: RepoFactory,
) -> None:
    # 300 lines of a 4,000-line file is past the line cap but cheap.
    root = make_mapped_repo(_BIG)
    out = hooks.pre_bash(_bash(root, "head -n 300 src/big.py"))
    assert out is None
