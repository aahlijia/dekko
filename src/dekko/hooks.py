"""Claude Code hook entrypoints: the opt-in push layer (Pillar A).

Every other dekko surface is *pull* — it helps only when the agent knows
to ask. This module is the **push** wiring: thin handlers that Claude Code
invokes on session/prompt/read events, read the event JSON on stdin, and
emit a budget-capped, task-ranked, dedup-aware context block back through
the documented ``additionalContext`` channel. They are composition over
the existing tools (``render_lean``, ``relevance``, ``ledger``,
``outline``) — no new extraction.

Three events, each individually opt-in (``dekko hooks install``):

* **SessionStart** (``session-start``) — a steering preamble plus a
  budget-capped ``lean`` map, so the first turn already holds a navigation
  map and reads fewer whole files. Enabled by default on install.
* **UserPromptSubmit** (``prompt-submit``) — for the submitted prompt,
  a short pointer to the most task-relevant files *not already in
  context* (relevance ⋈ ledger dedup), with the list tightening as the
  session's token budget fills (FR-C3).
* **PreToolUse / Read** (``pre-read``) — a non-blocking advisory to
  outline a large file first (``permissionDecision: "defer"`` — never
  denies the read; Resolved Q5).

Every handler is **fail-silent**: any error, missing map, or empty signal
yields no output and a clean exit, so a hook can never break or hijack a
session (NFR-3, NFR-4). State is read from the transcript Claude Code
already maintains; dekko persists none of its own.
"""

import json
import sys
from pathlib import Path

from . import ledger, outline, relevance, render_lean, summary
from .mapfile import MapIndex
from .orient import _PREAMBLE

EXIT_OK = 0

# SessionStart lean-map cap: tighter than a manual `dekko lean`, since it
# is injected unprompted and must stay cheap.
SESSION_MAP_BUDGET = 2000
# Assumed session token budget the prompt-submit nudge adapts against. Not
# a hard limit — it scales how many files we point at as context fills.
SESSION_TOKEN_BUDGET = 180_000
# Most files the prompt-submit pointer lists, before budget scaling.
PROMPT_TOP_FILES = 5
# `pre-read` advises only above this whole-file token cost.
READ_THRESHOLD = 1000
# Symbol names sampled into a file's relevance text.
_NAME_SAMPLE = 8

# install-time map: our event name -> (Claude event, PreToolUse matcher).
EVENTS: dict[str, tuple[str, str | None]] = {
    "session-start": ("SessionStart", None),
    "prompt-submit": ("UserPromptSubmit", None),
    "pre-read": ("PreToolUse", "Read"),
}

_HOOK_COMMAND_PREFIX = "dekko hooks run "


# --- shared helpers --------------------------------------------------


def _root_from(payload: dict) -> Path:
    """Resolve the repo root from a hook payload's ``cwd``."""
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd:
        return Path(cwd)
    return Path(".")


def _load_index(root: Path, *, allow_regen: bool) -> MapIndex | None:
    """Load the map; optionally auto-regenerate a stale one."""
    from . import cli, mapfile

    if not allow_regen:
        return mapfile.load_map(root)
    index, _ = cli._load_or_regen(root, no_regen=False)
    return index


def _additional_context(event_name: str, text: str) -> dict:
    """The hookSpecificOutput envelope that injects ``text`` as context."""
    return {
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "additionalContext": text,
        }
    }


# --- SessionStart ----------------------------------------------------


def session_start(payload: dict) -> dict | None:
    """Inject a steering preamble + budget-capped lean map (FR-A1)."""
    root = _root_from(payload)
    index = _load_index(root, allow_regen=True)
    if index is None:
        return None
    lines, report = render_lean.generate(
        index, root, render_lean.CapConfig(override=SESSION_MAP_BUDGET)
    )
    if report.total_symbols == 0 and not index.languages_by_path:
        return None
    text = _PREAMBLE + "\n\n" + "\n".join(lines)
    return _additional_context("SessionStart", text)


# --- UserPromptSubmit ------------------------------------------------


