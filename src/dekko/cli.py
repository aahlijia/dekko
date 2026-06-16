"""dekko: programmatically map a repository into MAP.md/map.json.

Walks the repo, parses every supported source file with tree-sitter,
extracts functions/parameters/types, resolves call relationships, and
writes a human-readable MAP.md plus a machine-readable map.json.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from importlib.metadata import version as _pkg_version
from importlib.resources import files as _pkg_files
from pathlib import Path

from . import affected
from . import cache as cache_mod
from . import classify
from . import contextpack
from . import diff
from . import export
from . import languages
from . import mapfile
from . import notes as notes_mod
from . import orient as orient_mod
from . import outline as outline_mod
from . import query
from . import render_html
from . import render_lean
from . import render_md
from . import server
from . import stats
from . import summary
from . import trace
from . import unused
from . import walker
from . import workset as workset_mod
from .extractor import extract_file
from .extractor_generic import extract_file_generic
from .model import FileMap
from .render_json import render_json
from .resolver import resolve


SUBCOMMANDS = (
    "map",
    "query",
    "outline",
    "lean",
    "context",
    "trace",
    "diff",
    "affected",
    "workset",
    "status",
    "serve",
    "unused",
    "stats",
    "summary",
    "orient",
    "note",
    "export",
)

# Below this many cache-miss files, a process pool costs more in startup
# and pickling than it saves, so extraction stays sequential.
_PARALLEL_MIN = 50


def build_legacy_parser() -> argparse.ArgumentParser:
    """Construct the legacy flag-based parser (v0.2 aliases)."""
    parser = argparse.ArgumentParser(
        prog="dekko",
        description="Generate MAP.md and map.json for a repository.",
    )
    parser.add_argument(
        "subpath",
        nargs="?",
        default=None,
        help="optional repo-relative subtree to map (with --map)",
    )
    parser.add_argument(
        "--map",
        dest="map_dir",
        nargs="?",
        const=".",
        default=None,
        metavar="DIR",
        help="map DIR (default: the current directory)",
    )
    parser.add_argument(
        "--claude-install",
        action="store_true",
        help="install the dekko plugin into Claude Code",
    )
    parser.add_argument(
        "--claude-uninstall",
        action="store_true",
        help="remove the dekko plugin from Claude Code",
    )
    parser.add_argument(
        "--mcp-install",
        action="store_true",
        help="register the MCP server with Claude Code (claude mcp add)",
    )
    parser.add_argument(
        "--mcp-uninstall",
        action="store_true",
        help="remove the MCP server from Claude Code (claude mcp remove)",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"dekko {_pkg_version('dekko')}",
    )
    _add_map_options(parser)
    return parser


def _add_map_options(parser: argparse.ArgumentParser) -> None:
    """Attach the mapping output/filter options shared by both parsers."""
    parser.add_argument(
        "--output",
        default=None,
        metavar="PATH",
        help="markdown output file, or a directory to receive "
        "MAP.md and map.json (default: a .dekko/ dir under the "
        "mapped directory). An explicit file path forces --shard "
        "never; a directory shards into <dir>/map/ when sharding "
        "applies",
    )
    parser.add_argument(
        "--shard",
        choices=render_md.SHARD_MODES,
        default="auto",
        help="split MAP.md into per-directory map/ pages: auto "
        "(shard large maps; the default), always, or never",
    )
    parser.add_argument(
        "--order",
        choices=render_md.ORDER_MODES,
        default="path",
        help="order file sections by path (default), name, or fan-in "
        "(most depended-on first; also orders symbols within a file)",
    )
    parser.add_argument(
        "--json",
        dest="json_output",
        default=None,
        metavar="PATH",
        help="JSON output path (default: alongside the markdown)",
    )
    parser.add_argument(
        "--no-json", action="store_true", help="skip writing map.json"
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help="extra glob pattern to skip (repeatable)",
    )
    parser.add_argument(
        "--max-file-size",
        type=int,
        default=walker.DEFAULT_MAX_FILE_SIZE,
        metavar="BYTES",
        help="skip files larger than this (default: 1000000)",
    )
    parser.add_argument(
        "--quiet", action="store_true", help="suppress the summary on stdout"
    )


def _add_read_options(parser: argparse.ArgumentParser) -> None:
    """Attach the options shared by map-reading subcommands."""
    parser.add_argument(
        "--root",
        default=".",
        metavar="DIR",
        help="repo root containing map.json (default: cwd)",
    )
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="emit structured JSON",
    )
    parser.add_argument(
        "--no-regen",
        action="store_true",
        help="fail (exit 5) instead of regenerating a stale map",
    )
    parser.add_argument(
        "--no-tests",
        action="store_true",
        help="exclude test files' symbols and edges from results",
    )


def build_subcommand_parser() -> argparse.ArgumentParser:
    """Construct the subcommand parser (map/query/context/status)."""
    parser = argparse.ArgumentParser(
        prog="dekko",
        description=("Generate and query MAP.md/map.json for a repository."),
        epilog=(
            "legacy aliases: dekko --map [DIR] [SUBPATH], "
            "dekko --claude-install, dekko --version"
        ),
    )
    sub = parser.add_subparsers(
        dest="command", required=True, metavar="COMMAND"
    )

    p_map = sub.add_parser("map", help="generate MAP.md and map.json")
    p_map.add_argument(
        "dir",
        nargs="?",
        default=".",
        metavar="DIR",
        help="directory to map (default: cwd)",
    )
    p_map.add_argument(
        "subpath",
        nargs="?",
        default=None,
        help="optional repo-relative subtree to map",
    )
    p_map.add_argument(
        "--if-stale",
        action="store_true",
        help="skip regeneration when the existing map is fresh",
    )
    p_map.add_argument(
        "--full",
        action="store_true",
        help="ignore the .dekko cache and re-parse every file",
    )
    p_map.add_argument(
        "--jobs",
        type=int,
        default=1,
        metavar="N",
        help="parallel extraction workers (1 = sequential, 0 = all cores)",
    )
    _add_map_options(p_map)
    p_map.set_defaults(func=_cmd_map)

    p_query = sub.add_parser("query", help="query the call graph")
    p_query.add_argument("action", choices=query.ACTIONS)
    p_query.add_argument(
        "target",
        help="symbol (name, Class.method, file.py:func), file path, or "
        "(for uses) an external base identifier",
    )
    p_query.add_argument(
        "--limit",
        type=int,
        default=50,
        help="max text result lines (default: 50)",
    )
    p_query.add_argument(
        "--budget",
        type=int,
        default=None,
        metavar="TOKENS",
        help="approximate token budget; drops lowest-relevance rows",
    )
    p_query.add_argument(
        "--sites",
        action="store_true",
        help="for callers/callees: one row per call site (path:line of "
        "each call expression) instead of one per definition",
    )
    p_query.add_argument(
        "--notes",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="show notes anchored to the symbol (default: on)",
    )
    _add_read_options(p_query)
    p_query.set_defaults(func=run_query)

    p_outline = sub.add_parser(
        "outline",
        help="a file's (or directory's) structure: signatures, no bodies",
    )
    p_outline.add_argument(
        "target", help="mapped file path or directory (default: whole repo)"
    )
    p_outline.add_argument(
        "--limit",
        type=int,
        default=200,
        help="max symbol rows (default: 200)",
    )
    p_outline.add_argument(
        "--budget",
        type=int,
        default=None,
        metavar="TOKENS",
        help="approximate token budget for the outline",
    )
    _add_read_options(p_outline)
    p_outline.set_defaults(func=run_outline)

    p_ctx = sub.add_parser(
        "context", help="emit a context pack for a symbol or file"
    )
    p_ctx.add_argument(
        "target", help="symbol (name, file.py:func) or file path"
    )
    p_ctx.add_argument(
        "--hops",
        type=int,
        default=1,
        help="neighborhood radius (default: 1)",
    )
    p_ctx.add_argument(
        "--budget",
        type=int,
        default=None,
        metavar="TOKENS",
        help="approximate token budget for the pack",
    )
    p_ctx.add_argument(
        "--with-source",
        action="store_true",
        help="inline the target's source body and hop-1 call-site "
        "lines (counts against --budget)",
    )
    p_ctx.add_argument(
        "--notes",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="include notes anchored to the target (default: on)",
    )
    _add_read_options(p_ctx)
    p_ctx.set_defaults(func=run_context)

    p_trace = sub.add_parser(
        "trace", help="shortest call path(s) between two symbols"
    )
    p_trace.add_argument(
        "frm",
        metavar="FROM",
        help="source symbol (name, Class.method, file.py:func)",
    )
    p_trace.add_argument(
        "to",
        metavar="TO",
        help="destination symbol (name, Class.method, file.py:func)",
    )
    p_trace.add_argument(
        "--max-paths",
        type=int,
        default=3,
        help="max distinct shortest paths to report (default: 3)",
    )
    _add_read_options(p_trace)
    p_trace.set_defaults(func=run_trace)

    p_diff = sub.add_parser(
        "diff", help="changed symbols since a git rev, with callers"
    )
    p_diff.add_argument(
        "rev",
        nargs="?",
        default=None,
        help="git rev to compare against (default: the commit the map "
        "was generated at, else HEAD)",
    )
    p_diff.add_argument(
        "--root",
        default=".",
        metavar="DIR",
        help="repo root containing map.json (default: cwd)",
    )
    p_diff.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="emit structured JSON",
    )
    p_diff.add_argument(
        "--limit",
        type=int,
        default=8,
        help="max impacted callers shown per symbol (default: 8)",
    )
    p_diff.set_defaults(func=run_diff)

    p_affected = sub.add_parser(
        "affected", help="test files impacted by changes since a git rev"
    )
    p_affected.add_argument(
        "rev",
        nargs="?",
        default=None,
        help="git rev to compare against (default: the commit the map "
        "was generated at, else HEAD)",
    )
    p_affected.add_argument(
        "--root",
        default=".",
        metavar="DIR",
        help="repo root containing map.json (default: cwd)",
    )
    p_affected.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="emit structured JSON",
    )
    p_affected.add_argument(
        "--limit",
        type=int,
        default=8,
        help="max impacted symbols shown per test file (default: 8)",
    )
    p_affected.add_argument(
        "--budget",
        type=int,
        default=None,
        metavar="TOKENS",
        help="approximate token budget; drops weakest-tier files first",
    )
    p_affected.set_defaults(func=run_affected)

    p_workset = sub.add_parser(
        "workset",
        help="one budgeted bundle for a change: impacts, outlines, packs",
    )
    p_workset.add_argument(
        "rev",
        nargs="?",
        default=None,
        help="git rev to compare against (default: the commit the map "
        "was generated at, else HEAD); omit when using --symbol",
    )
    p_workset.add_argument(
        "--symbol",
        default=None,
        metavar="NAME",
        help="seed from a symbol instead of a diff (name, Class.method, "
        "file.py:name); mutually exclusive with REV",
    )
    p_workset.add_argument(
        "--budget",
        type=int,
        default=workset_mod.DEFAULT_BUDGET,
        metavar="TOKENS",
        help=f"shared token budget for the bundle "
        f"(default: {workset_mod.DEFAULT_BUDGET})",
    )
    p_workset.add_argument(
        "--packs",
        type=int,
        default=workset_mod.DEFAULT_PACKS,
        help=f"top-centrality touched symbols to deep-pack "
        f"(default: {workset_mod.DEFAULT_PACKS})",
    )
    p_workset.add_argument(
        "--root",
        default=".",
        metavar="DIR",
        help="repo root containing map.json (default: cwd)",
    )
    p_workset.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="emit structured JSON",
    )
    p_workset.add_argument(
        "--no-regen",
        action="store_true",
        help="fail (exit 5) instead of regenerating a stale map",
    )
    p_workset.set_defaults(func=run_workset)

    p_status = sub.add_parser(
        "status", help="report whether map.json is fresh"
    )
    p_status.add_argument(
        "--root",
        default=".",
        metavar="DIR",
        help="repo root containing map.json (default: cwd)",
    )
    p_status.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="emit structured JSON",
    )
    p_status.set_defaults(func=run_status)

    p_serve = sub.add_parser("serve", help="run the MCP server over stdio")
    p_serve.add_argument(
        "--mcp",
        action="store_true",
        help="speak the Model Context Protocol (the only transport)",
    )
    p_serve.add_argument(
        "--root",
        default=".",
        metavar="DIR",
        help="default repo root for tool calls (default: cwd)",
    )
    p_serve.add_argument(
        "--no-regen",
        action="store_true",
        help="fail instead of regenerating a stale map on reads",
    )
    p_serve.set_defaults(func=run_serve)

    p_unused = sub.add_parser(
        "unused", help="symbols with no inbound calls (dead-code leads)"
    )
    p_unused.add_argument(
        "--roots",
        action="append",
        default=[],
        metavar="GLOB",
        help="extra path glob whose symbols are always roots (repeatable)",
    )
    p_unused.add_argument(
        "--limit",
        type=int,
        default=50,
        help="max text result lines (default: 50)",
    )
    p_unused.add_argument(
        "--budget",
        type=int,
        default=None,
        metavar="TOKENS",
        help="approximate token budget for the result rows",
    )
    _add_read_options(p_unused)
    p_unused.set_defaults(func=run_unused)

    p_stats = sub.add_parser(
        "stats", help="hotspots, largest files, language mix"
    )
    p_stats.add_argument(
        "--top",
        type=int,
        default=10,
        help="entries per ranked list (default: 10)",
    )
    _add_read_options(p_stats)
    p_stats.set_defaults(func=run_stats)

    p_summary = sub.add_parser(
        "summary", help="compact repo digest (dirs, hotspots, entrypoints)"
    )
    _add_read_options(p_summary)
    p_summary.set_defaults(func=run_summary)

    p_lean = sub.add_parser(
        "lean",
        help="budget-capped navigation map: files, symbols, module edges",
    )
    _add_read_options(p_lean)
    p_lean.add_argument(
        "--budget",
        type=int,
        default=None,
        metavar="TOKENS",
        help="hard token cap (default: scales with repo size; never "
        "below the file-backbone floor)",
    )
    p_lean.add_argument(
        "--output",
        default=None,
        metavar="PATH",
        help="write the map to PATH (e.g. .dekko/LEAN.md) instead of "
        "printing it",
    )
    p_lean.set_defaults(func=run_lean)

    p_orient = sub.add_parser(
        "orient",
        help="opt-in orientation: a steering digest, or a pre-read nudge",
    )
    p_orient.add_argument(
        "--read",
        dest="read_path",
        default=None,
        metavar="PATH",
        help="advisory mode: nudge to outline PATH first when it is "
        "large (silent for small/unmapped files; never blocks)",
    )
    p_orient.add_argument(
        "--budget",
        type=int,
        default=orient_mod.DEFAULT_BUDGET,
        metavar="TOKENS",
        help=f"session digest token budget "
        f"(default: {orient_mod.DEFAULT_BUDGET})",
    )
    p_orient.add_argument(
        "--threshold",
        type=int,
        default=orient_mod.DEFAULT_THRESHOLD,
        metavar="TOKENS",
        help=f"--read advises only when the file reaches this many "
        f"tokens (default: {orient_mod.DEFAULT_THRESHOLD})",
    )
    p_orient.add_argument(
        "--root",
        default=".",
        metavar="DIR",
        help="repo root containing map.json (default: cwd)",
    )
    p_orient.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="emit structured JSON (session mode)",
    )
    p_orient.add_argument(
        "--no-regen",
        action="store_true",
        help="fail (exit 5) instead of regenerating a stale map",
    )
    p_orient.set_defaults(func=run_orient)

    p_note = sub.add_parser(
        "note", help="add, list, or remove symbol-anchored notes"
    )
    note_sub = p_note.add_subparsers(
        dest="note_action", required=True, metavar="ACTION"
    )
    p_note_add = note_sub.add_parser("add", help="anchor a note to a symbol")
    p_note_add.add_argument(
        "target", help="symbol (name, Class.method, file.py:func)"
    )
    p_note_add.add_argument("text", help="the note text")
    p_note_list = note_sub.add_parser(
        "list", help="list notes (all, or for one symbol)"
    )
    p_note_list.add_argument(
        "target",
        nargs="?",
        default=None,
        help="symbol to list notes for (default: all)",
    )
    p_note_list.add_argument(
        "--orphaned",
        action="store_true",
        help="only notes whose symbol is no longer in the map",
    )
    p_note_rm = note_sub.add_parser("rm", help="remove a note from a symbol")
    p_note_rm.add_argument(
        "target", help="symbol (name, Class.method, file.py:func)"
    )
    p_note_rm.add_argument(
        "index",
        nargs="?",
        type=int,
        default=None,
        help="1-based note index to remove (default: all for the symbol)",
    )
    for sp in (p_note_add, p_note_list, p_note_rm):
        sp.add_argument(
            "--root",
            default=".",
            metavar="DIR",
            help="repo root containing map.json (default: cwd)",
        )
        sp.add_argument(
            "--json",
            dest="as_json",
            action="store_true",
            help="emit structured JSON",
        )
    p_note_list.add_argument(
        "--no-regen",
        action="store_true",
        help="fail (exit 5) instead of regenerating a stale map",
    )
    p_note.set_defaults(func=run_note)

    p_export = sub.add_parser(
        "export", help="render the call graph as mermaid or dot"
    )
    p_export.add_argument(
        "--format",
        dest="fmt",
        choices=export.FORMATS,
        required=True,
        help="output graph format",
    )
    p_export.add_argument(
        "--scope",
        choices=export.SCOPES,
        default="symbol",
        help="node granularity (default: symbol)",
    )
    p_export.add_argument(
        "--max-nodes",
        type=int,
        default=export.DEFAULT_MAX_NODES,
        help="refuse to render more nodes than this (default: 300); "
        "ignored for html",
    )
    p_export.add_argument(
        "--output",
        default=None,
        metavar="PATH",
        help="write to this file (default: stdout for mermaid/dot, "
        ".dekko/map.html for html)",
    )
    p_export.add_argument(
        "--root",
        default=".",
        metavar="DIR",
        help="repo root containing map.json (default: cwd)",
    )
    p_export.add_argument(
        "--no-regen",
        action="store_true",
        help="fail (exit 5) instead of regenerating a stale map",
    )
    p_export.set_defaults(func=run_export)
    return parser


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
        return extract_file(root, rel, spec)
    grammar = languages.tier2_grammar_for_path(rel)
    if grammar is not None:
        return extract_file_generic(root, rel, grammar)
    return None


def _resolve_workers(jobs: int) -> int:
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
    """
    if workers <= 1 or len(misses) < _PARALLEL_MIN:
        return {rel: extract_one(root, rel) for rel in misses}
    with ProcessPoolExecutor(max_workers=workers) as pool:
        results = pool.map(extract_one, [root] * len(misses), misses)
        return dict(zip(misses, results))


