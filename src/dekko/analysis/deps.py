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
import re
import sys
from pathlib import Path

from dekko.analysis.query import paths_matching
from dekko.core.resolver import find_cycles, import_resolution_supported
from dekko.render import export
from dekko.render.mapfile import MapIndex, format_unsupported
from dekko.source import read_lines
from dekko.textutil import fit_to_budget, token_footer

EXIT_OK = 0
EXIT_ERROR = 2
EXIT_NOT_FOUND = 3
EXIT_TOO_BIG = 2
EXIT_AMBIGUOUS = 4

# Textual signatures of import mechanisms that resolve at *runtime*,
# which ``resolver.resolve_imports`` cannot follow statically and so
# never contributes an edge for. Round 31 found ``deps`` reporting a
# bare "imports (0)" on files wired up entirely this way -- on five
# repos across four language families, worst on tensorflow, where 6 of
# 9 real edges for a ``LazyLoader``-wired module were invisible with
# no caveat at all. dekko still cannot resolve these (that is a
# genuinely hard static-analysis problem, not a bug); what it can stop
# doing is presenting an incomplete zero as a confident one.
#
# Keyed by the grammar name in ``MapIndex.languages_by_path``. Each
# entry is ``(pattern, human-readable construct name)``.
_DYNAMIC_IMPORT_SIGNATURES: dict[str, tuple[tuple[str, str], ...]] = {
    "python": (
        (r"\bimportlib\.import_module\s*\(", "importlib.import_module()"),
        (r"\bimportlib\.util\.spec_from_file_location\s*\(", "importlib.util"),
        (r"\b__import__\s*\(", "__import__()"),
        (r"\bLazyLoader\s*\(", "LazyLoader()"),
    ),
    "javascript": (
        (r"(?<![\w.])import\s*\(", "dynamic import()"),
        (r"\brequire\.resolve\s*\(", "require.resolve()"),
    ),
    "typescript": (
        (r"(?<![\w.])import\s*\(", "dynamic import()"),
        (r"\brequire\.resolve\s*\(", "require.resolve()"),
    ),
    "tsx": ((r"(?<![\w.])import\s*\(", "dynamic import()"),),
    "rust": ((r"\binclude!\s*\(", "include!()"),),
    "java": (
        (r"\bClass\.forName\s*\(", "Class.forName()"),
        (r"\bServiceLoader\.load\s*\(", "ServiceLoader.load()"),
    ),
}

# Scanning cost guard: these are cheap line regexes, but ``--file`` is
# a single-file view and a pathological generated file shouldn't turn
# it into a long read.
_DYNAMIC_SCAN_MAX_LINES = 20_000


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


def _import_resolution_coverage_note(index: MapIndex) -> str | None:
    """Scope-gap disclosure for a "0 resolved import edges" headline.

    Round-29 Track 4a (flagged rounds 27/28/29 on awesome-go): an
    all-Go repo's ``dekko deps`` leads with "0 resolved import edges"
    before the ``external (K)`` breakdown explains it, reading as "did
    something break" every round it's re-found. Go (and any Tier-2/
    generic-grammar language — see ``resolver.import_resolution_
    supported``) has no per-language import resolver at all: every one
    of its imports reports external unconditionally by design, not a
    mapping failure.

    Mirrors ``query throws``'s "permanently excluded from this query"
    and ``query catches``'s "N of M mapped files are in a language
    this query doesn't cover" framing (same disclosure shape, applied
    to import-resolution coverage instead of exception-handling
    coverage — deps has no equivalent existing helper to reuse, since
    ``mapfile.format_unsupported`` covers unparsed/skipped *files*,
    not "parsed fine, but this language's imports never resolve").

    Args:
        index: Loaded map index.

    Returns:
        A one-line note naming the uncovered language(s) and their
        share of mapped files, or ``None`` when every mapped file's
        language has real import-resolution support.
    """
    total = len(index.languages_by_path)
    if not total:
        return None
    uncovered: dict[str, int] = {}
    for lang in index.languages_by_path.values():
        if not import_resolution_supported(lang):
            uncovered[lang] = uncovered.get(lang, 0) + 1
    if not uncovered:
        return None
    count = sum(uncovered.values())
    lang_list = "/".join(sorted(uncovered))
    return (
        f"{count:,} of {total:,} mapped files are in a language "
        f"({lang_list}) whose imports dekko does not resolve to "
        "in-repo files by design -- every import there reports "
        "external, not a mapping gap"
    )


def _dynamic_import_constructs(
    root: Path | None, path: str, lang: str | None
) -> list[tuple[str, int]]:
    """Runtime-import constructs visible in ``path``'s own source.

    A cheap textual scan, deliberately: dekko is not trying to
    *resolve* these edges here, only to notice that the file wires up
    imports in a way static extraction provably cannot see, so the
    report can say so instead of printing a bare zero.

    Args:
        root: Repository root, or ``None`` when the caller has no root
            to read source from (the scan is then skipped).
        path: Repo-relative POSIX path of the file to scan.
        lang: The file's grammar name, or ``None`` if unknown.

    Returns:
        ``(construct name, occurrence count)`` per distinct construct
        found, highest count first. Empty when the language has no
        known runtime-import shape, the file is unreadable, or nothing
        matched.
    """
    if root is None or lang is None:
        return []
    signatures = _DYNAMIC_IMPORT_SIGNATURES.get(lang)
    if not signatures:
        return []
    lines = read_lines(root, path)[:_DYNAMIC_SCAN_MAX_LINES]
    if not lines:
        return []

    found: list[tuple[str, int]] = []
    for pattern, label in signatures:
        rx = re.compile(pattern)
        count = sum(1 for ln in lines if rx.search(ln))
        if count:
            found.append((label, count))

    found.sort(key=lambda pair: (-pair[1], pair[0]))
    return found


