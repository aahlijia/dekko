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
import os
import subprocess
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from dekko import repo_ops
from dekko.storage import cache as cache_mod
from dekko.render import mapfile
from dekko.storage import filelock, revcache
from dekko.core import walker
from dekko.core.model import Import, Symbol
from dekko.textutil import signature
from dekko.core.resolver import MODULE_CALLER_SUFFIX, resolve

EXIT_SAME = 0
EXIT_DIFFERENT = 1
EXIT_ERROR = 2

# A bare `diff`/`affected`/`workset` invocation with no rev-cache entry for
# its target commit falls into old_snapshot()'s cache-miss path,
# which -- at the default `--jobs 1` -- re-parses and resolves every
# tracked file at that rev single-threaded. On the fleet's largest
# repos this produced several minutes of zero-feedback silence
# indistinguishable from a hang (tensorflow, 14,285 files: 5+ minutes
# sequential vs. ~35s with `--jobs 0`). Chosen empirically from
# measured per-repo file counts: comfortably above cline/zed/
# claude-code (up to ~2,730 files, none flagged as slow) and well
# below spring-boot/tensorflow (9,942/14,285 files, the two repos
# where this was actually noticeable), so the note only fires where
# it's likely to matter.
_SEQUENTIAL_DISCLOSURE_THRESHOLD = 5000

# How often to re-check the rev-cache while another
# process is already building the old-side snapshot for the same SHA,
# and how long to wait before giving up and building an uncoordinated
# copy locally. Mirrors repo_ops._REGEN_LOCK_POLL_INTERVAL/
# _REGEN_LOCK_WAIT_CAP's own values for consistency. A flat 30s cap is
# a starting guess, not a measured one -- a rev-cache-miss build on a
# tensorflow-scale repo can itself run into the hundreds of seconds
# (see daemon._TIMEOUT_SECONDS_PER_TRACKED_FILE's own comment), so a
# losing process waiting only 30s before falling open to its own
# redundant multi-minute build may rarely help on the largest repos.
# Scaling this the way daemon._scaled_client_timeout_for_revcache_miss
# already scales the client's own request timeout is a reasonable
# fast-follow once real wait-time data exists; not done here to avoid
# guessing a scaling constant with no measurement behind it.
_REV_CACHE_LOCK_POLL_INTERVAL = repo_ops._REGEN_LOCK_POLL_INTERVAL
_REV_CACHE_LOCK_WAIT_CAP = repo_ops._REGEN_LOCK_WAIT_CAP


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
            resolution (``resolve``). This call used to always run
            both single-threaded regardless of ``dekko map --full``'s
            own ``--jobs`` fix — a separate, unparallelized code path
            that made a first-touch/cold-rev-cache ``diff``/
            ``affected``/``workset`` call minutes slower than it
            needed to be on a large repo. Callers pass an
            already-resolved concrete count (see
            ``repo_ops.resolve_workers``), not the raw ``--jobs`` CLI
            value (which allows ``0`` for "all cores").
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
    graph = resolve(files, workers=jobs, root=root)
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
    rev), the dominant cost of either command on a large repo.
    ``target_rev`` is resolved to its full commit SHA first; a
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
        with filelock.try_named_lock(
            root, f"rev-cache/{sha}.lock"
        ) as acquired:
            if not acquired:
                waited = _wait_for_other_rev_cache_build(root, sha)
                if waited is not None:
                    return waited
                # Wait cap hit without the other process's build
                # landing -- fail open, fall through to an
                # uncoordinated local build below (same fail-open
                # philosophy filelock.py's own docstring states
                # explicitly).
            return _build_and_cache_old_snapshot(
                root,
                target_rev,
                subpath,
                excludes,
                max_file_size,
                old_cache,
                sha,
                jobs=jobs,
            )
    return _build_and_cache_old_snapshot(
        root,
        target_rev,
        subpath,
        excludes,
        max_file_size,
        old_cache,
        sha,
        jobs=jobs,
    )


def _wait_for_other_rev_cache_build(root: Path, sha: str) -> Snapshot | None:
    """Poll for another process's in-flight old-snapshot build to land.

    Called after ``filelock.try_named_lock`` reports that a different
    process already holds the per-SHA rev-cache build lock -- rather
    than redundantly building the same old-side snapshot in parallel,
    wait a short bounded interval for that process's build to finish
    and land in the rev-cache.

    Args:
        root: Repository root another process is building an
            old-snapshot for.
        sha: Full commit SHA being built.

    Returns:
        The cached snapshot if it landed within the wait cap; ``None``
        if the cap was hit first (caller should fail open and build
        locally).
    """
    print(
        "note: another dekko process is already building the "
        f"old-side snapshot for rev {sha[:12]} -- waiting up to "
        f"{_REV_CACHE_LOCK_WAIT_CAP:.0f}s for it to finish",
        file=sys.stderr,
    )
    deadline = time.monotonic() + _REV_CACHE_LOCK_WAIT_CAP
    while time.monotonic() < deadline:
        time.sleep(_REV_CACHE_LOCK_POLL_INTERVAL)
        cached = revcache.load(root, sha)
        if cached is not None:
            return cached
    print(
        "note: gave up waiting for the other process's rev-cache "
        "build -- running an independent build now (the other "
        "process may still be in progress)",
        file=sys.stderr,
    )
    return None


