"""Claude Code hook entrypoints: the opt-in push layer (Pillar A).

Every other dekko surface is *pull* — it helps only when the agent knows
to ask. This module is the **push** wiring: thin handlers that Claude Code
invokes on session/prompt/read events, read the event JSON on stdin, and
emit a budget-capped, task-ranked, dedup-aware context block back through
the documented ``additionalContext`` channel. They are composition over
the existing tools (``render_lean``, ``relevance``, ``ledger``,
``outline``) — no new extraction.

Four events, each individually opt-in (``dekko hooks install``):

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
* **PreToolUse / Bash** (``pre-bash``) — the enforcement tier: a
  ``grep``/``rg``/``ag`` repo-wide search, a ``find -name`` hunt, or a
  ``cat``/``head``/``sed`` on a large mapped file surfaces
  ``permissionDecision: "ask"`` (or ``"deny"`` under ``--strict``) with
  the dekko-equivalent command, instead of the purely advisory text
  every other hook here emits. Off by default even when other hooks
  are installed — see :func:`pre_bash`.

Every handler is **fail-silent**: any error, missing map, or empty signal
yields no output and a clean exit, so a hook can never break or hijack a
session (NFR-3, NFR-4). State is read from the transcript Claude Code
already maintains; dekko persists none of its own.
"""

import json
import shlex
import sys
from pathlib import Path

from dekko import repo_ops
from dekko.storage import ledger
from dekko.analysis import ambiguous, outline, relevance, summary
from dekko.render import render_lean
from dekko.render.mapfile import MapIndex
from dekko.integrations.orient import _PREAMBLE

EXIT_OK = 0

# SessionStart lean-map cap: tighter than a manual `dekko lean`, since it
# is injected unprompted and must stay cheap.
SESSION_MAP_BUDGET = 2000
# Hard ceiling on SessionStart's own payload, independent of
# `render_lean.effective_cap()`'s floor guarantee (which never returns
# less than the path-only floor, however large that is). Round 25
# spring-boot.md finding 1: on a 9,942-file repo, the floor itself was
# ~113K tok -- ~56x SESSION_MAP_BUDGET -- and `session_start` rendered
# it anyway (disclosed, per the round-13 note below, but still injected
# unconditionally into every new session). When even the floor exceeds
# this ceiling, `session_start` drops the map body entirely rather than
# inject an unboundedly large payload automatically; a wider map is
# still one `dekko lean --budget N` or `dekko summary` away.
SESSION_MAP_HARD_CEILING = 20_000
# Assumed session token budget the prompt-submit nudge adapts against. Not
# a hard limit — it scales how many files we point at as context fills.
SESSION_TOKEN_BUDGET = 180_000
# Most files the prompt-submit pointer lists, before budget scaling.
PROMPT_TOP_FILES = 5
# `pre-read`/`pre-bash` advise only above this whole-file token cost.
READ_THRESHOLD = 1000
# Symbol names sampled into a file's relevance text.
_NAME_SAMPLE = 8

