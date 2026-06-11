# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "tree-sitter>=0.23",
#   "tree-sitter-language-pack>=0.6",
#   "pathspec>=0.12",
# ]
# ///
"""lidar: programmatically map a repository into MAP.md/map.json.

Walks the repo, parses every supported source file with tree-sitter,
extracts functions/parameters/types, resolves call relationships, and
writes a human-readable MAP.md plus a machine-readable map.json.
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

import languages
import walker
from extractor import extract_file
from extractor_generic import extract_file_generic
from model import FileMap
from render_json import render_json
from render_md import render_markdown
from resolver import resolve


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
        help="optional repo-relative subtree to map",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="repository root (default: cwd)",
    )
    parser.add_argument(
        "--output",
        default="MAP.md",
        help="markdown output path, relative to root",
    )
    parser.add_argument(
        "--json",
        dest="json_output",
        default="map.json",
        help="JSON output path, relative to root",
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
    langs = ", ".join(
        f"{lang} {n}" 
        for lang, n 
        in by_lang.most_common()
    )
    
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
            f"{reason} {n}"
            for reason, n
            in reasons.most_common()
        )

        lines.append(f"  skipped: {detail}")

    sizes = ", ".join(
        f"{p.name} ({p.stat().st_size
        / 1024:.1f} KB)" for p in outputs
    )

    lines.append(f"  wrote {sizes}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Argument list, or ``None`` for ``sys.argv``.

    Returns:
        Process exit code.
    """
    args = build_arg_parser().parse_args(argv)
    root = args.root.resolve()
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

    outputs: list[Path] = []
    md_path = root / args.output
    md_path.write_text(render_markdown(files, graph, label))
    outputs.append(md_path)
    if not args.no_json:
        json_path = root / args.json_output
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


if __name__ == "__main__":
    sys.exit(main())
