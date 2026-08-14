"""Idempotent dekko usage-policy block in a project's ``CLAUDE.md``.

Tier 1 of the "enforce dekko usage" plan
(``.features/plans/usages/enforce-dekko-usage.md``): every other push
lever dekko has (``hooks.py``'s ``additionalContext`` injections,
``orient.py``'s ``_PREAMBLE``) is per-turn *context* an agent is free to
weigh against convenience and drop. Project ``CLAUDE.md`` content is
documented as overriding default agent behavior instead — a materially
stronger lever, loaded once per session rather than competing for
attention against everything else stuffed into the transcript.

This module writes/removes a short, marker-bounded block, mirroring the
idempotent read/merge/write shape :mod:`dekko.hooks` already uses for
``.claude/settings.json`` and :mod:`dekko.cline` uses for
``cline_mcp_settings.json`` — except the target here is a file the user
directly owns and reads, so it is a **separate, explicitly-opt-in**
command (``dekko --claude-md-install`` / ``--claude-md-uninstall``), not
bundled into ``dekko hooks install`` or ``dekko --claude-install``.

Re-running install always replaces the marked block in place (so a
wording update ships by re-running the same command); uninstall strips
exactly that block and leaves the rest of the file untouched.
"""

import re
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
    "- See a file's shape -> `outline <file>`, not `Read` "
    "(~1/10 the cost)\n"
    "- Who calls/depends on something -> `get_callers` / "
    "`get_callees` / `find_usages` (exact call edges, not string "
    "matches)\n"
    "- Work a whole change -> `workset [REV]`, one bundle instead of "
    "N reads\n"
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


def _claude_md_path(root: Path) -> Path:
    """The project ``CLAUDE.md`` path for ``root``."""
    return root / "CLAUDE.md"


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


def install(root: Path) -> int:
    """Write or replace the usage-policy block in ``CLAUDE.md``.

    Args:
        root: Repository root containing (or to receive) ``CLAUDE.md``.

    Returns:
        Process exit code (always ``0``).
    """
    path = _claude_md_path(root)
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
    return 0


def uninstall(root: Path) -> int:
    """Remove the usage-policy block from ``CLAUDE.md``, if present.

    Args:
        root: Repository root whose ``CLAUDE.md`` to edit.

    Returns:
        Process exit code (always ``0``).
    """
    path = _claude_md_path(root)
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
    new_text = f"{new_text}\n" if new_text else ""
    path.write_text(new_text, encoding="utf-8")
    print(f"dekko: removed usage-policy block from {path}.")
    return 0
