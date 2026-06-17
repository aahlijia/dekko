"""Render the extracted symbol/call graph as map.json."""

import json
from dataclasses import asdict
from datetime import datetime, timezone

from .model import CallGraph, FileMap


def render_json(
    files: list[FileMap],
    graph: CallGraph,
    root_label: str,
    provenance: dict | None = None,
) -> str:
    """Serialize the full graph (including external calls) to JSON.

    Args:
        files: Per-file extraction results.
        graph: Resolved call graph.
        root_label: Display name of the mapped root.
        provenance: Freshness stamp (tool version, git commit,
            discovery options, per-file hashes), or ``None``.

    Returns:
        Pretty-printed JSON text.
    """
    when = datetime.now(timezone.utc).isoformat(timespec="seconds")
    doc = {
        "generator": "dekko",
        "version": 3,
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
        "edges": [asdict(edge) for edge in graph.edges],
        "ambiguous": [
            {"caller": caller, "name": name, "candidates": cands}
            for caller, name, cands in graph.ambiguous
        ],
        "external": [asdict(ext) for ext in graph.external],
    }
    return json.dumps(doc, indent=2, sort_keys=False) + "\n"
