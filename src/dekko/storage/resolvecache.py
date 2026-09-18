"""Per-file call-resolution cache stored under ``.dekko/``.

``resolve()`` is a pure function of the *whole* file list and used to run
unconditionally on every ``dekko map``, so an incremental run saved only
the tree-sitter extraction step. On a large repo that made "incremental"
nearly worthless: round 30 measured one edited file out of 9,942 costing
93% of a full rebuild, with repo-wide resolution the floor. See
``.features/fixes/round30/01-incremental-resolution.md``.

This caches the *call* pass (``_resolve_all``) per owning file, so an
edit only re-resolves the files that changed. Two properties already in
the tree make it sound rather than heuristic:

1. A call's ``caller_id`` always belongs to the file the call was
   extracted from, so resolution partitions cleanly by owning file --
   the same invariant ``_resolve_all``'s parallel chunking relies on.
2. Symbol ids are line-independent (``relpath::Qualname``), so editing a
   function body cannot dangle a cached edge pointing into that file.

Reuse is gated on the global resolution inputs being provably
*identical* to the cached run for the files it decides to trust
(``build_reuse``) -- never on a heuristic guess. When a dirty file's own
symbol set is unchanged (the dominant agent-loop edit: a body edit, a
new call, a literal fix), every other file's cached resolution is
provably still correct outright. When it *did* change, v2's name-delta
analysis (``resolver.name_delta``) identifies exactly which bare names
changed meaning and re-resolves only the files that could depend on one
of them -- see ``build_reuse``'s docstring for the full rule, including
the two cases (a type-kind name, or no prior symbols to diff against)
that still fall back to re-resolving the whole repo.

Only the call pass is cached. Refs/heritage/imports/throws/catches are
always recomputed: together they are a small share of the cost, and
caching them would drag in path-set and tsconfig invalidation questions
the call pass doesn't have.

Stored gzipped at ``<root>/.dekko/resolved-calls.json.gz``. The name
deliberately avoids the substring ``cache.json``, which an existing
atomic-write test reasonably reads as a temp-file leftover of the
extraction cache.

Both the interning (``_intern``) and the compression are load-bearing,
not tidiness. The payload is overwhelmingly the same long symbol ids
repeated once per edge; stored verbatim, spring-boot measured 511 MB raw
/ 51 MB gzipped, and just serializing that cost more than the call
resolution it was meant to skip. Interning plus gzip level 1 brings the
same data to **4.9 MB** — about 2% of that repo's extraction cache.

A separate file from ``cache.json`` so a ``--full`` run, which never
consults it, pays nothing to parse it.
"""

import gzip
from pathlib import Path

from dekko.core.resolver import (
    ResolveReuse,
    alias_original_name,
    name_delta,
    resolve_fingerprint,
    resolved_id_name,
    symbol_projection,
    workspace_fingerprint,
)
from dekko.core.languages import spec_fingerprint
from dekko.core.model import FileMap
from dekko.render.mapfile import (
    _callee_base,
    _json_dumps,
    _json_loads,
    atomic_write_bytes,
)
from dekko.storage.cache import (
    CACHE_DIR,
    IncrementalCache,
    _tool_version,
    ensure_dir,
)

RESOLVE_CACHE_VERSION = 1
RESOLVE_CACHE_FILE = "resolved-calls.json.gz"

# Compression level. 1 rather than the default 9: measured on tensorflow,
# level 1 gives 16.6 MB in 0.4s and level 6 gives 12.9 MB in 1.7s. Saving
# 3.7 MB is not worth 1.3s on a path whose whole purpose is being fast.
_GZIP_LEVEL = 1


def _path(root: Path) -> Path:
    """Location of the resolve cache for ``root``."""
    return root / CACHE_DIR / RESOLVE_CACHE_FILE


