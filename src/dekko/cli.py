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
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from importlib.metadata import version as _pkg_version
from importlib.resources import files as _pkg_files
from pathlib import Path

from . import affected
from . import cache as cache_mod
from . import classify
from . import cline as cline_mod
from . import contextpack
from . import daemon as daemon_mod
from . import diff
from . import export
from . import grammars
from . import hooks as hooks_mod
from . import languages
from . import ledger as ledger_mod
from . import mapfile
from . import notes as notes_mod
from . import orient as orient_mod
from . import outline as outline_mod
from . import query
from . import relevance
from . import render_html
from . import render_lean
from . import render_md
from . import search
from . import server
from . import stats
from . import summary
from . import trace
from . import unused
from . import walker
from . import workset as workset_mod
from .extractor import extract_file
from .extractor_generic import extract_file_generic
from .model import TYPE_KINDS, CallGraph, FileMap
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
    "search",
    "status",
    "ledger",
    "hooks",
    "daemon",
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
        "--dry-run",
        action="store_true",
        help="with --claude-install/--claude-uninstall, print the "
        "command(s) that would run instead of running them",
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
        "--cline-install",
        action="store_true",
        help="register the MCP server in Cline's cline_mcp_settings.json",
    )
    parser.add_argument(
        "--cline-uninstall",
        action="store_true",
        help="remove the MCP server from Cline's cline_mcp_settings.json",
    )
    parser.add_argument(
        "--cline-scope",
        choices=cline_mod.SCOPES,
        default="vscode",
        help="Cline install to target: the VS Code extension "
        "(default) or a standalone cline CLI's global config",
    )
    parser.add_argument(
        "--cline-config",
        default=None,
        metavar="PATH",
        help="explicit cline_mcp_settings.json path, overriding "
        "--cline-scope auto-detection",
    )
    parser.add_argument(
        "--cline-force",
        action="store_true",
        help="reset a malformed cline_mcp_settings.json instead of "
        "aborting (--cline-install/--cline-uninstall)",
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
        help="extra glob pattern to skip (repeatable); also persisted to "
        ".dekko/.dekkoignore for future bare runs — note that patterns "
        "there are matched with gitignore (not fnmatch) semantics, so "
        "e.g. 'dir/*.py' stops matching nested files once persisted",
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


def _add_task_option(parser: argparse.ArgumentParser) -> None:
    """Attach the ``--task`` relevance flag (Pillar B)."""
    parser.add_argument(
        "--task",
        default=None,
        metavar="TEXT",
        help="rank output by relevance to this task description, blended "
        "with structural centrality and the working diff",
    )


def build_subcommand_parser() -> argparse.ArgumentParser:
    """Construct the subcommand parser (map/query/context/status)."""
    parser = argparse.ArgumentParser(
        prog="dekko",
        description=("Generate and query MAP.md/map.json for a repository."),
        epilog=(
            "note: 'map' takes DIR positionally; every other command "
            "uses --root DIR\n"
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
        "(for uses) an external base identifier; append "
        "':LINE' (file.py:Class.method:LINE) to pick one candidate out "
        "of an overload set the ambiguous-candidate error reports",
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
    _add_task_option(p_ctx)
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
    p_diff.add_argument(
        "--jobs",
        type=int,
        default=1,
        metavar="N",
        help="parallel workers for a rev-cache-miss old-side re-parse/"
        "resolve (1 = sequential, 0 = all cores)",
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
        default=affected.DEFAULT_BUDGET,
        metavar="TOKENS",
        help="approximate token budget; drops weakest-tier files first "
        f"(default: {affected.DEFAULT_BUDGET})",
    )
    p_affected.add_argument(
        "--jobs",
        type=int,
        default=1,
        metavar="N",
        help="parallel workers for a rev-cache-miss old-side re-parse/"
        "resolve (1 = sequential, 0 = all cores)",
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
    p_workset.add_argument(
        "--jobs",
        type=int,
        default=1,
        metavar="N",
        help="parallel workers for a rev-cache-miss old-side re-parse/"
        "resolve on a rev seed (1 = sequential, 0 = all cores); no "
        "effect on a --symbol seed",
    )
    _add_task_option(p_workset)
    p_workset.set_defaults(func=run_workset)

    p_search = sub.add_parser(
        "search",
        help="free-text relevance search over every symbol in the map",
    )
    p_search.add_argument(
        "query",
        nargs="+",
        help="free-text description of the code you're looking for "
        "(quoting is optional; unquoted words are joined with spaces)",
    )
    p_search.add_argument(
        "--limit",
        type=int,
        default=search.DEFAULT_LIMIT,
        help=f"max hits to return (default: {search.DEFAULT_LIMIT})",
    )
    p_search.add_argument(
        "--budget",
        type=int,
        default=search.DEFAULT_BUDGET,
        metavar="TOKENS",
        help="approximate token budget for the rendered output "
        f"(default: {search.DEFAULT_BUDGET})",
    )
    p_search.add_argument(
        "--kind",
        default=None,
        metavar="KIND[,KIND...]",
        help="restrict to these comma-separated symbol kinds "
        "(function, method, class, ...; default: all kinds)",
    )
    p_search.add_argument(
        "--include-tests",
        action="store_true",
        help="include test-path symbols (default: excluded)",
    )
    p_search.add_argument(
        "--scorer",
        choices=list(search.SCORER_CHOICES),
        default=search.DEFAULT_SCORER,
        help="relevance scorer: 'lexical' (default, BM25, always "
        "available) or 'embedding' (Phase 2, hashing-trick "
        "embedding, requires `pip install dekko[search]`)",
    )
    p_search.add_argument(
        "--root",
        default=".",
        metavar="DIR",
        help="repo root containing map.json (default: cwd)",
    )
    p_search.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="emit structured JSON",
    )
    p_search.add_argument(
        "--no-regen",
        action="store_true",
        help="fail (exit 5) instead of regenerating a stale map",
    )
    p_search.set_defaults(func=run_search)

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

    p_ledger = sub.add_parser(
        "ledger",
        help="what this session has put in context (from the transcript)",
    )
    p_ledger.add_argument(
        "--transcript",
        default=None,
        metavar="PATH",
        help="session JSONL to read (default: latest for this repo under "
        "~/.claude)",
    )
    p_ledger.add_argument(
        "--session",
        default=None,
        metavar="ID",
        help="resolve a specific session id when discovering a transcript",
    )
    p_ledger.add_argument(
        "--budget",
        type=int,
        default=None,
        metavar="TOKENS",
        help="report remaining tokens against this session budget",
    )
    p_ledger.add_argument(
        "--root",
        default=".",
        metavar="DIR",
        help="repo root containing map.json (default: cwd)",
    )
    p_ledger.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="emit structured JSON",
    )
    p_ledger.set_defaults(func=run_ledger)

    p_hooks = sub.add_parser(
        "hooks",
        help="manage opt-in Claude Code push hooks (orientation, "
        "task pointers, read advisories)",
    )
    hooks_sub = p_hooks.add_subparsers(
        dest="hooks_action", required=True, metavar="ACTION"
    )
    p_hooks_install = hooks_sub.add_parser(
        "install",
        help="enable dekko hooks in .claude/settings.json (opt-in)",
    )
    p_hooks_install.add_argument(
        "--enable",
        action="append",
        choices=list(hooks_mod.EVENTS),
        default=None,
        metavar="EVENT",
        help="hook event(s) to enable; repeatable (default: session-start)",
    )
    p_hooks_install.add_argument(
        "--root",
        default=".",
        metavar="DIR",
        help="repo root whose .claude/settings.json to edit (default: cwd)",
    )
    p_hooks_install.set_defaults(func=run_hooks_install)
    p_hooks_uninstall = hooks_sub.add_parser(
        "uninstall", help="remove all dekko hooks from .claude/settings.json"
    )
    p_hooks_uninstall.add_argument(
        "--root",
        default=".",
        metavar="DIR",
        help="repo root whose .claude/settings.json to edit (default: cwd)",
    )
    p_hooks_uninstall.set_defaults(func=run_hooks_uninstall)
    p_hooks_run = hooks_sub.add_parser(
        "run", help="execute a hook handler (reads event JSON on stdin)"
    )
    p_hooks_run.add_argument("event", choices=list(hooks_mod.EVENTS))
    p_hooks_run.set_defaults(func=run_hooks_run)

    p_daemon = sub.add_parser(
        "daemon",
        help="manage the per-repo warm-cache daemon (start/stop/status)",
    )
    daemon_sub = p_daemon.add_subparsers(
        dest="daemon_action", required=True, metavar="ACTION"
    )
    p_daemon_start = daemon_sub.add_parser(
        "start", help="start the daemon for this repo root"
    )
    p_daemon_start.add_argument(
        "--root",
        default=".",
        metavar="DIR",
        help="repo root to serve (default: cwd)",
    )
    p_daemon_start.add_argument(
        "--idle-timeout",
        type=float,
        default=daemon_mod.DEFAULT_IDLE_TIMEOUT,
        metavar="SECONDS",
        help="self-shutdown after this many idle seconds "
        f"(default: {daemon_mod.DEFAULT_IDLE_TIMEOUT:.0f})",
    )
    p_daemon_start.set_defaults(func=run_daemon_start)

    p_daemon_stop = daemon_sub.add_parser(
        "stop", help="stop the daemon for this repo root"
    )
    p_daemon_stop.add_argument(
        "--root",
        default=".",
        metavar="DIR",
        help="repo root whose daemon to stop (default: cwd)",
    )
    p_daemon_stop.set_defaults(func=run_daemon_stop)

    p_daemon_status = daemon_sub.add_parser(
        "status",
        help="report whether a daemon is running for this repo root",
    )
    p_daemon_status.add_argument(
        "--root",
        default=".",
        metavar="DIR",
        help="repo root to check (default: cwd)",
    )
    p_daemon_status.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="emit structured JSON",
    )
    p_daemon_status.set_defaults(func=run_daemon_status)

    p_daemon_serve = daemon_sub.add_parser("_serve", help=argparse.SUPPRESS)
    p_daemon_serve.add_argument("--root", default=".", metavar="DIR")
    p_daemon_serve.add_argument(
        "--idle-timeout",
        type=float,
        default=daemon_mod.DEFAULT_IDLE_TIMEOUT,
        metavar="SECONDS",
    )
    p_daemon_serve.set_defaults(func=run_daemon_serve)

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
    p_summary.add_argument(
        "--budget",
        type=int,
        default=summary.DEFAULT_BUDGET,
        metavar="TOKENS",
        help="approximate token cap; trailing sections are shed to fit "
        f"(default: {summary.DEFAULT_BUDGET})",
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
    p_lean.add_argument(
        "--dense",
        action="store_true",
        help="terser skin: signatures only on the most-central symbols, "
        "names for the rest (FR-D1)",
    )
    _add_task_option(p_lean)
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
    p_note_rm = note_sub.add_parser(
        "rm", aliases=["remove"], help="remove a note from a symbol"
    )
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
        help="node granularity for the whole rendered graph -- symbol "
        "or file (default: symbol); does not scope the graph to a "
        "single symbol's neighborhood, use 'dekko context' for that",
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


def _run_subprocess(cmd: list[str]) -> subprocess.CompletedProcess:
    """Run a CLI command, capturing its output as text."""
    return subprocess.run(cmd, capture_output=True, text=True)


def _claude_exe() -> str | None:
    """Resolve the ``claude`` CLI to its full path, or warn and None.

    Returns the absolute path so callers invoke the resolved executable
    rather than the bare name. On Windows ``subprocess.run`` will not
    launch a ``claude.cmd`` shim found only by name; the full path that
    ``shutil.which`` returns does work.
    """
    exe = shutil.which("claude")
    if exe is None:
        print(
            "dekko: 'claude' CLI not found on PATH. Install Claude Code "
            "first: https://claude.com/claude-code",
            file=sys.stderr,
        )
    return exe


def claude_install(dry_run: bool = False) -> int:
    """Register the bundled plugin with the Claude Code CLI.

    Args:
        dry_run: Print the command(s) that would run instead of running
            them; leaves Claude Code's config untouched.

    Returns:
        Process exit code.
    """
    exe = _claude_exe()
    if exe is None:
        return 1

    plugin_dir = Path(str(_pkg_files("dekko"))) / "_plugin"
    if not (plugin_dir / ".claude-plugin").is_dir():
        print(
            f"dekko: bundled plugin not found at {plugin_dir}",
            file=sys.stderr,
        )
        return 1

    if dry_run:
        print("dekko: --dry-run, would run:")
        print(f"  {exe} plugin marketplace add {plugin_dir}")
        print(f"  {exe} plugin install dekko@dekko")
        return 0

    added = _run_subprocess(
        [exe, "plugin", "marketplace", "add", str(plugin_dir)]
    )
    if added.returncode != 0:
        # Likely already registered (e.g. a previous install or a dev
        # checkout): refresh it instead.
        updated = _run_subprocess(
            [exe, "plugin", "marketplace", "update", "dekko"]
        )
        if updated.returncode != 0:
            print(added.stderr.strip(), file=sys.stderr)
            print(updated.stderr.strip(), file=sys.stderr)
            return 1

    installed = _run_subprocess([exe, "plugin", "install", "dekko@dekko"])
    if installed.returncode != 0:
        print(installed.stderr.strip(), file=sys.stderr)
        return 1

    print("dekko: plugin installed. Restart Claude Code to activate /map.")
    return 0


def claude_uninstall(dry_run: bool = False) -> int:
    """Remove the bundled plugin from the Claude Code CLI.

    Reverses :func:`claude_install`: uninstalls the ``dekko`` plugin and
    drops its marketplace registration. A step that reports the plugin or
    marketplace is already absent is surfaced as a warning rather than a
    failure, so the command is safe to run on a partial install.

    Args:
        dry_run: Print the command(s) that would run instead of running
            them; leaves Claude Code's config untouched.

    Returns:
        Process exit code (``1`` only when the ``claude`` CLI is missing).
    """
    exe = _claude_exe()
    if exe is None:
        return 1

    if dry_run:
        print("dekko: --dry-run, would run:")
        for cmd in (
            [exe, "plugin", "uninstall", "dekko@dekko"],
            [exe, "plugin", "marketplace", "remove", "dekko"],
        ):
            print(f"  {' '.join(cmd)}")
        return 0

    for cmd in (
        [exe, "plugin", "uninstall", "dekko@dekko"],
        [exe, "plugin", "marketplace", "remove", "dekko"],
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
    exe = _claude_exe()
    if exe is None:
        return 1

    added = _run_subprocess(
        [exe, "mcp", "add", "dekko", "--", "dekko", "serve", "--mcp"]
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
    exe = _claude_exe()
    if exe is None:
        return 1

    removed = _run_subprocess([exe, "mcp", "remove", "dekko"])
    if removed.returncode != 0:
        print(
            "dekko: 'claude mcp remove dekko' failed (already removed?): "
            f"{removed.stderr.strip()}",
            file=sys.stderr,
        )
        return 0

    print("dekko: MCP server 'dekko' removed. Restart Claude Code.")
    return 0


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
    still be stale even when the cache looks fully warm.

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

    graph = resolve(files, workers=_resolve_workers(getattr(args, "jobs", 1)))
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
        render_json(files, graph, label, provenance).encode("utf-8"),
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
# ``.features/daemon-mode/``). ``_load_or_regen`` is the single
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


def _load_or_regen(
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

    code = regen_map(root, quiet=True)
    if code != 0:
        return None, code
    index = mapfile.load_map(root)
    if index is not None and _daemon_cache_put is not None:
        _daemon_cache_put(root, index)
    return index, 0


def load_current_index_no_regen(root: Path) -> mapfile.MapIndex | None:
    """Load the current-tree map, checking the daemon's warm cache first.

    ``diff.run``/``affected.changes`` are the one partial exception to
    ``_load_or_regen`` being the single daemon-cache chokepoint every
    other read subcommand funnels through (see
    ``.features/daemon-mode/daemon-mode-cli-plan.md`` §2.4's last
    bullet and Phase 4 of ``.features/daemon-mode/TRACKER.md``): their
    current-tree side calls ``mapfile.load_map`` directly, so a
    daemon-routed ``diff``/``affected`` request previously always paid
    a full JSON-parse/index-rebuild, even with a warm cache populated
    by a prior ``query``/``search``/... request against the same
    root. This function is the fix — it checks the same
    ``_daemon_cache_get``/``_daemon_cache_put`` hooks
    ``_load_or_regen`` uses, so a cache hit here skips the reload the
    same way it would for any other daemon-eligible command.

    It deliberately does **not** reuse ``_load_or_regen`` itself,
    because that function's stale/missing-map behavior is to call
    ``regen_map`` (writing a fresh ``map.json`` to disk) — a side
    effect ``diff``/``affected`` don't want and have never had: they
    already tolerate a stale on-disk index by falling back to an
    in-memory re-parse (``diff.snapshot_new_side`` -> ``diff.
    snapshot()``) that never touches ``map.json``. Adopting
    ``_load_or_regen``'s regen-on-stale behavior here would be a
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
    root = Path(args.root).resolve()
    task = relevance.task_context(args.task, root) if args.task else None
    return contextpack.run(
        index,
        args.target,
        hops=args.hops,
        budget=args.budget,
        as_json=args.as_json,
        root=root,
        with_source=args.with_source,
        notes=args.notes,
        task=task,
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
        jobs=_resolve_workers(getattr(args, "jobs", 1)),
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
        jobs=_resolve_workers(getattr(args, "jobs", 1)),
    )


def run_workset(args: argparse.Namespace) -> int:
    """Handle ``dekko workset [REV] | --symbol NAME``."""
    if args.symbol is not None and args.rev is not None:
        print("dekko: give a REV or --symbol, not both", file=sys.stderr)
        return workset_mod.EXIT_ERROR
    root = Path(args.root).resolve()
    task = relevance.task_context(args.task, root) if args.task else None
    return workset_mod.run(
        root,
        args.rev,
        args.symbol,
        budget=args.budget,
        packs=args.packs,
        as_json=args.as_json,
        no_regen=args.no_regen,
        task=task,
        jobs=_resolve_workers(getattr(args, "jobs", 1)),
    )


def run_search(args: argparse.Namespace) -> int:
    """Handle ``dekko search "<query>"``.

    Unlike the other read commands, ``search`` defaults to *excluding*
    test-path symbols (opt in with ``--include-tests``) rather than
    the ``--no-tests`` opt-out convention every other read command
    uses — a relevance-ranked result competing for a rank slot
    shouldn't default to including test noise the way an exhaustive
    caller list should default to completeness (deliberate deviation,
    see the search feature plan §9.6).
    """
    query_text = " ".join(args.query)
    root = Path(args.root).resolve()
    index, code = _load_or_regen(root, args.no_regen)
    if index is None:
        return code
    excluded_test_count = 0
    if not args.include_tests:
        filtered = index.without_tests()
        excluded_test_count = len(index.symbols_by_id) - len(
            filtered.symbols_by_id
        )
        index = filtered
    kinds = search.parse_kinds(args.kind)
    return search.run(
        index,
        query_text,
        kinds=kinds,
        limit=args.limit,
        budget=args.budget,
        as_json=args.as_json,
        root=root,
        scorer_name=args.scorer,
        excluded_test_count=excluded_test_count,
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
    return summary.run(index, as_json=args.as_json, budget=args.budget)


def run_lean(args: argparse.Namespace) -> int:
    """Handle ``dekko lean``."""
    index, code = _read_index(args)
    if index is None:
        return code
    out = Path(args.output).resolve() if args.output else None
    root = Path(args.root).resolve()
    task = relevance.task_context(args.task, root) if args.task else None
    return render_lean.run(
        index,
        root,
        budget=args.budget,
        as_json=args.as_json,
        out_path=out,
        task=task,
        dense=args.dense,
    )


def run_ledger(args: argparse.Namespace) -> int:
    """Handle ``dekko ledger``."""
    transcript = Path(args.transcript).resolve() if args.transcript else None
    return ledger_mod.run(
        Path(args.root).resolve(),
        transcript,
        args.session,
        args.budget,
        as_json=args.as_json,
    )


def run_hooks_install(args: argparse.Namespace) -> int:
    """Handle ``dekko hooks install``."""
    events = args.enable or ["session-start"]
    return hooks_mod.install(Path(args.root).resolve(), events)


def run_hooks_uninstall(args: argparse.Namespace) -> int:
    """Handle ``dekko hooks uninstall``."""
    return hooks_mod.uninstall(Path(args.root).resolve())


def run_hooks_run(args: argparse.Namespace) -> int:
    """Handle ``dekko hooks run <event>`` (reads JSON on stdin)."""
    return hooks_mod.dispatch(args.event, sys.stdin.read())


def run_daemon_start(args: argparse.Namespace) -> int:
    """Handle ``dekko daemon start``."""
    return daemon_mod.start(Path(args.root).resolve(), args.idle_timeout)


def run_daemon_stop(args: argparse.Namespace) -> int:
    """Handle ``dekko daemon stop``."""
    return daemon_mod.stop(Path(args.root).resolve())


def run_daemon_status(args: argparse.Namespace) -> int:
    """Handle ``dekko daemon status``."""
    return daemon_mod.status(Path(args.root).resolve(), args.as_json)


def run_daemon_serve(args: argparse.Namespace) -> int:
    """Handle ``dekko daemon _serve`` (internal daemon process entry).

    Not meant to be invoked directly by a human -- this is the
    command ``daemon.start()`` spawns as a detached background
    process; it blocks in ``serve_daemon``'s accept loop until an
    explicit ``dekko daemon stop`` or the idle timeout fires.
    """
    return daemon_mod.serve_daemon(
        Path(args.root).resolve(), args.idle_timeout
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
    if args.note_action in ("rm", "remove"):
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
        return None, query.report_unresolved(target, candidates, index)
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
    """Handle ``dekko status`` (never regenerates).

    Reads only the small provenance sidecar (``mapfile.
    load_provenance``) rather than the full ``map.json`` — this
    command only ever needs the freshness stamp, not the parsed
    symbol/call graph other read commands pay to build. Falls back to
    a full ``mapfile.load_map`` for maps written before the sidecar
    existed (or with a missing/corrupt one), matching its prior
    behavior exactly.
    """
    root = Path(args.root).resolve()
    prov = mapfile.load_provenance(root)
    if prov is not None:
        fresh = mapfile.check_freshness_provenance(root, prov)
    else:
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
        prov = index.provenance

    unsupported = (prov or {}).get("unsupported")
    if args.as_json:
        doc = {
            "status": "fresh" if fresh.fresh else "stale",
            "reason": fresh.reason,
            "added": fresh.added,
            "removed": fresh.removed,
            "changed": fresh.changed,
            "unsupported": unsupported,
        }
        print(json.dumps(doc, indent=2))
        return 0 if fresh.fresh else 1

    if fresh.fresh:
        _print_fresh_status(prov)
        return 0

    _print_stale_status(fresh, prov)
    return 1


def _print_fresh_status(provenance: dict | None) -> None:
    """Print the one-line ``dekko status`` message for a fresh map."""
    prov = provenance or {}
    commit = (prov.get("git_commit") or "no git")[:12]
    n = len(prov.get("files", {}))
    print(f"dekko: map fresh ({n} files, commit {commit})")
    note = mapfile.format_unsupported(prov)
    if note:
        print(f"  {note}")


def _print_stale_status(
    fresh: mapfile.Freshness, provenance: dict | None
) -> None:
    """Print the ``dekko status`` message for a stale map.

    A ``reason="version"`` verdict has no added/removed/changed lists
    to show (``check_freshness`` returns immediately on a version
    mismatch, before diffing file content) — print the actionable
    built-vs-running note instead.
    """
    print("dekko: map is stale")
    if fresh.reason == "version":
        print(f"  {_version_stale_note(provenance)}")
        return
    note = mapfile.format_unsupported(provenance)
    if note:
        print(f"  {note}")
    for title, items in (
        ("added", fresh.added),
        ("changed", fresh.changed),
        ("removed", fresh.removed),
    ):
        for path in items[:10]:
            print(f"  {title}: {path}")
        if len(items) > 10:
            print(f"  ... and {len(items) - 10} more {title}")


def _version_stale_note(provenance: dict | None) -> str:
    """One-line explanation for a ``reason="version"`` freshness verdict."""
    built = (provenance or {}).get("tool_version", "unknown")
    running = _pkg_version("dekko")
    return f"built by dekko {built}, running {running} — run `dekko map`"


def _legacy_main(args_list: list[str]) -> int:
    """Parse and dispatch the legacy flag-based invocation."""
    parser = build_legacy_parser()
    args = parser.parse_args(args_list)

    if args.claude_install:
        return claude_install(dry_run=args.dry_run)

    if args.claude_uninstall:
        return claude_uninstall(dry_run=args.dry_run)

    if args.mcp_install:
        return mcp_install()

    if args.mcp_uninstall:
        return mcp_uninstall()

    if args.cline_install:
        config = Path(args.cline_config) if args.cline_config else None
        return cline_mod.install(
            config, args.cline_scope, force=args.cline_force
        )

    if args.cline_uninstall:
        config = Path(args.cline_config) if args.cline_config else None
        return cline_mod.uninstall(
            config, args.cline_scope, force=args.cline_force
        )

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

    no_daemon = "--no-daemon" in args_list
    if no_daemon:
        args_list = [a for a in args_list if a != "--no-daemon"]

    if args_list and args_list[0] in SUBCOMMANDS:
        args = build_subcommand_parser().parse_args(args_list)
        if not no_daemon:
            routed = daemon_mod.try_daemon(args)
            if routed is not None:
                exit_code, out, err = routed
                if out:
                    sys.stdout.write(out)
                if err:
                    sys.stderr.write(err)
                return exit_code
        return args.func(args)

    if args_list and args_list[0] in ("-h", "--help"):
        build_subcommand_parser().print_help()
        return 0

    return _legacy_main(args_list)


if __name__ == "__main__":
    sys.exit(main())
