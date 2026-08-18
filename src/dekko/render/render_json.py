"""Render the extracted symbol/call graph as map.json."""

from dataclasses import asdict
from datetime import datetime, timezone

from dekko.render.mapfile import MAP_DOC_VERSION, _json_dumps, build_id_table
from dekko.core.model import CallGraph, FileMap


def render_json(
    files: list[FileMap],
    graph: CallGraph,
    root_label: str,
    provenance: dict | None = None,
) -> bytes:
    """Serialize the full graph (including external calls) to JSON.

    Args:
        files: Per-file extraction results.
        graph: Resolved call graph.
        root_label: Display name of the mapped root.
        provenance: Freshness stamp (tool version, git commit,
            discovery options, per-file hashes), or ``None``.

    Returns:
        Compact JSON bytes. ``map.json`` has no human reader — only
        ``mapfile.load_map()`` parses it, and ``MAP.md``
        (``render_md.py``) is the actual human-facing artifact — so
        this is written densely instead of pretty-printed
        (round-15 plan). Caller/callee/candidate id strings that
        repeat across ``"edges"``/``"ambiguous"``/``"external"``/
        ``"referenced"``/``"heritage"``/``"heritage_ambiguous"``/
        ``"heritage_external"``/``"throws"``/``"throws_ambiguous"``/
        ``"throws_external"``/``"throws_bare"``/``"catches"`` are
        interned once into a top-level
        ``"ids"`` table (``mapfile.build_id_table``) and referenced
        there by integer index instead of being spelled out at every
        occurrence. ``"module_graph"``'s file paths share that same
        table (a path string never collides with a symbol id, which
        always contains ``"::"``).
    """
    when = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ids, id_index = build_id_table(graph)
    doc = {
        "generator": "dekko",
        "version": MAP_DOC_VERSION,
        "root": root_label,
        "generated_at": when,
        "provenance": provenance,
        "files": [
            {
                "path": fm.path,
                "language": fm.language,
                "error": fm.error,
                "doc": fm.doc,
                "imports": [asdict(i) for i in fm.imports],
            }
            for fm in files
        ],
        "symbols": [asdict(sym) for fm in files for sym in fm.symbols],
        "ids": ids,
        "edges": [
            {
                "caller": id_index[edge.caller],
                "callee": id_index[edge.callee],
                "lines": edge.lines,
            }
            for edge in graph.edges
        ],
        "ambiguous": [
            {
                "caller": id_index[caller],
                "name": name,
                "candidates": [id_index[c] for c in cands],
            }
            for caller, name, cands in graph.ambiguous
        ],
        "external": [
            {
                "caller": id_index[ext.caller],
                "callee": id_index[ext.callee],
                "lines": ext.lines,
            }
            for ext in graph.external
        ],
        "referenced": [
            {
                "caller": id_index[edge.caller],
                "callee": id_index[edge.callee],
                "lines": edge.lines,
            }
            for edge in graph.referenced
        ],
        "heritage": [
            {
                "subtype": id_index[edge.subtype],
                "supertype": id_index[edge.supertype],
                "relation": edge.relation,
                "lines": edge.lines,
            }
            for edge in graph.heritage
        ],
        "heritage_ambiguous": [
            {
                "subtype": id_index[subtype],
                "name": name,
                "candidates": [id_index[c] for c in cands],
            }
            for subtype, name, cands in graph.heritage_ambiguous
        ],
        "heritage_external": [
            {
                "caller": id_index[ext.caller],
                "callee": id_index[ext.callee],
                "lines": ext.lines,
            }
            for ext in graph.heritage_external
        ],
        "module_graph": {
            "edges": [
                {
                    "importer": id_index[edge.importer],
                    "imported": id_index[edge.imported],
                    "names": edge.names,
                }
                for edge in graph.modules.edges
            ],
            "external": [
                {"path": id_index[path], "sources": sources}
                for path, sources in sorted(graph.modules.external.items())
            ],
        },
        "throws": [
            {
                "caller": id_index[edge.caller],
                "type": id_index[edge.type],
                "lines": edge.lines,
            }
            for edge in graph.throws
        ],
        "throws_ambiguous": [
            {
                "caller": id_index[caller],
                "name": name,
                "candidates": [id_index[c] for c in cands],
            }
            for caller, name, cands in graph.throws_ambiguous
        ],
        "throws_external": [
            {
                "caller": id_index[ext.caller],
                "callee": id_index[ext.callee],
                "lines": ext.lines,
            }
            for ext in graph.throws_external
        ],
        "throws_bare": [
            {"caller": id_index[caller], "path": path, "line": line}
            for caller, path, line in graph.throws_bare
        ],
        "catches": [
            {
                "caller": id_index[site.caller],
                "path": site.path,
                "type_names": site.type_names,
                "repo_types": {
                    name: id_index[type_id]
                    for name, type_id in site.repo_types.items()
                },
                "bare": site.bare,
                "line": site.line,
            }
            for site in graph.catches
        ],
        # Not routed through ``id_index`` like every section above —
        # ``EnvRead`` needs no resolution pass, so ``caller_id`` is
        # written as a plain string or ``null`` directly (see
        # ``model.EnvRead``'s docstring).
        "env_reads": [
            {
                "caller_id": r.caller_id,
                "path": r.path,
                "key": r.key,
                "call": r.call,
                "line": r.line,
            }
            for r in graph.env_reads
        ],
    }
    return _json_dumps(doc) + b"\n"
