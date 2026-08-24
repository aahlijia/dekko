"""Shared repo-mapping pipeline: discover, extract, resolve, render.

Extracted out of ``integrations/cli.py`` (see the sc:analyze
post-0.31.1 fixes plan, item 2) as a fourth top-level "shared kernel"
module alongside ``classify.py``/``textutil.py``/``source.py`` --
this is the one contiguous "discover, extract, resolve, render, and
persist a repo map" pipeline that used to live inside ``cli.py``
(``map_repository``, ``load_or_regen``,
``load_current_index_no_regen``, and everything ``run_map`` calls).
``integrations/cli.py`` keeps the argparse-Namespace-in/exit-code-out
CLI adapters (``run_map`` dispatch, argument parsing) and now calls
into this module; ``analysis/affected.py``, ``analysis/workset.py``,
``analysis/diff.py``, and ``daemon/daemon.py`` import this module
directly at the top level instead of doing a function-local
``from dekko.integrations import cli`` -- this module imports none of
``analysis/``, ``daemon/``, or ``integrations/``, so no cycle exists.
"""

import argparse
import os
import sys
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from importlib.metadata import version as _pkg_version
from pathlib import Path

from dekko.storage import cache as cache_mod
from dekko import classify
from dekko.core import grammars
from dekko.render import mapfile
from dekko.render import render_md
from dekko.storage import filelock
from dekko.core import walker
from dekko.core.extractor import extract_file, looks_like_cpp_header
from dekko.core.extractor_generic import extract_file_generic
from dekko.core import languages
from dekko.core.model import TYPE_KINDS, CallGraph, FileMap
from dekko.render.render_json import render_json
from dekko.core.resolver import (
    _run_pool_bounded,
    resolve,
    run_pooled_with_retry,
)


# Below this many cache-miss files, a process pool costs more in startup
# and pickling than it saves, so extraction stays sequential.
_PARALLEL_MIN = 50


def extract_one(root: Path, rel: str) -> FileMap | None:
    """Extract a single file, or ``None`` when it is unsupported.

    Args:
        root: Repository root.
        rel: Repo-relative path of the file.

    Returns:
        The file's ``FileMap``, or ``None`` if no tier-1 spec or tier-2
        grammar handles it.
    """
    spec = languages.spec_for_path(rel)
    if spec is not None:
        spec = _resolve_header_spec(root, rel, spec)
        return extract_file(root, rel, spec)
    grammar = languages.tier2_grammar_for_path(rel)
    if grammar is not None:
        return extract_file_generic(root, rel, grammar)
    return None


def _resolve_header_spec(
    root: Path, rel: str, spec: languages.LanguageSpec
) -> languages.LanguageSpec:
    """Disambiguate a ``.h`` file between the C and C++ grammars.

    ``.h`` is claimed unconditionally by the C ``LanguageSpec``
    (``languages.EXTENSION_MAP`` has no separate C++-header
    extension), but ``.h`` is also the dominant convention for C++
    headers in large codebases (LLVM, gRPC, Abseil, tensorflow, ...).
    Parsing a genuine C++ header with the C grammar silently drops
    every ``class``/``namespace``/``template`` construct instead of
    erroring, producing confidently wrong call/heritage resolution
    downstream instead of just a coverage gap (round 18's tensorflow
    finding -- see ``test-repos/reports/18-tokentest-7repo-post0404/
    IMPLEMENTATION-PLAN-h-header-cpp-c-grammar.md``). Sniff the file's
    own content, not the extension, by parsing it with the C++ grammar
    and checking for a real C++ construct.

    Args:
        root: Repository root.
        rel: Repo-relative path of the file.
        spec: The extension-resolved spec (both ``.c`` and ``.h``
            resolve to ``languages.C`` before this check runs).

    Returns:
        ``languages.CPP`` when ``rel`` is a ``.h`` file whose content
        contains a genuine C++ construct; ``spec`` unchanged otherwise
        -- including for ``.c`` files, which are never content-sniffed,
        and for a ``.h`` file that fails to read here (the extraction
        call right after this one will surface that same failure).
    """
    if spec is not languages.C or not rel.lower().endswith(".h"):
        return spec
    try:
        source = (root / rel).read_bytes()
    except OSError:
        return spec
    return languages.CPP if looks_like_cpp_header(source) else spec


