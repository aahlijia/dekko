"""Best-effort reads of repository source files.

Shared by the read commands that need raw file text (context-pack
source inlining, outline size estimates). Reads never raise: an
unreadable file yields empty content so callers degrade gracefully.
"""

from pathlib import Path


def read_lines(root: Path, rel: str) -> list[str]:
    """Read a repo file's lines, or an empty list on failure.

    Args:
        root: Repository root the path is relative to.
        rel: Repo-relative POSIX path of the file.

    Returns:
        The file's lines (newlines stripped), or ``[]`` if the file
        cannot be read.
    """
    try:
        text = (root / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return text.splitlines()
