"""Best-effort reads of repository source files.

Shared by the read commands that need raw file text (context-pack
source inlining, outline size estimates). Reads never raise: an
unreadable file yields empty content so callers degrade gracefully.
"""

from pathlib import Path

from dekko.core import languages


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


def unmapped_reason(
    root: Path | None, target: str, provenance: dict | None
) -> str | None:
    """Why a path that exists on disk has nothing in the map.

    For the "no mapped file" replies: without it, ``outline README.md``
    and ``outline nope.py`` printed the same line, and the agent can't
    tell a typo from a file dekko skipped.

    Args:
        root: Repository root, or ``None`` when the caller has none.
        target: The repo-relative path as typed (``./`` already gone).
        provenance: The map's provenance, for its size-cap list.

    Returns:
        A short clause for the reply's parentheses, or ``None`` when
        the path doesn't exist (or there is no root to look in).
    """
    if root is None or not target:
        return None
    path = root / target
    if path.is_dir():
        return "exists, but holds no mapped files"
    if not path.is_file():
        return None
    name = path.name
    if (
        languages.spec_for_path(name) is None
        and languages.tier2_grammar_for_path(name) is None
    ):
        suffix = path.suffix
        what = f"{suffix} files" if suffix else "it"
        return f"exists, but no grammar maps {what}"
    if path.is_symlink():
        return "a symlink; pass --follow-symlinks to map it"
    too_large = ((provenance or {}).get("too_large") or {}).get("paths", [])
    if target in too_large:
        return (
            "exists, but exceeded the size cap; pass --max-file-size N to "
            "map it"
        )

    return "exists, but isn't mapped: ignored, vendored, or excluded"