def resolve_workers(jobs: int) -> int:
    """Map a ``--jobs`` value to a concrete worker count (0 → all cores)."""
    if jobs > 0:
        return jobs
    return os.cpu_count() or 1


def _extract_misses(
    root: Path, misses: list[str], workers: int
) -> dict[str, FileMap | None]:
    """Extract the cache-miss files, in parallel when it pays off.

    Args:
        root: Repository root.
        misses: Repo-relative paths that were not served from cache.
        workers: Resolved worker count (1 = sequential).

    Returns:
        ``rel -> FileMap`` (or ``None`` for unsupported files).

    A ``BrokenProcessPool`` on the parallel path (e.g. sibling
    multiprocessing contention from another concurrent ``dekko``
    process on this machine — round 17) gets one bounded retry at
    reduced parallelism via ``run_pooled_with_retry`` before
    propagating. A worker that never returns a result at all within
    ``POOL_RESULT_TIMEOUT_S`` (round 21 Track A: a spawned worker
    resolving a completely different Python interpreter than its own
    parent, hanging indefinitely at 0% CPU with no error) surfaces as
    ``resolver.PoolStalledError`` instead of hanging forever — see
    ``run_pooled_with_retry``'s docstring.
    """
    if workers <= 1 or len(misses) < _PARALLEL_MIN:
        return {rel: extract_one(root, rel) for rel in misses}

    def _run(w: int) -> dict[str, FileMap | None]:
        pool = ProcessPoolExecutor(max_workers=w)
        try:
            futures = [pool.submit(extract_one, root, rel) for rel in misses]
            results = _run_pool_bounded(pool, futures)
            return dict(zip(misses, results))
        finally:
            pool.shutdown(wait=False)

    return run_pooled_with_retry(_run, workers, "file extraction")


def map_repository(
    root: Path,
    subpath: str | None,
    excludes: tuple[str, ...],
    max_file_size: int,
    cache: cache_mod.IncrementalCache | None = None,
    jobs: int = 1,
    candidates: list[str] | None = None,
) -> tuple[list[FileMap], list[tuple[str, str]]]:
    """Discover and extract every mappable file under a root.

    Cache hits are gathered in-process; the remaining files are extracted
    sequentially or across a process pool (``jobs``), then results are
    re-assembled in discovery order so output is independent of how many
    workers ran.

    Args:
        root: Repository root.
        subpath: Optional repo-relative subtree restriction.
        excludes: Extra glob patterns to skip.
        max_file_size: Size cap in bytes.
        cache: Incremental cache to reuse unchanged files from and
            record fresh extractions into, or ``None`` for a cold run.
        jobs: Worker count for extraction (1 = sequential, 0 = all cores).
        candidates: Explicit repo-relative paths to consider, bypassing
            ``walker.discover``'s own tracked-file discovery — see that
            function's ``candidates`` parameter.

    Returns:
        ``(file_maps, skipped)`` where ``skipped`` pairs paths with
        skip reasons.
    """
    paths, skipped = walker.discover(
        root,
        subpath=subpath,
        excludes=excludes,
        max_file_size=max_file_size,
        candidates=candidates,
    )
    extracted: dict[str, FileMap] = {}
    misses: list[str] = []
    for rel in paths:
        fm = cache.reuse(root, rel) if cache is not None else None
        if fm is not None:
            extracted[rel] = fm
        else:
            misses.append(rel)

    fresh = _extract_misses(root, misses, resolve_workers(jobs))
    for rel, fm in fresh.items():
        if fm is None:
            continue
        if cache is not None:
            cache.store(root, rel, fm)
        extracted[rel] = fm

    file_maps = [extracted[rel] for rel in paths if rel in extracted]
    for fm in file_maps:
        if classify.is_test_path(fm.path):
            for sym in fm.symbols:
                sym.test = True
    return file_maps, skipped


