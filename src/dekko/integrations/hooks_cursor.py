"""Cursor hook flavor: read a Cursor payload, answer in Cursor's format.

dekko's hook handlers read Claude Code's payload shape and answer in
Claude's nested ``hookSpecificOutput``. Cursor sends a different payload
and expects a flat answer per event, and it **blocks** a permission
hook (shell, read, ``preToolUse``) whose output doesn't match what it
expects. Cursor also runs the hooks in a project's
``.claude/settings.json`` by default, so a hook installed for Claude
Code can receive a Cursor payload too.

This module is the translation, kept pure so it can be tested without
a map: :func:`is_cursor_payload` decides the flavor from the payload
alone, :func:`normalize_payload` reshapes a Cursor payload into the
Claude shape the handlers read, and :func:`cursor_output` converts a
handler's answer into Cursor's per-event format.

The flavor is never decided from the environment: a ``claude`` session
started from Cursor's integrated terminal inherits ``CURSOR_*``
variables and must still get Claude's format.
"""

import json
import os

# Cursor throws away an ``additional_context`` past this many chars.
CURSOR_CONTEXT_HARD_LIMIT = 10_000
# What dekko aims under, leaving room for Cursor joining several hooks'
# contexts with separators.
CURSOR_CONTEXT_CHAR_LIMIT = 9_500
# Seconds Cursor gives each dekko hook. Cursor's default is
# undocumented, and session-start may regenerate a stale map.
CURSOR_HOOK_TIMEOUT = 60

# dekko event -> (Cursor event, ``preToolUse`` matcher). ``pre-read``
# uses ``preToolUse`` rather than ``beforeReadFile`` because only the
# former can pass a message to the model.
CURSOR_EVENTS: dict[str, tuple[str, str | None]] = {
    "session-start": ("sessionStart", None),
    "prompt-submit": ("beforeSubmitPrompt", None),
    "pre-read": ("preToolUse", "Read"),
    "pre-bash": ("beforeShellExecution", None),
}
# Keys a Cursor read tool may name its file under.
_READ_PATH_KEYS = ("file_path", "path", "target_file")


def is_cursor_payload(payload: dict) -> bool:
    """Whether a hook payload came from Cursor.

    Args:
        payload: The parsed hook JSON.

    Returns:
        ``True`` when it carries Cursor's ``cursor_version`` key.
    """
    return "cursor_version" in payload


def _root(payload: dict) -> str | None:
    """The project root a Cursor payload points at, or ``None``.

    ``sessionStart`` and ``beforeSubmitPrompt`` carry no ``cwd``, so the
    first workspace root stands in, then the project dir Cursor exports
    to its hooks. The environment only picks a root here; the payload
    was already judged to be Cursor's.
    """
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd:
        return cwd

    roots = payload.get("workspace_roots")
    if isinstance(roots, list) and roots and isinstance(roots[0], str):
        return roots[0]

    return os.environ.get("CURSOR_PROJECT_DIR") or None


def _tool_input(payload: dict) -> dict:
    """A copy of the payload's tool input, as a dict.

    ``tool_input`` may arrive as an object or as a JSON string; a shell
    event sends its ``command`` at the top level instead.
    """
    raw = payload.get("tool_input")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = {}

    tool_input = dict(raw) if isinstance(raw, dict) else {}
    command = payload.get("command")
    if "command" not in tool_input and isinstance(command, str):
        tool_input["command"] = command

    if "file_path" not in tool_input:
        for key in _READ_PATH_KEYS[1:]:
            if isinstance(tool_input.get(key), str):
                tool_input["file_path"] = tool_input[key]
                break

    return tool_input


def normalize_payload(payload: dict) -> dict:
    """Reshape a Cursor payload into the Claude shape handlers read.

    Args:
        payload: A Cursor hook payload. It isn't modified.

    Returns:
        A new dict with ``cwd`` set to the project root when one is
        known and ``tool_input`` holding ``command`` / ``file_path``
        where Cursor put them elsewhere.
    """
    normalized = dict(payload)
    root = _root(payload)
    if root is not None:
        normalized["cwd"] = root

    normalized["tool_input"] = _tool_input(payload)
    return normalized


def _first_line(text: str) -> str:
    """The first line of ``text``, for a short user-facing message."""
    return text.splitlines()[0] if text else text


def _permission_output(event_name: str, decision: str, reason: str) -> dict:
    """A permission decision in Cursor's flat format.

    ``preToolUse`` doesn't enforce ``ask``, so an ask there becomes an
    ``allow`` that still hands the model the reason.
    """
    if event_name == "preToolUse" and decision != "deny":
        return {"permission": "allow", "agent_message": reason}

    return {
        "permission": decision,
        "agent_message": reason,
        "user_message": _first_line(reason),
    }


def _context_output(event_name: str, context: str) -> dict | None:
    """Context-only output in Cursor's flat format, or ``None``."""
    if event_name == "sessionStart":
        return {"additional_context": context}

    if event_name == "beforeSubmitPrompt":
        return {"continue": True, "additional_context": context}

    if event_name == "preToolUse":
        return {"permission": "allow", "agent_message": context}

    # ``beforeShellExecution`` has no context channel, and answering
    # ``allow`` could skip an approval the user's settings would ask
    # for.
    return None


def cursor_output(event_name: str, output: dict | None) -> dict | None:
    """Convert a handler's Claude-shaped output into Cursor's format.

    Args:
        event_name: The Cursor event being answered (``sessionStart``,
            ``beforeSubmitPrompt``, ``preToolUse`` or
            ``beforeShellExecution``).
        output: The handler's ``hookSpecificOutput`` dict, or ``None``.

    Returns:
        The Cursor answer, or ``None`` when there's nothing to say, the
        event isn't one dekko answers, or a message is too large for
        Cursor to accept.
    """
    events = {cursor_event for cursor_event, _ in CURSOR_EVENTS.values()}
    if output is None or event_name not in events:
        return None

    specific = output.get("hookSpecificOutput")
    if not isinstance(specific, dict):
        return None

    decision = specific.get("permissionDecision")
    reason = specific.get("permissionDecisionReason")
    context = specific.get("additionalContext")
    if isinstance(decision, str) and isinstance(reason, str):
        converted = _permission_output(event_name, decision, reason)
    elif isinstance(context, str) and context:
        converted = _context_output(event_name, context)
    else:
        return None

    if converted is None:
        return None

    too_large = any(
        isinstance(value, str) and len(value) > CURSOR_CONTEXT_HARD_LIMIT
        for value in converted.values()
    )
    return None if too_large else converted
