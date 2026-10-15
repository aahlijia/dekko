"""Idempotent dekko usage-policy block in a project's instruction file.

Two targets share one block: ``CLAUDE.md`` (read by Claude Code) and
``AGENTS.md`` (read by OpenAI Codex, and by Cursor, Amp, Jules and
others). The text is agent-neutral, so both get the same body between
the same markers.

Every other push lever dekko has (``hooks.py``'s ``additionalContext``
injections, ``orient.py``'s ``_PREAMBLE``) is per-turn *context* an
agent is free to weigh against convenience and drop. A project's
``CLAUDE.md`` / ``AGENTS.md`` content is standing instruction
instead — a materially stronger lever, loaded once per session rather
than competing for attention against everything else stuffed into the
transcript.

This module writes/removes a short, marker-bounded block, mirroring the
idempotent read/merge/write shape :mod:`dekko.hooks` already uses for
``.claude/settings.json`` and :mod:`dekko.cline` uses for
``cline_mcp_settings.json`` — except the target here is a file the user
directly owns and reads, so it is a **separate, explicitly-opt-in**
command (``dekko install claude-md`` / ``dekko install agents-md`` and
their ``uninstall`` forms), not bundled into ``dekko hooks install`` or
``dekko install claude``.

Re-running install always replaces the marked block in place (so a
wording update ships by re-running the same command); uninstall strips
exactly that block and leaves the rest of the file untouched. Reads and
writes go through a symlink rather than replacing it, so a repo whose
``CLAUDE.md`` links to ``AGENTS.md`` (or the reverse) ends up with one
block whichever target is installed.
"""

import re
import sys
from pathlib import Path

_START_MARKER = "<!-- dekko:usage-policy:start -->"
_END_MARKER = "<!-- dekko:usage-policy:end -->"

_POLICY_BODY = (
    "## dekko usage policy\n"
    "\n"
    "This repo has a dekko map (`.dekko/map.json`). Before grepping or "
    "reading a file whole, check whether dekko already answers the "
    "question structurally — it is cheaper and exact where grep "
    "guesses:\n"
    "\n"
    "- Find/understand a symbol -> `search_code` / `query_symbol`, "
    "not `grep`\n"
    "- See a file's shape -> `outline <file>`, not reading it "
    "whole with `Read` or `cat` (~1/10 the cost)\n"
    "- Who calls/depends on something -> `get_callers` / "
    "`get_callees` / `find_usages` (exact call edges, not string "
    "matches)\n"
    "- Work a whole change -> `workset [REV]`, one bundle instead of "
    "N reads\n"
    "- Pick tests to run -> `impacted_tests [REV]` / `dekko affected`, "
    "not grepping test files or running the whole suite\n"
    "\n"
    "Keep using them after your own edits: every tool regenerates a "
    "stale map itself, so never run `dekko map` by hand mid-task or "
    'fall back to grep because the map "might be stale".\n'
    "\n"
    "`grep`/`cat`/reading a file whole is still correct for string "
    "literals, comments, config/data files, and anything outside "
    "dekko's language coverage — this is a default preference, not a "
    "hard rule.\n"
)
_POLICY_BLOCK = f"{_START_MARKER}\n{_POLICY_BODY}{_END_MARKER}\n"

# Collapses 3+ consecutive newlines left behind by a block removal back
# down to a single blank line, so repeated install/uninstall cycles
# converge instead of accumulating blank lines.
_EXCESS_BLANK_LINES = re.compile(r"\n{3,}")


TARGETS: dict[str, str] = {"claude": "CLAUDE.md", "agents": "AGENTS.md"}

# Codex's default ``project_doc_max_bytes``: it reads at most this much
# of the AGENTS.md files from the project root down, and cuts the tail
# of the file that crosses it, which is where this block sits.
AGENTS_MD_MAX_BYTES = 32 * 1024

# Codex reads this instead of AGENTS.md in any directory that has one.
_AGENTS_OVERRIDE = "AGENTS.override.md"


def policy_path(root: Path, target: str = "claude") -> Path:
    """The instruction file ``target`` names under ``root``.

    Args:
        root: Repository root.
        target: A key of :data:`TARGETS`.

    Returns:
        The file's path (it may not exist yet).
    """
    return root / TARGETS[target]