def resolve_outputs(
    root: Path, output: str | None, json_output: str | None
) -> tuple[Path, Path]:
    """Resolve the markdown and JSON output paths.

    Args:
        root: The mapped repository root.
        output: ``--output`` value — a markdown file path, or a
            directory to receive MAP.md and map.json.
        json_output: Explicit ``--json`` path, if any.

    Returns:
        ``(markdown_path, json_path)``.
    """
    if output is None:
        md_path = root / cache_mod.CACHE_DIR / "MAP.md"
    else:
        out = Path(output)
        if out.is_dir() or output.endswith("/"):
            md_path = out / "MAP.md"
        else:
            md_path = out

    if json_output is not None:
        json_path = Path(json_output)
    elif md_path.name == "MAP.md":
        json_path = md_path.parent / "map.json"
    else:
        json_path = md_path.with_suffix(".json")

    return md_path, json_path


def _resolve_shard(shard: str, output: str | None, md_path: Path) -> str:
    """Apply the ``--output`` precedence rule to the shard mode.

    An explicit ``--output FILE`` (a path that is not a directory and
    does not resolve to ``MAP.md``) means the user asked for one file,
    so sharding is forced off. ``--output DIR`` keeps the requested
    mode and shards into ``DIR/map/``.

    Args:
        shard: Requested mode (``auto``/``always``/``never``).
        output: Raw ``--output`` value, if any.
        md_path: Resolved markdown output path.

    Returns:
        The effective shard mode.
    """
    if output is not None and md_path.name != "MAP.md":
        return "never"
    return shard


def _write_pages(md_path: Path, pages: list[tuple[str, str]]) -> list[Path]:
    """Write the index and any directory pages; wipe stale pages first.

    The first pair is the index, written to ``md_path``. Remaining
    pairs are ``map/<slug>.md`` pages written under ``md_path``'s
    directory. Any ``map/*.md`` from a previous run is removed first so
    renamed or deleted directories never leave orphan pages behind.

    Args:
        md_path: Path for the index page (e.g. ``.dekko/MAP.md``).
        pages: ``(page_path, content)`` pairs from ``render_map``.

    Returns:
        Every path written, in write order.
    """
    map_dir = md_path.parent / "map"
    if map_dir.is_dir():
        for stale in map_dir.glob("*.md"):
            stale.unlink()

    written = [md_path]
    # round-13 spring-boot.md: a `FileNotFoundError` writing MAP.md was
    # seen once, immediately after `test-repos/reset.sh` (which removes
    # `.dekko/` entirely), and claude-buddy.md's report independently
    # saw the softer, non-crashing shape of the same thing (a write
    # reporting success before `.dekko/` was visible on disk). This
    # function already re-asserts `page_path.parent.mkdir(...)` for
    # every *subsequent* page below, guarding against exactly this --
    # the index page was the one write in this function that instead
    # relied entirely on `run_map`'s much-earlier `md_path.parent.mkdir`
    # call (well before the potentially long `resolve()`/`render_map`
    # call in between) still holding by the time this line runs. This
    # call is idempotent (`exist_ok=True`) and effectively free, so
    # there's no reason the index write should be the only one in this
    # function not self-sufficient against the directory transiently
    # not existing yet.
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(pages[0][1], encoding="utf-8")
    for name, content in pages[1:]:
        page_path = md_path.parent / name
        page_path.parent.mkdir(parents=True, exist_ok=True)
        page_path.write_text(content, encoding="utf-8")
        written.append(page_path)
    return written


