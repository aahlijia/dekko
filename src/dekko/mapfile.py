"""Read map.json back into a queryable index; provenance + freshness.

The map subcommand stamps map.json with provenance (tool version, git
commit, discovery options, per-file content hashes). Read commands load
the document into a ``MapIndex`` and compare provenance against the
working tree to decide whether the map is still fresh.
"""

import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from importlib.metadata import version as _pkg_version
from pathlib import Path

from . import walker
from .classify import is_test_path
from .languages import spec_fingerprint
from .model import CallGraph, ExternalCall, FileMap, Import, Param, Symbol

try:
    import orjson
except ImportError:  # pragma: no cover - exercised in stdlib-only envs
    orjson = None  # type: ignore[assignment]

MAP_DOC_VERSION = 4
_MAP_DIR = ".dekko"
_BASE_SPLIT = re.compile(r"::|\.|->|/")
_UNSUPPORTED_PREFIX = "no parser ("
_VENDORED_PREFIX = "vendored ("
_PROVENANCE_FILE = "provenance.json"


def _json_loads(data: bytes) -> object:
    """Parse JSON bytes, preferring ``orjson`` (2-10x faster) when
    installed (the ``dekko[fastjson]`` extra); falls back to stdlib
    ``json`` otherwise. Both raise a ``ValueError`` subclass on bad
    input, so callers can catch ``ValueError`` uniformly regardless of
    which backend is active.
    """
    if orjson is not None:
        return orjson.loads(data)
    return json.loads(data)


