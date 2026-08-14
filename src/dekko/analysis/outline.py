"""Structural outline of a file or directory: signatures, no bodies.

``dekko outline <path>`` renders a file's module doc and every symbol's
signature, first doc line, and line number — the file's shape at roughly
a tenth of the cost of reading it. ``<path>`` may also be a directory,
which rolls every mapped file under it into one budgeted report.

The outline is built entirely from the loaded map; the only file read is
a best-effort size estimate for the "full file vs outline" comparison,
which is simply omitted when the source is unreadable.
"""

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from dekko.classify import is_test_path
from dekko.render.mapfile import MapIndex
from dekko.core.model import TYPE_KINDS, Symbol
from dekko.analysis.query import paths_matching
from dekko.source import read_lines
from dekko.textutil import estimate_tokens, fit_to_budget, oneline

EXIT_OK = 0
EXIT_NOT_FOUND = 3
EXIT_AMBIGUOUS = 4

_DOC_LIMIT = 80


@dataclass
class FileOutline:
    """One file's structural outline, drawn from the map index.

    Attributes:
        path: Repo-relative file path.
        language: Language name, or empty when unknown.
        doc: Module docstring first line, or ``None``.
        error: Parse error message, or ``None``.
        symbols: The file's symbols in definition order.
    """

    path: str
    language: str
    doc: str | None = None
    error: str | None = None
    symbols: list[Symbol] = field(default_factory=list)


def build(index: MapIndex, path: str) -> FileOutline:
    """Assemble the outline of one (already validated) file."""
    return FileOutline(
        path=path,
        language=index.languages_by_path.get(path, ""),
        doc=index.docs_by_path.get(path),
        error=index.errors_by_path.get(path),
        symbols=list(index.symbols_by_path.get(path, [])),
    )


def _under(path: str, prefix: str) -> bool:
    """Whether a repo-relative path sits under a directory prefix."""
    if prefix in ("", "."):
        return True
    return path == prefix or path.startswith(prefix + "/")


def collect_dir(index: MapIndex, prefix: str) -> list[FileOutline]:
    """Outlines of every mapped file under a directory, prod first."""
    norm = prefix.strip("/")
    files = [p for p in index.languages_by_path if _under(p, norm)]
    files.sort(key=lambda p: (is_test_path(p), p))
    return [build(index, p) for p in files]


def _outline_sig(sym: Symbol) -> str:
    """Signature using the bare name; nesting is shown by indentation."""
    if sym.kind in TYPE_KINDS:
        return f"{sym.kind} {sym.name}"
    if sym.kind == "variable":
        return sym.name
    parts = [f"{p.name}: {p.type}" if p.type else p.name for p in sym.params]
    sig = f"{sym.name}({', '.join(parts)})"
    if sym.returns:
        sig += f" -> {sym.returns}"
    return sig


def _symbol_row(sym: Symbol) -> str:
    """One outline row: indent + line + signature + first doc line."""
    indent = "  " * (sym.qualname.count(".") + 1)
    row = f"{indent}{sym.start_line}  {_outline_sig(sym)}"
    if sym.doc:
        row += f" — {oneline(sym.doc, _DOC_LIMIT)}"
    return row


def _file_header(fo: FileOutline) -> list[str]:
    """Header rows for a file: identity, module doc, parse error."""
    lines = [f"outline: {fo.path}  [{fo.language}]"]
    if fo.doc:
        lines.append(f"  {oneline(fo.doc, _DOC_LIMIT)}")
    if fo.error:
        lines.append(f"  (parse error: {fo.error})")
    return lines


def _sym_json(sym: Symbol) -> dict:
    """Structured rendering of one outlined symbol."""
    return {
        "line": sym.start_line,
        "kind": sym.kind,
        "signature": _outline_sig(sym),
        "doc": sym.doc,
    }


def _full_tokens(root: Path | None, path: str) -> int:
    """Estimated token cost of reading the whole file (0 if unknown)."""
    if root is None:
        return 0
    return estimate_tokens("\n".join(read_lines(root, path)))


