"""Compare the working tree's symbols against an earlier git rev.

``dekko diff [REV]`` maps the current working tree and the sources at a
git rev, then reports which symbols were added, removed, or changed
(their source text differs) — each with the symbols that call them, so
a reviewer sees the blast radius. The default rev is the commit the map
on disk was generated at; ``REV`` overrides it.

The old-side snapshot (a full export + tree-sitter re-parse of ``REV``)
is cached under ``.dekko/rev-cache/<sha>.json`` (see ``revcache.py``),
keyed on the rev's resolved commit SHA — repeated ``diff``/``affected``
calls against the same rev after the first reuse the cached snapshot
instead of paying the export/re-parse cost again.
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

from dekko import repo_ops
from dekko.storage import cache as cache_mod
from dekko.render import mapfile
from dekko.storage import revcache
from dekko.core import walker
from dekko.core.model import Import, Symbol
from dekko.textutil import signature
from dekko.core.resolver import MODULE_CALLER_SUFFIX, resolve

EXIT_SAME = 0
EXIT_DIFFERENT = 1
EXIT_ERROR = 2

# Round-15 finding (round15-jobs-default-latency-plan.md): a bare
# `diff`/`affected`/`workset` invocation with no rev-cache entry for
# its target commit falls into old_snapshot()'s cache-miss path,
# which -- at the default `--jobs 1` -- re-parses and resolves every
# tracked file at that rev single-threaded. On the fleet's largest
# repos this produced several minutes of zero-feedback silence
# indistinguishable from a hang (tensorflow, 14,285 files: 5+ minutes
# sequential vs. ~35s with `--jobs 0`). Chosen empirically from
# round-15's own per-repo file counts: comfortably above cline/zed/
# claude-code (up to ~2,730 files, none flagged as slow) and well
# below spring-boot/tensorflow (9,942/14,285 files, the two repos
# where this was actually noticeable), so the note only fires where
# it's likely to matter.
_SEQUENTIAL_DISCLOSURE_THRESHOLD = 5000


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


def _body_hashes_for_path(
    root: Path, path: str, syms: list[Symbol]
) -> dict[str, str]:
    """Hash every symbol defined in one file from a single read+split.

    Reading and re-splitting a symbol's whole defining file from disk
    once per symbol (the previous approach) meant a file with several
    symbols paid that cost once per symbol. Grouping by path first
    cuts snapshot construction from O(total symbols) file reads to
    O(total files).
    """
    try:
        lines = (
            (root / path)
            .read_text(encoding="utf-8", errors="replace")
            .splitlines()
        )
    except OSError:
        return {s.id: "" for s in syms}
    out: dict[str, str] = {}
    for s in syms:
        body = "\n".join(lines[s.start_line - 1 : s.end_line])
        out[s.id] = hashlib.sha256(body.encode()).hexdigest()[:16]
    return out


def _body_hashes(root: Path, syms: list[Symbol]) -> dict[str, str]:
    """Body-hash every symbol in ``syms``, reading each file once."""
    by_path: dict[str, list[Symbol]] = {}
    for sym in syms:
        by_path.setdefault(sym.path, []).append(sym)
    out: dict[str, str] = {}
    for path, path_syms in by_path.items():
        out.update(_body_hashes_for_path(root, path, path_syms))
    return out


def snapshot(
    root: Path,
    subpath: str | None,
    excludes: tuple[str, ...],
    max_file_size: int,
    cache: cache_mod.IncrementalCache | None = None,
    candidates: list[str] | None = None,
    jobs: int = 1,
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
        jobs: Resolved worker count (1 = sequential) for both file
            extraction (``repo_ops.map_repository``) and call-graph
            resolution (``resolve``). Round-12 master report §3.3:
            this call used to always run both single-threaded
            regardless of ``dekko map --full``'s own ``--jobs``
            fix — a separate, unparallelized code path that made a
            first-touch/cold-rev-cache ``diff``/``affected``/
            ``workset`` call minutes slower than it needed to be on
            a large repo. Callers pass an already-resolved concrete
            count (see ``repo_ops.resolve_workers``), not the raw
            ``--jobs`` CLI value (which allows ``0`` for "all
            cores").
    """
    files, _ = repo_ops.map_repository(
        root,
        subpath,
        excludes,
        max_file_size,
        cache=cache,
        jobs=jobs,
        candidates=candidates,
    )
    graph = resolve(files, workers=jobs)
    snap = Snapshot()
    all_syms: list[Symbol] = []
    for fm in files:
        for sym in fm.symbols:
            snap.symbols[sym.id] = sym
            all_syms.append(sym)
        if fm.imports:
            snap.imports[fm.path] = fm.imports
    snap.body = _body_hashes(root, all_syms)
    snap.callers = graph.calls_in
    return snap


