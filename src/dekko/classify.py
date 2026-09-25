"""Shared path classification: test code vs production code.

Used to tag symbols with ``test: true`` at map time and by ``unused``
to exclude test files from dead-code candidates. Detection is purely
path-based (directory parts and filename globs) so it is cheap,
deterministic, and language-independent.

Two levels, because "not production code" and "a test a runner would
pick up" are different questions with different consumers:

- ``is_test_path``: test **code**. Anything under a test directory or
  matching a test filename, plus files under a test-*support*
  directory (``testing/``: mocks, matchers, fixtures, test-case
  generators, tools that only exist for tests). This is what
  ``--no-tests``, ``unused``, ``search``, and the map's ``test`` flag
  mean by "test".
- ``is_test_file``: a test **file**, i.e. something a test runner
  discovers by convention. Excludes the support-directory case. This
  is what ``affected``/``impacted_tests`` report, since "which tests
  does this change impact" is a question about tests to run, and a
  support file is impacted code, not a test.
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
# Directories test runners discover tests under by name: pytest/mocha/
# cargo (`test`/`tests`), Jest (`__tests__`), RSpec/Jasmine (`spec`/
# `specs`), Maven/Gradle (`src/test`).
TEST_DIR_PARTS = frozenset({"test", "tests", "__tests__", "spec", "specs"})
# Directories that hold test-support code no runner discovers tests
# under: Go's `testing` package convention, Bazel monorepos' `testing/`
# helper packages (tensorflow has 226 such files: matchers, mocks,
# op-test generators), a `testing/` feature directory for tooling that
# only exists in a test build. Test code, not test files.
TEST_SUPPORT_DIR_PARTS = frozenset({"testing"})

# Cache size for the memoization below. Sized generously above the
# largest distinct-path count seen in the 7-repo eval (tensorflow,
# 14,285 files) — a path is a much smaller-cardinality key than
# relevance.py's per-symbol-text cache, so this can stay modest.
# Mirrors relevance._TERM_CACHE_SIZE's sizing rationale.
_PATH_CACHE_SIZE = 50_000


@lru_cache(maxsize=_PATH_CACHE_SIZE)
def is_test_path(path: str) -> bool:
    """Whether a repo-relative POSIX path looks like test code.

    The broad level: every ``is_test_file`` path plus test-support
    directories (see the module docstring for who consumes which).

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
        True when any directory part is a known test or test-support
        directory or the basename matches a test filename pattern.
    """
    parts = path.split("/")
    if _dir_parts_count(parts) and TEST_SUPPORT_DIR_PARTS.intersection(parts):
        return True

    return is_test_file(path)


@lru_cache(maxsize=_PATH_CACHE_SIZE)
def is_test_file(path: str) -> bool:
    """Whether a repo-relative POSIX path is a test a runner would run.

    The narrow level: test directories and test filename patterns
    only. A file under a test-support directory (``testing/``) that
    matches neither is test code but not a test file.

    Args:
        path: Repo-relative path, e.g. ``tests/test_cli.py``.

    Returns:
        True when any directory part is a known test directory or the
        basename matches a test filename pattern.
    """
    parts = path.split("/")
    if _dir_parts_count(parts) and TEST_DIR_PARTS.intersection(parts):
        return True

    return _basename_is_test(parts[-1])


def _dir_parts_count(parts: list[str]) -> bool:
    """Whether directory names are evidence at all for this path.

    Maven/Gradle standard directory layout: everything under
    ``src/main/`` is production code by definition, regardless of
    whether a later path segment happens to spell a package/module
    name that collides with a test-directory keyword (e.g.
    ``org.springframework.boot.test``, a real, large, production
    namespace, not a test directory). Only the filename-glob check
    still applies there.

    Args:
        parts: The path split on ``/``.

    Returns:
        False under ``src/main/``, True everywhere else.
    """
    if "src" not in parts:
        return True

    idx = parts.index("src")
    after = parts[idx + 1] if idx + 1 < len(parts) else None

    return after != "main"


def _basename_is_test(base: str) -> bool:
    """Whether a filename alone looks like a test file.

    Args:
        base: The final path component (filename).

    Returns:
        True when the basename matches a known test filename pattern.
    """
    # Case-sensitive on every OS: ``fnmatch.fnmatch`` folds case on
    # Windows, where ``*Test.*`` would match ``test.rs`` and
    # ``latest.py``.
    return any(fnmatch.fnmatchcase(base, pat) for pat in TEST_NAME_GLOBS)


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