# install-time map: our event name -> (Claude event, PreToolUse matcher).
EVENTS: dict[str, tuple[str, str | None]] = {
    "session-start": ("SessionStart", None),
    "prompt-submit": ("UserPromptSubmit", None),
    "pre-read": ("PreToolUse", "Read"),
    "pre-bash": ("PreToolUse", "Bash"),
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
    from dekko.render import mapfile

    if not allow_regen:
        return mapfile.load_map(root)
    index, _ = repo_ops.load_or_regen(root, no_regen=False)
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
    parts = [_PREAMBLE]
    note = ambiguous.high_rate_note(index)
    if note:
        parts.append(note)
    if report.cap > SESSION_MAP_HARD_CEILING:
        # round-25 spring-boot.md finding 1: on a repo whose path-only
        # floor exceeds even this hard ceiling (spring-boot: ~113K tok,
        # ~56x SESSION_MAP_BUDGET), rendering the floor anyway -- the
        # round-13 behavior below -- means a hook that fires
        # automatically, with zero user choice, on every new session
        # costs more than the whole session's context is worth
        # protecting. There is currently no rung between the full
        # path-only floor and "nothing" for `render_lean` to fall back
        # to (see the design doc), so this drops the map body entirely
        # rather than inject an unboundedly large payload.
        parts.append(
            f"note: this repo's path-only floor (~{report.cap} tok) is "
            f"far larger than dekko's {SESSION_MAP_BUDGET}-token "
            "session-start budget -- even the narrowest available map "
            "rendering would cost more than this hook should inject "
            "automatically. Run `dekko lean --budget N` or `dekko "
            "summary` directly for a repo map at a budget you choose."
        )
    else:
        if report.cap > SESSION_MAP_BUDGET:
            # round-13 tensorflow.md: on a repo whose path-only floor
            # alone exceeds SESSION_MAP_BUDGET (a large monorepo can
            # need ~80K tokens just to list every file), `effective_cap`
            # bends the cap upward the same way `render_lean.run()`'s
            # `--budget` floor already does for the `lean` CLI command
            # -- but this hook called `generate()` directly and never
            # surfaced that, so the injected map silently cost up to
            # ~40x its documented budget with no visible signal
            # anywhere. Mirror the CLI wrapper's disclosure so an agent
            # (and anyone reading the transcript) can see why the map
            # below is larger than SESSION_MAP_BUDGET. Only applies
            # below SESSION_MAP_HARD_CEILING -- above it, the branch
            # above takes over and no map body is rendered at all.
            parts.append(
                f"note: this repo's path-only floor (~{report.cap} tok) "
                f"exceeds dekko's {SESSION_MAP_BUDGET}-token "
                "session-start budget; the map below uses the floor "
                "instead."
            )
        parts.append("\n".join(lines))
    text = "\n\n".join(parts)
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
            continue  # dedup (FR-C2)
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
        "dekko — files most relevant to this task (not yet fully read).\n"
        "Outline or query these before Read/grep — do not read one of "
        "them whole without checking its outline first:\n"
        f"{body}\n"
        "  expand: `dekko outline <file>` · `dekko context <sym>`"
    )
    return _additional_context("UserPromptSubmit", text)


def _view(payload: dict, index: MapIndex, root: Path) -> ledger.LedgerView:
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


# --- PreToolUse / Bash -------------------------------------------------

# Statement separators shlex leaves as standalone tokens when they're
# whitespace-delimited (the common case); anything shlex can't tokenize
# at all (unbalanced quotes, etc.) is treated as "no match" rather than
# guessed at.
_SHELL_SEPARATORS = {";", "&&", "||", "|"}
_GREP_CMDS = {"grep", "egrep", "fgrep", "rgrep", "rg", "ag"}
_CAT_CMDS = {"cat", "head", "sed"}
# Recursive/repo-wide flags for the grep family. `rg`/`ag` are recursive
# by default, so any invocation of those two counts; the plain `grep`
# family only counts once one of these explicit flags is present —
# `grep somepattern one_file.py` is a targeted read, not a blind search.
_RECURSIVE_FLAGS = {"-r", "-R", "-rn", "-nr", "-Rn", "-nR", "--recursive"}


