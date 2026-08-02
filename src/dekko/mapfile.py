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
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from importlib.metadata import version as _pkg_version
from pathlib import Path

from . import walker
from .classify import is_test_path
from .languages import spec_fingerprint
from .model import CallGraph, ExternalCall, FileMap, Import, Param, Symbol

MAP_DOC_VERSION = 4
_MAP_DIR = ".dekko"
_BASE_SPLIT = re.compile(r"::|\.|->|/")
_UNSUPPORTED_PREFIX = "no parser ("


def _unsupported_summary(
    skipped: list[tuple[str, str]] | None,
) -> dict | None:
    """Aggregate ``walker.discover``'s skip reasons into a coverage note.

    Only ``"no parser (<language>)"`` reasons are counted here — the
    confirmed language-support gaps recorded in
    ``languages.KNOWN_UNSUPPORTED`` — not the ordinary
    ``"excluded"``/``"generated"``/``"too large"`` skips a repo owner
    asked for or expects. This is what lets a "no callers found"
    answer be qualified as "no callers among parsed files" instead of
    presented as unconditional truth (see ``query`` module).

    Args:
        skipped: ``(path, reason)`` pairs from ``walker.discover``.

    Returns:
        ``{"count": N, "languages": {lang: N, ...}}``, or ``None``
        when there is nothing to report.
    """
    if not skipped:
        return None
    by_lang: Counter[str] = Counter()
    for _, reason in skipped:
        if reason.startswith(_UNSUPPORTED_PREFIX) and reason.endswith(")"):
            by_lang[reason[len(_UNSUPPORTED_PREFIX) : -1]] += 1
    if not by_lang:
        return None
    return {
        "count": sum(by_lang.values()),
        "languages": dict(sorted(by_lang.items())),
    }


def format_unsupported(provenance: dict | None) -> str | None:
    """One-line coverage note from a provenance dict's ``unsupported``.

    Shared by ``dekko status``, ``map_status``, and the CLI build
    summary so the wording is identical everywhere a caller might see
    it.

    Args:
        provenance: A map's provenance dict, or ``None``.

    Returns:
        E.g. ``"12 files unparsed — no parser for: astro (12)"``, or
        ``None`` when there is nothing to report.
    """
    if not provenance:
        return None
    unsupported = provenance.get("unsupported")
    if not unsupported:
        return None
    by_lang = unsupported.get("languages", {})
    detail = ", ".join(f"{lang} ({n})" for lang, n in by_lang.items())
    count = unsupported.get("count", 0)
    return f"{count} files unparsed — no parser for: {detail}"


