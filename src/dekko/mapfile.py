"""Read map.json back into a queryable index; provenance + freshness.

The map subcommand stamps map.json with provenance (tool version, git
commit, discovery options, per-file content hashes). Read commands load
the document into a ``MapIndex`` and compare provenance against the
working tree to decide whether the map is still fresh.
"""

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass, field
from importlib.metadata import version as _pkg_version
from pathlib import Path

from . import walker
from .classify import is_test_path
from .model import CallGraph, ExternalCall, FileMap, Import, Param, Symbol

MAP_DOC_VERSION = 3
_MAP_DIR = ".dekko"
_BASE_SPLIT = re.compile(r"::|\.|->|/")


def compute_provenance(
    root: Path,
    paths: list[str],
    subpath: str | None,
    excludes: tuple[str, ...],
    max_file_size: int,
) -> dict:
    """Build the provenance stamp for a freshly generated map.

    Args:
        root: Repository root that was mapped.
        paths: Repo-relative paths of every mapped file.
        subpath: Subtree restriction used for discovery, if any.
        excludes: Extra exclude globs used for discovery.
        max_file_size: Size cap used for discovery.

    Returns:
        JSON-serializable provenance dict.
    """
    return {
        "tool_version": _pkg_version("dekko"),
        "git_commit": _git_commit(root),
        "subpath": subpath,
        "excludes": list(excludes),
        "max_file_size": max_file_size,
        "files": {rel: _file_hash(root / rel) for rel in paths},
        "stat": {rel: _stat_sig(root / rel) for rel in paths},
    }


def _git_commit(root: Path) -> str | None:
    """Return the HEAD commit of the repo at root, or ``None``."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _file_hash(path: Path) -> str:
    """Short content hash used for staleness comparison."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return "unreadable"


def _stat_sig(path: Path) -> list[int]:
    """``[mtime_ns, size]`` signature for the freshness fast path.

    Returns an empty list on error so it never matches a recorded
    signature, forcing a content hash for that file.
    """
    try:
        st = path.stat()
    except OSError:
        return []
    return [st.st_mtime_ns, st.st_size]


@dataclass
class MapIndex:
    """map.json loaded into lookup structures.

    Attributes:
        root_label: Display label of the mapped root.
        symbols_by_id: Symbol id → symbol.
        symbols_by_name: Bare name → symbols sharing it.
        symbols_by_qualname: Qualified name → symbols sharing it.
        symbols_by_path: File path → its symbols in definition order.
        calls_in: Symbol id → caller ids.
        calls_out: Symbol id → callee ids.
        edge_lines: ``(caller id, callee id)`` → call-site lines
            (empty for maps written before doc version 3).
        imports_by_path: File path → imports declared in it.
        languages_by_path: File path → language name.
        docs_by_path: File path → module doc first line, or ``None``.
        errors_by_path: File path → parse error message (only files
            that failed to parse appear).
        externals_by_name: Base callee identifier → external calls
            referencing it (e.g. ``run`` for ``subprocess.run``).
        notes: Symbol id → note texts loaded from ``.dekko/notes.json``.
        provenance: Provenance stamp, or ``None`` for v1 documents.
    """

    root_label: str
    symbols_by_id: dict[str, Symbol] = field(default_factory=dict)
    symbols_by_name: dict[str, list[Symbol]] = field(default_factory=dict)
    symbols_by_qualname: dict[str, list[Symbol]] = field(default_factory=dict)
    symbols_by_path: dict[str, list[Symbol]] = field(default_factory=dict)
    calls_in: dict[str, list[str]] = field(default_factory=dict)
    calls_out: dict[str, list[str]] = field(default_factory=dict)
    edge_lines: dict[tuple[str, str], list[int]] = field(default_factory=dict)
    imports_by_path: dict[str, list[Import]] = field(default_factory=dict)
    languages_by_path: dict[str, str] = field(default_factory=dict)
    docs_by_path: dict[str, str | None] = field(default_factory=dict)
    errors_by_path: dict[str, str] = field(default_factory=dict)
    externals_by_name: dict[str, list[ExternalCall]] = field(
        default_factory=dict
    )
    notes: dict[str, list[str]] = field(default_factory=dict)
    provenance: dict | None = None

    def degree(self, sym_id: str) -> int:
        """Total fan-in + fan-out of a symbol id."""
        return len(self.calls_in.get(sym_id, [])) + len(
            self.calls_out.get(sym_id, [])
        )

    def without_tests(self) -> "MapIndex":
        """A filtered view with all test-path code removed.

        Drops symbols defined in test files, edges touching them
        (including module-level test callers), and external calls made
        from test files. Classification is path-based so it also works
        on pre-v3 documents that lack the ``test`` flag.

        Returns:
            A new ``MapIndex``; ``self`` is left untouched.
        """
        out = MapIndex(root_label=self.root_label, provenance=self.provenance)
        for sid, sym in self.symbols_by_id.items():
            if not _prod_id(sid):
                continue
            out.symbols_by_id[sid] = sym
            out.symbols_by_name.setdefault(sym.name, []).append(sym)
            out.symbols_by_qualname.setdefault(sym.qualname, []).append(sym)
            out.symbols_by_path.setdefault(sym.path, []).append(sym)
        out.calls_in = _filter_adjacency(self.calls_in)
        out.calls_out = _filter_adjacency(self.calls_out)
        out.edge_lines = {
            key: lines
            for key, lines in self.edge_lines.items()
            if _prod_id(key[0]) and _prod_id(key[1])
        }
        out.imports_by_path = _filter_paths(self.imports_by_path)
        out.languages_by_path = _filter_paths(self.languages_by_path)
        out.docs_by_path = _filter_paths(self.docs_by_path)
        out.errors_by_path = _filter_paths(self.errors_by_path)
        out.notes = {
            sid: texts for sid, texts in self.notes.items() if _prod_id(sid)
        }
        for name, exts in self.externals_by_name.items():
            kept = [e for e in exts if _prod_id(e.caller)]
            if kept:
                out.externals_by_name[name] = kept
        return out