def map_repository(
    root: Path,
    subpath: str | None,
    excludes: tuple[str, ...],
    max_file_size: int,
    cache: cache_mod.IncrementalCache | None = None,
    jobs: int = 1,
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

    Returns:
        ``(file_maps, skipped)`` where ``skipped`` pairs paths with
        skip reasons.
    """
    paths, skipped = walker.discover(
        root,
        subpath=subpath,
        excludes=excludes,
        max_file_size=max_file_size,
    )
    extracted: dict[str, FileMap] = {}
    misses: list[str] = []
    for rel in paths:
        fm = cache.reuse(root, rel) if cache is not None else None
        if fm is not None:
            extracted[rel] = fm
        else:
            misses.append(rel)

    fresh = _extract_misses(root, misses, _resolve_workers(jobs))
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
    md_path.write_text(pages[0][1])
    for name, content in pages[1:]:
        page_path = md_path.parent / name
        page_path.parent.mkdir(parents=True, exist_ok=True)
        page_path.write_text(content)
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

    classes = sum(1 for fm in files for s in fm.symbols if s.kind == "class")
    errors = sum(1 for fm in files if fm.error)
    lines = [
        f"dekko: mapped {len(files)} files ({langs})",
        f"  symbols: {funcs} functions/methods, {classes} classes",
        f"  call edges: {edges} resolved, {ambiguous} ambiguous, "
        f"{external} external",
    ]

    if skipped or errors:
        reasons = Counter(reason for _, reason in skipped)
        if errors:
            reasons["parse error"] = errors

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


def _run_subprocess(cmd: list[str]) -> subprocess.CompletedProcess:
    """Run a CLI command, capturing its output as text."""
    return subprocess.run(cmd, capture_output=True, text=True)


def _claude_cli_present() -> bool:
    """Return True if the ``claude`` CLI is on PATH, else warn and False."""
    if shutil.which("claude") is None:
        print(
            "dekko: 'claude' CLI not found on PATH. Install Claude Code "
            "first: https://claude.com/claude-code",
            file=sys.stderr,
        )
        return False
    return True


def claude_install() -> int:
    """Register the bundled plugin with the Claude Code CLI.

    Returns:
        Process exit code.
    """
    if not _claude_cli_present():
        return 1

    plugin_dir = Path(str(_pkg_files("dekko"))) / "_plugin"
    if not (plugin_dir / ".claude-plugin").is_dir():
        print(
            f"dekko: bundled plugin not found at {plugin_dir}",
            file=sys.stderr,
        )
        return 1

    added = _run_subprocess(
        ["claude", "plugin", "marketplace", "add", str(plugin_dir)]
    )
    if added.returncode != 0:
        # Likely already registered (e.g. a previous install or a dev
        # checkout): refresh it instead.
        updated = _run_subprocess(
            ["claude", "plugin", "marketplace", "update", "dekko"]
        )
        if updated.returncode != 0:
            print(added.stderr.strip(), file=sys.stderr)
            print(updated.stderr.strip(), file=sys.stderr)
            return 1

    installed = _run_subprocess(["claude", "plugin", "install", "dekko@dekko"])
    if installed.returncode != 0:
        print(installed.stderr.strip(), file=sys.stderr)
        return 1

    print("dekko: plugin installed. Restart Claude Code to activate /map.")
    return 0


def claude_uninstall() -> int:
    """Remove the bundled plugin from the Claude Code CLI.

    Reverses :func:`claude_install`: uninstalls the ``dekko`` plugin and
    drops its marketplace registration. A step that reports the plugin or
    marketplace is already absent is surfaced as a warning rather than a
    failure, so the command is safe to run on a partial install.

    Returns:
        Process exit code (``1`` only when the ``claude`` CLI is missing).
    """
    if not _claude_cli_present():
        return 1

    for cmd in (
        ["claude", "plugin", "uninstall", "dekko@dekko"],
        ["claude", "plugin", "marketplace", "remove", "dekko"],
    ):
        result = _run_subprocess(cmd)
        if result.returncode != 0:
            print(
                f"dekko: '{' '.join(cmd)}' failed (already removed?): "
                f"{result.stderr.strip()}",
                file=sys.stderr,
            )

    print("dekko: plugin removed. Restart Claude Code to drop /map.")
    return 0


def mcp_install() -> int:
    """Register the MCP server with Claude Code via ``claude mcp add``.

    Returns:
        Process exit code.
    """
    if not _claude_cli_present():
        return 1

    added = _run_subprocess(
        ["claude", "mcp", "add", "dekko", "--", "dekko", "serve", "--mcp"]
    )
    if added.returncode != 0:
        print(added.stderr.strip(), file=sys.stderr)
        return 1

    print("dekko: MCP server registered as 'dekko'. Restart Claude Code.")
    return 0


def mcp_uninstall() -> int:
    """Remove the standalone MCP server via ``claude mcp remove``.

    Reverses :func:`mcp_install`. A "not found" report (the server was
    never registered, or only via the plugin's bundled ``.mcp.json``) is
    surfaced as a warning rather than a failure.

    Returns:
        Process exit code (``1`` only when the ``claude`` CLI is missing).
    """
    if not _claude_cli_present():
        return 1

    removed = _run_subprocess(["claude", "mcp", "remove", "dekko"])
    if removed.returncode != 0:
        print(
            "dekko: 'claude mcp remove dekko' failed (already removed?): "
            f"{removed.stderr.strip()}",
            file=sys.stderr,
        )
        return 0

    print("dekko: MCP server 'dekko' removed. Restart Claude Code.")
    return 0


def run_map(args: argparse.Namespace) -> int:
    """Execute the mapping action for parsed CLI arguments.

    Args:
        args: Parsed arguments with ``map_dir`` set.

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

    graph = resolve(files)
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
        provenance = mapfile.compute_provenance(
            root,
            [fm.path for fm in files],
            subpath=args.subpath,
            excludes=tuple(args.exclude),
            max_file_size=args.max_file_size,
        )
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(render_json(files, graph, label, provenance))
        outputs.append(json_path)

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
    return True


def _load_or_regen(
    root: Path, no_regen: bool
) -> tuple[mapfile.MapIndex | None, int]:
    """Load the map at root, regenerating when missing or stale.

    Args:
        root: Repo root containing map.json.
        no_regen: Fail instead of regenerating.

    Returns:
        ``(index, exit_code)`` — index is ``None`` on failure.
    """
    index = mapfile.load_map(root)
    if index is not None and mapfile.check_freshness(root, index).fresh:
        return index, 0
    if no_regen:
        print(
            f"dekko: map.json missing or stale under {root} "
            "(run `dekko map`, or drop --no-regen)",
            file=sys.stderr,
        )
        return None, 5

    code = regen_map(root, quiet=True)
    if code != 0:
        return None, code
    return mapfile.load_map(root), 0


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
        jobs=1,
    )
    return run_map(regen_args)


def _cmd_map(args: argparse.Namespace) -> int:
    """Adapter: ``dekko map DIR`` → ``run_map`` namespace."""
    args.map_dir = args.dir
    return run_map(args)


def _read_index(
    args: argparse.Namespace,
) -> tuple[mapfile.MapIndex | None, int]:
    """Load (auto-regen) the index for a read command, applying filters.

    Args:
        args: Parsed namespace carrying ``root``, ``no_regen``, and
            ``no_tests``.

    Returns:
        ``(index, exit_code)`` — index is ``None`` on failure.
    """
    root = Path(args.root).resolve()
    index, code = _load_or_regen(root, args.no_regen)
    if index is None:
        return None, code
    if getattr(args, "no_tests", False):
        index = index.without_tests()
    return index, 0


def run_query(args: argparse.Namespace) -> int:
    """Handle ``dekko query``."""
    index, code = _read_index(args)
    if index is None:
        return code
    return query.run(
        index,
        args.action,
        args.target,
        as_json=args.as_json,
        limit=args.limit,
        sites=args.sites,
        notes=args.notes,
        budget=args.budget,
    )


def run_outline(args: argparse.Namespace) -> int:
    """Handle ``dekko outline <path>``."""
    index, code = _read_index(args)
    if index is None:
        return code
    return outline_mod.run(
        index,
        args.target,
        root=Path(args.root).resolve(),
        budget=args.budget,
        limit=args.limit,
        as_json=args.as_json,
    )


def run_context(args: argparse.Namespace) -> int:
    """Handle ``dekko context``."""
    index, code = _read_index(args)
    if index is None:
        return code
    return contextpack.run(
        index,
        args.target,
        hops=args.hops,
        budget=args.budget,
        as_json=args.as_json,
        root=Path(args.root).resolve(),
        with_source=args.with_source,
        notes=args.notes,
    )


def run_trace(args: argparse.Namespace) -> int:
    """Handle ``dekko trace <from> <to>``."""
    index, code = _read_index(args)
    if index is None:
        return code
    return trace.run(
        index,
        args.frm,
        args.to,
        max_paths=args.max_paths,
        as_json=args.as_json,
    )


def run_diff(args: argparse.Namespace) -> int:
    """Handle ``dekko diff [REV]``."""
    root = Path(args.root).resolve()
    return diff.run(
        root,
        args.rev,
        as_json=args.as_json,
        limit=args.limit,
    )


def run_affected(args: argparse.Namespace) -> int:
    """Handle ``dekko affected [REV]``."""
    root = Path(args.root).resolve()
    return affected.run(
        root,
        args.rev,
        as_json=args.as_json,
        limit=args.limit,
        budget=args.budget,
    )


def run_workset(args: argparse.Namespace) -> int:
    """Handle ``dekko workset [REV] | --symbol NAME``."""
    if args.symbol is not None and args.rev is not None:
        print("dekko: give a REV or --symbol, not both", file=sys.stderr)
        return workset_mod.EXIT_ERROR
    return workset_mod.run(
        Path(args.root).resolve(),
        args.rev,
        args.symbol,
        budget=args.budget,
        packs=args.packs,
        as_json=args.as_json,
        no_regen=args.no_regen,
    )


def run_unused(args: argparse.Namespace) -> int:
    """Handle ``dekko unused``."""
    index, code = _read_index(args)
    if index is None:
        return code
    return unused.run(
        index,
        tuple(args.roots),
        as_json=args.as_json,
        limit=args.limit,
        budget=args.budget,
    )


def run_stats(args: argparse.Namespace) -> int:
    """Handle ``dekko stats``."""
    index, code = _read_index(args)
    if index is None:
        return code
    return stats.run(index, args.top, as_json=args.as_json)


def run_summary(args: argparse.Namespace) -> int:
    """Handle ``dekko summary``."""
    index, code = _read_index(args)
    if index is None:
        return code
    return summary.run(index, as_json=args.as_json)


def run_lean(args: argparse.Namespace) -> int:
    """Handle ``dekko lean``."""
    index, code = _read_index(args)
    if index is None:
        return code
    out = Path(args.output).resolve() if args.output else None
    return render_lean.run(
        index,
        Path(args.root).resolve(),
        budget=args.budget,
        as_json=args.as_json,
        out_path=out
    )


def run_orient(args: argparse.Namespace) -> int:
    """Handle ``dekko orient [--read PATH]``."""
    return orient_mod.run(
        Path(args.root).resolve(),
        args.read_path,
        budget=args.budget,
        threshold=args.threshold,
        as_json=args.as_json,
        no_regen=args.no_regen,
    )


def run_note(args: argparse.Namespace) -> int:
    """Handle ``dekko note add|list|rm``."""
    if args.note_action == "add":
        return _note_add(args)
    if args.note_action == "rm":
        return _note_rm(args)
    return _note_list(args)


def _resolve_for_note(root: Path, target: str) -> tuple[str | None, int]:
    """Resolve a note target to a symbol id (no map regeneration)."""
    index = mapfile.load_map(root)
    if index is None:
        print(f"dekko: no map under {root} (run `dekko map`)", file=sys.stderr)
        return None, 5
    sym, candidates = query.resolve_target(index, target)
    if sym is None:
        return None, query.report_unresolved(target, candidates)
    return sym.id, 0


def _note_add(args: argparse.Namespace) -> int:
    """Anchor a note to a resolved symbol."""
    root = Path(args.root).resolve()
    sym_id, code = _resolve_for_note(root, args.target)
    if sym_id is None:
        return code
    notes_mod.add(root, sym_id, args.text)
    if args.as_json:
        print(json.dumps({"symbol": sym_id, "text": args.text}))
    else:
        print(f"dekko: noted {sym_id}")
    return 0


def _note_rm(args: argparse.Namespace) -> int:
    """Remove one note (or all) from a resolved symbol."""
    root = Path(args.root).resolve()
    sym_id, code = _resolve_for_note(root, args.target)
    if sym_id is None:
        return code
    removed = notes_mod.remove(root, sym_id, args.index)
    if args.as_json:
        print(json.dumps({"symbol": sym_id, "removed": removed}))
    else:
        print(f"dekko: removed {removed} note(s) from {sym_id}")
    return 0


def _note_list(args: argparse.Namespace) -> int:
    """List notes: all, orphaned, or for a single symbol."""
    root = Path(args.root).resolve()
    if args.orphaned:
        index, code = _load_or_regen(root, args.no_regen)
        if index is None:
            return code
        data = notes_mod.orphaned(root, set(index.symbols_by_id))
    elif args.target is not None:
        sym_id, code = _resolve_for_note(root, args.target)
        if sym_id is None:
            return code
        all_notes = notes_mod.load(root)
        data = {sym_id: all_notes.get(sym_id, [])}
    else:
        data = notes_mod.load(root)
    if args.as_json:
        print(json.dumps(data, indent=2))
        return 0
    if not any(data.values()):
        print("dekko: no notes")
        return 0
    for sym_id, records in sorted(data.items()):
        for record in records:
            print(f"{sym_id}: {record.get('text', '')}")
    return 0


def run_export(args: argparse.Namespace) -> int:
    """Handle ``dekko export``."""
    root = Path(args.root).resolve()
    index, code = _load_or_regen(root, args.no_regen)
    if index is None:
        return code
    if args.fmt == "html":
        out = (
            Path(args.output)
            if args.output
            else root / cache_mod.CACHE_DIR / "map.html"
        )
        return render_html.run(index, out)
    out = Path(args.output) if args.output else None
    return export.run(index, args.fmt, args.scope, args.max_nodes, out)


def run_serve(args: argparse.Namespace) -> int:
    """Handle ``dekko serve --mcp``."""
    if not args.mcp:
        print(
            "dekko: serve requires --mcp (the only transport)",
            file=sys.stderr,
        )
        return 2
    return server.serve(Path(args.root), no_regen=args.no_regen)


def run_status(args: argparse.Namespace) -> int:
    """Handle ``dekko status`` (never regenerates)."""
    root = Path(args.root).resolve()
    index = mapfile.load_map(root)
    if index is None:
        if args.as_json:
            print(json.dumps({"status": "missing"}))
        else:
            print(
                f"dekko: no map.json under {root} - run `dekko map`",
                file=sys.stderr,
            )
        return 1

    fresh = mapfile.check_freshness(root, index)
    if args.as_json:
        doc = {
            "status": "fresh" if fresh.fresh else "stale",
            "added": fresh.added,
            "removed": fresh.removed,
            "changed": fresh.changed,
        }
        print(json.dumps(doc, indent=2))
        return 0 if fresh.fresh else 1

    if fresh.fresh:
        prov = index.provenance or {}
        commit = (prov.get("git_commit") or "no git")[:12]
        n = len(prov.get("files", {}))
        print(f"dekko: map fresh ({n} files, commit {commit})")
        return 0

    print("dekko: map is stale")
    for title, items in (
        ("added", fresh.added),
        ("changed", fresh.changed),
        ("removed", fresh.removed),
    ):
        for path in items[:10]:
            print(f"  {title}: {path}")
        if len(items) > 10:
            print(f"  ... and {len(items) - 10} more {title}")
    return 1


def _legacy_main(args_list: list[str]) -> int:
    """Parse and dispatch the legacy flag-based invocation."""
    parser = build_legacy_parser()
    args = parser.parse_args(args_list)

    if args.claude_install:
        return claude_install()

    if args.claude_uninstall:
        return claude_uninstall()

    if args.mcp_install:
        return mcp_install()

    if args.mcp_uninstall:
        return mcp_uninstall()

    if args.map_dir is None:
        build_subcommand_parser().print_help()
        return 0

    args.if_stale = False
    return run_map(args)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Argument list, or ``None`` for ``sys.argv``.

    Returns:
        Process exit code.
    """
    args_list = list(sys.argv[1:] if argv is None else argv)

    if args_list and args_list[0] in SUBCOMMANDS:
        args = build_subcommand_parser().parse_args(args_list)
        return args.func(args)

    if args_list and args_list[0] in ("-h", "--help"):
        build_subcommand_parser().print_help()
        return 0

    return _legacy_main(args_list)


if __name__ == "__main__":
    sys.exit(main())