def _split_statements(command: str) -> list[list[str]]:
    """Tokenize a shell command into per-statement argv lists.

    Splits on ``;``/``&&``/``||``/``|`` tokens (only recognized when
    whitespace-delimited, matching normal shell formatting) so each
    piece of a chained/piped command is checked independently. Returns
    ``[]`` on anything ``shlex`` can't parse (unbalanced quotes, etc.)
    so the caller fails silent rather than guesses.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        return []
    statements: list[list[str]] = [[]]
    for tok in tokens:
        if tok in _SHELL_SEPARATORS:
            statements.append([])
        else:
            statements[-1].append(tok)
    return [s for s in statements if s]


def _grep_reason(stmt: list[str]) -> str | None:
    """Nudge for a repo-wide grep/rg/ag search, or None."""
    if stmt[0] not in _GREP_CMDS:
        return None
    recursive = stmt[0] in {"rg", "ag"} or any(
        tok in _RECURSIVE_FLAGS for tok in stmt[1:]
    )
    if not recursive:
        return None
    return (
        "dekko: this looks like a repo-wide text search — "
        "`search_code`/`query_symbol` answer 'where/what is X' from "
        "the parsed map without scanning every file; grep still wins "
        "for literal strings, comments, and non-code files."
    )


def _find_reason(stmt: list[str]) -> str | None:
    """Nudge for a `find -name` hunt for a file/definition, or None."""
    if stmt[0] != "find" or "-name" not in stmt:
        return None
    return (
        "dekko: hunting for a file/definition by guessed name — "
        "`search_code`/`outline` locate it from the parsed map instead "
        "of walking the tree."
    )


def _cat_reason(stmt: list[str], index: MapIndex, root: Path) -> str | None:
    """Nudge for `cat`/`head`/`sed` on a large mapped file, or None."""
    if stmt[0] not in _CAT_CMDS or len(stmt) < 2:
        return None
    candidate = stmt[-1]
    if candidate.startswith("-"):
        return None
    rel = _rel_to_root(candidate, root)
    if rel is None:
        return None
    est = outline.size_estimate(index, root, rel)
    if est is None or est[0] < READ_THRESHOLD:
        return None
    full, outline_tokens = est
    pct = round(100 * outline_tokens / full)
    return (
        f"dekko: {rel} ≈ {full} tok via {stmt[0]} — `dekko outline "
        f"{rel}` is ≈ {outline_tokens} tok ({pct}%); outline first if "
        "you only need its shape."
    )


def _bash_reason(command: str, index: MapIndex, root: Path) -> str | None:
    """The first dekko-equivalent nudge for a shell command, or None."""
    for stmt in _split_statements(command):
        reason = (
            _grep_reason(stmt)
            or _find_reason(stmt)
            or _cat_reason(stmt, index, root)
        )
        if reason is not None:
            return reason
    return None


def pre_bash(payload: dict, *, strict: bool = False) -> dict | None:
    """Ask (or, under --strict, deny) before a grep/find/cat fallback.

    The enforcement tier (Tier 2): unlike every other hook in this
    module, a match here interrupts the tool call instead of merely
    annotating it — ``"ask"`` forces a confirmation, ``"deny"`` (opt-in
    via ``--strict``) rejects the call outright and hands Claude the
    dekko-equivalent command to retry with. Matching is deliberately
    conservative (favor false negatives): a plain ``cat config.json``
    or a non-recursive, single-file ``grep`` never matches.

    Args:
        payload: The ``PreToolUse``/``Bash`` hook JSON.
        strict: Escalate a match from ``"ask"`` to ``"deny"``.

    Returns:
        A ``hookSpecificOutput`` dict on a match, else ``None``.
    """
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    command = tool_input.get("command")
    if not isinstance(command, str) or not command.strip():
        return None
    root = _root_from(payload)
    index = _load_index(root, allow_regen=False)
    if index is None:
        return None
    reason = _bash_reason(command, index, root)
    if reason is None:
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny" if strict else "ask",
            "permissionDecisionReason": reason,
        }
    }


# --- dispatch (the `dekko hooks run <event>` entrypoint) -------------

_HANDLERS = {
    "session-start": session_start,
    "prompt-submit": prompt_submit,
    "pre-read": pre_read,
    "pre-bash": pre_bash,
}


def dispatch(event: str, payload_text: str, *, strict: bool = False) -> int:
    """Run a hook handler over stdin JSON and print its output.

    Fail-silent by contract: a bad event, unparseable payload, or any
    handler error prints nothing and still exits ``0`` so the session is
    never disrupted.

    Args:
        event: One of ``session-start``, ``prompt-submit``, ``pre-read``,
            ``pre-bash``.
        payload_text: The raw hook JSON from stdin.
        strict: Forwarded to ``pre_bash``; ignored by every other
            handler.

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
        if event == "pre-bash":
            output = handler(payload, strict=strict)
        else:
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
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _detect_indent(path: Path) -> int | str:
    """Best-effort guess of an existing settings file's indent style.

    Round 25 finding #17: rewriting always hard-coded 2-space indent
    even when the file already on disk used a different width (or
    tabs), producing unnecessary whitespace-only diff noise for anyone
    tracking that file. Reads the first indented line above the file's
    own content and measures its leading whitespace; falls back to the
    2-space default (matching this module's own, unchanged, first-
    install formatting) when the file doesn't exist yet, can't be
    read, or has no indented line to measure.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 2
    for line in lines[1:]:
        if not line or line[0] not in " \t":
            continue
        stripped = line.lstrip(" \t")
        if not stripped:
            continue
        leading = line[: len(line) - len(stripped)]
        return "\t" if "\t" in leading else len(leading)
    return 2


def _write_settings(path: Path, settings: dict) -> None:
    """Write ``settings`` back to ``path``, preserving its existing
    on-disk indent style rather than hard-coding one (see
    ``_detect_indent``)."""
    indent = _detect_indent(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(settings, indent=indent) + "\n", encoding="utf-8"
    )


def _entry(event: str, matcher: str | None, *, strict: bool = False) -> dict:
    """One settings hooks entry invoking ``dekko hooks run <event>``.

    ``strict`` only affects ``pre-bash``: it appends ``--strict`` to the
    command, which ``run_hooks_run``/``dispatch`` forward so
    :func:`pre_bash` escalates its matches from ``"ask"`` to ``"deny"``.
    """
    command = f"{_HOOK_COMMAND_PREFIX}{event}"
    if event == "pre-bash" and strict:
        command += " --strict"
    block = {"hooks": [{"type": "command", "command": command}]}
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


def install(root: Path, events: list[str], *, strict: bool = False) -> int:
    """Merge dekko hook entries into project settings (idempotent).

    Args:
        root: Repository root whose ``.claude/settings.json`` to edit.
        events: dekko event names to enable.
        strict: Escalate ``pre-bash`` matches from ``"ask"`` to
            ``"deny"``. No effect unless ``"pre-bash"`` is in
            ``events``; changing it on an already-installed ``pre-bash``
            requires ``hooks uninstall`` first (install only merges).

    Returns:
        Process exit code (``0`` ok, ``2`` on an unknown event).
    """
    unknown = [e for e in events if e not in EVENTS]
    if unknown:
        print(
            f"dekko: unknown hook event(s): {', '.join(unknown)}",
            file=sys.stderr,
        )
        return 2
    path = settings_path(root)
    settings = _load_settings(path)
    hooks = settings.setdefault("hooks", {})
    for event in events:
        claude_event, matcher = EVENTS[event]
        bucket = hooks.setdefault(claude_event, [])
        if not _already_installed(bucket, event):
            bucket.append(_entry(event, matcher, strict=strict))
    _write_settings(path, settings)
    print(
        f"dekko: enabled hooks [{', '.join(events)}] in {path}. "
        "Restart Claude Code."
    )
    return 0


def _already_installed(bucket: list, event: str) -> bool:
    """Whether ``event`` is already wired in a settings bucket.

    Matches either the plain command or its ``--strict``-suffixed
    variant, so re-running install (with or without ``--strict``)
    never adds a duplicate entry.
    """
    command = f"{_HOOK_COMMAND_PREFIX}{event}"
    for entry in bucket:
        if not isinstance(entry, dict):
            continue
        for hook in entry.get("hooks", []):
            cmd = hook.get("command") if isinstance(hook, dict) else None
            if isinstance(cmd, str) and (
                cmd == command or cmd.startswith(f"{command} ")
            ):
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
            e
            for e in hooks[claude_event]
            if not (isinstance(e, dict) and _is_dekko_entry(e))
        ]
        removed += len(hooks[claude_event]) - len(kept)
        if kept:
            hooks[claude_event] = kept
        else:
            del hooks[claude_event]
    if not hooks:
        settings.pop("hooks", None)
    if not settings and path.exists():
        # Nothing left to keep -- remove the file (and the now-empty
        # .claude/ dir it lived in, if nothing else uses it) rather
        # than leaving a stray `{}` behind, matching --claude-md-
        # install/uninstall's own byte-identical restoration (round 25
        # finding #16).
        path.unlink()
        try:
            path.parent.rmdir()
        except OSError:
            pass  # directory holds other files -- leave it
    else:
        _write_settings(path, settings)
    print(
        f"dekko: removed {removed} dekko hook entr"
        f"{'y' if removed == 1 else 'ies'} from {path}."
    )
    return 0