def _size_line(full: int, outline_tokens: int) -> str | None:
    """The 'full file vs outline' savings line, or ``None``."""
    if full <= 0:
        return None
    raw_pct = 100 * outline_tokens / full
    # Rounding to the nearest integer reads a true ~0.1% ratio as a
    # misleading "(0%)" on very-high-savings files. Fall back to one
    # decimal place only in that band; normal cases (1%, 45%, ...)
    # render exactly as before, with no ".0" noise.
    pct = f"{raw_pct:.1f}" if 0 < raw_pct < 1 else f"{round(raw_pct)}"
    return f"full ≈ {full} tok · outline ≈ {outline_tokens} tok ({pct}%)"


# Thresholds for ``_sparse_note``: a file has to be substantial
# (``_SPARSE_MIN_FULL_TOKENS``) with very few named symbols
# (``_SPARSE_MAX_SYMBOLS``) *and* an outline that covers only a
# sliver of it (``_SPARSE_MAX_RATIO``) before the caveat fires — a
# genuinely small/simple file (little content either way) must not
# trip this.
_SPARSE_MIN_FULL_TOKENS = 500
_SPARSE_MAX_SYMBOLS = 8
_SPARSE_MAX_RATIO = 0.15


def _file_outline_tokens(fo: FileOutline) -> int:
    """Token estimate of one file's full (untrimmed) outline rows."""
    rows = _file_header(fo) + [_symbol_row(s) for s in fo.symbols]
    return estimate_tokens("\n".join(rows))


def _sparse_note(
    full: int,
    outline_tokens: int,
    symbol_count: int,
    error: str | None = None,
) -> str | None:
    """Caveat when a file's outline looks suspiciously thin for its size.

    A callback-heavy file — handlers registered via anonymous
    function expressions passed to a handful of top-level calls (an
    MCP server's ``server.tool("x", async () => {...})``, an Express
    app's ``router.get(...)``) — has almost no *named* symbols for
    the extractor to find, so its outline can look like an extreme
    (and perfectly legitimate-looking) token-savings ratio while
    actually hiding nearly the entire file's real content (bug #9/B9
    — claude-buddy's 1,344-line MCP server entrypoint outlined to 5
    rows/43 tokens, ~0.2% of the full file). This doesn't detect the
    callback pattern itself (that needs new extraction work) — it
    flags the precondition (large file, almost no named symbols, and
    an outline covering only a sliver of it) so a caller knows to
    cross-check with a targeted grep before trusting a near-empty
    outline as complete.

    Args:
        full: Estimated tokens of the whole file (0 if unknown).
        outline_tokens: Estimated tokens of the file's full,
            untrimmed outline (see ``_file_outline_tokens``).
        symbol_count: Number of symbols the outline lists.
        error: The file's ``FileOutline.error``, if any. A file that
            failed to parse at all (round-12 master report §3.9: most
            often an unsupported/uninstalled grammar — Kotlin/Groovy
            without ``pip install dekko[all]``) always has 0 symbols,
            which used to trip this heuristic en masse even though
            the real cause is already shown in the file's own
            ``(parse error: ...)`` header line, not a callback-heavy
            file the outline is silently missing content from.

    Returns:
        A one-line caveat, or ``None`` when the file doesn't look
        anomalously sparse.
    """
    if error:
        return None
    if full < _SPARSE_MIN_FULL_TOKENS or symbol_count > _SPARSE_MAX_SYMBOLS:
        return None
    if full <= 0 or outline_tokens / full > _SPARSE_MAX_RATIO:
        return None
    return (
        "this file has very few named symbols for its size — if it "
        "registers behavior via anonymous callbacks (route handlers, "
        "event listeners, tool/command registration, ...), this "
        "outline may be missing most of its real content; consider "
        "`grep` for the registration pattern"
    )


def size_estimate(
    index: MapIndex, root: Path, path: str
) -> tuple[int, int] | None:
    """Token cost of reading a file whole vs. outlining it.

    The single source of truth for the "full file vs outline" trade,
    reused by the proactive push layer (``orient --read``) so its
    advisory and ``outline``'s own size line agree.

    Args:
        index: Loaded map index.
        root: Repository root the path is relative to.
        path: Repo-relative POSIX path of a single mapped file.

    Returns:
        ``(full_tokens, outline_tokens)`` for a mapped, readable file,
        or ``None`` when ``path`` is not a mapped file or cannot be read.
    """
    if path not in index.languages_by_path:
        return None
    full = _full_tokens(root, path)
    if full <= 0:
        return None
    fo = build(index, path)
    rows = _file_header(fo) + [_symbol_row(s) for s in fo.symbols]
    return full, estimate_tokens("\n".join(rows))