def _summary(
    files: list[FileMap],
    edges: int,
    ambiguous: int,
    external: int,
    skipped: list[tuple[str, str]],
    outputs: list[Path],
) -> str:
    """Build the human-readable run summary."""
    by_lang = Counter(fm.language for fm in files)
    langs = ", ".join(f"{lang} {n}" for lang, n in by_lang.most_common())

    funcs = sum(
        1
        for fm in files
        for s in fm.symbols
        if s.kind in ("function", "method")
    )

    classes = sum(
        1 for fm in files for s in fm.symbols if s.kind in TYPE_KINDS
    )
    variables = sum(
        1 for fm in files for s in fm.symbols if s.kind == "variable"
    )
    # round-12 master report §3.10/§3.16: a missing *optional* grammar
    # (``pip install dekko[all]``) and a genuine parse failure used to
    # share one alarming "parse error N" bucket, even though the
    # per-file detail line already named the missing grammar
    # accurately -- see ``grammars.is_grammar_unavailable_message``.
    no_grammar = sum(
        1
        for fm in files
        if fm.error and grammars.is_grammar_unavailable_message(fm.error)
    )
    errors = sum(1 for fm in files if fm.error) - no_grammar
    lines = [
        f"dekko: mapped {len(files)} files ({langs})",
        f"  symbols: {funcs} functions/methods, {classes} types, "
        f"{variables} variables",
        f"  call edges: {edges} resolved, {ambiguous} ambiguous, "
        f"{external} external",
    ]

    if skipped or errors or no_grammar:
        reasons = Counter(reason for _, reason in skipped)
        if errors:
            reasons["parse error"] = errors
        if no_grammar:
            reasons["no grammar installed"] = no_grammar

        detail = ", ".join(
            f"{reason} {n}" for reason, n in reasons.most_common()
        )

        lines.append(f"  skipped: {detail}")

    pages = [
        p for p in outputs if p.parent.name == "map" and p.suffix == ".md"
    ]
    singles = [p for p in outputs if p not in pages]
    parts = [f"{p.name} ({p.stat().st_size / 1024:.1f} KB)" for p in singles]
    if pages:
        total = sum(p.stat().st_size for p in pages) / 1024
        parts.append(f"{len(pages)} pages under map/ ({total:.1f} KB)")

    lines.append(f"  wrote {', '.join(parts)}")
    return "\n".join(lines)


def _map_run_is_noop(
    root: Path,
    args: argparse.Namespace,
    cache: cache_mod.IncrementalCache | None,
    files: list[FileMap],
) -> bool:
    """True when this run would re-write byte-identical output.

    Guards a true no-op fast path for the default (non ``--full``)
    ``dekko map`` path: when nothing needed re-parsing, no file was
    added or removed since the cache was written, this run's discovery
    options match the on-disk map's provenance, and that map was built
    by the exact running dekko, re-serializing MAP.md/map.json/shards
    would produce the same bytes already on disk — skip resolve() and
    every render/write step entirely (prints a short summary unless
    ``--quiet``) rather than paying that cost on every invocation.

    The ``tool_version``/``spec_hash`` check exists because ``cache.
    parsed == 0`` alone is not quite sufficient: it is trustworthy for
    *the cache itself* (a cache from a different dekko build never
    survives to be reused — see bug #1's fix in ``cache.py``), but
    ``.dekko/cache.json`` and ``.dekko/map.json`` are two independent
    files, and a hand-edited or otherwise desynced map.json could
    still be stale even when the cache looks fully warm. The
    ``doc_version`` check exists for the same reason but a distinct
    axis: ``MAP_DOC_VERSION`` (the on-disk *format*, e.g. the round-15
    id-interning change) can bump independently of a package release
    — ``tool_version``/``spec_hash`` alone would call an old-format
    map.json "fresh" forever on an unchanged source tree, since
    neither of them moves just because the serialization shape did.



    Args:
        root: Repository root.
        args: Parsed CLI arguments for this run.
        cache: The incremental cache used for this run, or ``None``
            (``--no-json`` runs never take the fast path — there is no
            map.json to compare against).
        files: This run's extraction results.

    Returns:
        True when the run's summary was printed and nothing else
        needs to happen.
    """
    if getattr(args, "full", False) or cache is None:
        return False
    if cache.parsed != 0 or not cache.unchanged([fm.path for fm in files]):
        return False
    index = mapfile.load_map(root)
    if index is None or not index.provenance:
        return False
    prov = index.provenance
    options_match = (
        prov.get("subpath") == args.subpath
        and prov.get("excludes", []) == list(args.exclude)
        and prov.get("max_file_size") == args.max_file_size
    )
    version_match = (
        prov.get("tool_version") == _pkg_version("dekko")
        and prov.get("spec_hash") == languages.spec_fingerprint()
        and index.doc_version == mapfile.MAP_DOC_VERSION
    )
    if not (options_match and version_match):
        return False
    if not args.quiet:
        commit = (prov.get("git_commit") or "no git")[:12]
        print(
            f"dekko: unchanged ({len(files)} files, commit {commit}) "
            "— nothing written"
        )
    return True


