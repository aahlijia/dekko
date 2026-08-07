"""Disk-backed cache of mapped historical git revisions.

``diff``/``affected`` need a full :class:`diff.Snapshot` (symbols,
callers, body hashes, imports) of the tree at some historical git rev
to compare against the working tree. Building one means a ``git
archive`` + tarfile extraction into a temp directory, followed by a
full tree-sitter re-parse and resolve pass — the same cost as a cold
``dekko map`` — paid again on **every** ``diff``/``affected`` call,
even repeated calls against the identical rev (a realistic pattern: an
agent checks impact, adjusts, re-checks).

A commit's tree is immutable once it exists, so a snapshot cached
under its resolved full SHA never goes stale the way the working
tree's own map can — there is no freshness question to re-check on
every access here, only "have we already paid this cost for this
exact commit" (round-08 §2.6).

Bounded to :data:`MAX_ENTRIES` most-recently-used revisions (a simple
access-time cap via ``mtime``, not full LRU bookkeeping) so a repo
diffed against many different revs over time doesn't grow
``.dekko/rev-cache/`` unboundedly.
"""

import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

from .mapfile import _json_dumps, _json_loads, _symbol_from_dict
from .model import Import

if TYPE_CHECKING:
    from .diff import Snapshot

REV_CACHE_DIR = "rev-cache"
_MAP_DIR = ".dekko"

# Most-recently-used revisions to keep on disk before older entries are
# evicted. A plain access-time cap rather than true LRU bookkeeping —
# simpler to implement correctly, and a cache whose main value is "the
# last few revisions someone's actively iterating against" doesn't
# need more (round-08 §2.6's tradeoffs section).
MAX_ENTRIES = 20


def resolve_sha(root: Path, rev: str) -> str | None:
    """Resolve ``rev`` to its full commit SHA.

    Args:
        root: Repository root.
        rev: Any git revision expression (``HEAD``, ``HEAD~1``, a
            branch name, a short or full SHA, ...).

    Returns:
        The full 40-character commit SHA, or ``None`` if ``rev``
        cannot be resolved (unknown rev, not a git repo, ``git``
        unavailable).
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", rev],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    sha = proc.stdout.strip()
    return sha or None


def _cache_dir(root: Path) -> Path:
    """The ``.dekko/rev-cache/`` directory for a repository root."""
    return root / _MAP_DIR / REV_CACHE_DIR


def _entry_path(root: Path, sha: str) -> Path:
    """The cache file path for one resolved commit SHA."""
    return _cache_dir(root) / f"{sha}.json"


def load(root: Path, sha: str) -> "Snapshot | None":
    """Load a cached snapshot for a resolved commit SHA.

    Args:
        root: Repository root.
        sha: Full commit SHA, as returned by :func:`resolve_sha`.

    Returns:
        The cached ``Snapshot``, or ``None`` on a cache miss or a
        corrupt/unreadable entry (treated as a miss, never an error —
        the caller falls back to a fresh export + re-map).
    """
    path = _entry_path(root, sha)
    try:
        data = path.read_bytes()
    except OSError:
        return None
    try:
        doc = _json_loads(data)
    except ValueError:
        return None
    if not isinstance(doc, dict):
        return None
    snap = _snapshot_from_dict(doc)
    _touch(path)
    return snap


def save(root: Path, sha: str, snap: "Snapshot") -> None:
    """Persist a snapshot for a resolved commit SHA, then evict old
    entries past :data:`MAX_ENTRIES`.

    Args:
        root: Repository root.
        sha: Full commit SHA the snapshot was built from.
        snap: The snapshot to cache.
    """
    cache_dir = _cache_dir(root)
    cache_dir.mkdir(parents=True, exist_ok=True)
    _entry_path(root, sha).write_bytes(_json_dumps(_snapshot_to_dict(snap)))
    _evict(cache_dir)


def _touch(path: Path) -> None:
    """Bump a cache entry's mtime so it counts as recently used."""
    try:
        path.touch()
    except OSError:
        pass


def _evict(cache_dir: Path) -> None:
    """Drop the oldest-accessed entries once the cap is exceeded."""
    entries = sorted(cache_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
    stale = entries[:-MAX_ENTRIES] if len(entries) > MAX_ENTRIES else []
    for path in stale:
        path.unlink(missing_ok=True)


def _snapshot_to_dict(snap: "Snapshot") -> dict:
    """Serialize a ``Snapshot`` to a JSON-able dict."""
    return {
        "symbols": [asdict(s) for s in snap.symbols.values()],
        "callers": snap.callers,
        "body": snap.body,
        "imports": {
            path: [asdict(imp) for imp in imps]
            for path, imps in snap.imports.items()
        },
    }


def _snapshot_from_dict(doc: dict) -> "Snapshot":
    """Rebuild a ``Snapshot`` from its cached dict."""
    from .diff import Snapshot

    snap = Snapshot()
    for sym_dict in doc.get("symbols", []):
        sym = _symbol_from_dict(sym_dict)
        snap.symbols[sym.id] = sym
    snap.callers = doc.get("callers", {})
    snap.body = doc.get("body", {})
    snap.imports = {
        path: [Import(**imp) for imp in imps]
        for path, imps in doc.get("imports", {}).items()
    }
    return snap
