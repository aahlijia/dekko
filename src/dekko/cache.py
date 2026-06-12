"""Per-file extraction cache stored under ``.dekko/``.

Parsing every file with tree-sitter dominates a map run. The cache
keys each file's extracted ``FileMap`` on the same content hash used
for provenance: on the next run, files whose hash is unchanged reuse
their cached ``FileMap`` and skip re-parsing. Resolution still runs
repo-wide (it is cheap relative to parsing).

The cache lives in ``<root>/.dekko/cache.json``. On first creation the
directory is made self-ignoring (``.dekko/.gitignore`` of ``*``) and
``.dekko/`` is appended to the repository ``.gitignore``.
"""

import json
from dataclasses import asdict
from importlib.metadata import version as _pkg_version
from pathlib import Path

from .mapfile import _file_hash, _symbol_from_dict
from .model import FileMap, Import, RawCall

CACHE_VERSION = 1
CACHE_DIR = ".dekko"
CACHE_FILE = "cache.json"


def _tool_version() -> str:
    """Current dekko version, used to invalidate stale extractions."""
    return _pkg_version("dekko")


def _filemap_to_dict(fm: FileMap) -> dict:
    """Serialize a ``FileMap`` for the cache."""
    return asdict(fm)


def _filemap_from_dict(d: dict) -> FileMap:
    """Rebuild a ``FileMap`` from its cached dict."""
    return FileMap(
        path=d["path"],
        language=d["language"],
        symbols=[_symbol_from_dict(s) for s in d.get("symbols", [])],
        calls=[RawCall(**c) for c in d.get("calls", [])],
        imports=[Import(**i) for i in d.get("imports", [])],
        error=d.get("error"),
    )


class IncrementalCache:
    """A read-old / write-new view over the per-file extraction cache.

    Attributes:
        entries: Cache entries to persist after the run — populated by
            both reused and freshly extracted files.
    """

    def __init__(self, old: dict[str, dict]) -> None:
        """Initialize with the entries loaded from a prior run.

        Args:
            old: Previous ``path -> {"hash", "file"}`` entries, or an
                empty dict to force every file to re-parse.
        """
        self._old = old
        self.entries: dict[str, dict] = {}

    def reuse(self, root: Path, rel: str) -> FileMap | None:
        """Return the cached ``FileMap`` for an unchanged file.

        Args:
            root: Repository root.
            rel: Repo-relative path of the file.

        Returns:
            The cached ``FileMap`` when a prior entry's hash matches the
            current file, else ``None``.
        """
        entry = self._old.get(rel)
        if entry is None or entry.get("hash") != _file_hash(root / rel):
            return None
        self.entries[rel] = entry
        return _filemap_from_dict(entry["file"])

    def store(self, root: Path, rel: str, fm: FileMap) -> None:
        """Record a freshly extracted ``FileMap`` for persistence."""
        self.entries[rel] = {
            "hash": _file_hash(root / rel),
            "file": _filemap_to_dict(fm),
        }


def load(root: Path) -> dict[str, dict]:
    """Load the prior cache entries for a repository.

    Args:
        root: Repository root.

    A cache written by a different dekko version is discarded, so
    extractor changes always take effect on the next run without a
    manual ``--full``.

    Returns:
        ``path -> entry`` mapping, or an empty dict when no usable
        cache exists.
    """
    path = root / CACHE_DIR / CACHE_FILE
    try:
        doc = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if doc.get("version") != CACHE_VERSION:
        return {}
    if doc.get("tool_version") != _tool_version():
        return {}
    files = doc.get("files")
    return files if isinstance(files, dict) else {}


def save(root: Path, cache: IncrementalCache) -> None:
    """Persist a cache and ensure ``.dekko/`` is git-ignored.

    Args:
        root: Repository root.
        cache: The cache whose ``entries`` should be written.
    """
    cache_dir = root / CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)
    _ensure_ignored(root, cache_dir)
    doc = {
        "version": CACHE_VERSION,
        "tool_version": _tool_version(),
        "files": cache.entries,
    }
    (cache_dir / CACHE_FILE).write_text(json.dumps(doc) + "\n")


def ensure_dir(root: Path) -> Path:
    """Create ``.dekko/`` and set up gitignore entries.

    Idempotent — safe to call on every map run. Returns the cache dir.

    Args:
        root: Repository root.

    Returns:
        Path to the ``.dekko/`` directory.
    """
    cache_dir = root / CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)
    _ensure_ignored(root, cache_dir)
    return cache_dir


def _ensure_ignored(root: Path, cache_dir: Path) -> None:
    """Make ``.dekko/`` self-ignoring and ignored by the repo."""
    inner = cache_dir / ".gitignore"
    if not inner.exists():
        inner.write_text("*\n")

    gitignore = root / ".gitignore"
    entry = f"{CACHE_DIR}/"
    text = gitignore.read_text() if gitignore.exists() else ""
    if entry in text.splitlines():
        return
    if text and not text.endswith("\n"):
        text += "\n"
    gitignore.write_text(text + entry + "\n")