def _maybe_persist_excludes(
    root: Path, args: argparse.Namespace, persist_excludes: bool
) -> None:
    """Append ``args.exclude`` to ``.dekko/.dekkoignore`` if requested.

    Args:
        root: Repository root.
        args: Parsed arguments for this run.
        persist_excludes: Whether this call site is allowed to persist
            (``False`` for ``regen_map``'s replayed provenance).
    """
    if persist_excludes and args.exclude:
        cache_mod.persist_dekkoignore(root, args.exclude)


def run_map(args: argparse.Namespace, persist_excludes: bool = True) -> int:
    """Execute the mapping action for parsed CLI arguments.

    Args:
        args: Parsed arguments with ``map_dir`` set.
        persist_excludes: Append ``args.exclude`` to
            ``.dekko/.dekkoignore`` on a successful run. Set to
            ``False`` by ``regen_map`` — its ``exclude`` values are
            already-persisted provenance replayed for a re-render, not
            a fresh user-supplied ``--exclude``, so re-persisting them
            on every ``--if-stale``/auto-regen cycle would be a no-op
            at best and a surprise write at worst.

    Returns:
        Process exit code.
    """
    root = Path(args.map_dir).resolve()
    if not root.is_dir():
        print(f"dekko: not a directory: {root}", file=sys.stderr)
        return 2

    if getattr(args, "if_stale", False) and _map_is_fresh(root, args):
        return 0

    cache = None
    if not args.no_json:
        old = {} if getattr(args, "full", False) else cache_mod.load(root)
        cache = cache_mod.IncrementalCache(old)

    start = time.perf_counter()
    files, skipped = map_repository(
        root,
        subpath=args.subpath,
        excludes=tuple(args.exclude),
        max_file_size=args.max_file_size,
        cache=cache,
        jobs=getattr(args, "jobs", 1),
    )
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    if not files:
        print(
            f"dekko: no supported source files found under {root}",
            file=sys.stderr,
        )
        return 1

    _maybe_persist_excludes(root, args, persist_excludes)

    if _map_run_is_noop(root, args, cache, files):
        return 0

    graph = resolve(files, workers=resolve_workers(getattr(args, "jobs", 1)))
    label = root.name + (f"/{args.subpath}" if args.subpath else "")

    md_path, json_path = resolve_outputs(root, args.output, args.json_output)

    cache_mod.ensure_dir(root)
    outputs: list[Path] = []
    md_path.parent.mkdir(parents=True, exist_ok=True)
    shard = _resolve_shard(
        getattr(args, "shard", "auto"), args.output, md_path
    )
    if cache is not None:
        reused, parsed = cache.reused, cache.parsed
    else:
        reused, parsed = 0, len(files)
    run_stats = render_md.RunStats(
        elapsed_ms=elapsed_ms, reused=reused, parsed=parsed
    )
    pages = render_md.render_map(
        files,
        graph,
        label,
        shard,
        run_stats=run_stats,
        root=root,
        order=getattr(args, "order", "path"),
    )
    outputs += _write_pages(md_path, pages)
    if not args.no_json:
        outputs.append(
            _write_json_output(
                root, args, files, graph, label, json_path, skipped
            )
        )

    if cache is not None:
        cache_mod.save(root, cache)

    if not args.quiet:
        print(
            _summary(
                files,
                edges=len(graph.edges),
                ambiguous=len(graph.ambiguous),
                external=len(graph.external),
                skipped=skipped,
                outputs=outputs,
            )
        )
    return 0