def compute_provenance(
    root: Path,
    paths: list[str],
    subpath: str | None,
    excludes: tuple[str, ...],
    max_file_size: int,
    skipped: list[tuple[str, str]] | None = None,
) -> dict:
    """Build the provenance stamp for a freshly generated map.

    Args:
        root: Repository root that was mapped.
        paths: Repo-relative paths of every mapped file.
        subpath: Subtree restriction used for discovery, if any.
        excludes: Extra exclude globs used for discovery.
        max_file_size: Size cap used for discovery.
        skipped: ``(path, reason)`` pairs from the same ``walker.
            discover`` call that produced ``paths``, used to record a
            coverage note for confirmed-unsupported languages.

    Returns:
        JSON-serializable provenance dict.
    """
    return {
        "tool_version": _pkg_version("dekko"),
        "spec_hash": spec_fingerprint(),
        "git_commit": _git_commit(root),
        "subpath": subpath,
        "excludes": list(excludes),
        "max_file_size": max_file_size,
        "files": {rel: _file_hash(root / rel) for rel in paths},
        "stat": {rel: _stat_sig(root / rel) for rel in paths},
        "unsupported": _unsupported_summary(skipped),
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
        ambiguous_in: Candidate symbol id → ``(caller_id, name)`` pairs
            that could have resolved to it but didn't, because the
            name matched more than one repo-wide candidate. These
            never contribute to ``calls_in``/fan-in (see ``resolver``'s
            module docstring) — this is how a caller can tell "N more
            call sites exist but weren't resolvable to this symbol
            specifically" instead of reading a low fan-in as complete.
        referenced_in: Symbol id → ids that reference it as a value
            (object-literal property, array element, call argument,
            assignment RHS) without calling it — e.g. a callback wired
            up by name (empty for maps written before doc version 4).
        referenced_out: Symbol id → ids it references as a value
            without calling them.
        ref_lines: ``(caller id, callee id)`` → reference-site lines
            for the ``referenced`` edge table (empty for maps written
            before doc version 4). Kept separate from ``edge_lines``
            rather than merged into it, since a caller can in
            principle both call and reference the same callee and a
            single dict keyed only on ``(caller, callee)`` would let
            one overwrite the other.
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
    ambiguous_in: dict[str, list[tuple[str, str]]] = field(
        default_factory=dict
    )
    referenced_in: dict[str, list[str]] = field(default_factory=dict)
    referenced_out: dict[str, list[str]] = field(default_factory=dict)
    ref_lines: dict[tuple[str, str], list[int]] = field(default_factory=dict)
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
        for cand, pairs in self.ambiguous_in.items():
            if not _prod_id(cand):
                continue
            kept_pairs = [
                (caller, name) for caller, name in pairs if _prod_id(caller)
            ]
            if kept_pairs:
                out.ambiguous_in[cand] = kept_pairs
        out.referenced_in = _filter_adjacency(self.referenced_in)
        out.referenced_out = _filter_adjacency(self.referenced_out)
        out.ref_lines = {
            key: lines
            for key, lines in self.ref_lines.items()
            if _prod_id(key[0]) and _prod_id(key[1])
        }
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
    """Result of comparing a map's provenance to the working tree.

    Attributes:
        reason: ``None`` when fresh; otherwise ``"missing"`` (no
            provenance at all — a pre-v2 map), ``"version"`` (the map
            predates the running dekko build — its ``tool_version``
            or ``spec_hash`` no longer matches, so ``added``/
            ``removed``/``changed`` are not computed), or ``"content"``
            (source files were added, changed, or removed).
    """

    fresh: bool
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    reason: str | None = None


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
        doc = json.loads(
            (root / _MAP_DIR / "notes.json").read_text(encoding="utf-8")
        )
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
        doc = json.loads(path.read_text(encoding="utf-8"))
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
    index.ambiguous_in = _invert_ambiguous(
        (d.get("caller", ""), d.get("name", ""), d.get("candidates", []))
        for d in doc.get("ambiguous", [])
    )
    for edge in doc.get("referenced", []):
        caller, callee = edge["caller"], edge["callee"]
        index.referenced_out.setdefault(caller, []).append(callee)
        index.referenced_in.setdefault(callee, []).append(caller)
        index.ref_lines[(caller, callee)] = edge.get("lines", [])
    return index


def _invert_ambiguous(
    entries: Iterator[tuple[str, str, list[str]]],
) -> dict[str, list[tuple[str, str]]]:
    """Invert ambiguous-call records into candidate id → callers.

    An ambiguous call never becomes a resolved edge (see ``resolver``'s
    module docstring), so it never shows up in any candidate's
    ``calls_in``. This is the read side of that gap: for each
    candidate a name collision could have pointed at, record who tried
    and under what name, so a low fan-in can be qualified as "+N
    ambiguous call sites not counted" instead of read as exhaustive.

    Args:
        entries: ``(caller_id, name, candidate_ids)`` triples.

    Returns:
        Candidate symbol id → list of ``(caller_id, name)`` pairs.
    """
    out: dict[str, list[tuple[str, str]]] = {}
    for caller, name, candidates in entries:
        for cand in candidates:
            out.setdefault(cand, []).append((caller, name))
    return out


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
    index.ambiguous_in = _invert_ambiguous(iter(graph.ambiguous))
    for edge in graph.referenced:
        index.referenced_out.setdefault(edge.caller, []).append(edge.callee)
        index.referenced_in.setdefault(edge.callee, []).append(edge.caller)
        index.ref_lines[(edge.caller, edge.callee)] = edge.lines
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
        return Freshness(
            fresh=False,
            changed=sorted(index.symbols_by_path),
            reason="missing",
        )

    prov = index.provenance
    version_stale = prov.get("tool_version") != _pkg_version("dekko")
    spec_stale = prov.get("spec_hash") != spec_fingerprint()
    if version_stale or spec_stale:
        # The map was built by a different dekko (or an unreleased
        # extractor change under the same version string). Source
        # content may be byte-identical, but what dekko would extract
        # from it has changed, so no amount of file-hash diffing can
        # answer "is this map still correct" — treat it as stale
        # outright, once, until the next `dekko map` re-stamps it.
        return Freshness(fresh=False, reason="version")

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
    is_stale = bool(added or removed or changed)
    return Freshness(
        fresh=not is_stale,
        added=added,
        removed=removed,
        changed=changed,
        reason="content" if is_stale else None,
    )
