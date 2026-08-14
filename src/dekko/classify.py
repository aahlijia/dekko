"""Shared path classification: test code vs production code.

Used to tag symbols with ``test: true`` at map time and by ``unused``
to exclude test files from dead-code candidates. Detection is purely
path-based (directory parts and filename globs) so it is cheap,
deterministic, and language-independent.
"""

import fnmatch
from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dekko.render.mapfile import MapIndex
    from dekko.core.model import Symbol

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

# Cache size for the memoization below. Sized generously above the
# largest distinct-path count seen in the 7-repo eval (tensorflow,
# 14,285 files) — a path is a much smaller-cardinality key than
# relevance.py's per-symbol-text cache, so this can stay modest.
# Mirrors relevance._TERM_CACHE_SIZE's sizing rationale.
_PATH_CACHE_SIZE = 50_000


@lru_cache(maxsize=_PATH_CACHE_SIZE)
def is_test_path(path: str) -> bool:
    """Whether a repo-relative POSIX path looks like test code.

    Memoized: ``mapfile.without_tests()`` calls this (directly and via
    ``_symbol_is_test``/``_prod_id``) for every symbol and both
    endpoints of every edge, re-deriving the same answer for the same
    small set of distinct paths millions of times on a large repo. A
    pure function of an immutable string with no side effects, so
    caching is behavior-preserving by construction — only performance
    changes.

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