def _intern(
    partitioned: dict[str, dict], cache: IncrementalCache
) -> tuple[list[str], dict[str, dict]]:
    """Rewrite per-file resolution with every string interned to an int.

    This is load-bearing, not a micro-optimization. Stored verbatim, the
    payload is the same long ``relpath::Qualname`` ids repeated once per
    edge: spring-boot measured **511 MB raw / 51 MB gzipped**, and
    serializing that cost 2.88s to write plus 1.35s to read — more than
    the 4.10s of call resolution the cache exists to skip, making the
    whole feature net-negative. Interning collapses the repetition, since
    each id then appears once in the table and as a small integer
    everywhere else.

    Keys are single letters for the same reason: at hundreds of thousands
    of entries, the key strings themselves are a measurable share.

    Args:
        partitioned: ``resolver.partition_resolution`` output.
        cache: The run's extraction cache, for per-file content hashes.

    Returns:
        ``(id_table, files)`` — every int in ``files`` indexes
        ``id_table``.
    """
    ids: dict[str, int] = {}

    def idx(text: str) -> int:
        got = ids.get(text)
        if got is None:
            got = len(ids)
            ids[text] = got
        return got

    files: dict[str, dict] = {}
    for path, entry in partitioned.items():
        known = cache.entries.get(path)
        if known is None:
            # No content hash to key on, so this entry could never be
            # validated on read. Omitting it just means re-resolving
            # that file next run.
            continue
        files[path] = {
            "h": known["hash"],
            "e": [[idx(c), idx(t), lines] for c, t, lines in entry["edges"]],
            "a": [
                [idx(c), idx(n), [idx(x) for x in cands]]
                for c, n, cands in entry["ambiguous"]
            ],
            "x": [
                [idx(c), idx(t), lines] for c, t, lines in entry["external"]
            ],
        }
    table = [""] * len(ids)
    for text, i in ids.items():
        table[i] = text
    return table, files


def _expand(table: list[str], files: dict[str, dict]) -> dict[str, dict]:
    """Inverse of ``_intern``: ints back to the strings they index.

    Every occurrence of an id becomes a reference to the *same* string
    object from ``table``, so expanding costs one pointer per occurrence
    rather than re-materializing the text.
    """
    out: dict[str, dict] = {}
    for path, entry in files.items():
        out[path] = {
            "hash": entry["h"],
            "edges": [
                [table[c], table[t], lines] for c, t, lines in entry["e"]
            ],
            "ambiguous": [
                [table[c], table[n], [table[x] for x in cands]]
                for c, n, cands in entry["a"]
            ],
            "external": [
                [table[c], table[t], lines] for c, t, lines in entry["x"]
            ],
        }
    return out


def load(root: Path) -> dict[str, dict] | None:
    """Load the prior run's per-file resolution, if it is still usable.

    Discards the cache outright when anything that could change what
    resolution *produces* has moved: the cache format, the dekko version,
    the extraction spec (different symbols in, different edges out),
    the resolver's own source (``resolve_fingerprint`` -- the extraction
    spec hash says nothing about the resolution ladder), or the JS/TS
    workspace package table (``workspace_fingerprint`` -- a renamed
    ``package.json`` or edited ``workspaces`` glob changes which imports
    count as in-repo without touching a single source file, so neither
    ``build_reuse``'s path-set check nor its symbol check would see it).

    Args:
        root: Repository root.

    Returns:
        ``path -> {"hash", "edges", "ambiguous", "external"}``, or
        ``None`` when there is no usable cache.
    """
    try:
        raw = gzip.decompress(_path(root).read_bytes())
        doc = _json_loads(raw)
    except (OSError, ValueError, EOFError, gzip.BadGzipFile):
        return None
    if doc.get("version") != RESOLVE_CACHE_VERSION:
        return None
    if doc.get("tool_version") != _tool_version():
        return None
    if doc.get("spec_hash") != spec_fingerprint():
        return None
    if doc.get("resolve_hash") != resolve_fingerprint():
        return None
    if doc.get("workspace_hash", "") != workspace_fingerprint(root):
        return None
    files = doc.get("files")
    table = doc.get("ids")
    if not isinstance(files, dict) or not isinstance(table, list):
        return None
    try:
        return _expand(table, files)
    except (IndexError, KeyError, TypeError, ValueError):
        # A truncated or hand-edited cache is a cache miss, not a crash.
        return None


def save(
    root: Path, partitioned: dict[str, dict], cache: IncrementalCache
) -> None:
    """Persist per-file resolution for the next run.

    Must be called after *every* successful map run, including runs whose
    reuse gate missed — otherwise a single miss leaves no cache behind and
    the next run misses too, permanently.

    Args:
        root: Repository root.
        partitioned: ``resolver.partition_resolution`` output.
        cache: The run's extraction cache, whose per-file content hashes
            are stored alongside each entry so the two files can be
            validated against each other without a separate protocol.
    """
    table, files = _intern(partitioned, cache)
    doc = {
        "version": RESOLVE_CACHE_VERSION,
        "tool_version": _tool_version(),
        "spec_hash": spec_fingerprint(),
        "resolve_hash": resolve_fingerprint(),
        "workspace_hash": workspace_fingerprint(root),
        "ids": table,
        "files": files,
    }
    ensure_dir(root)
    atomic_write_bytes(
        _path(root),
        gzip.compress(_json_dumps(doc), compresslevel=_GZIP_LEVEL),
    )


