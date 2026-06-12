"""lidar: programmatically map a repository into MAP.md/map.json.

Walks the repo, parses every supported source file with tree-sitter,
extracts functions/parameters/types, resolves call relationships, and
writes a human-readable MAP.md plus a machine-readable map.json.
"""

import argparse
import shutil
import subprocess
import sys
from collections import Counter
from importlib.metadata import version as _pkg_version
from importlib.resources import files as _pkg_files
from pathlib import Path

from . import languages
from . import walker
from .extractor import extract_file
from .extractor_generic import extract_file_generic
from .model import FileMap
from .render_json import render_json
from .render_md import render_markdown
from .resolver import resolve


def build_arg_parser() -> argparse.ArgumentParser:
    """Construct the CLI argument parser."""
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
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(render_json(files, graph, label))
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


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Argument list, or ``None`` for ``sys.argv``.

    Returns:
        Process exit code.
    """
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.claude_install:
        return claude_install()

    if args.map_dir is None:
        parser.print_help()
        return 0

    return run_map(args)


if __name__ == "__main__":
    sys.exit(main())
