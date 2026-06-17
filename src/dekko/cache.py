"""Per-file extraction cache stored under ``.dekko/``.

Parsing every file with tree-sitter dominates a map run. The cache
keys each file's extracted ``FileMap`` on the same content hash used
for provenance: on the next run, files whose hash is unchanged reuse
their cached ``FileMap`` and skip re-parsing. Resolution still runs
repo-wide (it is cheap relative to parsing).

The cache lives in ``<root>/.dekko/cache.json``. On first creation the
directory is made self-ignoring with an inner ``.dekko/.gitignore`` that
ignores generated files (cache, maps) while keeping ``notes.json`` (and
the ignore file itself) tracked, so symbol annotations can be committed.
The repository ``.gitignore`` is left untouched — a blanket ``.dekko/``
entry there would make ``notes.json`` impossible to track.
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

# Inner ``.dekko/.gitignore``: ignore everything the tool generates, but
# keep this file and committable symbol notes tracked.
_INNER_GITIGNORE = "*\n!.gitignore\n!notes.json\n"
# The pre-notes inner ignore; migrated in place when seen.
_LEGACY_INNER_GITIGNORE = "*\n"


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
        doc=d.get("doc"),
    )


class IncrementalCache:
    """A read-old / write-new view over the per-file extraction cache.

    Attributes:
        entries: Cache entries to persist after the run — populated by
            both reused and freshly extracted files.
        reused: Count of files served from the prior cache this run.
        parsed: Count of files freshly extracted this run.
    """

    def __init__(self, old: dict[str, dict]) -> None:
        """Initialize with the entries loaded from a prior run.

        Args:
            old: Previous ``path -> {"hash", "file"}`` entries, or an
                empty dict to force every file to re-parse.
        """
        self._old = old
        self.entries: dict[str, dict] = {}
        self.reused = 0
        self.parsed = 0

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
        self.reused += 1
        return _filemap_from_dict(entry["file"])

    def store(self, root: Path, rel: str, fm: FileMap) -> None:
        """Record a freshly extracted ``FileMap`` for persistence."""
        self.entries[rel] = {
            "hash": _file_hash(root / rel),
            "file": _filemap_to_dict(fm),
        }
        self.parsed += 1


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
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if doc.get("version") != CACHE_VERSION:
        return {}
    if doc.get("tool_version") != _tool_version():
        return {}
    files = doc.get("files")
    return files if isinstance(files, dict) else {}


def save(root: Path, cache: IncrementalCache) -> None:
    """Persist a cache, wiring gitignore only if ``.dekko/`` is new.

    Args:
        root: Repository root.
        cache: The cache whose ``entries`` should be written.
    """
    cache_dir = _make_cache_dir(root)
    doc = {
        "version": CACHE_VERSION,
        "tool_version": _tool_version(),
        "files": cache.entries,
    }
    (cache_dir / CACHE_FILE).write_text(
        json.dumps(doc) + "\n", encoding="utf-8"
    )


def ensure_dir(root: Path) -> Path:
    """Create ``.dekko/``, wiring gitignore only when it is new.

    Safe to call on every map run. The inner ``.gitignore`` and the
    repo ``.gitignore`` entry are written only when this call actually
    creates the directory; an existing ``.dekko/`` is left untouched.

    Args:
        root: Repository root.

    Returns:
        Path to the ``.dekko/`` directory.
    """
    return _make_cache_dir(root)


def ensure_notes_tracked(root: Path) -> Path:
    """Ensure ``.dekko/`` exists and its inner gitignore tracks notes.

    Unlike the map-run path, this migrates a legacy inner ``.gitignore``
    (the bare ``*``) to the notes-aware form even when ``.dekko/``
    already exists, so adding a note makes ``notes.json`` committable.

    Args:
        root: Repository root.

    Returns:
        Path to the ``.dekko/`` directory.
    """
    cache_dir = _make_cache_dir(root)
    _write_inner_gitignore(cache_dir)
    return cache_dir


def _make_cache_dir(root: Path) -> Path:
    """Return ``.dekko/``, creating it and wiring gitignore if absent.

    The inner ``.gitignore`` is written only when this call creates the
    directory. An existing ``.dekko/`` is returned untouched, so a user
    who edits the ignore file will not have it overwritten on a map run.

    Args:
        root: Repository root.

    Returns:
        Path to the ``.dekko/`` directory.
    """
    cache_dir = root / CACHE_DIR
    if cache_dir.exists():
        return cache_dir
    cache_dir.mkdir(parents=True, exist_ok=True)
    _write_inner_gitignore(cache_dir)
    return cache_dir


def _write_inner_gitignore(cache_dir: Path) -> None:
    """Write the notes-aware inner ``.gitignore`` if safe to do so.

    Writes when the file is absent or still holds the legacy bare ``*``;
    a user-customized ignore file is left untouched.
    """
    inner = cache_dir / ".gitignore"
    if (
        not inner.exists()
        or inner.read_text(encoding="utf-8") == _LEGACY_INNER_GITIGNORE
    ):
        inner.write_text(_INNER_GITIGNORE, encoding="utf-8")
