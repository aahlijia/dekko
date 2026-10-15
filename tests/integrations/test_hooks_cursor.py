"""Cursor hooks: payload detection, normalization and output format."""

import json

import pytest

from dekko.integrations import hooks_cursor as hc

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