def _adaptive_top(view: ledger.LedgerView) -> int:
    """Fewer files as the session's token budget fills (FR-C3)."""
    remaining = view.remaining(SESSION_TOKEN_BUDGET)
    scaled = PROMPT_TOP_FILES * remaining // SESSION_TOKEN_BUDGET
    return max(1, min(PROMPT_TOP_FILES, scaled))


def _file_candidates(
    index: MapIndex, view: ledger.LedgerView
) -> tuple[list[relevance.Candidate], dict[str, float]]:
    """Relevance candidates for files not already fully in context."""
    candidates: list[relevance.Candidate] = []
    centrality: dict[str, float] = {}
    for path in index.languages_by_path:
        state = view.files.get(path)
        if state is not None and state.fully_read:
            continue                       # dedup (FR-C2)
        doc = index.docs_by_path.get(path) or ""
        names = " ".join(
            s.name for s in index.symbols_by_path.get(path, [])[:_NAME_SAMPLE]
        )
        candidates.append(
            relevance.Candidate(path, f"{path} {doc} {names}", path)
        )
        centrality[path] = float(summary._file_fan_in(index, path))
    return candidates, centrality


def _relevant_files(
    index: MapIndex, task: relevance.TaskContext, view: ledger.LedgerView
) -> list[str]:
    """Top task-relevant, not-yet-read files, budget-scaled and gated.

    Only files the task actually matched (positive lexical relevance) are
    returned, so an unmatched prompt produces no nudge at all.
    """
    candidates, centrality = _file_candidates(index, view)
    if not candidates:
        return []
    rel = relevance.LexicalScorer().score(task, candidates)
    matched = [c for c in candidates if rel[c.id] > 0]
    if not matched:
        return []
    scores = relevance.blended_scores(
        task, matched, {c.id: centrality[c.id] for c in matched}
    )
    ranked = sorted(matched, key=lambda c: (-scores[c.id], c.id))
    return [c.id for c in ranked[: _adaptive_top(view)]]


def prompt_submit(payload: dict) -> dict | None:
    """Point at the files most relevant to the new prompt (FR-A2)."""
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return None
    root = _root_from(payload)
    index = _load_index(root, allow_regen=False)
    if index is None:
        return None
    task = relevance.task_context(prompt, root)
    view = _view(payload, index, root)
    files = _relevant_files(index, task, view)
    if not files:
        return None
    body = "\n".join(f"  {p}" for p in files)
    text = (
        "dekko — files most relevant to this task (not yet fully read):\n"
        f"{body}\n"
        "  expand: `dekko outline <file>` · `dekko context <sym>`"
    )
    return _additional_context("UserPromptSubmit", text)


def _view(
    payload: dict, index: MapIndex, root: Path
) -> ledger.LedgerView:
    """Build the session ledger from the payload's transcript, if any."""
    transcript = payload.get("transcript_path")
    if isinstance(transcript, str) and transcript:
        return ledger.build_view(Path(transcript), index, root)
    return ledger.LedgerView()


# --- PreToolUse / Read -----------------------------------------------


def pre_read(payload: dict) -> dict | None:
    """Advise outlining a large file first — non-blocking (FR-A3, Q5)."""
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str) or not file_path:
        return None
    root = _root_from(payload)
    index = _load_index(root, allow_regen=False)
    if index is None:
        return None
    rel = _rel_to_root(file_path, root)
    if rel is None:
        return None
    est = outline.size_estimate(index, root, rel)
    if est is None or est[0] < READ_THRESHOLD:
        return None
    full, outline_tokens = est
    pct = round(100 * outline_tokens / full)
    reason = (
        f"dekko: {rel} ≈ {full} tok — `dekko outline {rel}` is "
        f"≈ {outline_tokens} tok ({pct}%); outline first if you only "
        "need its shape."
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "defer",
            "permissionDecisionReason": reason,
        }
    }


def _rel_to_root(file_path: str, root: Path) -> str | None:
    """Normalize an absolute read path to repo-relative POSIX, or None."""
    p = Path(file_path)
    if not p.is_absolute():
        return p.as_posix()
    try:
        return p.relative_to(root).as_posix()
    except ValueError:
        return None


# --- dispatch (the `dekko hooks run <event>` entrypoint) -------------

