"""Module-level dependency graph report: ``dekko deps``.

File/package-granularity import dependencies — distinct from every
other dekko command's symbol or single-file granularity. Answers
"which files import which files" (a build-time/compile-time coupling
question) rather than "which files call into which files" (the
runtime call-graph view ``export.py``'s ``dir_graph``/``_file_graph``
already answer) — the two views usually roughly agree but can diverge:
a file can import a module purely for a type annotation or a
side-effect (``import "./polyfill"``) with zero calls ever crossing
that edge.

No new extraction happens at query time — ``resolver.resolve_imports``
already resolved every import to an in-repo file (or left it external)
once, at ``dekko map`` time, the same way call/heritage resolution
already does; this module only reads ``MapIndex.module_deps_out``/
``module_deps_in``/``module_external`` and runs cycle detection
(``resolver.find_cycles``) over the loaded adjacency, mirroring
``ambiguous.py``'s/``stats.py``'s "read-only repo-wide report" shape
rather than a single-target relational lookup like ``query
callers``/``callees``.
"""

import json
import sys
from pathlib import Path

from dekko.core.resolver import find_cycles
from dekko.render import export
from dekko.render.mapfile import MapIndex
from dekko.textutil import fit_to_budget, token_footer

EXIT_OK = 0
EXIT_ERROR = 2
EXIT_NOT_FOUND = 3
EXIT_TOO_BIG = 2


def compute(index: MapIndex, top: int) -> dict:
    """Build the full ``dekko deps`` summary document.

    Args:
        index: Loaded map index.
        top: How many entries to keep in the most-depended-on ranking.

    Returns:
        A JSON-serializable report dict.
    """
    files = sorted(index.languages_by_path)
    edge_count = sum(len(v) for v in index.module_deps_out.values())
    external_count = sum(len(v) for v in index.module_external.values())
    external_only = sum(
        1
        for p in files
        if not index.module_deps_out.get(p) and p in index.module_external
    )
    cycles = find_cycles(index.module_deps_out)
    multi = [c for c in cycles if len(c) >= 2]
    self_cycles = [c for c in cycles if len(c) == 1]
    ranked_in = sorted(
        ((p, len(v)) for p, v in index.module_deps_in.items()),
        key=lambda row: (-row[1], row[0]),
    )
    return {
        "files": len(files),
        "edges": edge_count,
        "external_sources": external_count,
        "external_only_files": external_only,
        "cycles": len(multi),
        "cycle_files": sum(len(c) for c in multi),
        "self_cycles": len(self_cycles),
        "top_by_deps_in": [
            {"path": p, "count": n} for p, n in ranked_in[:top]
        ],
    }


def _print_summary_text(doc: dict) -> None:
    """Print the default (no ``--file``/``--cycles``) text summary."""
    lines = [
        f"dekko: {doc['files']} files, {doc['edges']} resolved import "
        f"edges, {doc['external_sources']} external sources across "
        f"{doc['external_only_files']} external-only files",
    ]
    if doc["cycles"] or doc["self_cycles"]:
        parts = []
        if doc["cycles"]:
            parts.append(
                f"{doc['cycles']} cycles ({doc['cycle_files']} files)"
            )
        if doc["self_cycles"]:
            parts.append(f"{doc['self_cycles']} self-import(s)")
        lines.append(
            f"  {', '.join(parts)} detected — see `dekko deps --cycles`"
        )
    lines.append("")
    lines.append("most-depended-on files:")
    lines.extend(
        f"    {row['count']:>4}  {row['path']}"
        for row in doc["top_by_deps_in"]
    )
    text = "\n".join(lines)
    print(text)
    print(token_footer(text))


def _run_summary(index: MapIndex, top: int, as_json: bool) -> int:
    """Handle the default (no ``--file``/``--cycles``) summary view."""
    doc = compute(index, top)
    if as_json:
        print(json.dumps(doc, indent=2))
        return EXIT_OK
    if doc["files"] == 0:
        print("dekko: no mapped files")
        return EXIT_OK
    _print_summary_text(doc)
    return EXIT_OK


def _run_file(
    index: MapIndex, path: str, limit: int, budget: int | None, as_json: bool
) -> int:
    """Handle ``--file PATH``: one file's imports/importers/external."""
    if path not in index.languages_by_path:
        print(f"dekko: no mapped file '{path}'", file=sys.stderr)
        return EXIT_NOT_FOUND

    imports = index.module_deps_out.get(path, [])
    imported_by = index.module_deps_in.get(path, [])
    external = index.module_external.get(path, [])
    if as_json:
        doc = {
            "path": path,
            "imports": imports,
            "imported_by": imported_by,
            "external": external,
        }
        print(json.dumps(doc, indent=2))
        return EXIT_OK

    rows = [f"    {p}" for p in imports]
    kept_imports, meter_imports = fit_to_budget(rows, budget, limit)
    rows_in = [f"    {p}" for p in imported_by]
    kept_in, meter_in = fit_to_budget(rows_in, budget, limit)

    print(f"imports ({len(imports)}):")
    for row in kept_imports:
        print(row)
    if meter_imports.omitted:
        print(f"    {meter_imports.footer()}")
    print(f"imported by ({len(imported_by)}):")
    for row in kept_in:
        print(row)
    if meter_in.omitted:
        print(f"    {meter_in.footer()}")
    if external:
        print(f"external ({len(external)}): {', '.join(external)}")
    else:
        print("external (0):")
    return EXIT_OK