def agents_md_overflow(root: Path) -> int | None:
    """The root ``AGENTS.md``'s size when it exceeds Codex's cap.

    Args:
        root: Repository root.

    Returns:
        The size in bytes when over :data:`AGENTS_MD_MAX_BYTES`, else
        ``None`` (including when the file doesn't exist).
    """
    path = policy_path(root, "agents")
    if not path.is_file():
        return None
    size = path.stat().st_size

    return size if size > AGENTS_MD_MAX_BYTES else None


def agents_md_override(root: Path) -> Path | None:
    """The root ``AGENTS.override.md`` that shadows ``AGENTS.md``, if any.

    Args:
        root: Repository root.

    Returns:
        The override file's path when it exists, else ``None``.
    """
    path = root / _AGENTS_OVERRIDE

    return path if path.is_file() else None


def _find_block(text: str) -> tuple[int, int] | None:
    """Character offsets ``(start, end)`` of an existing marked block.

    Returns ``None`` if either marker is missing (including a start
    marker with no matching end — treated as "no block" rather than
    guessing where it ends).
    """
    start = text.find(_START_MARKER)
    if start == -1:
        return None
    end = text.find(_END_MARKER, start)
    if end == -1:
        return None
    return start, end + len(_END_MARKER)


def _separator(text: str) -> str:
    """Whitespace to join ``text`` and a freshly appended block."""
    if text.endswith("\n\n"):
        return ""
    if text.endswith("\n"):
        return "\n"
    return "\n\n"


def install(root: Path, target: str = "claude") -> int:
    """Write or replace the usage-policy block in ``target``'s file.

    Args:
        root: Repository root containing (or to receive) the file.
        target: A key of :data:`TARGETS`.

    Returns:
        Process exit code (always ``0``).
    """
    path = policy_path(root, target)
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    span = _find_block(text)
    if span is not None:
        start, end = span
        new_text = text[:start] + _POLICY_BLOCK + text[end:]
        verb = "updated"
    elif text.strip():
        new_text = text + _separator(text) + _POLICY_BLOCK
        verb = "added"
    else:
        new_text = _POLICY_BLOCK
        verb = "added"
    path.write_text(new_text, encoding="utf-8")
    print(f"dekko: {verb} usage-policy block in {path}.")
    if target == "agents":
        _warn_codex_visibility(root)

    return 0


def _warn_codex_visibility(root: Path) -> None:
    """Warn when Codex won't see the block in the root ``AGENTS.md``."""
    size = agents_md_overflow(root)
    if size is not None:
        print(
            f"dekko: warning: {policy_path(root, 'agents')} is {size} "
            f"bytes, over Codex's default {AGENTS_MD_MAX_BYTES}-byte "
            "limit; Codex cuts the end of the file, where the dekko "
            "block is.",
            file=sys.stderr,
        )

    override = agents_md_override(root)
    if override is not None:
        print(
            f"dekko: warning: {override} exists, and Codex reads it "
            "instead of AGENTS.md, so it won't see the dekko block.",
            file=sys.stderr,
        )


def uninstall(root: Path, target: str = "claude") -> int:
    """Remove the usage-policy block from ``target``'s file, if present.

    Args:
        root: Repository root whose file to edit.
        target: A key of :data:`TARGETS`.

    Returns:
        Process exit code (always ``0``).
    """
    path = policy_path(root, target)
    if not path.is_file():
        print(f"dekko: no {path}; nothing to remove.")
        return 0
    text = path.read_text(encoding="utf-8")
    span = _find_block(text)
    if span is None:
        print(f"dekko: no dekko usage-policy block in {path}.")
        return 0
    start, end = span
    remainder = text[:start] + text[end:]
    remainder = _EXCESS_BLANK_LINES.sub("\n\n", remainder)
    new_text = remainder.strip("\n")
    if not new_text:
        # Delete the file itself, not a symlink pointing at it: that
        # would leave the block in place behind the link.
        path.resolve().unlink()
        print(
            f"dekko: removed usage-policy block from {path} "
            "(file was created by install; deleted)."
        )
        return 0
    path.write_text(f"{new_text}\n", encoding="utf-8")
    print(f"dekko: removed usage-policy block from {path}.")
    return 0
