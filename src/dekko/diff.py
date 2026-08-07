"""Compare the working tree's symbols against an earlier git rev.

``dekko diff [REV]`` maps the current working tree and the sources at a
git rev, then reports which symbols were added, removed, or changed
(their source text differs) — each with the symbols that call them, so
a reviewer sees the blast radius. The default rev is the commit the map
on disk was generated at; ``REV`` overrides it.
"""

import hashlib
import io
import json
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import cache as cache_mod
from . import mapfile
from . import walker
from .model import Import, Symbol
from .textutil import signature
from .resolver import MODULE_CALLER_SUFFIX, resolve

EXIT_SAME = 0
EXIT_DIFFERENT = 1
EXIT_ERROR = 2


@dataclass
class Snapshot:
    """Symbols and inbound adjacency for one mapped tree.

    Attributes:
        symbols: Symbol id → symbol.
        callers: Symbol id → caller ids (resolved + module-level).
        body: Symbol id → short hash of the definition's source text.
        imports: File path → imports declared in it (used by
            ``affected`` for its import-edge fallback).
    """

    symbols: dict[str, Symbol] = field(default_factory=dict)
    callers: dict[str, list[str]] = field(default_factory=dict)
    body: dict[str, str] = field(default_factory=dict)
    imports: dict[str, list[Import]] = field(default_factory=dict)


@dataclass
class SymbolDelta:
    """One changed symbol and the symbols that call it."""

    symbol: Symbol
    callers: list[str]


@dataclass
class DiffResult:
    """Added/removed/changed symbols between two snapshots."""

    rev: str
    added: list[SymbolDelta] = field(default_factory=list)
    removed: list[SymbolDelta] = field(default_factory=list)
    changed: list[SymbolDelta] = field(default_factory=list)

    def empty(self) -> bool:
        """True when nothing was added, removed, or changed."""
        return not (self.added or self.removed or self.changed)


def _body_hash(root: Path, sym: Symbol) -> str:
    """Short hash of a symbol's defining source lines."""
    try:
        lines = (
            (root / sym.path)
            .read_text(encoding="utf-8", errors="replace")
            .splitlines()
        )
    except OSError:
        return ""
    body = "\n".join(lines[sym.start_line - 1 : sym.end_line])
    return hashlib.sha256(body.encode()).hexdigest()[:16]


def snapshot(
    root: Path,
    subpath: str | None,
    excludes: tuple[str, ...],
    max_file_size: int,
    cache: cache_mod.IncrementalCache | None = None,
    candidates: list[str] | None = None,
) -> Snapshot:
    """Map a tree and capture its symbols, callers, and body hashes.

    Args:
        root: Directory to map.
        subpath: Optional repo-relative subtree restriction.
        excludes: Extra glob patterns to skip.
        max_file_size: Size cap in bytes.
        cache: Optional incremental extraction cache. Entries are keyed
            on file content hash, not path, so passing the *current*
            tree's cache in for an old-rev extraction still pays off:
            any file whose content at the old rev is byte-identical to
            the current tree's cached entry skips its tree-sitter parse.
        candidates: Explicit repo-relative paths to map, bypassing
            ``walker.discover``'s own tracked-file discovery. ``root``
            for the old side of a diff is a plain ``git archive``
            extraction with no ``.git/`` of its own, so discovery there
            falls back to a bare filesystem walk that reapplies
            ``.gitignore`` with no tracked/untracked distinction — wrong
            for any ignore pattern that happens to match an
            already-tracked path. Callers building the old side should
            pass ``tracked_at_rev(root, rev)`` (queried against the
            *real* repo, which does have ``.git/``) here instead.
    """
    from . import cli

    files, _ = cli.map_repository(
        root,
        subpath,
        excludes,
        max_file_size,
        cache=cache,
        candidates=candidates,
    )
    graph = resolve(files)
    snap = Snapshot()
    for fm in files:
        for sym in fm.symbols:
            snap.symbols[sym.id] = sym
            snap.body[sym.id] = _body_hash(root, sym)
        if fm.imports:
            snap.imports[fm.path] = fm.imports
    snap.callers = graph.calls_in
    return snap