def _prod_id(sym_or_module_id: str) -> bool:
    """Whether a symbol/module id belongs to production (non-test) code."""
    return not is_test_path(sym_or_module_id.split("::", 1)[0])


def _filter_adjacency(table: dict[str, list[str]]) -> dict[str, list[str]]:
    """Drop test-path nodes from an adjacency table, keys and values."""
    out: dict[str, list[str]] = {}
    for sid, others in table.items():
        if not _prod_id(sid):
            continue
        kept = [o for o in others if _prod_id(o)]
        if kept:
            out[sid] = kept
    return out


def _filter_paths(mapping: dict) -> dict:
    """Drop test-path keys from a path-keyed mapping."""
    return {
        path: value
        for path, value in mapping.items()
        if not is_test_path(path)
    }


@dataclass
class Freshness:
    """Result of comparing a map's provenance to the working tree."""

    fresh: bool
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)


def _symbol_from_dict(d: dict) -> Symbol:
    """Rebuild a ``Symbol`` (with ``Param``s) from its JSON dict."""
    params = [Param(**p) for p in d.get("params", [])]
    return Symbol(
        id=d["id"],
        name=d["name"],
        qualname=d["qualname"],
        kind=d["kind"],
        path=d["path"],
        language=d["language"],
        params=params,
        returns=d.get("returns"),
        start_line=d.get("start_line", 0),
        end_line=d.get("end_line", 0),
        decorated=d.get("decorated", False),
        exported=d.get("exported", False),
        doc=d.get("doc"),
        test=d.get("test", False),
    )


