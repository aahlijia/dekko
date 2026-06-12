"""Read map.json back into a queryable index; provenance + freshness.

The map subcommand stamps map.json with provenance (tool version, git
commit, discovery options, per-file content hashes). Read commands load
the document into a ``MapIndex`` and compare provenance against the
working tree to decide whether the map is still fresh.
"""

import hashlib
import json
import subprocess
from dataclasses import dataclass, field
from importlib.metadata import version as _pkg_version
from pathlib import Path

from . import walker
from .model import Import, Param, Symbol

MAP_DOC_VERSION = 2


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
        "tool_version": _pkg_version("lidar-map"),
        "git_commit": _git_commit(root),
        "subpath": subpath,
        "excludes": list(excludes),
        "max_file_size": max_file_size,
        "files": {rel: _file_hash(root / rel) for rel in paths},
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
        imports_by_path: File path → imports declared in it.
        languages_by_path: File path → language name.
        provenance: Provenance stamp, or ``None`` for v1 documents.
    """

    root_label: str
    symbols_by_id: dict[str, Symbol] = field(default_factory=dict)
    symbols_by_name: dict[str, list[Symbol]] = field(default_factory=dict)
    symbols_by_qualname: dict[str, list[Symbol]] = field(default_factory=dict)
    symbols_by_path: dict[str, list[Symbol]] = field(default_factory=dict)
    calls_in: dict[str, list[str]] = field(default_factory=dict)
    calls_out: dict[str, list[str]] = field(default_factory=dict)
    imports_by_path: dict[str, list[Import]] = field(default_factory=dict)
    languages_by_path: dict[str, str] = field(default_factory=dict)
    provenance: dict | None = None

    def degree(self, sym_id: str) -> int:
        """Total fan-in + fan-out of a symbol id."""
        return len(self.calls_in.get(sym_id, [])) + len(
            self.calls_out.get(sym_id, [])
        )


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
    )


def load_map(root: Path) -> MapIndex | None:
    """Load ``root/map.json`` into a ``MapIndex``.

    Args:
        root: Directory containing map.json.

    Returns:
        The index, or ``None`` if the file is missing or unparsable.
    """
    path = root / "map.json"
    try:
        doc = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None

    index = MapIndex(
        root_label=doc.get("root", root.name), provenance=doc.get("provenance")
    )
    for entry in doc.get("files", []):
        fpath = entry["path"]
        index.languages_by_path[fpath] = entry.get("language", "")
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
    current_paths, _ = walker.discover(
        root,
        subpath=prov.get("subpath"),
        excludes=tuple(prov.get("excludes", [])),
        max_file_size=prov.get("max_file_size", walker.DEFAULT_MAX_FILE_SIZE),
    )
    current = {rel: _file_hash(root / rel) for rel in current_paths}

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
