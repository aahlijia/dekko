"""Cursor hooks: payload detection, normalization and output format."""

import json
from pathlib import Path

import pytest

from dekko.integrations import hooks
from dekko.integrations import hooks_cursor as hc
from dekko.render.mapfile import load_map
from dekko.storage import ledger

from conftest import RepoFactory

# The output keys Cursor documents for each event dekko answers. Cursor
# publishes no hook schemas, and it blocks a permission hook whose
# output doesn't match what it expects, so nothing else may appear.
ALLOWED_KEYS: dict[str, set[str]] = {
    "sessionStart": {"env", "additional_context"},
    "beforeSubmitPrompt": {"continue", "user_message", "additional_context"},
    "preToolUse": {
        "permission",
        "user_message",
        "agent_message",
        "updated_input",
        "additional_context",
    },
    "beforeShellExecution": {"permission", "user_message", "agent_message"},
}
REASON = "dekko: use `dekko search foo`\nsecond line of detail"


def _context(event: str, text: str) -> dict:
    """A handler's Claude-shaped context output."""
    return {
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": text,
        }
    }


def _decision(decision: str, reason: str = REASON) -> dict:
    """A handler's Claude-shaped permission output."""
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    }


# --- detection -------------------------------------------------------


def test_cursor_payload_is_detected_by_its_version_key() -> None:
    assert hc.is_cursor_payload({"cursor_version": "3.15.19"})


def test_a_claude_payload_is_never_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A Claude session started in Cursor's terminal inherits these.
    monkeypatch.setenv("CURSOR_VERSION", "3.15.19")
    monkeypatch.setenv("CURSOR_PROJECT_DIR", "/repo")
    payload = {"cwd": "/repo", "tool_input": {"command": "ls"}}

    assert not hc.is_cursor_payload(payload)


def test_cursor_events_cover_every_hook() -> None:
    assert set(hc.CURSOR_EVENTS) == {
        "session-start",
        "prompt-submit",
        "pre-read",
        "pre-bash",
    }
    for event, _matcher in hc.CURSOR_EVENTS.values():
        assert event in ALLOWED_KEYS


# --- normalization ---------------------------------------------------


def test_normalize_lifts_a_top_level_command() -> None:
    payload = {"cursor_version": "1", "command": "grep -r x ."}

    assert hc.normalize_payload(payload)["tool_input"] == {
        "command": "grep -r x .",
    }


def test_normalize_parses_a_string_tool_input() -> None:
    good = {"tool_input": json.dumps({"command": "ls"})}
    bad = {"tool_input": "{bad"}

    assert hc.normalize_payload(good)["tool_input"] == {"command": "ls"}
    assert hc.normalize_payload(bad)["tool_input"] == {}


@pytest.mark.parametrize("key", ["file_path", "path", "target_file"])
def test_normalize_finds_the_read_path(key: str) -> None:
    payload = {"tool_name": "Read", "tool_input": {key: "/repo/a.py"}}

    tool_input = hc.normalize_payload(payload)["tool_input"]

    assert tool_input["file_path"] == "/repo/a.py"


def test_normalize_finds_a_read_path_in_a_string_tool_input() -> None:
    payload = {"tool_input": json.dumps({"target_file": "/repo/a.py"})}

    tool_input = hc.normalize_payload(payload)["tool_input"]

    assert tool_input["file_path"] == "/repo/a.py"


def test_normalize_takes_the_root_from_workspace_roots_then_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CURSOR_PROJECT_DIR", "/from-env")
    with_roots = {"workspace_roots": ["/first", "/second"]}

    assert hc.normalize_payload(with_roots)["cwd"] == "/first"
    assert hc.normalize_payload({})["cwd"] == "/from-env"


def test_normalize_leaves_the_root_unset_without_any_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CURSOR_PROJECT_DIR", raising=False)

    assert "cwd" not in hc.normalize_payload({"workspace_roots": []})