def _load_notes(root: Path) -> dict[str, list[str]]:
    """Read ``.dekko/notes.json`` into symbol id → note texts.

    Read inline (rather than via the ``notes`` module) to keep this
    low-level loader free of higher-level imports.
    """
    try:
        doc = json.loads((root / _MAP_DIR / "notes.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    raw = doc.get("notes")
    if not isinstance(raw, dict):
        return {}
    return {
        sym_id: [r.get("text", "") for r in records]
        for sym_id, records in raw.items()
    }


def _callee_base(text: str) -> str:
    """Base identifier of an external callee text.

    ``subprocess.run`` → ``run``; ``a::b`` → ``b``; ``Path`` → ``Path``.
    """
    parts = [p for p in _BASE_SPLIT.split(text) if p]
    return parts[-1] if parts else ""


def load_map(root: Path) -> MapIndex | None:
    """Load ``root/.dekko/map.json`` into a ``MapIndex``.

    Args:
        root: Repository root whose ``.dekko/map.json`` should be read.

    Returns:
        The index, or ``None`` if the file is missing or unparsable.
    """
    path = root / _MAP_DIR / "map.json"
    try:
        doc = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None

    index = MapIndex(
        root_label=doc.get("root", root.name),
        provenance=doc.get("provenance"),
        notes=_load_notes(root),
    )
    for entry in doc.get("files", []):
        fpath = entry["path"]
        index.languages_by_path[fpath] = entry.get("language", "")
        index.docs_by_path[fpath] = entry.get("doc")
        if entry.get("error"):
            index.errors_by_path[fpath] = entry["error"]
        index.imports_by_path[fpath] = [
            Import(**imp) for imp in entry.get("imports", [])
        ]
    for d in doc.get("symbols", []):
        sym = _symbol_from_dict(d)
        index.symbols_by_id[sym.id] = sym
        index.symbols_by_name.setdefault(sym.name, []).append(sym)
        index.symbols_by_qualname.setdefault(sym.qualname, []).append(sym)
        index.symbols_by_path.setdefault(sym.path, []).append(sym)
    for edge in doc.get("edges", []):
        caller, callee = edge["caller"], edge["callee"]
        index.calls_out.setdefault(caller, []).append(callee)
        index.calls_in.setdefault(callee, []).append(caller)
        index.edge_lines[(caller, callee)] = edge.get("lines", [])
    for d in doc.get("external", []):
        ext = ExternalCall(
            caller=d.get("caller") or "",
            callee=d.get("callee", ""),
            lines=d.get("lines", []),
        )
        base = _callee_base(ext.callee)
        if base:
            index.externals_by_name.setdefault(base, []).append(ext)
    return index


def index_from_maps(
    files: list[FileMap], graph: CallGraph, root_label: str
) -> MapIndex:
    """Build a ``MapIndex`` from in-memory extraction results.

    The in-process counterpart to ``load_map``: it produces the same
    index the read commands get from ``map.json``, so MAP.md rendering
    can reuse the ``summary``/``stats`` computations at generation time
    without a round trip through disk. Notes are not loaded — the
    overview describes structure, not annotations.

    Args:
        files: Per-file extraction results.
        graph: Resolved call graph.
        root_label: Display label of the mapped root.

    Returns:
        A populated ``MapIndex``.
    """
    index = MapIndex(root_label=root_label)
    for fm in files:
        index.languages_by_path[fm.path] = fm.language
        index.docs_by_path[fm.path] = fm.doc
        if fm.error:
            index.errors_by_path[fm.path] = fm.error
        index.imports_by_path[fm.path] = list(fm.imports)
        for sym in fm.symbols:
            index.symbols_by_id[sym.id] = sym
            index.symbols_by_name.setdefault(sym.name, []).append(sym)
            index.symbols_by_qualname.setdefault(sym.qualname, []).append(sym)
            index.symbols_by_path.setdefault(sym.path, []).append(sym)
    for edge in graph.edges:
        index.calls_out.setdefault(edge.caller, []).append(edge.callee)
        index.calls_in.setdefault(edge.callee, []).append(edge.caller)
        index.edge_lines[(edge.caller, edge.callee)] = edge.lines
    for ext in graph.external:
        base = _callee_base(ext.callee)
        if base:
            index.externals_by_name.setdefault(base, []).append(ext)
    return index


def check_freshness(root: Path, index: MapIndex) -> Freshness:
    """Compare an index's provenance against the current tree.

    Discovery re-runs with the options recorded in the provenance so
    subtree or filtered maps are judged on their own terms. Maps
    without provenance (v1 documents) are always stale.

    Args:
        root: Repository root.
        index: Loaded map index.

    Returns:
        Freshness verdict with per-file difference lists.
    """
    if not index.provenance:
        return Freshness(fresh=False, changed=sorted(index.symbols_by_path))

    prov = index.provenance
    recorded: dict[str, str] = prov.get("files", {})
    recorded_stat: dict[str, list[int]] = prov.get("stat", {})
    current_paths, _ = walker.discover(
        root,
        subpath=prov.get("subpath"),
        excludes=tuple(prov.get("excludes", [])),
        max_file_size=prov.get("max_file_size", walker.DEFAULT_MAX_FILE_SIZE),
    )
    # Fast path: a file whose (mtime, size) signature is unchanged is
    # assumed unchanged and not re-hashed. Files that are new, lack a
    # recorded signature, or whose stat moved fall back to hashing —
    # the content hash remains the decider for those.
    current: dict[str, str] = {}
    for rel in current_paths:
        sig = recorded_stat.get(rel)
        if sig and sig == _stat_sig(root / rel):
            current[rel] = recorded.get(rel, "")
        else:
            current[rel] = _file_hash(root / rel)

    added = sorted(set(current) - set(recorded))
    removed = sorted(set(recorded) - set(current))
    changed = sorted(
        rel
        for rel in set(recorded) & set(current)
        if recorded[rel] != current[rel]
    )
    return Freshness(
        fresh=not (added or removed or changed),
        added=added,
        removed=removed,
        changed=changed,
    )
