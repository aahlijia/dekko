"""File discovery: enumerate mappable source files in a repository."""

import fnmatch
import itertools
import os
import subprocess
from pathlib import Path

import pathspec

from dekko.core import languages

# VCS metadata and tool-generated caches. Never first-party source, so
# files under these are ignored silently (no skip-reason entry at
# all) — a coverage note about ``.git/`` would be pure noise.
_NOISE_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    ".tox",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
}

# Excluded by default because they're overwhelmingly third-party or
# build output, but occasionally hold real first-party source (e.g. a
# Bazel monorepo's ``third_party/xla``) — worth a coverage note rather
# than silent, unqualified exclusion. See ``_classify``.
_VENDORED_DIRS = {
    "node_modules",
    "target",
    "dist",
    "build",
    "vendor",
    "third_party",
}

# JVM package-naming conventions can put an ordinary directory named
# after a ``_VENDORED_DIRS`` entry (most commonly ``build``, per Spring
# Boot's own ``org.springframework.boot.build`` package) directly
# beneath a language source root — nothing to do with build output.
# Once a ``src/main/<lang>`` or ``src/test/<lang>`` prefix is seen,
# everything beneath it is package path, not a vendored/build-output
# directory, and is exempted from ``_VENDORED_DIRS`` matching. See
# ``_source_root_end``.
_SOURCE_ROOT_LANGS = {"java", "kotlin", "groovy", "scala"}

DEFAULT_EXCLUDE_DIRS = _NOISE_DIRS | _VENDORED_DIRS

GENERATED_PATTERNS = (
    "*.min.js",
    "*.min.css",
    "*_pb2.py",
    "*_pb2_grpc.py",
    "*.pb.go",
    "*.generated.*",
    "*.d.ts",
)

DEFAULT_MAX_FILE_SIZE = 1_000_000

# Heuristic for vendored/minified files that don't follow a `.min.`
# naming convention (e.g. a vendored `book.js`/`highlight.js` under a
# docs theme directory). Sampled over just the first few dozen lines,
# so the cost is bounded even relative to a full parse; a false
# positive here just means one legitimately dense file goes unmapped,
# versus a false negative polluting every fuzzy-match/search result
# derived from it.
_MINIFIED_SAMPLE_LINES = 50
_MINIFIED_AVG_LINE_LEN = 300


def _git_files(root: Path) -> list[str] | None:
    """List repo files via git, or ``None`` when not a git repo.

    Args:
        root: Directory to enumerate.

    Returns:
        Repo-relative POSIX paths of tracked and untracked
        (non-ignored) files, or ``None`` if git is unavailable or the
        directory is not inside a work tree.
    """
    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "-z",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return [p for p in proc.stdout.split("\0") if p]


def _load_pathspec(path: Path) -> pathspec.PathSpec | None:
    """Build a ``gitwildmatch`` ``PathSpec`` from a file, or ``None``.

    Args:
        path: Path to a gitignore-syntax file.

    Returns:
        The compiled spec, or ``None`` when ``path`` doesn't exist.
    """
    if not path.is_file():
        return None
    return pathspec.PathSpec.from_lines(
        "gitwildmatch",
        path.read_text(encoding="utf-8", errors="replace").splitlines(),
    )


def _load_dekkoignore(root: Path) -> pathspec.PathSpec | None:
    """Load the persistent ``.dekko/.dekkoignore``, if present.

    Read live from disk on every call — not cached, not stamped into
    provenance — so a hand-edit takes effect on the very next
    ``discover()`` call and staleness falls out of the existing
    freshness check's file-set diff.

    Args:
        root: Repository root.

    Returns:
        The compiled spec, or ``None`` when no ``.dekkoignore`` exists.
    """
    # Deferred import: `cache` depends on `mapfile`, which imports this
    # module at load time, so a top-level
    # `from dekko.storage.cache import ...` here would be a circular
    # import. By call time, both modules are fully initialized.
    from dekko.storage.cache import CACHE_DIR, DEKKOIGNORE_FILE

    return _load_pathspec(root / CACHE_DIR / DEKKOIGNORE_FILE)