def test_normalize_keeps_an_existing_cwd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CURSOR_PROJECT_DIR", "/from-env")
    payload = {"cwd": "/shell-cwd", "workspace_roots": ["/first"]}

    assert hc.normalize_payload(payload)["cwd"] == "/shell-cwd"


def test_normalize_does_not_mutate_its_input() -> None:
    tool_input = {"path": "/repo/a.py"}
    payload = {
        "cursor_version": "1",
        "command": "ls",
        "tool_input": tool_input,
        "workspace_roots": ["/repo"],
    }
    before = json.dumps(payload, sort_keys=True)

    hc.normalize_payload(payload)

    assert json.dumps(payload, sort_keys=True) == before
    assert tool_input == {"path": "/repo/a.py"}


# --- output ----------------------------------------------------------


def _cases() -> list[tuple[str, dict, dict]]:
    """(Cursor event, handler output, expected Cursor output)."""
    first_line = REASON.splitlines()[0]
    return [
        (
            "beforeShellExecution",
            _decision("ask"),
            {
                "permission": "ask",
                "agent_message": REASON,
                "user_message": first_line,
            },
        ),
        (
            "beforeShellExecution",
            _decision("deny"),
            {
                "permission": "deny",
                "agent_message": REASON,
                "user_message": first_line,
            },
        ),
        (
            "preToolUse",
            _decision("ask"),
            {"permission": "allow", "agent_message": REASON},
        ),
        (
            "preToolUse",
            _decision("deny"),
            {
                "permission": "deny",
                "agent_message": REASON,
                "user_message": first_line,
            },
        ),
        (
            "preToolUse",
            _context("PreToolUse", "outline a.py first"),
            {"permission": "allow", "agent_message": "outline a.py first"},
        ),
        (
            "beforeSubmitPrompt",
            _context("UserPromptSubmit", "files: a.py"),
            {"continue": True, "additional_context": "files: a.py"},
        ),
        (
            "sessionStart",
            _context("SessionStart", "the map"),
            {"additional_context": "the map"},
        ),
    ]


@pytest.mark.parametrize(("event", "output", "expected"), _cases())
def test_cursor_output_converts_each_event(
    event: str,
    output: dict,
    expected: dict,
) -> None:
    assert hc.cursor_output(event, output) == expected


@pytest.mark.parametrize(("event", "output", "_expected"), _cases())
def test_output_keys_are_documented(
    event: str,
    output: dict,
    _expected: dict,
) -> None:
    converted = hc.cursor_output(event, output)

    assert converted is not None
    assert set(converted) <= ALLOWED_KEYS[event]


def test_no_output_and_unknown_events_give_none() -> None:
    assert hc.cursor_output("sessionStart", None) is None
    assert hc.cursor_output("stop", _context("Stop", "x")) is None
    assert hc.cursor_output("sessionStart", {}) is None


def test_shell_context_without_a_decision_gives_none() -> None:
    # ``beforeShellExecution`` has no context channel, and answering
    # ``allow`` could skip an approval the user's settings would ask
    # for, so an advisory there says nothing.
    output = _context("PreToolUse", "a hint")

    assert hc.cursor_output("beforeShellExecution", output) is None


def test_oversized_context_is_dropped() -> None:
    text = "x" * (hc.CURSOR_CONTEXT_HARD_LIMIT + 1)

    assert hc.cursor_output("sessionStart", _context("x", text)) is None
    assert hc.cursor_output("preToolUse", _decision("deny", text)) is None


def test_context_limit_leaves_headroom_under_cursors_cap() -> None:
    assert hc.CURSOR_CONTEXT_CHAR_LIMIT < hc.CURSOR_CONTEXT_HARD_LIMIT


# --- dispatch --------------------------------------------------------

_FILES = {
    "src/auth.py": (
        '"""User login and authentication."""\n'
        "def login() -> None:\n    pass\n"
    ),
    "src/db.py": (
        '"""Database connection pool."""\ndef connect() -> None:\n    pass\n'
    ),
}