def _write_json_output(
    root: Path,
    args: argparse.Namespace,
    files: list[FileMap],
    graph: CallGraph,
    label: str,
    json_path: Path,
    skipped: list[tuple[str, str]],
) -> Path:
    """Write ``map.json`` (and its provenance sidecar) for a map run.

    Split out of ``run_map`` to keep it under the complexity budget.

    The sidecar is written only when this run's ``json_path`` is the
    canonical ``.dekko/map.json`` location — the fixed path
    ``load_map``/``check_freshness``/``load_provenance`` always read,
    regardless of ``--output``. A custom ``--output``/``--json-output``
    run doesn't touch that canonical file, so writing the sidecar then
    would desync it from whatever map.json (if any) is still sitting
    at the canonical path.

    Args:
        root: Repository root.
        args: Parsed ``dekko map`` arguments.
        files: This run's extraction results.
        graph: Resolved call graph.
        label: Display label of the mapped root.
        json_path: Resolved output path for ``map.json``.
        skipped: ``(path, reason)`` pairs from discovery.

    Returns:
        ``json_path``, for the caller's ``outputs`` list.
    """
    provenance = mapfile.compute_provenance(
        root,
        [fm.path for fm in files],
        subpath=args.subpath,
        excludes=tuple(args.exclude),
        max_file_size=args.max_file_size,
        skipped=skipped,
    )
    json_path.parent.mkdir(parents=True, exist_ok=True)
    mapfile.atomic_write_bytes(
        json_path,
        render_json(files, graph, label, provenance),
    )
    if json_path == root / cache_mod.CACHE_DIR / "map.json":
        mapfile.write_provenance_sidecar(root, provenance)
    return json_path


def _map_is_fresh(root: Path, args: argparse.Namespace) -> bool:
    """True when the existing map matches the request and is fresh.

    Prints the one-line freshness summary (unless ``--quiet``) so
    ``--if-stale`` callers still get a status line.
    """
    index = mapfile.load_map(root)
    if index is None or not index.provenance:
        return False
    prov = index.provenance
    options_match = (
        prov.get("subpath") == args.subpath
        and prov.get("excludes", []) == list(args.exclude)
        and prov.get("max_file_size") == args.max_file_size
    )
    if not options_match:
        return False
    if not mapfile.check_freshness(root, index).fresh:
        return False
    if not args.quiet:
        commit = (prov.get("git_commit") or "no git")[:12]
        n = len(prov.get("files", {}))
        print(f"dekko: map fresh ({n} files, commit {commit})")
        note = mapfile.format_unsupported(prov)
        if note:
            print(f"  {note}")
    return True


# Optional daemon-installed warm-cache hook (Phase 3 of
# ``.features/daemon-mode/``). ``load_or_regen`` is the single
# chokepoint essentially every read subcommand funnels through
# directly or via ``_read_index`` (``query``/``outline``/``context``/
# ``trace``/``unused``/``stats``/``summary``/``lean``), or calls
# directly (``run_search``, ``run_export``, ``workset.run``) — caching
# at this one point benefits every daemon-eligible subcommand without
# each one needing its own cache-awareness, the same "one choke
# point" property that makes ``server.py``'s ``Context.index_cache``
# sufficient for the whole MCP tool surface (see ``server.py``'s
# ``_index_for``).
#
# Both hooks are ``None`` for every direct CLI invocation — the
# overwhelming majority of calls — so ``cli.py``'s own behavior is
# completely unchanged unless a daemon process has explicitly
# installed them via ``set_daemon_cache_hook``. Only
# ``daemon.serve_daemon`` ever calls that, once at startup (and clears
# it again on shutdown).
_daemon_cache_get: Callable[[Path], mapfile.MapIndex | None] | None = None
_daemon_cache_put: Callable[[Path, mapfile.MapIndex], None] | None = None


def set_daemon_cache_hook(
    get: Callable[[Path], mapfile.MapIndex | None] | None,
    put: Callable[[Path, mapfile.MapIndex], None] | None,
) -> None:
    """Install (or clear, passing ``None``/``None``) the daemon's cache.

    ``get(root)`` must return a still-fresh cached index for ``root``
    (having already re-validated it via ``mapfile.check_freshness``
    itself — this seam trusts the hook's own answer, it does not
    re-check), or ``None`` on a miss/stale hit. ``put(root, index)``
    records a freshly loaded index for later ``get`` calls.

    Args:
        get: Cache-check callback, or ``None`` to disable cache
            lookups (the default, direct-CLI behavior).
        put: Cache-store callback, or ``None`` to disable caching.
    """
    global _daemon_cache_get, _daemon_cache_put
    _daemon_cache_get = get
    _daemon_cache_put = put


