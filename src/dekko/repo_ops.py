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
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from multiprocessing.context import BaseContext
from pathlib import Path

from dekko.storage import cache as cache_mod
from dekko.storage import resolvecache
from dekko import classify
from dekko import selfcheck
from dekko.core import grammars
from dekko.core import resolver as resolver_mod
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

    def _run(w: int, ctx: BaseContext) -> dict[str, FileMap | None]:
        pool = ProcessPoolExecutor(max_workers=w, mp_context=ctx)
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
    follow_symlinks: bool = False,
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
        follow_symlinks: See ``walker.discover``'s parameter of the
            same name.

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
        follow_symlinks=follow_symlinks,
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
    # Round 31 claude-buddy.md S2: a file whose grammar isn't installed
    # yields zero symbols, yet used to be counted in "mapped N files
    # (... bash 12 ...)" -- which reads as "these 12 bash files were
    # mapped". They are reported on their own line instead, with the
    # fix, so the top line only ever claims what was actually parsed.
    unparsed = Counter(
        fm.language
        for fm in files
        if fm.error and grammars.is_grammar_unavailable_message(fm.error)
    )
    by_lang = Counter(fm.language for fm in files) - unparsed
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
    no_grammar = sum(unparsed.values())
    errors = sum(1 for fm in files if fm.error) - no_grammar
    lines = [
        f"dekko: mapped {len(files) - no_grammar} files ({langs})",
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

    if unparsed:
        mix = ", ".join(f"{lang} {n}" for lang, n in unparsed.most_common())
        lines.append(
            f"  NOT parsed (no symbols, no edges): {mix} -- grammar not "
            "installed; install the extras to map them: "
            "pip install 'dekko[all]'  (or: uv tool install 'dekko[all]')"
        )

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
        and prov.get("follow_symlinks", False)
        == getattr(args, "follow_symlinks", False)
    )
    version_match = (
        prov.get("tool_version") == selfcheck.loaded_version()
        and prov.get("spec_hash") == selfcheck.loaded_spec()
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


def _maybe_block_scoped_overwrite(
    root: Path, args: argparse.Namespace, new_file_count: int
) -> bool:
    """Refuse to silently narrow an existing broader map.

    Round 22 cline.md §3.2: ``dekko map DIR SUBPATH`` (or its legacy
    ``dekko --map DIR SUBPATH`` alias) used to overwrite a full
    N-file map with a 1-file scoped one at the same default ``.dekko/``
    location, with the same success-message shape as an ordinary run
    and no warning that anything destructive happened. Only applies
    when writing to that default location — an explicit
    ``--output``/``--json-output`` redirect is an intentional "write
    it somewhere else," never blocked.

    Args:
        root: Repository root.
        args: Parsed CLI arguments for this run.
        new_file_count: File count this run is about to write.

    Returns:
        True when the run should be refused (caller exits non-zero
        without writing); False when it's safe to proceed.
    """
    if not args.subpath or args.output or args.json_output:
        return False
    if getattr(args, "force", False):
        return False
    existing = mapfile.load_map(root)
    if existing is None or not existing.provenance:
        return False  # no existing map (or one with no provenance)
    if existing.provenance.get("subpath"):
        return False  # it was already scoped too -- narrowing is fine
    existing_count = len(existing.languages_by_path)
    if new_file_count >= existing_count:
        return False
    print(
        f"dekko: refusing to overwrite the existing {existing_count}-file "
        f"full-repo map with a {new_file_count}-file scoped one at the "
        "same .dekko/ location -- pass --output/--json-output to write "
        "the scoped map elsewhere, or --force to overwrite anyway.",
        file=sys.stderr,
    )
    return True


def _maybe_short_circuit(
    root: Path,
    args: argparse.Namespace,
    cache: cache_mod.IncrementalCache | None,
    files: list[FileMap],
) -> int | None:
    """Either of ``run_map``'s two pre-write early-exit checks.

    Folded into one helper (rather than two sequential ``if``s inline
    in ``run_map``) purely to keep that function's own cyclomatic
    complexity under this repo's Ruff-enforced cap -- the two checks
    are otherwise independent and unrelated.

    Returns:
        ``0`` (unchanged no-op), ``2`` (refused scoped overwrite), or
        ``None`` to let the write proceed.
    """
    if _map_run_is_noop(root, args, cache, files):
        return 0
    if _maybe_block_scoped_overwrite(root, args, len(files)):
        return 2
    return None


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


def _reuse_plan(
    root: Path,
    args: argparse.Namespace,
    cache: cache_mod.IncrementalCache | None,
    files: list[FileMap],
) -> resolver_mod.ResolveReuse | None:
    """Cached call resolution this run may reuse, if any.

    Round 30 Track 1: an incremental run used to re-resolve the whole
    repo, so it only ever saved tree-sitter extraction. This lets
    unchanged files keep their previously resolved call edges.

    Args:
        root: Repository root.
        args: Parsed map arguments; ``--full`` opts out entirely.
        cache: This run's extraction cache, or ``None`` with
            ``--no-json`` (no cache, so nothing to key reuse on).
        files: Every mapped file.

    Returns:
        A reuse plan, or ``None`` to resolve the whole repo as before.
    """
    if cache is None or getattr(args, "full", False):
        return None

    return resolvecache.build_reuse(root, files, cache)


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
        follow_symlinks=getattr(args, "follow_symlinks", False),
    )
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    if not files:
        print(
            f"dekko: no supported source files found under {root}",
            file=sys.stderr,
        )
        return 1

    _maybe_persist_excludes(root, args, persist_excludes)

    early_exit = _maybe_short_circuit(root, args, cache, files)
    if early_exit is not None:
        return early_exit

    graph = resolve(
        files,
        workers=resolve_workers(getattr(args, "jobs", 1)),
        root=root,
        reuse=_reuse_plan(root, args, cache, files),
    )
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
        # Written on every successful run, gate hit or miss: a miss that
        # left no cache behind would make the next run miss too, forever.
        resolvecache.save(
            root, resolver_mod.partition_resolution(files, graph), cache
        )

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
        graph=graph,
        skipped=skipped,
        follow_symlinks=getattr(args, "follow_symlinks", False),
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
        and prov.get("follow_symlinks", False)
        == getattr(args, "follow_symlinks", False)
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

    Round-23 §14: this wait used to print nothing at all -- on a large
    repo (tensorflow.md §2.2: 14,285 files) a command blocked here for
    up to ``_REGEN_LOCK_WAIT_CAP`` seconds read as indistinguishable
    from a hang, reintroducing for the concurrent-process case exactly
    the "long silent wait with no diagnostic" experience round 15
    already fixed for the single-command case
    (``diff._maybe_warn_sequential``). Mirrors that same pattern: one
    stderr ``note:`` line, printed once, before the poll loop starts.

    Args:
        root: Repository root another process is regenerating.

    Returns:
        A freshly loaded, fresh index if the wait succeeded within
        the cap; ``None`` if the cap was hit first (caller should
        fail open and regen locally).
    """
    print(
        "note: another dekko process is already regenerating this "
        f"repo's map -- waiting up to {_REGEN_LOCK_WAIT_CAP:.0f}s for "
        "it to finish",
        file=sys.stderr,
    )
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
            # -- fail open, fall through to a local regen below. This
            # is an *uncoordinated*, independent regen running
            # alongside whatever the other process is still doing
            # (round-23 §14) -- disclosed here so a caller watching
            # stderr sees "still not landed, proceeding with my own
            # regen" rather than the silence that used to follow the
            # first note straight into more silence.
            print(
                "note: gave up waiting for the other process's regen "
                "-- running an independent regen now (the other "
                "process may still be in progress)",
                file=sys.stderr,
            )

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
    fresh = mapfile.check_freshness(root, index) if index is not None else None
    if index is not None and fresh is not None and fresh.fresh:
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

    _note_foreign_build(fresh)
    regenerated, code = _locked_regen(root)
    if regenerated is None and index is not None and fresh.process_outdated:
        # An outdated long-lived process whose delegated regen failed
        # (broken install, timeout). It must not fall back to
        # extracting with its own stale code; the map on disk is the
        # best honest answer it has.
        print(
            "note: could not regenerate via the installed dekko "
            f"(exit {code}) -- serving the existing map, which may be "
            "stale",
            file=sys.stderr,
        )
        return index, 0

    return regenerated, code


def _note_foreign_build(fresh: mapfile.Freshness | None) -> None:
    """Flag a map written by same-version, different-code dekko.

    A one-shot process about to regenerate a map whose ``tool_version``
    matches its own but whose ``spec_hash`` doesn't is looking at one
    of two things: a dev rebuild, or an outdated long-lived dekko
    process (one predating round 33 Track 1) that keeps rewriting the
    map with older extractor code. This process can't stop the second
    case, but it can stop it being invisible: without this note the
    only symptom is every other command being mysteriously slow.
    """
    if fresh is None or selfcheck.is_long_lived():
        return
    if fresh.reason != "version" or fresh.version_stale:
        return
    print(
        "note: map.json was last written by a different dekko build "
        f"(spec {(fresh.built_spec_hash or 'unknown')[:12]}) under the "
        "same version string. If this repeats, an outdated long-lived "
        "dekko process is rewriting it: run `dekko doctor`.",
        file=sys.stderr,
    )


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


# Generous: the child is a full `dekko map` on the largest repo this
# process can be pointed at (tensorflow cold: ~190s, ~500s contended).
_DELEGATED_REGEN_TIMEOUT = 1800.0
_DELEGATED_REGEN_SNIPPET = (
    "import sys;"
    "from pathlib import Path;"
    "from dekko import repo_ops;"
    "sys.exit(repo_ops.regen_map("
    "Path(sys.argv[1]), full=sys.argv[2] == '1', quiet=sys.argv[3] == '1'))"
)


def _delegated_regen(root: Path, full: bool, quiet: bool) -> int:
    """Regenerate via the installed dekko, not this process's stale code.

    Called when a long-lived process has found itself outdated
    (``selfcheck.process_outdated``). Extracting in-process would stamp
    the map with this process's older spec, which the next current
    process would call stale and rewrite, which this process would
    then call stale and rewrite... (round 33 Track 1). A child
    interpreter imports whatever is on disk *now*, so its map and its
    caches carry the current identity, and this process becomes a
    client of the current code instead of a competitor to it.

    The child runs the same ``regen_map`` (it is one-shot, so it takes
    the in-process branch), so recorded provenance options are honored
    identically. The caller's regen lock, if held, stays held around
    it. Output is captured and relayed rather than inherited: inside
    the MCP server, the real stdout is the protocol channel.

    Args:
        root: Repository root to map.
        full: Ignore the ``.dekko`` cache and re-parse every file.
        quiet: Suppress the one-line summary on stdout.

    Returns:
        The child's exit code, or 1 if it couldn't be run at all.
    """
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                _DELEGATED_REGEN_SNIPPET,
                str(root),
                "1" if full else "0",
                "1" if quiet else "0",
            ],
            capture_output=True,
            text=True,
            timeout=_DELEGATED_REGEN_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"dekko: delegated regen failed to run: {exc}", file=sys.stderr)
        return 1
    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    return result.returncode


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
    if selfcheck.process_outdated():
        return _delegated_regen(root, full=full, quiet=quiet)

    index = mapfile.load_map(root)
    prov = (index.provenance if index else None) or {}
    regen_args = argparse.Namespace(
        map_dir=str(root),
        subpath=prov.get("subpath"),
        exclude=list(prov.get("excludes", [])),
        max_file_size=prov.get("max_file_size", walker.DEFAULT_MAX_FILE_SIZE),
        follow_symlinks=prov.get("follow_symlinks", False),
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
