"""lidar: programmatically map a repository into MAP.md/map.json.

Walks the repo, parses every supported source file with tree-sitter,
extracts functions/parameters/types, resolves call relationships, and
writes a human-readable MAP.md plus a machine-readable map.json.
"""

import argparse
import json
import shutil
import subprocess
import sys
from collections import Counter
from importlib.metadata import version as _pkg_version
from importlib.resources import files as _pkg_files
from pathlib import Path

from . import contextpack
from . import languages
from . import mapfile
from . import query
from . import walker
from .extractor import extract_file
from .extractor_generic import extract_file_generic
from .model import FileMap
from .render_json import render_json
from .render_md import render_markdown
from .resolver import resolve


SUBCOMMANDS = ("map", "query", "context", "status")


def build_legacy_parser() -> argparse.ArgumentParser:
    """Construct the legacy flag-based parser (v0.2 aliases)."""
    parser = argparse.ArgumentParser(
        prog="lidar",
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
        help="install the lidar-map plugin into Claude Code",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"lidar {_pkg_version('lidar-map')}",
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
        "MAP.md and map.json (default: the mapped directory)",
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


def build_subcommand_parser() -> argparse.ArgumentParser:
    """Construct the subcommand parser (map/query/context/status)."""
    parser = argparse.ArgumentParser(
        prog="lidar",
        description=("Generate and query MAP.md/map.json for a repository."),
        epilog=(
            "legacy aliases: lidar --map [DIR] [SUBPATH], "
            "lidar --claude-install, lidar --version"
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
    _add_map_options(p_map)
    p_map.set_defaults(func=_cmd_map)

    p_query = sub.add_parser("query", help="query the call graph")
    p_query.add_argument("action", choices=query.ACTIONS)
    p_query.add_argument(
        "target",
        help="symbol (name, Class.method, file.py:func) or file path",
    )
    p_query.add_argument(
        "--limit",
        type=int,
        default=50,
        help="max text result lines (default: 50)",
    )
    _add_read_options(p_query)
    p_query.set_defaults(func=run_query)

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
    _add_read_options(p_ctx)
    p_ctx.set_defaults(func=run_context)

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
    return parser


def map_repository(
    root: Path,
    subpath: str | None,
    excludes: tuple[str, ...],
    max_file_size: int,
) -> tuple[list[FileMap], list[tuple[str, str]]]:
    """Discover and extract every mappable file under a root.

    Args:
        root: Repository root.
        subpath: Optional repo-relative subtree restriction.
        excludes: Extra glob patterns to skip.
        max_file_size: Size cap in bytes.

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
    file_maps: list[FileMap] = []
    for rel in paths:
        spec = languages.spec_for_path(rel)
        if spec is not None:
            file_maps.append(extract_file(root, rel, spec))
            continue

        grammar = languages.tier2_grammar_for_path(rel)
        if grammar is not None:
            file_maps.append(extract_file_generic(root, rel, grammar))

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
        md_path = root / "MAP.md"
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
        f"lidar: mapped {len(files)} files ({langs})",
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

    sizes = ", ".join(
        f"{p.name} ({p.stat().st_size / 1024:.1f} KB)" for p in outputs
    )

    lines.append(f"  wrote {sizes}")
    return "\n".join(lines)


def _run_subprocess(cmd: list[str]) -> subprocess.CompletedProcess:
    """Run a CLI command, capturing its output as text."""
    return subprocess.run(cmd, capture_output=True, text=True)


def claude_install() -> int:
    """Register the bundled plugin with the Claude Code CLI.

    Returns:
        Process exit code.
    """
    if shutil.which("claude") is None:
        print(
            "lidar: 'claude' CLI not found on PATH. Install Claude Code "
            "first: https://claude.com/claude-code",
            file=sys.stderr,
        )
        return 1

    plugin_dir = Path(str(_pkg_files("lidar_map"))) / "_plugin"
    if not (plugin_dir / ".claude-plugin").is_dir():
        print(
            f"lidar: bundled plugin not found at {plugin_dir}",
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
            ["claude", "plugin", "marketplace", "update", "lidar-map"]
        )
        if updated.returncode != 0:
            print(added.stderr.strip(), file=sys.stderr)
            print(updated.stderr.strip(), file=sys.stderr)
            return 1

    installed = _run_subprocess(
        ["claude", "plugin", "install", "lidar-map@lidar-map"]
    )
    if installed.returncode != 0:
        print(installed.stderr.strip(), file=sys.stderr)
        return 1

    print("lidar: plugin installed. Restart Claude Code to activate /map.")
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
        print(f"lidar: not a directory: {root}", file=sys.stderr)
        return 2

    if getattr(args, "if_stale", False) and _map_is_fresh(root, args):
        return 0

    files, skipped = map_repository(
        root,
        subpath=args.subpath,
        excludes=tuple(args.exclude),
        max_file_size=args.max_file_size,
    )
    if not files:
        print(
            f"lidar: no supported source files found under {root}",
            file=sys.stderr,
        )
        return 1

    graph = resolve(files)
    label = root.name + (f"/{args.subpath}" if args.subpath else "")

    md_path, json_path = resolve_outputs(root, args.output, args.json_output)

    outputs: list[Path] = []
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(render_markdown(files, graph, label))
    outputs.append(md_path)
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
        print(f"lidar: map fresh ({n} files, commit {commit})")
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
            f"lidar: map.json missing or stale under {root} "
            "(run `lidar map`, or drop --no-regen)",
            file=sys.stderr,
        )
        return None, 5

    prov = (index.provenance if index else None) or {}
    regen_args = argparse.Namespace(
        map_dir=str(root),
        subpath=prov.get("subpath"),
        exclude=list(prov.get("excludes", [])),
        max_file_size=prov.get("max_file_size", walker.DEFAULT_MAX_FILE_SIZE),
        output=None,
        json_output=None,
        no_json=False,
        quiet=True,
        if_stale=False,
    )
    code = run_map(regen_args)
    if code != 0:
        return None, code
    return mapfile.load_map(root), 0


def _cmd_map(args: argparse.Namespace) -> int:
    """Adapter: ``lidar map DIR`` → ``run_map`` namespace."""
    args.map_dir = args.dir
    return run_map(args)


def run_query(args: argparse.Namespace) -> int:
    """Handle ``lidar query``."""
    root = Path(args.root).resolve()
    index, code = _load_or_regen(root, args.no_regen)
    if index is None:
        return code
    return query.run(
        index,
        args.action,
        args.target,
        as_json=args.as_json,
        limit=args.limit,
    )


def run_context(args: argparse.Namespace) -> int:
    """Handle ``lidar context``."""
    root = Path(args.root).resolve()
    index, code = _load_or_regen(root, args.no_regen)
    if index is None:
        return code
    return contextpack.run(
        index,
        args.target,
        hops=args.hops,
        budget=args.budget,
        as_json=args.as_json,
    )


def run_status(args: argparse.Namespace) -> int:
    """Handle ``lidar status`` (never regenerates)."""
    root = Path(args.root).resolve()
    index = mapfile.load_map(root)
    if index is None:
        if args.as_json:
            print(json.dumps({"status": "missing"}))
        else:
            print(
                f"lidar: no map.json under {root} - run `lidar map`",
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
        print(f"lidar: map fresh ({n} files, commit {commit})")
        return 0

    print("lidar: map is stale")
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