# Regen-lock wait: how often to re-check freshness while another
# process holds the ``.dekko/regen.lock`` (round-12 §4.1b), and how
# long to wait before giving up and fail-opening into a redundant
# local regen anyway. The cap matches daemon.py's own "generous but
# bounded" convention (_CLIENT_TIMEOUT/_REQUEST_TIMEOUT, both 30s) —
# never block indefinitely on another process.
_REGEN_LOCK_POLL_INTERVAL = 0.2
_REGEN_LOCK_WAIT_CAP = 30.0


def _wait_for_other_regen(root: Path) -> mapfile.MapIndex | None:
    """Poll for another process's in-flight regen to land.

    Called after ``filelock.try_regen_lock`` reports that a different
    process already holds the regen lock for ``root`` — rather than
    redundantly regenerating in parallel, wait a short bounded
    interval for that process's regen to finish and re-check
    freshness.

    Args:
        root: Repository root another process is regenerating.

    Returns:
        A freshly loaded, fresh index if the wait succeeded within
        the cap; ``None`` if the cap was hit first (caller should
        fail open and regen locally).
    """
    deadline = time.monotonic() + _REGEN_LOCK_WAIT_CAP
    while time.monotonic() < deadline:
        time.sleep(_REGEN_LOCK_POLL_INTERVAL)
        index = mapfile.load_map(root)
        if index is not None and mapfile.check_freshness(root, index).fresh:
            return index
    return None


def _locked_regen(root: Path) -> tuple[mapfile.MapIndex | None, int]:
    """Regenerate ``root``'s map, coordinating via the advisory regen
    lock (round-12 §4.1b).

    A best-effort advisory lock (``filelock.try_regen_lock``)
    coordinates against other processes (bare CLI, daemon-triggered
    regen, MCP server) regenerating the same root concurrently: the
    lock holder regens as before; a non-holder waits briefly for the
    holder's regen to land rather than redundantly repeating the same
    work, falling open to its own local regen if the wait cap is hit
    or locking isn't available at all.

    Args:
        root: Repo root containing (or about to contain) map.json.

    Returns:
        ``(index, exit_code)`` — index is ``None`` on failure.
    """
    with filelock.try_regen_lock(root) as acquired:
        if not acquired:
            fresh = _wait_for_other_regen(root)
            if fresh is not None:
                if _daemon_cache_put is not None:
                    _daemon_cache_put(root, fresh)
                return fresh, 0
            # Wait cap hit without the other process's regen landing
            # -- fail open, fall through to a local regen below.

        code = regen_map(root, quiet=True)
        if code != 0:
            return None, code
        index = mapfile.load_map(root)
        if index is not None and _daemon_cache_put is not None:
            _daemon_cache_put(root, index)
        return index, 0


def load_or_regen(
    root: Path, no_regen: bool
) -> tuple[mapfile.MapIndex | None, int]:
    """Load the map at root, regenerating when missing or stale.

    When running inside the daemon process (``set_daemon_cache_hook``
    has installed a hook), a still-fresh cached index is returned
    outright, skipping ``map.json``'s JSON parse and the full symbol/
    call-graph index rebuild entirely — the dominant cost of a reload
    (Phase 3 of ``.features/daemon-mode/``, mirroring ``server.py``'s
    ``Context.index_cache``/``_index_for``). A direct CLI invocation
    never installs this hook, so its behavior here is unchanged.

    On a missing/stale map, the regen itself is coordinated with other
    concurrent processes via ``_locked_regen`` (round-12 §4.1b).

    Args:
        root: Repo root containing map.json.
        no_regen: Fail instead of regenerating.

    Returns:
        ``(index, exit_code)`` — index is ``None`` on failure.
    """
    if _daemon_cache_get is not None:
        cached = _daemon_cache_get(root)
        if cached is not None:
            return cached, 0

    index = mapfile.load_map(root)
    if index is not None and mapfile.check_freshness(root, index).fresh:
        if _daemon_cache_put is not None:
            _daemon_cache_put(root, index)
        return index, 0
    if no_regen:
        print(
            f"dekko: map.json missing or stale under {root} "
            "(run `dekko map`, or drop --no-regen)",
            file=sys.stderr,
        )
        return None, 5

    return _locked_regen(root)