def _render_text_file(
    fo: FileOutline, root: Path | None, budget: int | None, limit: int
) -> int:
    """Render a single-file outline; header is non-droppable prefix."""
    prefix = "\n".join(_file_header(fo))
    rows = [_symbol_row(s) for s in fo.symbols]
    kept, meter = fit_to_budget(rows, budget, limit, prefix=prefix)
    print(prefix)
    for row in kept:
        print(row)
    full = _full_tokens(root, fo.path)
    size = _size_line(full, meter.tokens)
    if size:
        print(size)
    note = _sparse_note(
        full, _file_outline_tokens(fo), len(fo.symbols), fo.error
    )
    if note:
        print(f"  note: {note}", file=sys.stderr)
    print(meter.footer())
    return EXIT_OK


def _render_text_dir(
    target: str,
    outlines: list[FileOutline],
    root: Path | None,
    budget: int | None,
    limit: int,
) -> int:
    """Render a directory rollup; whole low-priority files drop first."""
    banner = f"outline: {target} — {len(outlines)} files"
    rows: list[str] = []
    for fo in outlines:
        rows += _file_header(fo)
        rows += [_symbol_row(s) for s in fo.symbols]
    kept, meter = fit_to_budget(rows, budget, limit, prefix=banner)
    print(banner)
    for row in kept:
        print(row)
    full = sum(_full_tokens(root, fo.path) for fo in outlines)
    size = _size_line(full, meter.tokens)
    if size:
        print(size)
    for fo in outlines:
        note = _sparse_note(
            _full_tokens(root, fo.path),
            _file_outline_tokens(fo),
            len(fo.symbols),
            fo.error,
        )
        if note:
            print(f"  note: {fo.path} — {note}", file=sys.stderr)
    print(meter.footer())
    return EXIT_OK


def _render_json(
    target: str,
    outlines: list[FileOutline],
    root: Path | None,
    budget: int | None,
    limit: int,
) -> int:
    """Emit the outline(s) as structured JSON with a cost meter."""
    flat = [(fi, s) for fi, fo in enumerate(outlines) for s in fo.symbols]
    serialized = [json.dumps(_sym_json(s)) for _, s in flat]
    kept, meter = fit_to_budget(serialized, budget, limit)
    keep_by_file: dict[int, list[Symbol]] = {}
    for fi, sym in flat[: len(kept)]:
        keep_by_file.setdefault(fi, []).append(sym)
    files = []
    for fi, fo in enumerate(outlines):
        kept_syms = keep_by_file.get(fi, [])
        full = _full_tokens(root, fo.path)
        entry = {
            "path": fo.path,
            "language": fo.language,
            "doc": fo.doc,
            "error": fo.error,
            "symbols": [_sym_json(s) for s in kept_syms],
            "full_tokens": full,
        }
        note = _sparse_note(full, _file_outline_tokens(fo), len(fo.symbols))
        if note:
            entry["sparse_note"] = note
        files.append(entry)
    doc = {"target": target, "files": files, "meta": meter.as_dict()}
    print(json.dumps(doc, indent=2))
    return EXIT_OK


def run(
    index: MapIndex,
    target: str,
    root: Path | None,
    budget: int | None,
    limit: int,
    as_json: bool,
) -> int:
    """Render the outline of a file or directory.

    Args:
        index: Loaded map index.
        target: A mapped file path (or trailing suffix) or a directory.
        root: Repository root, for the best-effort size estimate.
        budget: Approximate token budget, or ``None``.
        limit: Cap on symbol rows.
        as_json: Emit structured JSON instead of text.

    Returns:
        Process exit code.
    """
    matches = paths_matching(index, target)
    if len(matches) > 1:
        print(f"dekko: '{target}' is ambiguous; candidates:", file=sys.stderr)
        for p in matches:
            print(f"  {p}", file=sys.stderr)
        return EXIT_AMBIGUOUS
    if len(matches) == 1:
        outlines = [build(index, matches[0])]
    else:
        outlines = collect_dir(index, target)
    if not outlines:
        print(
            f"dekko: no mapped file or directory '{target}'", file=sys.stderr
        )
        return EXIT_NOT_FOUND

    if as_json:
        return _render_json(target, outlines, root, budget, limit)
    if len(outlines) == 1:
        return _render_text_file(outlines[0], root, budget, limit)
    return _render_text_dir(target, outlines, root, budget, limit)