def _cycle_label(cycle: list[str]) -> str:
    """One text block for a single cycle (multi-file or self-import)."""
    if len(cycle) == 1:
        return f"  {cycle[0]}  (self-import)"
    chain = " -> ".join([*cycle, cycle[0]])
    return f"  {chain}"


def _run_cycles(
    index: MapIndex, limit: int, budget: int | None, as_json: bool
) -> int:
    """Handle ``--cycles``: every detected circular-import cluster."""
    cycles = find_cycles(index.module_deps_out)
    if as_json:
        entries = [{"files": c, "self_import": len(c) == 1} for c in cycles]
        serialized = [json.dumps(e) for e in entries]
        kept_ser, meter = fit_to_budget(serialized, budget, limit)
        doc = {"results": entries[: len(kept_ser)], "meta": meter.as_dict()}
        print(json.dumps(doc, indent=2))
        return EXIT_OK

    if not cycles:
        print("dekko: no circular imports detected")
        return EXIT_OK
    rows = [
        f"cycle {i} ({len(c)} file{'s' if len(c) != 1 else ''}):\n"
        f"{_cycle_label(c)}"
        for i, c in enumerate(cycles, start=1)
    ]
    kept, meter = fit_to_budget(rows, budget, limit)
    for row in kept:
        print(row)
    print(meter.footer())
    return EXIT_OK


def _module_graph_pairs(
    index: MapIndex,
) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """Build ``(labels, edges)`` for the module graph, matching the
    shape ``export.render_mermaid``/``export.render_dot`` already
    accept — export.py's own renderers are reused verbatim, not
    reimplemented, per this design's own reuse plan.
    """
    edges = sorted(
        (importer, imported)
        for importer, targets in index.module_deps_out.items()
        for imported in targets
    )
    labels = {node: node for edge in edges for node in edge}
    return labels, edges


def _run_export(
    index: MapIndex, fmt: str, max_nodes: int, out_path: Path | None
) -> int:
    """Handle ``--export {mermaid,dot}``."""
    labels, edges = _module_graph_pairs(index)
    if len(labels) > max_nodes:
        print(
            f"dekko: graph has {len(labels)} nodes (limit {max_nodes}); "
            "use a subtree map or raise --max-nodes",
            file=sys.stderr,
        )
        return EXIT_TOO_BIG
    render = export.render_dot if fmt == "dot" else export.render_mermaid
    output = render(labels, edges)
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output + "\n", encoding="utf-8")
        print(f"dekko: wrote {out_path}")
    else:
        print(output)
    return EXIT_OK


def run(
    index: MapIndex,
    file: str | None,
    cycles: bool,
    top: int,
    limit: int,
    budget: int | None,
    as_json: bool,
    export_fmt: str | None = None,
    max_nodes: int = export.DEFAULT_MAX_NODES,
    out_path: Path | None = None,
) -> int:
    """Execute ``dekko deps`` against a loaded index.

    Callers must enforce ``file``/``cycles``/``export_fmt`` mutual
    exclusivity themselves (the CLI does this in ``cli.py``'s
    ``run_deps``, matching ``ambiguous``'s own "give one, not both"
    precedent for its ``--by``/``--name``) — this function assumes at
    most one of the three is set.

    Args:
        index: Loaded map index.
        file: Drill down to one file's imports/importers/external, or
            ``None``.
        cycles: Show every detected circular-import cluster instead of
            the default summary.
        top: Entries in the default summary's most-depended-on ranking.
        limit: Max text/JSON result rows for ``--file``/``--cycles``.
        budget: Approximate token budget for those rows, or ``None``.
        as_json: Emit structured JSON instead of text.
        export_fmt: ``"mermaid"``/``"dot"`` to emit the graph in that
            format instead of any report view, or ``None``.
        max_nodes: Refuse to render an export graph bigger than this.
        out_path: Write an export to this file instead of stdout.

    Returns:
        ``0`` on success, ``2`` on a too-big export graph, ``3`` when
        ``--file`` names an unmapped path.
    """
    if export_fmt is not None:
        return _run_export(index, export_fmt, max_nodes, out_path)
    if file is not None:
        return _run_file(index, file, limit, budget, as_json)
    if cycles:
        return _run_cycles(index, limit, budget, as_json)
    return _run_summary(index, top, as_json)