def load_current_index_no_regen(root: Path) -> mapfile.MapIndex | None:
    """Load the current-tree map, checking the daemon's warm cache first.

    ``diff.run``/``affected.changes`` are the one partial exception to
    ``load_or_regen`` being the single daemon-cache chokepoint every
    other read subcommand funnels through (see
    ``.features/daemon-mode/daemon-mode-cli-plan.md`` §2.4's last
    bullet and Phase 4 of ``.features/daemon-mode/TRACKER.md``): their
    current-tree side calls ``mapfile.load_map`` directly, so a
    daemon-routed ``diff``/``affected`` request previously always paid
    a full JSON-parse/index-rebuild, even with a warm cache populated
    by a prior ``query``/``search``/... request against the same
    root. This function is the fix — it checks the same
    ``_daemon_cache_get``/``_daemon_cache_put`` hooks
    ``load_or_regen`` uses, so a cache hit here skips the reload the
    same way it would for any other daemon-eligible command.

    It deliberately does **not** reuse ``load_or_regen`` itself,
    because that function's stale/missing-map behavior is to call
    ``regen_map`` (writing a fresh ``map.json`` to disk) — a side
    effect ``diff``/``affected`` don't want and have never had: they
    already tolerate a stale on-disk index by falling back to an
    in-memory re-parse (``diff.snapshot_new_side`` -> ``diff.
    snapshot()``) that never touches ``map.json``. Adopting
    ``load_or_regen``'s regen-on-stale behavior here would be a
    real behavior change (an on-disk write a plain ``diff``/
    ``affected`` call never made before), not just a cache-hit
    optimization, so this seam only ever *reads* — same contract as
    the ``mapfile.load_map(root)`` call it replaces.

    Outside the daemon process (``_daemon_cache_get``/``_put`` are
    both ``None``, true for every direct CLI invocation), this is
    exactly ``mapfile.load_map(root)`` — same return value, same
    possibly-``None``/possibly-stale semantics ``diff.run``/
    ``affected.changes`` already handle via their own freshness checks
    downstream (``diff.snapshot_new_side``).

    Args:
        root: Repository root containing map.json.

    Returns:
        The loaded index (possibly stale, possibly ``None``) — never
        regenerated as a side effect of this call.
    """
    if _daemon_cache_get is not None:
        cached = _daemon_cache_get(root)
        if cached is not None:
            return cached

    index = mapfile.load_map(root)
    if (
        index is not None
        and _daemon_cache_put is not None
        and mapfile.check_freshness(root, index).fresh
    ):
        _daemon_cache_put(root, index)
    return index


def regen_map(root: Path, full: bool = False, quiet: bool = True) -> int:
    """Re-generate the map at ``root`` with its recorded options.

    Reuses the discovery options (subpath, excludes, size cap) recorded
    in the existing map's provenance, defaulting to a whole-repo map
    when none exists.

    Args:
        root: Repository root to map.
        full: Ignore the ``.dekko`` cache and re-parse every file.
        quiet: Suppress the one-line summary on stdout.

    Returns:
        Process exit code from ``run_map``.
    """
    index = mapfile.load_map(root)
    prov = (index.provenance if index else None) or {}
    regen_args = argparse.Namespace(
        map_dir=str(root),
        subpath=prov.get("subpath"),
        exclude=list(prov.get("excludes", [])),
        max_file_size=prov.get("max_file_size", walker.DEFAULT_MAX_FILE_SIZE),
        output=None,
        json_output=None,
        no_json=False,
        quiet=quiet,
        if_stale=False,
        full=full,
        # 0 = all cores. This is the auto-regen path every other read
        # subcommand funnels through on a stale map (a single-file
        # edit included) — its own extraction work is tiny (usually
        # one changed file, via the incremental cache), but call-graph
        # resolution (resolve()/resolve_refs(), see resolver.py's
        # ``_resolve_all``) is O(the whole repo's calls) regardless of
        # diff size, and was previously left sequential here even on a
        # many-core machine. See round 11 §1: a one-file edit's
        # auto-regen on tensorflow (14,285 files) took *longer* than a
        # from-scratch --full remap because of exactly this.
        jobs=0,
    )
    return run_map(regen_args, persist_excludes=False)
