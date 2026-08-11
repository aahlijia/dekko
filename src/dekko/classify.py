"""Shared path classification: test code vs production code.

Used to tag symbols with ``test: true`` at map time and by ``unused``
to exclude test files from dead-code candidates. Detection is purely
path-based (directory parts and filename globs) so it is cheap,
deterministic, and language-independent.
"""

import fnmatch
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .mapfile import MapIndex
    from .model import Symbol

TEST_NAME_GLOBS = (
    "test_*",
    "*_test.*",
    "*.test.*",
    "*.spec.*",
    "*Test.*",
    "*Tests.*",
)
TEST_DIR_PARTS = frozenset(
    {"test", "tests", "__tests__", "spec", "specs", "testing"}
)


def is_test_path(path: str) -> bool:
    """Whether a repo-relative POSIX path looks like test code.

    Args:
        path: Repo-relative path, e.g. ``tests/test_cli.py``.

    Returns:
        True when any directory part is a known test directory or the
        basename matches a test filename pattern.
    """
    parts = path.split("/")
    if "src" in parts:
        idx = parts.index("src")
        after = parts[idx + 1] if idx + 1 < len(parts) else None
        if after == "main":
            # Maven/Gradle standard directory layout: everything under
            # src/main/ is production code by definition, regardless of
            # whether a later path segment happens to spell a package/
            # module name that collides with a test-directory keyword
            # (e.g. `org.springframework.boot.test`, a real, large,
            # production namespace, not a test directory). Only the
            # filename-glob check still applies.
            return _basename_is_test(parts[-1])
        if after in TEST_DIR_PARTS:
            return True
    if TEST_DIR_PARTS.intersection(parts):
        return True
    return _basename_is_test(parts[-1])


def _basename_is_test(base: str) -> bool:
    """Whether a filename alone looks like a test file.

    Args:
        base: The final path component (filename).

    Returns:
        True when the basename matches a known test filename pattern.
    """
    return any(fnmatch.fnmatch(base, pat) for pat in TEST_NAME_GLOBS)


def relevance_key(
    sym: "Symbol", index: "MapIndex"
) -> tuple[bool, int, str, int]:
    """Sort key for budget-drop ordering (lowest-ranked dropped first).

    Ranks production code before tests, then more-connected symbols
    before leaves, then by path and line for stable determinism.

    Args:
        sym: The symbol to rank.
        index: Loaded map index, for degree (fan-in + fan-out).

    Returns:
        A tuple usable as a ``sorted`` key; ascending order puts the
        most relevant rows first.
    """
    return (
        is_test_path(sym.path),
        -index.degree(sym.id),
        sym.path,
        sym.start_line,
    )