def clear(root: Path) -> None:
    """Remove the resolve cache, ignoring a missing file."""
    _path(root).unlink(missing_ok=True)


def _entry_names(entry: dict) -> set[str]:
    """Every bare name one cached file's call resolution depends on.

    Mirrors the three buckets ``partition_resolution`` writes:

    - ``edges``: the *resolved* callee's own bare name, recovered from
      its id (``resolver.resolved_id_name``). Correct even when the
      call reached that target via ``_alias_candidates`` -- the callee
      id is always the real target's id, never the local alias text.
    - ``ambiguous``: both the stored call name (the common case, where
      it already equals every candidate's own name) **and** each
      candidate id's own recovered name -- the second is what makes an
      aliased ambiguous call track its *real* dependency rather than
      the alias text, since an alias's candidates can have a different
      bare name than ``call.name``.
    - ``external``: the base identifier of the raw callee text, the
      same split ``render.mapfile``'s ``externals_by_name`` index uses
      (``_callee_base``) -- so a newly-defined symbol that would now
      resolve a previously-unresolved *direct* (non-aliased) call is
      still caught by name.

    What this does **not** catch: an external entry that came from an
    aliased import whose target didn't exist yet
    (``_alias_candidates`` found zero candidates for the recovered
    name). Nothing in the cached entry records that recovered name --
    only ``call.text``, which reflects the alias, not the target. See
    ``_files_importing`` for how that residual gap is closed instead.
    """
    names: set[str] = set()
    for _caller, callee, _lines in entry.get("edges", ()):
        names.add(resolved_id_name(callee))
    for _caller, name, cands in entry.get("ambiguous", ()):
        names.add(name)
        names.update(resolved_id_name(c) for c in cands)
    for _caller, text, _lines in entry.get("external", ()):
        names.add(_callee_base(text))
    names.discard("")
    return names


def _files_naming(
    cached: dict[str, dict], dirty: set[str], names: set[str]
) -> set[str]:
    """Clean files whose cached resolution names a delta symbol.

    A single linear pass over every cached entry -- equivalent to
    building the "name -> {paths}" index the design describes and then
    looking up each delta name in it, just without materializing the
    whole reverse index when ``names`` (typically a handful of symbols)
    is far smaller than the repo's full name vocabulary.

    Args:
        cached: This run's loaded resolve cache (``load()``'s output).
        dirty: Paths already known dirty -- skipped, since they're
            re-resolved from scratch regardless.
        names: ``NameDelta.changed`` names to test against.

    Returns:
        Additional paths (disjoint from ``dirty``) whose cached entry
        must be discarded.
    """
    found: set[str] = set()
    for path, entry in cached.items():
        if path in dirty:
            continue
        if _entry_names(entry) & names:
            found.add(path)
    return found


def _files_importing(
    files: list[FileMap], dirty: set[str], added_names: set[str]
) -> set[str]:
    """Clean files whose own import could newly resolve via an alias.

    Closes the one gap ``_files_naming`` can't reach (see
    ``_entry_names``): an import-alias miss recorded in ``external``
    carries no trace of the name ``_alias_candidates`` actually looked
    up (``resolver.alias_original_name``), only the alias text as
    written at the call site. A file is at risk here regardless of
    whether it currently has a cached miss for that particular import --
    checking every import directly, rather than trying to first prove
    one produced a miss, is the cheap side of "over-invalidate, never
    under."

    Args:
        files: Every mapped file (fresh, current ``fm.imports`` --
            unaffected by the reuse gate, since imports are always
            re-extracted for every file every run).
        dirty: Paths already known dirty -- skipped.
        added_names: ``NameDelta.newly_defined`` names to test against
            -- only a name with *no* prior candidates can flip an
            alias miss to a hit.

    Returns:
        Additional paths (disjoint from ``dirty``) whose cached entry
        must be discarded.
    """
    found: set[str] = set()
    for fm in files:
        if fm.path in dirty:
            continue
        for imp in fm.imports:
            if alias_original_name(imp.source) in added_names:
                found.add(fm.path)
                break
    return found