_HANDLERS = {
    "session-start": session_start,
    "prompt-submit": prompt_submit,
    "pre-read": pre_read,
}


def dispatch(event: str, payload_text: str) -> int:
    """Run a hook handler over stdin JSON and print its output.

    Fail-silent by contract: a bad event, unparseable payload, or any
    handler error prints nothing and still exits ``0`` so the session is
    never disrupted.

    Args:
        event: One of ``session-start``, ``prompt-submit``, ``pre-read``.
        payload_text: The raw hook JSON from stdin.

    Returns:
        Always ``0``.
    """
    handler = _HANDLERS.get(event)
    if handler is None:
        return EXIT_OK
    try:
        payload = json.loads(payload_text) if payload_text.strip() else {}
        if not isinstance(payload, dict):
            return EXIT_OK
        output = handler(payload)
    except Exception:
        return EXIT_OK
    if output is not None:
        print(json.dumps(output))
    return EXIT_OK


# --- install / uninstall into project settings -----------------------


def settings_path(root: Path) -> Path:
    """Project-local Claude Code settings file for ``root``."""
    return root / ".claude" / "settings.json"


def _load_settings(path: Path) -> dict:
    """Read existing settings, or an empty object (best-effort)."""
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _entry(event: str, matcher: str | None) -> dict:
    """One settings hooks entry invoking ``dekko hooks run <event>``."""
    block = {
        "hooks": [
            {"type": "command", "command": f"{_HOOK_COMMAND_PREFIX}{event}"}
        ]
    }
    if matcher is not None:
        block = {"matcher": matcher, **block}
    return block


def _is_dekko_entry(entry: dict) -> bool:
    """Whether a settings hooks entry is one of ours."""
    for hook in entry.get("hooks", []):
        cmd = hook.get("command", "") if isinstance(hook, dict) else ""
        if isinstance(cmd, str) and cmd.startswith(_HOOK_COMMAND_PREFIX):
            return True
    return False


def install(root: Path, events: list[str]) -> int:
    """Merge dekko hook entries into project settings (idempotent).

    Args:
        root: Repository root whose ``.claude/settings.json`` to edit.
        events: dekko event names to enable.

    Returns:
        Process exit code (``0`` ok, ``2`` on an unknown event).
    """
    unknown = [e for e in events if e not in EVENTS]
    if unknown:
        print(f"dekko: unknown hook event(s): {', '.join(unknown)}",
              file=sys.stderr)
        return 2
    path = settings_path(root)
    settings = _load_settings(path)
    hooks = settings.setdefault("hooks", {})
    for event in events:
        claude_event, matcher = EVENTS[event]
        bucket = hooks.setdefault(claude_event, [])
        if not _already_installed(bucket, event):
            bucket.append(_entry(event, matcher))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n")
    print(
        f"dekko: enabled hooks [{', '.join(events)}] in {path}. "
        "Restart Claude Code."
    )
    return 0


def _already_installed(bucket: list, event: str) -> bool:
    """Whether ``event`` is already wired in a settings bucket."""
    command = f"{_HOOK_COMMAND_PREFIX}{event}"
    for entry in bucket:
        if not isinstance(entry, dict):
            continue
        for hook in entry.get("hooks", []):
            if isinstance(hook, dict) and hook.get("command") == command:
                return True
    return False


def uninstall(root: Path) -> int:
    """Remove all dekko hook entries from project settings.

    Args:
        root: Repository root whose settings to clean.

    Returns:
        Process exit code (always ``0``).
    """
    path = settings_path(root)
    settings = _load_settings(path)
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        print("dekko: no dekko hooks to remove.")
        return 0
    removed = 0
    for claude_event in list(hooks):
        kept = [
            e for e in hooks[claude_event]
            if not (isinstance(e, dict) and _is_dekko_entry(e))
        ]
        removed += len(hooks[claude_event]) - len(kept)
        if kept:
            hooks[claude_event] = kept
        else:
            del hooks[claude_event]
    if not hooks:
        settings.pop("hooks", None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n")
    print(f"dekko: removed {removed} dekko hook entr"
          f"{'y' if removed == 1 else 'ies'} from {path}.")
    return 0