def snapshot_from_index(index: mapfile.MapIndex, root: Path) -> Snapshot:
    """Build a ``Snapshot`` directly from an already-loaded ``MapIndex``.

    Reuses the index's symbol/caller/import tables outright instead of
    a full tree-sitter re-parse plus ``resolve()`` pass — only each
    symbol's body hash needs fresh work (one file read + hash, via
    ``_body_hash``). This is the fix for the redundant-reparse
    performance defect: ``affected.changes()``/``diff.run()`` already
    load a fully-populated ``MapIndex`` for the current working tree
    before ever touching ``snapshot()``; using it here instead of
    re-parsing every file from scratch is the dominant cost saving on a
    large repo.

    Callers must confirm ``index`` is fresh against the working tree
    first (see ``snapshot_new_side``) — a stale index's symbol table no
    longer reflects what's on disk, and using it here would silently
    reintroduce drift between what ``diff``/``affected`` report and
    what actually changed.
    """
    snap = Snapshot()
    snap.symbols = dict(index.symbols_by_id)
    snap.callers = index.calls_in
    # Match snapshot()'s own construction exactly: only files with at
    # least one import get an entry. index.imports_by_path (loaded from
    # map.json) has a key for every mapped file, even ones with an
    # empty import list, so this isn't a no-op — leaving it as a plain
    # reassignment would diverge from the "real" extraction path.
    snap.imports = {
        path: imports
        for path, imports in index.imports_by_path.items()
        if imports
    }
    for sym_id, sym in snap.symbols.items():
        snap.body[sym_id] = _body_hash(root, sym)
    return snap


def snapshot_new_side(
    root: Path,
    subpath: str | None,
    excludes: tuple[str, ...],
    max_file_size: int,
    index: mapfile.MapIndex | None,
) -> Snapshot:
    """New-side (working tree) snapshot, reusing a fresh index when possible.

    Falls back to a full re-parse (``snapshot``) whenever ``index`` is
    missing or stale against the current working tree, so the
    performance win never comes at the cost of correctness — a caller
    that forgot to regenerate the map first still gets an accurate
    diff, just without the speedup.
    """
    if index is not None and mapfile.check_freshness(root, index).fresh:
        return snapshot_from_index(index, root)
    return snapshot(root, subpath, excludes, max_file_size)


def tracked_at_rev(root: Path, rev: str) -> list[str] | None:
    """Repo-relative paths tracked at ``rev``, or ``None`` on failure.

    Queried against ``root`` (the real repository, which has ``.git/``)
    rather than a ``git archive`` extraction of that rev — see
    ``snapshot``'s ``candidates`` parameter for why the extraction
    directory can't answer this question on its own.
    """
    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "ls-tree",
                "-r",
                "--name-only",
                "-z",
                rev,
            ],
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    text = proc.stdout.decode("utf-8", errors="replace")
    return [p for p in text.split("\0") if p]


def _safe_extractall(tf: tarfile.TarFile, dest: Path) -> None:
    """Extract every member into ``dest``, refusing path traversal.

    On 3.12+ this delegates to the stdlib ``data`` filter. On the
    3.10/3.11 floor (no ``filter`` argument) it drops any member whose
    resolved path would escape ``dest``. ``git archive`` output is
    trusted, but the guard is cheap and correct.
    """
    if sys.version_info >= (3, 12):
        tf.extractall(dest, filter="data")
        return
    dest_resolved = dest.resolve()
    safe = []
    for member in tf.getmembers():
        target = (dest / member.name).resolve()
        if target == dest_resolved or dest_resolved in target.parents:
            safe.append(member)
    tf.extractall(dest, members=safe)


