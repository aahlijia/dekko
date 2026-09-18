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
*identical* to the cached run (``build_reuse``), never on a guess about
which names an edit could have affected. When the gate fails, the whole
repo is re-resolved exactly as before.

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
    resolve_fingerprint,
    symbol_projection,
)
from dekko.core.languages import spec_fingerprint
from dekko.core.model import FileMap
from dekko.render.mapfile import _json_dumps, _json_loads, atomic_write_bytes
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
    the extraction spec (different symbols in, different edges out), or
    the resolver's own source (``resolve_fingerprint`` -- the extraction
    spec hash says nothing about the resolution ladder).

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


def build_reuse(
    root: Path, files: list[FileMap], cache: IncrementalCache
) -> ResolveReuse | None:
    """Decide what cached resolution this run may reuse.

    Returns a reuse plan only when the global resolution inputs are
    provably identical to the cached run, which makes an unchanged file's
    resolution identical *by construction*:

    - **No file added, deleted, or renamed.** The ladder consults several
      structures derived from the set of file paths, not from file
      contents (``repo_stems``, ``crate_roots``, ``_module_matches``, the
      tsconfig alias tables). A path-set change can therefore alter
      resolution for a file that references no changed *name*, which no
      name-based invalidation would catch. Requiring an identical path
      set removes that whole class of question.
    - **No changed file altered its resolution-relevant symbols**
      (``resolver.symbol_projection``). If every symbol a file exposes is
      unchanged, the repo-wide name index every other file resolves
      against is unchanged too.

    When either fails, returns ``None`` and the caller re-resolves the
    whole repo. That is the v1 trade: a provable gate that covers the
    dominant agent-loop edit (change a body, add a call, fix a literal --
    none of which alter the symbol set), rather than a narrower
    name-delta analysis whose correctness rests on case analysis.

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

    # Every dirty file must leave the repo-wide symbol picture untouched,
    # or unchanged files' cached resolution can't be trusted.
    for fm in files:
        if fm.path not in dirty:
            continue
        old = cache.old_symbols(fm.path)
        if old is None:
            return None
        if symbol_projection(old) != symbol_projection(fm.symbols):
            return None

    return ResolveReuse(cached=cached, dirty=frozenset(dirty))