def _dispatch(
    capsys: pytest.CaptureFixture[str],
    event: str,
    payload: dict,
    **kwargs: object,
) -> str:
    """Run ``dispatch`` on ``payload`` and return what it printed."""
    assert hooks.dispatch(event, json.dumps(payload), **kwargs) == 0
    return capsys.readouterr().out


def _session_payload(root: Path) -> dict:
    """A Cursor ``sessionStart`` payload for a workspace at ``root``."""
    return {
        "cursor_version": "3.15.19",
        "hook_event_name": "sessionStart",
        "workspace_roots": [str(root)],
        "session_id": "s1",
    }


def _shell_payload(root: Path, command: str) -> dict:
    """A native Cursor ``beforeShellExecution`` payload."""
    return {
        "cursor_version": "3.15.19",
        "hook_event_name": "beforeShellExecution",
        "command": command,
        "cwd": str(root),
    }


def test_cursor_session_start_answers_in_cursor_format(
    make_mapped_repo: RepoFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = make_mapped_repo(_FILES)

    out = _dispatch(
        capsys,
        "session-start",
        _session_payload(root),
        agent="cursor",
    )
    doc = json.loads(out)

    assert set(doc) == {"additional_context"}
    assert "dekko orientation" in doc["additional_context"]


def test_a_cursor_payload_under_agent_claude_answers_in_cursor_format(
    make_mapped_repo: RepoFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # What a hook from .claude/settings.json gets when Cursor imports it.
    root = make_mapped_repo(_FILES)
    payload = {
        "cursor_version": "3.15.19",
        "hook_event_name": "preToolUse",
        "tool_name": "Shell",
        "tool_input": {"command": 'grep -rn "login" .'},
        "cwd": str(root),
    }

    doc = json.loads(_dispatch(capsys, "pre-bash", payload, agent="claude"))

    assert doc["permission"] == "allow"
    assert "search_code" in doc["agent_message"]
    assert set(doc) == {"permission", "agent_message"}


def test_an_imported_pre_bash_with_a_string_tool_input_matches(
    make_mapped_repo: RepoFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = make_mapped_repo(_FILES)
    payload = {
        "cursor_version": "3.15.19",
        "hook_event_name": "preToolUse",
        "tool_name": "Shell",
        "tool_input": json.dumps({"command": "rg login"}),
        "cwd": str(root),
    }

    doc = json.loads(_dispatch(capsys, "pre-bash", payload))

    assert doc["permission"] == "allow"


@pytest.mark.parametrize(
    ("strict", "permission"),
    [(False, "ask"), (True, "deny")],
)
def test_native_shell_hook_asks_and_strict_denies(
    make_mapped_repo: RepoFactory,
    capsys: pytest.CaptureFixture[str],
    strict: bool,
    permission: str,
) -> None:
    root = make_mapped_repo(_FILES)

    out = _dispatch(
        capsys,
        "pre-bash",
        _shell_payload(root, "rg login"),
        strict=strict,
        agent="cursor",
    )
    doc = json.loads(out)

    assert doc["permission"] == permission
    assert "search_code" in doc["agent_message"]
    assert set(doc) <= ALLOWED_KEYS["beforeShellExecution"]


def test_a_cursor_read_advisory_reaches_the_model(
    make_mapped_repo: RepoFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    body = "".join(f"def f{i}() -> None:\n    pass\n" for i in range(400))
    root = make_mapped_repo({"src/big.py": body})
    payload = {
        "cursor_version": "3.15.19",
        "hook_event_name": "preToolUse",
        "tool_name": "Read",
        "tool_input": {"target_file": str(root / "src" / "big.py")},
        "cwd": str(root),
    }

    doc = json.loads(_dispatch(capsys, "pre-read", payload, agent="cursor"))

    assert doc["permission"] == "allow"
    assert "dekko outline src/big.py" in doc["agent_message"]


@pytest.mark.parametrize("agent", ["claude", "cursor"])
def test_a_cursor_miss_prints_nothing(
    make_mapped_repo: RepoFactory,
    capsys: pytest.CaptureFixture[str],
    agent: str,
) -> None:
    root = make_mapped_repo(_FILES)

    out = _dispatch(
        capsys,
        "pre-bash",
        _shell_payload(root, "ls"),
        agent=agent,
    )

    assert out == ""


def test_agent_cursor_without_a_map_prints_nothing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = _dispatch(
        capsys,
        "session-start",
        _session_payload(tmp_path),
        agent="cursor",
    )

    assert out == ""


def test_claude_output_is_unchanged_under_cursor_env(
    make_mapped_repo: RepoFactory,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Claude started from Cursor's integrated terminal.
    root = make_mapped_repo(_FILES)
    monkeypatch.setenv("CURSOR_VERSION", "3.15.19")
    monkeypatch.setenv("CURSOR_PROJECT_DIR", str(root))
    payload = {"cwd": str(root), "tool_input": {"command": "rg login"}}
    expected = hooks.pre_bash(payload)

    out = _dispatch(capsys, "pre-bash", payload)

    assert expected is not None
    assert json.loads(out) == expected
    assert expected["hookSpecificOutput"]["permissionDecision"] == "ask"


def _many_files() -> dict[str, str]:
    """~600 small files: a path-only floor too big for Cursor's cap."""
    return {
        f"pkg_{d:02d}/a_fairly_long_module_name_{f:03d}.py": (
            f"def fn_{f}() -> None:\n    pass\n"
        )
        for d in range(30)
        for f in range(20)
    }


def test_cursor_session_start_fits_the_char_limit(
    make_mapped_repo: RepoFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = make_mapped_repo(_many_files())

    out = _dispatch(
        capsys,
        "session-start",
        _session_payload(root),
        agent="cursor",
    )
    context = json.loads(out)["additional_context"]

    assert len(context) <= hc.CURSOR_CONTEXT_CHAR_LIMIT
    assert "dekko lean --budget" in context
    assert "a_fairly_long_module_name_000.py" not in context


def test_cursor_session_start_on_a_small_repo_includes_the_map(
    make_mapped_repo: RepoFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = make_mapped_repo(_FILES)

    out = _dispatch(
        capsys,
        "session-start",
        _session_payload(root),
        agent="cursor",
    )
    context = json.loads(out)["additional_context"]

    assert len(context) <= hc.CURSOR_CONTEXT_CHAR_LIMIT
    assert "auth.py" in context
    assert "dekko lean --budget" not in context


def test_claude_session_start_has_no_char_limit(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(_many_files())

    out = hooks.session_start({"cwd": str(root)})

    assert out is not None
    context = out["hookSpecificOutput"]["additionalContext"]
    assert "a_fairly_long_module_name_000.py" in context


def test_a_cursor_transcript_folds_reads_and_estimates_usage(
    make_mapped_repo: RepoFactory,
    tmp_path: Path,
) -> None:
    # Cursor's transcript rows use ``role`` rather than ``type`` and
    # carry no usage counts. A ``Read`` block with a ``file_path`` is
    # still folded in like Claude's, and the budget falls back to an
    # estimate from the text blocks.
    root = make_mapped_repo(_FILES)
    index = load_map(root)
    assert index is not None
    transcript = tmp_path / "cursor-transcript.jsonl"
    row = {
        "role": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": "Reading auth.py"},
                {
                    "type": "tool_use",
                    "name": "Read",
                    "input": {"file_path": str(root / "src" / "auth.py")},
                },
            ],
        },
    }
    transcript.write_text(json.dumps(row) + "\n", encoding="utf-8")

    view = ledger.build_view(transcript, index, root)

    assert view.consumed_tokens > 0
    assert view.turns == 0
    assert view.has_file("src/auth.py")
