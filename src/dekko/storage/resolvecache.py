"""Per-file call-resolution cache stored under ``.dekko/``.

``resolve()`` is a pure function of the *whole* file list and used to run
unconditionally on every ``dekko map``, so an incremental run saved only
the tree-sitter extraction step. On a large repo that made "incremental"
nearly worthless: one edited file out of 9,942 measured as costing 93%
of a full rebuild, with repo-wide resolution the floor.

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
    cargo_fingerprint,
    cpp_scope_names,
    name_delta,
    reexport_closure,
    reexport_delta_names,
    resolve_fingerprint,
    resolved_id_name,
    rust_renames,
    symbol_projection,
    tsconfig_fingerprint,
    workspace_fingerprint,
)
from dekko.core.languages import spec_fingerprint
from dekko.core.model import FileMap, Import, RawHeritage
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


def load(
    root: Path, config_root: Path | None = None
) -> dict[str, dict] | None:
    """Load the prior run's per-file resolution, if it is still usable.

    Discards the cache outright when anything that could change what
    resolution *produces* has moved: the cache format, the dekko version,
    the extraction spec (different symbols in, different edges out),
    the resolver's own source (``resolve_fingerprint`` -- the extraction
    spec hash says nothing about the resolution ladder), the JS/TS
    workspace package table (``workspace_fingerprint`` -- a renamed
    ``package.json`` or edited ``workspaces`` glob changes which imports
    count as in-repo without touching a single source file, so neither
    ``build_reuse``'s path-set check nor its symbol check would see it),
    the tsconfig path-alias tables (``tsconfig_fingerprint`` -- an
    edited ``paths`` entry changes which file an aliased import names),
    or the Rust crate list (``cargo_fingerprint`` -- a crate added to a
    ``Cargo.toml`` turns its ``use``s from external to in-repo).

    Args:
        root: Repository root whose ``.dekko/`` holds the cache.
        config_root: The tree about to be resolved, whose config
            digests must match the cached run's. Defaults to ``root``;
            differs for a ``diff``'s old side, an exported rev that
            reuses the working tree's cache.

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
    if not _config_inputs_match(doc, config_root or root):
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


def _config_inputs_match(doc: dict, root: Path) -> bool:
    """Whether the cache's repo config digests match the repo's now.

    Args:
        doc: The loaded cache document.
        root: Repository root.

    Returns:
        True when the workspace, tsconfig and Cargo digests all agree.
    """
    return (
        doc.get("workspace_hash", "") == workspace_fingerprint(root)
        and doc.get("tsconfig_hash", "") == tsconfig_fingerprint(root)
        and doc.get("cargo_hash", "") == cargo_fingerprint(root)
    )


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
        "tsconfig_hash": tsconfig_fingerprint(root),
        "cargo_hash": cargo_fingerprint(root),
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
    files: list[FileMap],
    dirty: set[str],
    added_names: set[str],
    changed_names: set[str] | frozenset[str] = frozenset(),
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

    An *aliased* binding (``import { real as alias }``) is also at
    risk when ``real`` merely changed: its call sites are written
    ``alias(..)``, and when they resolve through the file the import
    names (``resolver._OriginLookup``), a ``real`` appearing in or
    leaving that file flips them while the cached entry names only
    ``alias`` or whatever it resolved to before.

    Args:
        files: Every mapped file (fresh, current ``fm.imports`` --
            unaffected by the reuse gate, since imports are always
            re-extracted for every file every run).
        dirty: Paths already known dirty -- skipped.
        added_names: ``NameDelta.newly_defined`` names to test against
            -- only a name with *no* prior candidates can flip an
            alias miss to a hit.
        changed_names: ``NameDelta.changed`` names, tested against
            aliased bindings only.

    Returns:
        Additional paths (disjoint from ``dirty``) whose cached entry
        must be discarded.
    """
    found: set[str] = set()
    for fm in files:
        if fm.path in dirty:
            continue
        for imp in fm.imports:
            original = alias_original_name(imp.source)
            if original in added_names or (
                original != imp.name and original in changed_names
            ):
                found.add(fm.path)
                break
    return found


_JS_LANGUAGES = frozenset({"javascript", "typescript", "tsx"})


def _files_using(
    files: list[FileMap], dirty: set[str], names: set[str]
) -> set[str]:
    """Clean JS/TS files that call or import one of ``names``.

    ``_files_naming`` reads what a cached call resolved *to*. A call
    that goes through a re-export can resolve to a symbol with another
    name entirely (``Text`` to ``ThemedText``), so when what a name
    leads to may have changed, the files are found by the name as
    they wrote it: at a call site, or in an import (whose members,
    ``Ns.name(..)``, are written under yet other names).

    Args:
        files: Every mapped file.
        dirty: Paths already known dirty -- skipped.
        names: The changed names, closed under renames (see
            ``resolver.reexport_closure``).

    Returns:
        Additional paths (disjoint from ``dirty``) whose cached entry
        must be discarded.
    """
    found: set[str] = set()
    for fm in files:
        if fm.path in dirty or fm.language not in _JS_LANGUAGES:
            continue
        if any(call.name in names for call in fm.calls) or any(
            imp.name in names or alias_original_name(imp.source) in names
            for imp in fm.imports
        ):
            found.add(fm.path)
    return found


def _rust_files_using(
    files: list[FileMap], dirty: set[str], names: set[str]
) -> set[str]:
    """Clean Rust files that write one of ``names`` as a type or import.

    Whether ``Name::new(..)`` can reach a repo symbol depends on
    whether some ``use .. as Name`` exists anywhere in the repo
    (``resolver._rust_unknown_type_path``) and what it renames
    (``resolver._rust_rename_target``), and the call names ``new``,
    not ``Name``. So the files are found by the receiver they wrote.

    Args:
        files: Every mapped file.
        dirty: Paths already known dirty -- skipped.
        names: The names whose renaming ``use`` was gained or lost.

    Returns:
        Additional paths (disjoint from ``dirty``) whose cached entry
        must be discarded.
    """
    found: set[str] = set()
    for fm in files:
        if fm.path in dirty or not fm.path.endswith(".rs"):
            continue
        receivers = {
            segment
            for call in fm.calls
            for segment in (call.receiver or "").split("::")
        }
        if receivers & names or any(imp.name in names for imp in fm.imports):
            found.add(fm.path)
    return found


def _rust_renamed_delta(
    fm: FileMap, old_imports: list[dict] | None
) -> set[str]:
    """Names one edited Rust file's renaming ``use``s gained or lost.

    Args:
        fm: The file as extracted now.
        old_imports: Its cached import bindings, as dicts.

    Returns:
        The local names some ``use .. as Name`` bound before or binds
        now, or binds to another original name now. Empty for a
        non-Rust file.
    """
    if not fm.path.endswith(".rs"):
        return set()
    before = FileMap(
        path=fm.path,
        language=fm.language,
        imports=[
            Import(path=fm.path, name=d["name"], source=d["source"])
            for d in old_imports or ()
        ],
    )
    changed = rust_renames([before]) ^ rust_renames([fm])
    return {name for name, _original in changed}


def _rust_rename_dirty(
    files: list[FileMap], cache: IncrementalCache, dirty: set[str]
) -> set[str]:
    """Clean Rust files a dirty file's renaming ``use`` edit invalidates.

    Args:
        files: Every mapped file.
        cache: This run's extraction cache.
        dirty: Paths already known dirty.

    Returns:
        Additional paths (disjoint from ``dirty``), see
        ``_rust_files_using``.
    """
    renamed: set[str] = set()
    for fm in files:
        if fm.path in dirty:
            renamed |= _rust_renamed_delta(fm, cache.old_imports(fm.path))
    if not renamed:
        return set()

    return _rust_files_using(files, dirty, renamed)


def _python_bound_delta(
    fm: FileMap,
    old_symbols: list[dict] | None,
    old_imports: list[dict] | None,
) -> set[str]:
    """Names one edited Python file gained or lost, as defined or
    imported (``resolver._PythonModules.names``).

    Args:
        fm: The file as extracted now.
        old_symbols: Its cached symbols, as dicts.
        old_imports: Its cached import bindings, as dicts.

    Returns:
        The names, with ``*`` for a star import gained or lost. Empty
        for a non-Python file.
    """
    if not fm.path.endswith((".py", ".pyi")):
        return set()
    before = {d["name"] for d in old_symbols or ()}
    before |= {d["name"] for d in old_imports or ()}
    now = {s.name for s in fm.symbols} | {i.name for i in fm.imports}

    return before ^ now


def _python_binding_dirty(
    files: list[FileMap], cache: IncrementalCache, dirty: set[str]
) -> set[str]:
    """Clean Python files whose import a dirty file's edit can flip.

    Whether ``from pkg import gen_ops`` binds anything in the repo
    (``resolver._PythonModules.dangling``) depends on what ``pkg``
    defines and imports, and an import binding is part of no symbol,
    so the name delta can't see it. Over-invalidates on purpose: any
    import whose last name was gained or lost anywhere, and every
    Python file with an import when a star import came or went.

    Args:
        files: Every mapped file.
        cache: This run's extraction cache.
        dirty: Paths already known dirty.

    Returns:
        Additional paths (disjoint from ``dirty``).
    """
    names: set[str] = set()
    for fm in files:
        if fm.path in dirty:
            names |= _python_bound_delta(
                fm, cache.old_symbols(fm.path), cache.old_imports(fm.path)
            )
    if not names:
        return set()

    star = "*" in names
    found: set[str] = set()
    for fm in files:
        if fm.path in dirty or not fm.path.endswith((".py", ".pyi")):
            continue
        if any(
            star or alias_original_name(imp.source) in names
            for imp in fm.imports
        ):
            found.add(fm.path)

    return found


def _cpp_decl_names(entries: set[str]) -> set[str]:
    """Bare names of ``FileMap.cpp_decls`` entries.

    Args:
        entries: ``"<qualname>/<count>=<defaults>"`` entries.

    Returns:
        The last qualname segment of each.
    """
    names: set[str] = set()
    for entry in entries:
        qualname = entry.rpartition("/")[0]
        names.add(qualname.replace("::", ".").rsplit(".", 1)[-1])
    return names


def _extended_names(clauses: list[RawHeritage] | list[dict]) -> set[str]:
    """Bare names of the types that carry an ``extends`` clause.

    Whether a class extends anything decides how a construction of it
    is read (``resolver._constructed_by_count``), and the clause is not
    part of the class's own symbol.

    Args:
        clauses: One file's heritage clauses, as ``RawHeritage``
            objects or the plain dicts the extraction cache stores.

    Returns:
        The subtype names, empty for a file with no such clause.
    """
    names: set[str] = set()
    for clause in clauses:
        d = clause if isinstance(clause, dict) else vars(clause)
        if d.get("subtype_id") and d.get("relation") == "extends":
            names.add(resolved_id_name(d["subtype_id"]))
    return names


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
        ``resolver.NameDelta.blocks_reuse`` -- or changes its C/C++
        ``using``-declarations or qualname scope names, or gains or
        loses a JS/TS star re-export, and the caller must fall back to
        a full resolve instead.
    """
    changed: set[str] = set()
    newly_defined: set[str] = set()
    for fm in files:
        if fm.path not in dirty:
            continue
        old = cache.old_symbols(fm.path)
        if old is None:
            return None
        if cache.old_cpp_using(fm.path) != fm.cpp_using or cpp_scope_names(
            old
        ) != cpp_scope_names(fm.symbols):
            # Repo-wide inputs of a C/C++ ``ns::Name`` call that no
            # name delta can see; see resolver._namespace_head_match.
            return None
        # A prototype's declared arity only changes what calls named
        # like it resolve to, wherever its definition lives.
        changed |= _cpp_decl_names(
            set(cache.old_cpp_decls(fm.path) or []) ^ set(fm.cpp_decls)
        )
        # So does an ``extends`` clause gained or lost: it changes how
        # a construction of that class resolves from any file.
        changed |= _extended_names(
            cache.old_heritage(fm.path) or []
        ) ^ _extended_names(fm.heritage)
        # And a JS/TS re-export or import binding gained, lost or
        # re-pointed: it changes where a name leads from any file that
        # imports it through this one.
        passed_on = reexport_delta_names(
            fm, cache.old_reexports(fm.path), cache.old_imports(fm.path)
        )
        if passed_on is None:
            return None
        changed |= passed_on
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
        reach = reexport_closure(files, changed)
        extra |= _files_naming(cached, dirty, reach)
        extra |= _files_using(files, dirty, reach)
    if changed or newly_defined:
        extra |= _files_importing(files, dirty, newly_defined, changed)
    # A Rust ``use .. as Name`` gained or lost decides whether
    # ``Name::f(..)`` can reach a repo symbol from any file.
    extra |= _rust_rename_dirty(files, cache, dirty)
    # A Python module gaining or losing a name decides whether another
    # file's import of that name is dangling.
    extra |= _python_binding_dirty(files, cache, dirty)

    return extra


def _torn(
    cached: dict[str, dict], cache: IncrementalCache, dirty: set[str]
) -> bool:
    """Whether the two cache files came from different map runs.

    The plan diffs each dirty file's names against the extraction
    cache's old entry while trusting the resolve cache's edges, which
    is only sound when both describe the same content. ``run_map``
    saves them one after the other, so a reader that doesn't hold the
    regen lock (``diff``, ``affected``) can load one from before a
    concurrent run and one from after it. After any normal run the two
    hashes agree for every file, so this never costs a reuse.

    Args:
        cached: The resolve cache's per-file entries.
        cache: This run's extraction cache, over the prior entries.
        dirty: Files whose content changed since the resolve cache.

    Returns:
        ``True`` when a dirty file's two recorded hashes differ.
    """
    for path in dirty:
        old = cache.old_hash(path)
        if old is not None and old != cached[path].get("hash"):
            return True

    return False


def build_reuse(
    root: Path,
    files: list[FileMap],
    cache: IncrementalCache,
    cache_root: Path | None = None,
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
      actually answer differently for, including the two gaps the v1
      design didn't anticipate (constructor-collapse, import-alias
      recovery) that this closes.
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

    None of these checks asks whether ``files`` is the tree the cache
    was written for, only how it differs from it. So a different tree,
    such as a ``diff``'s exported old rev, can reuse the working tree's
    cache through ``cache_root``: near the map's own commit it differs
    in a few files at most.

    Args:
        root: Root of the tree being resolved. Its config digests are
            the ones checked against the cached run.
        files: Every mapped file, freshly discovered this run.
        cache: This run's extraction cache, already populated by
            ``map_repository``.
        cache_root: Repository root whose ``.dekko/`` holds the resolve
            cache. Defaults to ``root``.

    Returns:
        A ``ResolveReuse``, or ``None`` to resolve everything.
    """
    cached = load(cache_root or root, config_root=root)
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

    if _torn(cached, cache, dirty):
        return None

    extra = _name_delta_dirty(files, cached, cache, dirty)
    if extra is None:
        return None
    dirty |= extra

    return ResolveReuse(cached=cached, dirty=frozenset(dirty))