def _walk_files(root: Path) -> list[str]:
    """Walk the tree manually, honoring a root ``.gitignore``.

    Only prunes ``_NOISE_DIRS`` (VCS metadata, tool caches) from the
    walk itself — ``_VENDORED_DIRS`` (``node_modules``, ``vendor``,
    ``third_party``, ...) are still walked into and yielded as
    candidates, so ``_classify`` can see and record them with a
    ``"vendored (<dirname>)"`` reason instead of them vanishing before
    classification ever runs. Pruning both sets here would silently
    reintroduce the "no signal at all" gap this module's vendored-dir
    handling exists to fix, for any repo without a ``.git/`` (the
    ``_git_files`` path has no equivalent pruning step to begin with).
    """
    spec = _load_pathspec(root / ".gitignore")
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        dirnames[:] = [d for d in dirnames if d not in _NOISE_DIRS]
        if spec is not None:
            dirnames[:] = [
                d
                for d in dirnames
                if not spec.match_file(_join(rel_dir, d) + "/")
            ]
        for fname in filenames:
            rel = _join(rel_dir, fname)
            if spec is not None and spec.match_file(rel):
                continue
            found.append(rel)
    return found


def _join(rel_dir: str, name: str) -> str:
    """Join a relative POSIX dir (possibly ``.``) and a name."""
    if rel_dir in ("", "."):
        return name
    return f"{rel_dir}/{name}"


def _in_noise_dir(rel: str) -> bool:
    """Check whether any path component is a silent-exclusion dir."""
    return any(part in _NOISE_DIRS for part in rel.split("/"))


def _source_root_end(parts: list[str]) -> int | None:
    """Index right past a ``src/main|test/<lang>`` prefix, if any.

    Args:
        parts: ``rel.split("/")`` path components (directories and,
            last, the filename).

    Returns:
        The index of the first component *beneath* a recognized JVM
        source root (e.g. ``3`` for ``src/main/java/...``), or
        ``None`` if no such prefix is present anywhere in ``parts``.
    """
    for i in range(len(parts) - 2):
        if (
            parts[i] == "src"
            and parts[i + 1] in ("main", "test")
            and parts[i + 2] in _SOURCE_ROOT_LANGS
        ):
            return i + 3
    return None


def _vendored_dir_hit(rel: str) -> str | None:
    """The first vendored-dir path component in ``rel``, if any.

    Components at or beyond a ``src/main/<lang>``/``src/test/<lang>``
    prefix (see ``_source_root_end``) are exempt — a directory literally
    named ``build`` there is a Java/Kotlin/Groovy/Scala package
    segment (e.g. ``org.springframework.boot.build``), not vendored
    build output.
    """
    parts = rel.split("/")
    root_end = _source_root_end(parts)
    for i, part in enumerate(parts[:-1]):  # last part is the filename
        if root_end is not None and i >= root_end:
            continue
        if part in _VENDORED_DIRS:
            return part
    return None


def _matches_any(rel: str, patterns: tuple[str, ...]) -> bool:
    """Match the basename against glob patterns."""
    base = rel.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(base, pat) for pat in patterns)