def _dynamic_import_note(
    constructs: list[tuple[str, int]], resolved: int
) -> str | None:
    """The ``--file`` disclosure for runtime-wired imports.

    Args:
        constructs: ``_dynamic_import_constructs``'s result.
        resolved: How many static import edges dekko did resolve, which
            decides whether the count below reads as "incomplete" or
            "not the whole story".

    Returns:
        A one-line note, or ``None`` when nothing was found.
    """
    if not constructs:
        return None
    detail = ", ".join(
        f"{label} x{count}" if count > 1 else label
        for label, count in constructs
    )
    headline = (
        "the 0 above is not proof of no dependencies"
        if resolved == 0
        else "the count above covers static imports only"
    )
    return (
        f"this file also resolves imports at runtime ({detail}) -- "
        f"dekko resolves static imports only, so {headline}"
    )


def _print_summary_text(doc: dict, coverage: str | None = None) -> None:
    """Print the default (no ``--file``/``--cycles``) text summary."""
    lines = []
    if coverage:
        lines.append(f"note: {coverage}")
    lines.append(
        f"dekko: {doc['files']} files, {doc['edges']} resolved import "
        f"edges, {doc['external_sources']} external sources across "
        f"{doc['external_only_files']} external-only files",
    )
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
    # Two independent coverage gaps, both round-29 Track 4: skipped
    # files (unsupported language, vendored/too-large/symlinked --
    # the same note status/summary/stats/search all carry), and a
    # language whose files *are* mapped but whose imports never
    # resolve (Go) -- only surfaced when it would actually explain
    # the headline, not on every run regardless of edge count.
    skipped = format_unsupported(index.provenance)
    import_gap = (
        _import_resolution_coverage_note(index) if doc["edges"] == 0 else None
    )
    if as_json:
        if skipped:
            doc["coverage_warning"] = skipped
        if import_gap:
            doc["import_resolution_coverage"] = import_gap
        print(json.dumps(doc, indent=2))
        return EXIT_OK
    if doc["files"] == 0:
        print("dekko: no mapped files")
        return EXIT_OK
    _print_summary_text(doc, import_gap)
    if skipped:
        print(f"coverage: {skipped} — results above may be incomplete")
    return EXIT_OK


def _run_file(
    index: MapIndex,
    path: str,
    limit: int,
    budget: int | None,
    as_json: bool,
    root: Path | None = None,
) -> int:
    """Handle ``--file PATH``: one file's imports/importers/external."""
    matches = paths_matching(index, path, pool=index.languages_by_path)
    if not matches:
        print(f"dekko: no mapped file '{path}'", file=sys.stderr)
        return EXIT_NOT_FOUND
    if len(matches) > 1:
        print(f"dekko: '{path}' is ambiguous; candidates:", file=sys.stderr)
        for p in matches:
            print(f"  {p}", file=sys.stderr)
        return EXIT_AMBIGUOUS
    path = matches[0]

    imports = index.module_deps_out.get(path, [])
    imported_by = index.module_deps_in.get(path, [])
    external = index.module_external.get(path, [])
    # Round 31: never report a bare "imports (0)" for a file that
    # plainly wires its imports up at runtime -- see
    # ``_dynamic_import_constructs``.
    dynamic = _dynamic_import_constructs(
        root, path, index.languages_by_path.get(path)
    )
    dynamic_note = _dynamic_import_note(dynamic, len(imports))

    if as_json:
        doc = {
            "path": path,
            "imports": imports,
            "imported_by": imported_by,
            "external": external,
        }
        if dynamic_note:
            doc["dynamic_imports"] = [
                {"construct": label, "occurrences": count}
                for label, count in dynamic
            ]
            doc["dynamic_import_note"] = dynamic_note
        print(json.dumps(doc, indent=2))
        return EXIT_OK

    _print_file_text(
        imports,
        imported_by,
        external,
        limit,
        budget,
        dynamic_note,
    )
    return EXIT_OK


def _print_file_text(
    imports: list[str],
    imported_by: list[str],
    external: list[str],
    limit: int,
    budget: int | None,
    dynamic_note: str | None,
) -> None:
    """Render ``--file``'s text view.

    Split out of ``_run_file`` purely to keep that function inside the
    project's complexity ceiling once the runtime-import disclosure
    was added -- resolution/lookup stays there, rendering lives here.
    """
    rows = [f"    {p}" for p in imports]
    kept_imports, meter_imports = fit_to_budget(rows, budget, limit)
    rows_in = [f"    {p}" for p in imported_by]
    kept_in, meter_in = fit_to_budget(rows_in, budget, limit)

    if dynamic_note:
        print(f"note: {dynamic_note}")
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
    root: Path | None = None,
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
        root: Repository root, used by ``--file`` to scan the named
            file for runtime-import constructs dekko cannot resolve
            statically. ``None`` skips that scan (the disclosure is
            simply omitted, never guessed) -- same optional-root shape
            ``contextpack.run`` already uses.

    Returns:
        ``0`` on success, ``2`` on a too-big export graph, ``3`` when
        ``--file`` names an unmapped path, ``4`` when ``--file`` names
        an ambiguous path suffix matching 2+ mapped files.
    """
    if export_fmt is not None:
        return _run_export(index, export_fmt, max_nodes, out_path)
    if file is not None:
        return _run_file(index, file, limit, budget, as_json, root)
    if cycles:
        return _run_cycles(index, limit, budget, as_json)
    return _run_summary(index, top, as_json)
