"""File discovery: enumerate mappable source files in a repository."""

import fnmatch
import os
import subprocess
from pathlib import Path

import pathspec

from . import languages

DEFAULT_EXCLUDE_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    ".tox",
    "node_modules",
    "target",
    "dist",
    "build",
    "vendor",
    "third_party",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
}

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
    # module at load time, so a top-level `from .cache import ...` here
    # would be a circular import. By call time, both modules are fully
    # initialized.
    from .cache import CACHE_DIR, DEKKOIGNORE_FILE

    return _load_pathspec(root / CACHE_DIR / DEKKOIGNORE_FILE)


def _walk_files(root: Path) -> list[str]:
    """Walk the tree manually, honoring a root ``.gitignore``."""
    spec = _load_pathspec(root / ".gitignore")
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        dirnames[:] = [d for d in dirnames if d not in DEFAULT_EXCLUDE_DIRS]
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


def _in_excluded_dir(rel: str) -> bool:
    """Check whether any path component is a default-excluded dir."""
    return any(part in DEFAULT_EXCLUDE_DIRS for part in rel.split("/"))


def _matches_any(rel: str, patterns: tuple[str, ...]) -> bool:
    """Match the basename against glob patterns."""
    base = rel.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(base, pat) for pat in patterns)


def discover(
    root: Path,
    subpath: str | None = None,
    excludes: tuple[str, ...] = (),
    max_file_size: int = DEFAULT_MAX_FILE_SIZE,
) -> tuple[list[str], list[tuple[str, str]]]:
    """Find all mappable source files under a root directory.

    Args:
        root: Repository root.
        subpath: Optional repo-relative prefix to restrict the map to.
        excludes: Extra glob patterns (matched against basenames and
            full relative paths) to skip.
        max_file_size: Files larger than this many bytes are skipped.

    Returns:
        A pair ``(files, skipped)``: sorted repo-relative paths to
        map, and ``(path, reason)`` pairs for files that were skipped
        — including files in a confirmed-unsupported language (reason
        ``"no parser (<language>)"``, see ``languages.KNOWN_UNSUPPORTED``)
        and files matched by the persistent ``.dekko/.dekkoignore``
        (reason ``"ignored"``, distinct from ``"excluded"`` — see
        ``_classify``). Extensions dekko simply doesn't recognize at
        all (non-code files) are still omitted with no entry here.
    """
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
    if _in_excluded_dir(rel):
        return None
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
    try:
        size = (root / rel).stat().st_size
    except OSError:
        return None
    if size > max_file_size:
        return "too large"
    return "ok"