def export_rev(root: Path, rev: str, dest: Path) -> bool:
    """Extract the tracked sources at ``rev`` into ``dest``.

    Args:
        root: Repository root.
        rev: Git revision to export.
        dest: Empty directory to receive the sources.

    Returns:
        ``True`` on success, ``False`` if the rev or git is unavailable.
    """
    try:
        archive = subprocess.run(
            ["git", "-C", str(root), "archive", "--format=tar", rev],
            capture_output=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if archive.returncode != 0:
        return False
    try:
        with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tf:
            _safe_extractall(tf, dest)
    except (tarfile.TarError, OSError):
        return False
    return True


def _render_caller(caller_id: str, syms: dict[str, Symbol]) -> str:
    """One-line label for a caller id (resolved or module-level)."""
    if caller_id.endswith(MODULE_CALLER_SUFFIX):
        return f"{caller_id[: -len(MODULE_CALLER_SUFFIX)]} (module level)"
    sym = syms.get(caller_id)
    if sym is not None:
        return f"{sym.path}:{sym.start_line} {sym.qualname}"
    return caller_id


def _callers_of(snap: Snapshot, sym_id: str) -> list[str]:
    """Caller labels for a symbol id within a snapshot."""
    return [
        _render_caller(cid, snap.symbols)
        for cid in snap.callers.get(sym_id, [])
    ]


def compare(rev: str, old: Snapshot, new: Snapshot) -> DiffResult:
    """Diff two snapshots into added/removed/changed deltas."""
    old_ids, new_ids = set(old.symbols), set(new.symbols)
    result = DiffResult(rev=rev)
    result.added = [
        SymbolDelta(new.symbols[i], _callers_of(new, i))
        for i in sorted(new_ids - old_ids)
    ]
    result.removed = [
        SymbolDelta(old.symbols[i], _callers_of(old, i))
        for i in sorted(old_ids - new_ids)
    ]
    result.changed = [
        SymbolDelta(new.symbols[i], _callers_of(new, i))
        for i in sorted(old_ids & new_ids)
        if old.body.get(i) != new.body.get(i)
    ]
    return result


def _delta_json(delta: SymbolDelta) -> dict:
    """Structured rendering of one symbol delta."""
    sym = delta.symbol
    return {
        "id": sym.id,
        "kind": sym.kind,
        "path": sym.path,
        "line": sym.start_line,
        "signature": signature(sym),
        "callers": delta.callers,
    }


def _print_delta(marker: str, delta: SymbolDelta, limit: int) -> None:
    """Print one symbol delta and a capped list of its callers."""
    sym = delta.symbol
    print(f"{marker} {sym.path}:{sym.start_line}  {signature(sym)}")
    for caller in delta.callers[:limit]:
        print(f"    called by: {caller}")
    extra = len(delta.callers) - limit
    if extra > 0:
        print(f"    ... and {extra} more callers")


def render(result: DiffResult, as_json: bool, limit: int) -> None:
    """Emit a diff result as text or JSON."""
    if as_json:
        doc = {
            "rev": result.rev,
            "added": [_delta_json(d) for d in result.added],
            "removed": [_delta_json(d) for d in result.removed],
            "changed": [_delta_json(d) for d in result.changed],
        }
        print(json.dumps(doc, indent=2))
        return

    if result.empty():
        print(f"dekko: no symbol changes vs {result.rev[:12]}")
        return

    print(
        f"dekko: {len(result.changed)} changed, {len(result.added)} added, "
        f"{len(result.removed)} removed vs {result.rev[:12]}"
    )
    for marker, deltas in (
        ("~", result.changed),
        ("+", result.added),
        ("-", result.removed),
    ):
        for delta in deltas:
            _print_delta(marker, delta, limit)


def run(root: Path, rev: str | None, as_json: bool, limit: int) -> int:
    """Execute ``dekko diff`` against a repository.

    Args:
        root: Repository root (its working tree is the new side).
        rev: Git rev for the old side, or ``None`` to derive a default.
        as_json: Emit structured JSON instead of text.
        limit: Max impacted callers shown per symbol.

    Returns:
        Process exit code (0 no changes, 1 changes, 2 error).
    """
    index = mapfile.load_map(root)
    prov = (index.provenance if index else None) or {}
    subpath = prov.get("subpath")
    excludes = tuple(prov.get("excludes", []))
    max_file_size = prov.get("max_file_size", walker.DEFAULT_MAX_FILE_SIZE)
    target_rev = rev or prov.get("git_commit") or "HEAD"

    old_cache = cache_mod.IncrementalCache(cache_mod.load(root))
    with tempfile.TemporaryDirectory(prefix="dekko-diff-") as tmp:
        old_root = Path(tmp)
        if not export_rev(root, target_rev, old_root):
            print(
                f"dekko: cannot export git rev '{target_rev}' "
                f"(unknown rev or not a git repo)",
                file=sys.stderr,
            )
            return EXIT_ERROR
        old = snapshot(
            old_root,
            subpath,
            excludes,
            max_file_size,
            cache=old_cache,
            candidates=tracked_at_rev(root, target_rev),
        )

    new = snapshot_new_side(root, subpath, excludes, max_file_size, index)
    result = compare(target_rev, old, new)
    render(result, as_json, limit)
    return EXIT_SAME if result.empty() else EXIT_DIFFERENT