def _name_delta_dirty(
    files: list[FileMap],
    cached: dict[str, dict],
    cache: IncrementalCache,
    dirty: set[str],
) -> set[str] | None:
    """Every clean file the dirty set's symbol-name changes invalidate.

    Args:
        files: Every mapped file, freshly discovered this run.
        cached: This run's loaded resolve cache.
        cache: This run's extraction cache.
        dirty: Paths already known dirty from the content-hash check.

    Returns:
        Additional paths to fold into ``dirty``, or ``None`` when any
        dirty file's delta includes a type-kind name -- see
        ``resolver.NameDelta.blocks_reuse`` -- and the caller must fall
        back to a full resolve instead.
    """
    changed: set[str] = set()
    newly_defined: set[str] = set()
    for fm in files:
        if fm.path not in dirty:
            continue
        old = cache.old_symbols(fm.path)
        if old is None:
            return None
        # Whole-file compare first: cheaper than the grouped analysis
        # below, and this is the dominant agent-loop edit (a body edit,
        # a new call, a literal fix -- none of which touch any symbol's
        # projection), so most dirty files skip name_delta entirely.
        if symbol_projection(old) == symbol_projection(fm.symbols):
            continue
        delta = name_delta(old, fm.symbols)
        if delta.blocks_reuse:
            return None
        changed |= delta.changed
        newly_defined |= delta.newly_defined

    extra: set[str] = set()
    if changed:
        extra |= _files_naming(cached, dirty, changed)
    if newly_defined:
        extra |= _files_importing(files, dirty, newly_defined)
    return extra


def build_reuse(
    root: Path, files: list[FileMap], cache: IncrementalCache
) -> ResolveReuse | None:
    """Decide what cached resolution this run may reuse.

    Returns a reuse plan only when the global resolution inputs are
    provably identical to the cached run for the files it marks
    reusable, which makes an unchanged file's resolution identical *by
    construction*:

    - **No file added, deleted, or renamed.** The ladder consults several
      structures derived from the set of file paths, not from file
      contents (``repo_stems``, ``crate_roots``, ``_module_matches``, the
      tsconfig alias tables). A path-set change can therefore alter
      resolution for a file that references no changed *name*, which no
      name-based invalidation would catch. Requiring an identical path
      set removes that whole class of question.
    - **Every name a dirty file's delta touches is propagated to every
      file that could depend on it** (``resolver.name_delta``, plus
      ``_files_naming``/``_files_importing`` here). Unlike v1's blanket
      "any symbol-set change forces a full resolve," this narrows the
      re-resolve to exactly the files ``_pick_candidate``'s ladder could
      actually answer differently for -- see WP-B's dependency-class
      audit in ``test-repos/reports/31-tokentest-7repo-post04355/
      FIX-PLAN-remaining.md`` for the full case analysis, including the
      two gaps the v1 design didn't anticipate (constructor-collapse,
      import-alias recovery) that this closes.
    - A dirty file whose delta includes a **type-kind** name (a class,
      struct, interface, enum, trait, or type alias) forces a full
      resolve outright, because the type-aware ladder steps
      (``_receiver_type_match`` and friends) read the whole repo-wide
      index for a type name regardless of which file wrote the call --
      there is no bounded set of "files that could be affected" to
      compute for that case, so the whole gate degrades to v1's original
      behavior for it.

    When the file-set check fails, returns ``None`` outright. When a
    dirty file can't be diffed (no prior symbols) or its delta blocks
    reuse, also returns ``None`` -- the caller re-resolves the whole
    repo. Otherwise every file not proven safe by the checks above is
    folded into ``dirty``.

    Args:
        root: Repository root.
        files: Every mapped file, freshly discovered this run.
        cache: This run's extraction cache, already populated by
            ``map_repository``.

    Returns:
        A ``ResolveReuse``, or ``None`` to resolve everything.
    """
    cached = load(root)
    if cached is None:
        return None

    if {fm.path for fm in files} != set(cached):
        return None

    dirty: set[str] = set()
    for fm in files:
        known = cache.entries.get(fm.path)
        if known is None:
            return None
        if cached[fm.path].get("hash") != known["hash"]:
            dirty.add(fm.path)

    extra = _name_delta_dirty(files, cached, cache, dirty)
    if extra is None:
        return None
    dirty |= extra

    return ResolveReuse(cached=cached, dirty=frozenset(dirty))