def _json_dumps(obj: object) -> bytes:
    """Serialize to compact JSON bytes, preferring ``orjson`` when
    installed; falls back to stdlib ``json``. Used for machine-only
    files (the provenance sidecar) where human-readable indentation
    doesn't matter.
    """
    if orjson is not None:
        return orjson.dumps(obj)
    return json.dumps(obj).encode("utf-8")


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` without ever exposing a partial file.

    Writes to a sibling temp file in the same directory, then
    ``os.replace()``s it into place — a single filesystem-level rename
    rather than an in-place ``write_text``/``write_bytes``. The
    temp file lives alongside ``path`` (not in a system temp dir) so
    the rename is guaranteed atomic (same filesystem).

    Without this, a reader (``load_map``, a concurrent daemon/MCP/CLI
    process's own regen) that opens ``path`` mid-write can observe
    however many bytes the writer has flushed so far — a truncated
    ``map.json``/``cache.json`` that fails JSON parsing outright, or
    worse, parses successfully as a valid-but-incomplete document.
    Round-12 master report §4.1b: no synchronization primitive of any
    kind previously guarded these writes, and multiple independent
    processes (bare CLI, daemon-triggered regen, MCP server) can each
    trigger one against the same root with zero coordination. Atomic
    replacement doesn't prevent two writers from racing each other,
    but it guarantees every reader sees either the old complete file
    or the new complete file, never a half-written one.

    Args:
        path: Destination file path; parent directory must exist.
        data: Exact bytes to write.
    """
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as tmp_file:
            tmp_file.write(data)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


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


def _vendored_summary(
    skipped: list[tuple[str, str]] | None,
) -> dict | None:
    """Aggregate ``walker.discover``'s ``"vendored (<dir>)"`` reasons.

    Mirrors ``_unsupported_summary`` but for files skipped solely
    because they live under a default-excluded directory that
    occasionally holds first-party code (``node_modules``, ``vendor``,
    ``third_party``, ``target``, ``dist``, ``build`` — see
    ``walker._VENDORED_DIRS``), as opposed to VCS metadata/tool caches
    (``walker._NOISE_DIRS``) where silence is correct and
    ``walker.discover`` never even records a skip entry.

    Args:
        skipped: ``(path, reason)`` pairs from ``walker.discover``.

    Returns:
        ``{"count": N, "dirs": {name: N, ...}}``, or ``None`` when
        there is nothing to report.
    """
    if not skipped:
        return None
    by_dir: Counter[str] = Counter()
    for _, reason in skipped:
        if reason.startswith(_VENDORED_PREFIX) and reason.endswith(")"):
            by_dir[reason[len(_VENDORED_PREFIX) : -1]] += 1
    if not by_dir:
        return None
    return {
        "count": sum(by_dir.values()),
        "dirs": dict(sorted(by_dir.items())),
    }


def format_unsupported(provenance: dict | None) -> str | None:
    """Coverage note(s) from a provenance dict's skip aggregates.

    Combines two independent coverage gaps into one caller-facing
    note, each included only when present: confirmed-unsupported
    languages (``provenance["unsupported"]``) and files skipped only
    because they live under a default-excluded directory that
    sometimes holds first-party code
    (``provenance["vendored_excluded"]`` — e.g. tensorflow's
    ``third_party/xla``). Shared by ``dekko status``, ``map_status``,
    ``dekko summary``, and ``query``'s not-found/ambiguous replies so
    the wording is identical everywhere a caller might see it.

    Args:
        provenance: A map's provenance dict, or ``None``.

    Returns:
        E.g. ``"12 files unparsed — no parser for: astro (12)"``,
        both notes joined by a newline when both apply, or ``None``
        when there is nothing to report.
    """
    if not provenance:
        return None
    parts: list[str] = []
    unsupported = provenance.get("unsupported")
    if unsupported:
        by_lang = unsupported.get("languages", {})
        detail = ", ".join(f"{lang} ({n})" for lang, n in by_lang.items())
        count = unsupported.get("count", 0)
        parts.append(f"{count} files unparsed — no parser for: {detail}")
    vendored = provenance.get("vendored_excluded")
    if vendored:
        by_dir = vendored.get("dirs", {})
        detail = ", ".join(f"{name} ({n})" for name, n in by_dir.items())
        count = vendored.get("count", 0)
        parts.append(
            f"{count} files under default-excluded directories "
            f"({detail}) were not mapped — pass --exclude '' or a "
            "narrower default-dir allowlist to include them if they "
            "hold first-party code"
        )
    if not parts:
        return None
    return "\n  ".join(parts)


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
            discover`` call that produced ``paths``, used to record
            coverage notes for confirmed-unsupported languages and
            for files skipped only because they live under a
            default-excluded (vendored/build-output) directory.

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
        "vendored_excluded": _vendored_summary(skipped),
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
        ambiguous_out: Caller symbol id → names it called ambiguously
            (2+ repo-wide candidates, none disambiguated). The
            outgoing-side counterpart to ``ambiguous_in`` — these never
            contribute to ``calls_out`` either, so ``query callees``
            can disclose "N outgoing call(s) not counted here" the same
            way ``query callers`` already discloses ``ambiguous_in``
            (round-09 §2.1 part A).
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
    ambiguous_out: dict[str, list[str]] = field(default_factory=dict)
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
        """A filtered view with all test code removed.

        Drops symbols classified as test code, edges touching them
        (including module-level test callers), and external calls made
        from test files. A symbol is dropped when either its file path
        is a test path (``classify.is_test_path``, works even on
        pre-v3 documents that lack the ``test`` flag) or the extractor
        set ``Symbol.test`` (path-based classification plus
        language-specific containers such as Rust's inline
        ``mod tests { ... }``).

        Returns:
            A new ``MapIndex``; ``self`` is left untouched.
        """
        out = MapIndex(root_label=self.root_label, provenance=self.provenance)
        for sid, sym in self.symbols_by_id.items():
            if _symbol_is_test(sym):
                continue
            out.symbols_by_id[sid] = sym
            out.symbols_by_name.setdefault(sym.name, []).append(sym)
            out.symbols_by_qualname.setdefault(sym.qualname, []).append(sym)
            out.symbols_by_path.setdefault(sym.path, []).append(sym)
        by_id = self.symbols_by_id
        out.calls_in = _filter_adjacency(self.calls_in, by_id)
        out.calls_out = _filter_adjacency(self.calls_out, by_id)
        out.edge_lines = {
            key: lines
            for key, lines in self.edge_lines.items()
            if _prod_id(key[0], by_id) and _prod_id(key[1], by_id)
        }
        out.imports_by_path = _filter_paths(self.imports_by_path)
        out.languages_by_path = _filter_paths(self.languages_by_path)
        out.docs_by_path = _filter_paths(self.docs_by_path)
        out.errors_by_path = _filter_paths(self.errors_by_path)
        out.notes = {
            sid: texts
            for sid, texts in self.notes.items()
            if _prod_id(sid, by_id)
        }
        for name, exts in self.externals_by_name.items():
            kept = [e for e in exts if _prod_id(e.caller, by_id)]
            if kept:
                out.externals_by_name[name] = kept
        for cand, pairs in self.ambiguous_in.items():
            if not _prod_id(cand, by_id):
                continue
            kept_pairs = [
                (caller, name)
                for caller, name in pairs
                if _prod_id(caller, by_id)
            ]
            if kept_pairs:
                out.ambiguous_in[cand] = kept_pairs
        out.ambiguous_out = {
            caller: names
            for caller, names in self.ambiguous_out.items()
            if _prod_id(caller, by_id)
        }
        out.referenced_in = _filter_adjacency(self.referenced_in, by_id)
        out.referenced_out = _filter_adjacency(self.referenced_out, by_id)
        out.ref_lines = {
            key: lines
            for key, lines in self.ref_lines.items()
            if _prod_id(key[0], by_id) and _prod_id(key[1], by_id)
        }
        return out


def _symbol_is_test(sym: Symbol) -> bool:
    """Whether a symbol is test code, by path or extractor flag.

    Two independent signals can mark test code: the defining file's
    path (``classify.is_test_path``) and the extractor's per-symbol
    ``Symbol.test`` flag (path-based classification plus
    language-specific containers such as Rust's inline
    ``mod tests { ... }`` — see ``Symbol.test``'s docstring). Either
    one is sufficient; this is the single place both are combined so
    ``without_tests()`` actually excludes everything ``Symbol.test``
    flags, not just what the path alone would catch.
    """
    return sym.test or is_test_path(sym.path)


def _prod_id(sym_or_module_id: str, symbols_by_id: dict[str, Symbol]) -> bool:
    """Whether a symbol/module id belongs to production (non-test) code.

    Resolves the id against ``symbols_by_id`` and defers to
    ``_symbol_is_test`` when it names a real symbol. Ids that don't
    resolve (module-level pseudo-callers, ``path::<module>``, which
    are never entered into ``symbols_by_id``) fall back to a
    path-only check.
    """
    sym = symbols_by_id.get(sym_or_module_id)
    if sym is not None:
        return not _symbol_is_test(sym)
    return not is_test_path(sym_or_module_id.split("::", 1)[0])


def _filter_adjacency(
    table: dict[str, list[str]], symbols_by_id: dict[str, Symbol]
) -> dict[str, list[str]]:
    """Drop test-path nodes from an adjacency table, keys and values."""
    out: dict[str, list[str]] = {}
    for sid, others in table.items():
        if not _prod_id(sid, symbols_by_id):
            continue
        kept = [o for o in others if _prod_id(o, symbols_by_id)]
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
        version_stale: True when the map's recorded ``tool_version``
            differs from the running process's own installed version.
            Only meaningful when ``reason == "version"``.
        spec_stale: True when the map's recorded ``spec_hash`` differs
            from what the running process's currently-loaded
            extraction specs would produce — the case a long-lived
            process (``dekko serve``) can hit silently, since a
            reinstall underneath it doesn't change ``tool_version``
            every release, so ``version_stale`` alone can read
            "identical" while the process is still running stale
            extractor code (round-09 §2.3: a ``dekko serve`` reported
            stale with ``built by dekko 0.21.3, running 0.21.3`` — the
            same string on both sides — with nothing distinguishing
            which check actually fired). Only meaningful when
            ``reason == "version"``.
        built_version: The map's recorded ``tool_version``, or
            ``None`` unless ``reason == "version"``.
        running_version: The checking process's own installed
            version, or ``None`` unless ``reason == "version"``.
        built_spec_hash: The map's recorded ``spec_hash``, or ``None``
            unless ``reason == "version"``.
        running_spec_hash: The checking process's own computed
            ``languages.spec_fingerprint()``, or ``None`` unless
            ``reason == "version"``.
    """

    fresh: bool
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    reason: str | None = None
    version_stale: bool = False
    spec_stale: bool = False
    built_version: str | None = None
    running_version: str | None = None
    built_spec_hash: str | None = None
    running_spec_hash: str | None = None


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
        doc = _json_loads(path.read_bytes())
    except (OSError, ValueError):
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
    index.ambiguous_in, index.ambiguous_out = _index_ambiguous(
        (d.get("caller", ""), d.get("name", ""), d.get("candidates", []))
        for d in doc.get("ambiguous", [])
    )
    for edge in doc.get("referenced", []):
        caller, callee = edge["caller"], edge["callee"]
        index.referenced_out.setdefault(caller, []).append(callee)
        index.referenced_in.setdefault(callee, []).append(caller)
        index.ref_lines[(caller, callee)] = edge.get("lines", [])
    return index


def _index_ambiguous(
    entries: Iterator[tuple[str, str, list[str]]],
) -> tuple[dict[str, list[tuple[str, str]]], dict[str, list[str]]]:
    """Index ambiguous-call records by candidate, and by caller.

    An ambiguous call never becomes a resolved edge (see ``resolver``'s
    module docstring), so it never shows up in either side's
    ``calls_in``/``calls_out``. This builds both read-side views in
    one pass over the same records:

    - ``ambiguous_in``: for each candidate a name collision could have
      pointed at, who tried and under what name — so a low fan-in can
      be qualified as "+N ambiguous call sites not counted" instead of
      read as exhaustive.
    - ``ambiguous_out``: for each caller, the names it called
      ambiguously — the outgoing-side counterpart, so ``query
      callees`` can disclose the same kind of gap ``query callers``
      already discloses via ``ambiguous_in`` (round-09 §2.1 part A).

    Args:
        entries: ``(caller_id, name, candidate_ids)`` triples.

    Returns:
        ``(ambiguous_in, ambiguous_out)``.
    """
    ambiguous_in: dict[str, list[tuple[str, str]]] = {}
    ambiguous_out: dict[str, list[str]] = {}
    for caller, name, candidates in entries:
        if candidates:
            ambiguous_out.setdefault(caller, []).append(name)
        for cand in candidates:
            ambiguous_in.setdefault(cand, []).append((caller, name))
    return ambiguous_in, ambiguous_out


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
    index.ambiguous_in, index.ambiguous_out = _index_ambiguous(
        iter(graph.ambiguous)
    )
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
    return _freshness_from_provenance(root, index.provenance)


def check_freshness_provenance(
    root: Path, provenance: dict | None
) -> Freshness:
    """Cheap freshness check driven by a provenance dict alone.

    Same comparison as ``check_freshness``, for callers that only have
    the provenance sidecar (``load_provenance``) rather than a full
    ``MapIndex`` — e.g. ``dekko status``, which has no other use for
    the parsed symbol/call graph. The one behavioral difference: a
    missing-provenance verdict here can't list every symbol as
    ``changed`` the way ``check_freshness(root, index)`` can, since
    that needs the full symbol table this path never builds. A caller
    that needs that detail on a ``provenance is None`` result should
    fall back to ``check_freshness(root, load_map(root))``.

    Args:
        root: Repository root.
        provenance: Provenance dict from ``load_provenance``, or
            ``None``.

    Returns:
        Freshness verdict; ``changed`` is empty (not exhaustive) when
        ``provenance`` is ``None``.
    """
    if not provenance:
        return Freshness(fresh=False, reason="missing")
    return _freshness_from_provenance(root, provenance)


def _freshness_from_provenance(root: Path, prov: dict) -> Freshness:
    """Shared comparison body for both freshness-check entry points."""
    built_version = prov.get("tool_version")
    running_version = _pkg_version("dekko")
    built_spec_hash = prov.get("spec_hash")
    running_spec_hash = spec_fingerprint()
    version_stale = built_version != running_version
    spec_stale = built_spec_hash != running_spec_hash
    if version_stale or spec_stale:
        # The map was built by a different dekko (or an unreleased
        # extractor change under the same version string). Source
        # content may be byte-identical, but what dekko would extract
        # from it has changed, so no amount of file-hash diffing can
        # answer "is this map still correct" — treat it as stale
        # outright, once, until the next `dekko map` re-stamps it.
        # Both signals (and the raw values behind them) are carried on
        # the verdict so a caller can tell the two apart — a
        # long-lived process (``dekko serve``) can have an identical
        # ``tool_version`` string on both sides while still running
        # stale extractor code, which ``version_stale`` alone can't
        # distinguish (round-09 §2.3).
        return Freshness(
            fresh=False,
            reason="version",
            version_stale=version_stale,
            spec_stale=spec_stale,
            built_version=built_version,
            running_version=running_version,
            built_spec_hash=built_spec_hash,
            running_spec_hash=running_spec_hash,
        )

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


def load_provenance(root: Path) -> dict | None:
    """Freshness-only load: read the provenance sidecar, not the map.

    ``dekko status`` (and any other caller that only needs to answer
    "is this map still fresh") does not need the parsed symbol/call
    graph ``load_map`` builds — only ``doc["provenance"]``. Reading
    the small ``.dekko/provenance.json`` sidecar (written alongside
    ``map.json`` by ``write_provenance_sidecar``) instead of the full
    map document is the win: ``map.json`` can run into the hundreds of
    MB on a large repo, while the sidecar is a few KB.

    The sidecar records the ``(mtime, size)`` signature of the
    ``map.json`` it was written next to (the same fast-path signature
    ``_freshness_from_provenance`` uses for individual source files).
    It is trusted only when that signature still matches — a
    hand-edited, externally regenerated, or otherwise desynced
    ``map.json`` (two independent files, same risk already called out
    for ``cache.json`` in ``cli._map_run_is_noop``'s docstring) falls
    back to a real parse rather than silently trusting stale data.

    Args:
        root: Repository root.

    Returns:
        The provenance dict, or ``None`` when ``map.json`` itself is
        missing (mirrors ``load_map``'s ``None`` in that case, so
        callers can tell "no map at all" from "map exists but has no
        provenance"). Falls back to parsing ``map.json`` directly —
        still cheaper than ``load_map``, since it skips building the
        symbol/call tables — when the sidecar is missing, stale, or
        unreadable.
    """
    map_path = root / _MAP_DIR / "map.json"
    current_sig = _stat_sig(map_path)
    if not current_sig:
        return None
    sidecar = root / _MAP_DIR / _PROVENANCE_FILE
    try:
        doc = _json_loads(sidecar.read_bytes())
    except (OSError, ValueError):
        doc = None
    if isinstance(doc, dict) and doc.get("map_stat") == current_sig:
        prov = doc.get("provenance")
        if isinstance(prov, dict):
            return prov
    try:
        full_doc = _json_loads(map_path.read_bytes())
    except (OSError, ValueError):
        return None
    prov = full_doc.get("provenance") if isinstance(full_doc, dict) else None
    return prov if isinstance(prov, dict) else None


def write_provenance_sidecar(root: Path, provenance: dict) -> None:
    """Write the small provenance-only sidecar next to ``map.json``.

    Lets ``load_provenance`` skip parsing the full ``map.json``
    document. ``map.json`` keeps its own embedded ``"provenance"`` key
    too — for backward compatibility with anything reading it
    directly, and as ``load_provenance``'s fallback when this sidecar
    is missing or stale. Must be called immediately after ``map.json``
    itself is written, so the recorded ``map_stat`` signature actually
    matches the file on disk.

    Args:
        root: Repository root; the sidecar is written under its
            ``.dekko/`` directory, which the caller is expected to
            have already created (e.g. via ``cache.ensure_dir``).
        provenance: The same provenance dict just embedded in
            ``map.json``.
    """
    map_path = root / _MAP_DIR / "map.json"
    path = root / _MAP_DIR / _PROVENANCE_FILE
    doc = {"provenance": provenance, "map_stat": _stat_sig(map_path)}
    atomic_write_bytes(path, _json_dumps(doc))