def _looks_minified(path: Path) -> bool:
    """Whether a file's sampled average line length flags it as dense.

    Reads only the first ``_MINIFIED_SAMPLE_LINES`` lines rather than
    the whole file. Returns ``False`` (never skip) on any read error
    or an empty file — this heuristic only ever adds a skip, it never
    overrides another gate.

    Args:
        path: Absolute path to a candidate file that has already
            passed every other classification gate.

    Returns:
        ``True`` when the sampled average line length exceeds
        ``_MINIFIED_AVG_LINE_LEN``.
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            lines = list(itertools.islice(f, _MINIFIED_SAMPLE_LINES))
    except OSError:
        return False
    if not lines:
        return False
    avg_len = sum(len(line) for line in lines) / len(lines)
    return avg_len > _MINIFIED_AVG_LINE_LEN


def discover(
    root: Path,
    subpath: str | None = None,
    excludes: tuple[str, ...] = (),
    max_file_size: int = DEFAULT_MAX_FILE_SIZE,
    candidates: list[str] | None = None,
) -> tuple[list[str], list[tuple[str, str]]]:
    """Find all mappable source files under a root directory.

    Args:
        root: Repository root.
        subpath: Optional repo-relative prefix to restrict the map to.
        excludes: Extra glob patterns (matched against basenames and
            full relative paths) to skip.
        max_file_size: Files larger than this many bytes are skipped.
        candidates: Explicit repo-relative paths to classify, bypassing
            this function's own tracked-file discovery (``git
            ls-files``, falling back to a bare filesystem walk). Used
            when the caller already knows the exact file set — e.g. a
            ``git archive`` extraction of a historical rev, where
            ``root`` has no ``.git/`` of its own and the fallback walk
            can't distinguish tracked from untracked paths against a
            ``.gitignore`` pattern (see ``diff.snapshot``'s
            ``candidates`` parameter).

    Returns:
        A pair ``(files, skipped)``: sorted repo-relative paths to
        map, and ``(path, reason)`` pairs for files that were skipped
        — including files in a confirmed-unsupported language (reason
        ``"no parser (<language>)"``, see ``languages.KNOWN_UNSUPPORTED``),
        files under a default-excluded directory that sometimes holds
        first-party code (reason ``"vendored (<dirname>)"``, see
        ``_VENDORED_DIRS`` — distinct from the purely-silent VCS/cache
        dirs in ``_NOISE_DIRS``, which are never recorded here at
        all), and files matched by the persistent
        ``.dekko/.dekkoignore`` (reason ``"ignored"``, distinct from
        ``"excluded"`` — see ``_classify``). Extensions dekko simply
        doesn't recognize at all (non-code files) are still omitted
        with no entry here.
    """
    if candidates is None:
        candidates = _git_files(root)
        if candidates is None:
            candidates = _walk_files(root)

    prefix = None
    if subpath:
        prefix = Path(subpath).as_posix().strip("/")

    ignore_spec = _load_dekkoignore(root)

    files: list[str] = []
    skipped: list[tuple[str, str]] = []
    for rel in sorted(set(candidates)):
        verdict = _classify(
            root, rel, prefix, excludes, ignore_spec, max_file_size
        )
        if verdict is None:
            continue
        if verdict == "ok":
            files.append(rel)
        else:
            skipped.append((rel, verdict))
    return files, skipped


def _classify(
    root: Path,
    rel: str,
    prefix: str | None,
    excludes: tuple[str, ...],
    ignore_spec: pathspec.PathSpec | None,
    max_file_size: int,
) -> str | None:
    """Categorize one candidate path.

    Returns:
        ``"ok"`` to map the file, ``None`` to ignore it silently, or
        a skip reason to report.
    """
    if prefix and not (rel == prefix or rel.startswith(prefix + "/")):
        return None
    if _in_noise_dir(rel):
        return None
    vendored = _vendored_dir_hit(rel)
    if vendored:
        # Unlike a noise dir, this is recorded (not just dropped) —
        # `mapfile._vendored_summary` aggregates it into a coverage
        # note, since a default-excluded dir occasionally holds real
        # first-party code (see this function's module docstring
        # note on `_VENDORED_DIRS`).
        return f"vendored ({vendored})"
    if _matches_any(rel, GENERATED_PATTERNS):
        return "generated"
    if _matches_any(rel, excludes) or any(
        fnmatch.fnmatch(rel, pat) for pat in excludes
    ):
        return "excluded"
    if ignore_spec is not None and ignore_spec.match_file(rel):
        # Distinct from "excluded": a different matching engine
        # (gitwildmatch vs. fnmatch) and a different source (the
        # committed .dekko/.dekkoignore vs. this run's CLI flags), so
        # a user debugging a vanished file knows where to look.
        return "ignored"
    if not languages.is_supported(rel):
        unsupported = languages.known_unsupported_language(rel)
        return f"no parser ({unsupported})" if unsupported else None
    return _size_and_content_gate(root, rel, max_file_size)


def _size_and_content_gate(
    root: Path, rel: str, max_file_size: int
) -> str | None:
    """Final classification gate: file size, then minified-content check.

    Only reached for a candidate that already passed every path-based
    gate (noise/vendored dirs, generated-name patterns, excludes,
    dekkoignore, and language support) — i.e. it's about to be marked
    ``"ok"`` unless it trips one of these two checks. Split out of
    ``_classify`` to keep that function's branch count under the
    project's complexity cap.

    Args:
        root: Repository root.
        rel: Repo-relative candidate path.
        max_file_size: Files larger than this many bytes are skipped.

    Returns:
        ``"too large"``, ``"generated"`` (minified-content heuristic),
        ``"ok"``, or ``None`` when the file can't be stat'd.
    """
    try:
        size = (root / rel).stat().st_size
    except OSError:
        return None
    if size > max_file_size:
        return "too large"
    if _looks_minified(root / rel):
        return "generated"
    return "ok"