def _build_and_cache_old_snapshot(
    root: Path,
    target_rev: str,
    subpath: str | None,
    excludes: tuple[str, ...],
    max_file_size: int,
    old_cache: cache_mod.IncrementalCache,
    sha: str | None,
    jobs: int = 1,
) -> Snapshot | None:
    """Export, re-parse, and (if resolvable) cache the old-side snapshot.

    The always-correct fallback path shared by every branch of
    :func:`old_snapshot`, whether or not per-SHA lock coordination
    applies (an unresolvable ``target_rev`` never has a SHA to lock
    on).
    """
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


def sequential_disclosure_message(
    tracked_count: int, *, workers: int
) -> str | None:
    """Build the "no rev-cache ... may take a while" note text.

    Factored out so the two places this note can fire -- the in-
    process warning below (daemon-side or direct-execution, always
    single-threaded by the time it's called) and the daemon client's
    pre-dispatch disclosure (``daemon.py::_timeout_and_args_for_
    command``, which knows *before sending the request* whether the
    ``--jobs 0`` override will apply) -- can't drift apart in wording.

    Args:
        tracked_count: Git-tracked file count at the target rev (see
            ``_maybe_warn_sequential``'s ``candidates`` docstring for
            why this is a ``git ls-tree`` count, not the mapped-file
            count).
        workers: The resolved worker count the resolve will run with:
            ``1`` is sequential, ``0`` means every core, any other
            value is an explicit ``--jobs N``. This used to be a
            bool that read every ``N > 1`` as "with all cores", which
            was true when the only parallel path *was* the daemon's
            all-cores override and stopped being true once ``--jobs
            N`` became a real choice on these commands.

    Returns:
        The note text (no trailing newline, not yet routed to
        stderr), or ``None`` when ``tracked_count`` is below
        ``_SEQUENTIAL_DISCLOSURE_THRESHOLD`` (small repos stay quiet).
    """
    if tracked_count < _SEQUENTIAL_DISCLOSURE_THRESHOLD:
        return None
    if workers != 1:
        cores = os.cpu_count() or 1
        with_what = (
            f"all {cores} cores"
            if workers <= 0 or workers >= cores
            else f"{workers} workers (of {cores} cores)"
        )
        return (
            f"note: no rev-cache for this commit; resolving "
            f"{tracked_count} git-tracked files with {with_what} may "
            f"take a while"
        )
    return (
        f"note: no rev-cache for this commit; single-threaded resolve "
        f"on {tracked_count} git-tracked files may take a while -- "
        f"pass --jobs 0 to use all cores"
    )


def _maybe_warn_sequential(jobs: int, candidates: list[str] | None) -> None:
    """Disclose a slow rev-cache-miss re-parse/resolve before it starts.

    ``diff``/``affected``/``workset`` now default to all cores,
    which makes the sequential case below an explicit
    ``--jobs 1`` choice. The note still fires for the parallel path,
    with its own wording: a cold tensorflow snapshot is a four-minute
    silence even with every core busy. The name predates that.

    At the old default ``--jobs 1``, a first-touch
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
    if candidates is None:
        return
    # `candidates` is `git ls-tree`'s full
    # tracked-file count at the target rev -- before `walker.discover`
    # excludes vendored/no-parser/too-large files -- so it can read
    # much larger than the repo's actual mapped file count (36,518
    # tracked vs. 14,285 mapped on tensorflow) and mislead a reader
    # into thinking the wait scales with the mapped set. Naming it
    # "git-tracked" makes that distinction explicit instead of
    # implying it's the same count `dekko map`'s own summary reports.
    message = sequential_disclosure_message(len(candidates), workers=jobs)
    if message is None:
        return
    print(message, file=sys.stderr)


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


# A corrupted rev-cache entry (every old-side symbol
# hashed to an empty body, see revcache._is_all_empty_body) makes
# every shared symbol report as "changed" with nothing added or
# removed -- exactly the tensorflow repro (171706 changed, 0 added, 0
# removed). Layer 1 (revcache.save's guard) stops *new* corrupted
# entries from being written, but an already-corrupted entry from
# before that fix (or one hand-placed, or the cache-side guard
# somehow bypassed) is still served as-is by design (a rev-cache hit
# needs no freshness check). This is the loud, consumer-side backstop:
# it fires regardless of *why* the old side looks this way, so it
# still helps someone carrying a pre-existing corrupted entry.
# Mirrors _SEQUENTIAL_DISCLOSURE_THRESHOLD's own "only fire where it's
# likely to matter" sizing convention -- avoids noise on tiny repos
# where "everything changed" is unremarkable.
_SUSPICIOUS_CHANGE_RATIO_MIN_SYMBOLS = 500


def _warn_if_suspicious(
    old: Snapshot, new: Snapshot, result: DiffResult
) -> None:
    """Warn on the known corrupted-rev-cache "everything changed" shape.

    Args:
        old: Old-side snapshot.
        new: New-side snapshot.
        result: The just-computed diff result to inspect.
    """
    common = set(old.symbols) & set(new.symbols)
    if (
        not result.added
        and not result.removed
        and len(common) >= _SUSPICIOUS_CHANGE_RATIO_MIN_SYMBOLS
        and len(result.changed) == len(common)
    ):
        print(
            "note: every symbol shared between both sides reports as "
            "changed, with none added or removed -- this can mean a "
            "real repo-wide rewrite, but also matches a known "
            "corrupted-rev-cache signature; if this is unexpected, "
            "retry with --no-daemon --jobs 0, or delete "
            f".dekko/rev-cache/{result.rev[:12]}*.json and re-run",
            file=sys.stderr,
        )


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
    _warn_if_suspicious(old, new, result)
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