def snapshot_from_index(index: mapfile.MapIndex, root: Path) -> Snapshot:
    """Build a ``Snapshot`` directly from an already-loaded ``MapIndex``.

    Reuses the index's symbol/caller/import tables outright instead of
    a full tree-sitter re-parse plus ``resolve()`` pass — only each
    symbol's body hash needs fresh work (one file read + hash per
    distinct path, via ``_body_hashes``). This is the fix for the
    redundant-reparse performance defect: ``affected.changes()``/
    ``diff.run()`` already load a fully-populated ``MapIndex`` for the
    current working tree before ever touching ``snapshot()``; using it
    here instead of re-parsing every file from scratch is the dominant
    cost saving on a large repo.

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
    snap.body = _body_hashes(root, list(snap.symbols.values()))
    return snap


def snapshot_new_side(
    root: Path,
    subpath: str | None,
    excludes: tuple[str, ...],
    max_file_size: int,
    index: mapfile.MapIndex | None,
    jobs: int = 1,
) -> Snapshot:
    """New-side (working tree) snapshot, reusing a fresh index when possible.

    Falls back to a full re-parse (``snapshot``) whenever ``index`` is
    missing or stale against the current working tree, so the
    performance win never comes at the cost of correctness — a caller
    that forgot to regenerate the map first still gets an accurate
    diff, just without the speedup. ``jobs`` (see ``snapshot``) only
    matters on that fallback path.
    """
    if index is not None and mapfile.check_freshness(root, index).fresh:
        return snapshot_from_index(index, root)
    return snapshot(root, subpath, excludes, max_file_size, jobs=jobs)


def old_snapshot(
    root: Path,
    target_rev: str,
    subpath: str | None,
    excludes: tuple[str, ...],
    max_file_size: int,
    old_cache: cache_mod.IncrementalCache,
    jobs: int = 1,
) -> Snapshot | None:
    """Old-side snapshot for ``target_rev``, from the rev-cache when possible.

    Shared by ``diff.run`` and ``affected.changes`` — both need the
    identical old-side snapshot (export + re-map of a historical git
    rev), the dominant cost of either command on a large repo (round-08
    §2.6). ``target_rev`` is resolved to its full commit SHA first; a
    commit's tree is immutable once it exists, so a cache hit here is
    unconditionally safe to reuse without any freshness check (unlike
    the working tree's own map). Falls back to the always-correct
    export/extract/parse path — which also populates the cache for
    next time — on a cache miss, an unresolvable SHA, or a corrupt
    cache entry.

    Args:
        root: Repository root (the real repo, with ``.git/``).
        target_rev: Git rev for the old side (already defaulted by the
            caller — see ``run``/``affected.changes``).
        subpath: Optional repo-relative subtree restriction.
        excludes: Extra glob patterns to skip.
        max_file_size: Size cap in bytes.
        old_cache: Incremental extraction cache to pass through to
            ``snapshot()`` on a rev-cache miss.
        jobs: Resolved worker count for the rev-cache-miss export/
            re-parse/resolve path — see ``snapshot``. No effect on a
            rev-cache hit, which skips ``snapshot()`` entirely.

    Returns:
        The old-side ``Snapshot``, or ``None`` if ``target_rev`` cannot
        be exported (unknown rev, not a git repo).
    """
    sha = revcache.resolve_sha(root, target_rev)
    if sha is not None:
        cached = revcache.load(root, sha)
        if cached is not None:
            return cached
    with tempfile.TemporaryDirectory(prefix="dekko-diff-") as tmp:
        old_root = Path(tmp)
        if not export_rev(root, target_rev, old_root):
            return None
        candidates = tracked_at_rev(root, target_rev)
        _maybe_warn_sequential(jobs, candidates)
        old = snapshot(
            old_root,
            subpath,
            excludes,
            max_file_size,
            cache=old_cache,
            candidates=candidates,
            jobs=jobs,
        )
    if sha is not None:
        revcache.save(root, sha, old)
    return old


def _maybe_warn_sequential(jobs: int, candidates: list[str] | None) -> None:
    """Disclose a slow single-threaded rev-cache-miss re-parse/resolve.

    Round-15 finding: at the default ``--jobs 1``, a first-touch
    ``diff``/``affected``/``workset`` call on a large repo re-parses
    and resolves every tracked file at the target rev single-threaded
    with no progress output -- on the largest repos in the fleet this
    ran for several minutes, indistinguishable from a hang (see
    ``_SEQUENTIAL_DISCLOSURE_THRESHOLD``). Mirrors the pattern
    ``render_lean.run`` already uses for its own budget-floor
    disclosure: a one-line ``note:`` to stderr, printed once, before
    the slow work starts -- no behavior change, purely additive.

    Args:
        jobs: Resolved worker count about to be passed to
            ``snapshot()`` (1 = sequential).
        candidates: The file list about to be re-parsed, or ``None``
            (an unreadable rev -- ``snapshot()`` will fall back to its
            own discovery, so there's nothing to count here).
    """
    if jobs > 1 or candidates is None:
        return
    if len(candidates) < _SEQUENTIAL_DISCLOSURE_THRESHOLD:
        return
    print(
        f"note: no rev-cache for this commit; single-threaded resolve "
        f"on {len(candidates)} files may take a while -- pass --jobs 0 "
        f"to use all cores",
        file=sys.stderr,
    )


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


def run(
    root: Path,
    rev: str | None,
    as_json: bool,
    limit: int,
    jobs: int = 1,
) -> int:
    """Execute ``dekko diff`` against a repository.

    Args:
        root: Repository root (its working tree is the new side).
        rev: Git rev for the old side, or ``None`` to derive a default.
        as_json: Emit structured JSON instead of text.
        limit: Max impacted callers shown per symbol.
        jobs: Resolved worker count for a rev-cache-miss old-side
            snapshot or a stale-index new-side re-parse — see
            ``snapshot``. No effect when both sides are already warm
            (rev-cache hit, fresh index).

    Returns:
        Process exit code (0 no changes, 1 changes, 2 error).
    """
    index = repo_ops.load_current_index_no_regen(root)
    prov = (index.provenance if index else None) or {}
    subpath = prov.get("subpath")
    excludes = tuple(prov.get("excludes", []))
    max_file_size = prov.get("max_file_size", walker.DEFAULT_MAX_FILE_SIZE)
    target_rev = rev or prov.get("git_commit") or "HEAD"

    old_cache = cache_mod.IncrementalCache(cache_mod.load(root))
    old = old_snapshot(
        root,
        target_rev,
        subpath,
        excludes,
        max_file_size,
        old_cache,
        jobs=jobs,
    )
    if old is None:
        print(
            f"dekko: cannot export git rev '{target_rev}' "
            f"(unknown rev or not a git repo)",
            file=sys.stderr,
        )
        return EXIT_ERROR

    new = snapshot_new_side(
        root, subpath, excludes, max_file_size, index, jobs=jobs
    )
    result = compare(target_rev, old, new)
    render(result, as_json, limit)
    return EXIT_SAME if result.empty() else EXIT_DIFFERENT
