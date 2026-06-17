"""Render the extracted symbol/call graph as MAP.md.

Two output shapes share one set of section renderers: a single
``MAP.md`` (the default, small repos) and a sharded set where ``MAP.md``
is an index and each directory's file sections live on a
``map/<dir-slug>.md`` page. A ``_Links`` context turns a global anchor
key into the right href for the current page, so a symbol's anchor is
the same in either shape — only the page prefix differs.
"""

import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import classify, export, mapfile, summary
from .model import CallGraph, FileMap, Symbol
from .resolver import MODULE_CALLER_SUFFIX
from .textutil import oneline, signature

_SLUG = re.compile(r"[^a-z0-9]+")

# File-section / symbol ordering for ``dekko map --order``.
ORDER_MODES = ("path", "name", "fan-in")


@dataclass
class RunStats:
    """Provenance counts for the human-facing freshness/trust line.

    Attributes:
        elapsed_ms: Wall-clock extraction time for the run.
        reused: Files served from the prior extraction cache.
        parsed: Files freshly parsed this run.
    """

    elapsed_ms: int
    reused: int
    parsed: int


SHARD_MODES = ("auto", "always", "never")
# Auto-shard once the single-file document would exceed either limit.
_SHARD_LINE_LIMIT = 4000
_SHARD_BYTE_LIMIT = 200_000


def _dir_of(path: str) -> str:
    """Directory portion of a repo-relative path (``.`` for the root)."""
    head, _, _ = path.rpartition("/")
    return head or "."


def _is_minor(fm: FileMap) -> bool:
    """A file with nothing worth its own section.

    No symbols, no module doc, and no parse error — e.g. a data file or
    an empty ``__init__``. These collapse into a per-directory
    ``also present:`` line instead of an empty section.
    """
    return not fm.error and not fm.symbols and not fm.doc


def _file_fan_in(fm: FileMap, graph: CallGraph) -> int:
    """Total inbound call edges across a file's symbols."""
    return sum(len(graph.calls_in.get(s.id, [])) for s in fm.symbols)


def _order_files(
    files: list[FileMap], graph: CallGraph, order: str
) -> list[FileMap]:
    """Reorder file sections, keeping each directory's files together.

    ``path`` keeps discovery order; ``name`` sorts by base filename;
    ``fan-in`` puts the most depended-on files first. The directory is
    always the primary key so the TOC's per-directory grouping and the
    flat single-file body stay aligned.
    """
    if order == "name":
        return sorted(
            files,
            key=lambda fm: (_dir_of(fm.path), fm.path.rsplit("/", 1)[-1]),
        )
    if order == "fan-in":
        return sorted(
            files,
            key=lambda fm: (
                _dir_of(fm.path),
                -_file_fan_in(fm, graph),
                fm.path,
            ),
        )
    return list(files)


def _ordered_symbols(
    fm: FileMap, graph: CallGraph, order: str
) -> list[Symbol]:
    """A file's symbols, by inbound degree when ``order == 'fan-in'``."""
    if order != "fan-in":
        return fm.symbols
    return sorted(
        fm.symbols,
        key=lambda s: (-len(graph.calls_in.get(s.id, [])), s.start_line),
    )


def _owner_path(key: str) -> str:
    """File path owning an anchor key (a file path or a symbol id)."""
    if key.endswith(MODULE_CALLER_SUFFIX):
        return key[: -len(MODULE_CALLER_SUFFIX)]
    return key.split("::", 1)[0]


class _Anchors:
    """Stable, unique markdown anchor ids for files and symbols."""

    def __init__(self) -> None:
        self._by_key: dict[str, str] = {}
        self._used: set[str] = set()

    def get(self, key: str) -> str:
        """Return the anchor id for a file path or symbol id."""
        anchor = self._by_key.get(key)
        if anchor is not None:
            return anchor
        base = _SLUG.sub("-", key.lower()).strip("-") or "x"
        anchor = base
        counter = 2
        while anchor in self._used:
            anchor = f"{base}-{counter}"
            counter += 1
        self._used.add(anchor)
        self._by_key[key] = anchor
        return anchor


class _Links:
    """Resolve markdown hrefs for anchors, single-file or sharded.

    Anchor keys (file paths, symbol ids) are global, so the anchor id
    for a target is identical in both shapes; only the page prefix
    differs. In sharded mode ``on_page`` tracks which page is being
    rendered so same-page links stay bare ``#anchor`` and cross-page
    links carry the sibling (or ``map/``) prefix.
    """

    def __init__(
        self,
        anchors: _Anchors,
        sharded: bool,
        dir_slugs: dict[str, str],
    ) -> None:
        self._anchors = anchors
        self._sharded = sharded
        self._slugs = dir_slugs
        self._page: str | None = None

    def on_page(self, slug: str | None) -> None:
        """Set the current page slug (``None`` for the index page)."""
        self._page = slug

    def anchor(self, key: str) -> str:
        """Anchor id for a key (for the ``<a id=...>`` target)."""
        return self._anchors.get(key)

    def href(self, key: str) -> str:
        """Full href to a key's anchor from the current page."""
        anchor = self._anchors.get(key)
        if not self._sharded:
            return f"#{anchor}"
        slug = self._slugs[_dir_of(_owner_path(key))]
        if self._page is None:
            return f"map/{slug}.md#{anchor}"
        if slug == self._page:
            return f"#{anchor}"
        return f"{slug}.md#{anchor}"


def _dir_slugs(files: list[FileMap]) -> dict[str, str]:
    """Unique page slug per directory, in first-seen order."""
    used: set[str] = set()
    out: dict[str, str] = {}
    for directory in dict.fromkeys(_dir_of(fm.path) for fm in files):
        base = _SLUG.sub("-", directory.lower()).strip("-") or "root"
        slug = base
        counter = 2
        while slug in used:
            slug = f"{base}-{counter}"
            counter += 1
        used.add(slug)
        out[directory] = slug
    return out


def _indexes(
    files: list[FileMap], graph: CallGraph
) -> tuple[dict[str, Symbol], dict[str, list[tuple[str, int]]]]:
    """Build the per-render lookup tables (symbols, ambiguous calls)."""
    symbols_by_id = {sym.id: sym for fm in files for sym in fm.symbols}
    ambiguous: dict[str, list[tuple[str, int]]] = {}
    for caller, name, cands in graph.ambiguous:
        ambiguous.setdefault(caller, []).append((name, len(cands)))
    return symbols_by_id, ambiguous


def render_markdown(
    files: list[FileMap],
    graph: CallGraph,
    root_label: str,
    run_stats: RunStats | None = None,
    root: Path | None = None,
    order: str = "path",
) -> str:
    """Render the single-file ``MAP.md`` document.

    Args:
        files: Per-file extraction results, in output order.
        graph: Resolved call graph.
        root_label: Display name of the mapped root.
        run_stats: Optional freshness counts for the trust line.
        root: Repository root, enabling the churn x fan-in hotspots
            view; omitted (e.g. in render-only tests) means no churn.
        order: File-section order — ``path`` (default), ``name``, or
            ``fan-in`` (which also orders symbols within a file).

    Returns:
        The complete markdown text.
    """
    files = _order_files(files, graph, order)
    links = _Links(_Anchors(), sharded=False, dir_slugs={})
    symbols_by_id, ambiguous = _indexes(files, graph)
    index = mapfile.index_from_maps(files, graph, root_label)

    lines = _header(files, graph, root_label, run_stats)
    lines += summary.render_overview(
        summary.compute(index),
        links.href,
        _overview_diagram(index),
        _hotspot_rows(index, root),
    )
    lines += _toc(files, links)
    for fm in files:
        if _is_minor(fm):
            continue
        lines += _file_section(
            fm, graph, symbols_by_id, ambiguous, links, order
        )
    return "\n".join(lines) + "\n"


def _hotspot_rows(index: mapfile.MapIndex, root: Path | None) -> list[dict]:
    """Churn x fan-in rows for the overview, or empty without a root."""
    if root is None:
        return []
    return summary.churn_hotspots(index, root)


def render_map(
    files: list[FileMap],
    graph: CallGraph,
    root_label: str,
    shard: str,
    run_stats: RunStats | None = None,
    root: Path | None = None,
    order: str = "path",
) -> list[tuple[str, str]]:
    """Render the map as one or more ``(page_path, content)`` pairs.

    The first pair is always the index (``MAP.md``). In sharded mode
    further pairs are ``map/<dir-slug>.md`` directory pages.

    Args:
        files: Per-file extraction results, in output order.
        graph: Resolved call graph.
        root_label: Display name of the mapped root.
        shard: ``never`` (always single-file), ``always`` (always
            sharded), or ``auto`` (shard once the single document
            crosses the size threshold).
        run_stats: Optional freshness counts for the trust line.
        root: Repository root, enabling the churn x fan-in hotspots
            view.
        order: File-section order (``path``/``name``/``fan-in``).

    Returns:
        One pair for single-file mode, index + pages for sharded mode.
    """
    if shard == "always":
        return _sharded(files, graph, root_label, run_stats, root, order)
    single = render_markdown(files, graph, root_label, run_stats, root, order)
    if shard == "auto" and _exceeds_threshold(single):
        return _sharded(files, graph, root_label, run_stats, root, order)
    return [("MAP.md", single)]


def _exceeds_threshold(text: str) -> bool:
    """Whether a single-file document is large enough to auto-shard."""
    return (
        text.count("\n") + 1 > _SHARD_LINE_LIMIT
        or len(text.encode("utf-8")) > _SHARD_BYTE_LIMIT
    )


def _sharded(
    files: list[FileMap],
    graph: CallGraph,
    root_label: str,
    run_stats: RunStats | None = None,
    root: Path | None = None,
    order: str = "path",
) -> list[tuple[str, str]]:
    """Render the index page plus one page per directory."""
    files = _order_files(files, graph, order)
    slugs = _dir_slugs(files)
    links = _Links(_Anchors(), sharded=True, dir_slugs=slugs)
    symbols_by_id, ambiguous = _indexes(files, graph)
    index = mapfile.index_from_maps(files, graph, root_label)

    links.on_page(None)
    idx = _header(files, graph, root_label, run_stats)
    idx += summary.render_overview(
        summary.compute(index),
        links.href,
        _overview_diagram(index),
        _hotspot_rows(index, root),
    )
    idx += _toc(files, links)
    pages = [("MAP.md", "\n".join(idx) + "\n")]

    by_dir: dict[str, list[FileMap]] = {}
    for fm in files:
        by_dir.setdefault(_dir_of(fm.path), []).append(fm)
    for directory, dir_files in by_dir.items():
        section = [fm for fm in dir_files if not _is_minor(fm)]
        if not section:
            # A directory of only data/empty files lives in the index's
            # "also present" line; it gets no page (nothing to link to).
            continue
        slug = slugs[directory]
        links.on_page(slug)
        page = [
            f"# `{directory}/`",
            "",
            f"[← {root_label} code map](../MAP.md)",
            "",
        ]
        for fm in section:
            page += _file_section(
                fm, graph, symbols_by_id, ambiguous, links, order
            )
        pages.append((f"map/{slug}.md", "\n".join(page) + "\n"))
    return pages


def _overview_diagram(index: mapfile.MapIndex) -> list[str]:
    """A fenced ``mermaid`` architecture block for the overview.

    Delegates graph selection and the scale guard to
    ``export.overview_graph`` so MAP.md and ``dekko export`` share one
    generator. GitHub renders the block natively — no toolchain or
    network is involved.

    Returns:
        Markdown lines: a ```` ```mermaid ```` block, a one-line
        pointer when the graph is too large, or an empty list when
        there is nothing to draw.
    """
    labels, edges, status = export.overview_graph(
        index, export.DEFAULT_MAX_NODES
    )
    if status == "empty":
        return []
    if status == "too_big":
        return [
            f"*Architecture diagram omitted: {len(labels)} directory "
            f"nodes exceed {export.DEFAULT_MAX_NODES}. Run "
            "`dekko export --format mermaid` for the full graph.*",
            "",
        ]
    return ["```mermaid", export.render_mermaid(labels, edges), "```", ""]


def _header(
    files: list[FileMap],
    graph: CallGraph,
    root_label: str,
    run_stats: RunStats | None = None,
) -> list[str]:
    """Document title and stats block."""
    by_lang = Counter(fm.language for fm in files)
    funcs = sum(
        1
        for fm in files
        for s in fm.symbols
        if s.kind in ("function", "method")
    )
    classes = sum(1 for fm in files for s in fm.symbols if s.kind == "class")
    when = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    langs = ", ".join(f"{lang} {n}" for lang, n in by_lang.most_common())
    lines = [
        f"# Code Map — {root_label}",
        "",
        f"Generated by dekko on {when}. Do not edit by hand.",
        "",
        "> **Agents:** prefer `dekko summary` for an overview and "
        "`dekko query | context | affected` (or the dekko MCP tools) "
        "for specifics — this file is the human-readable index and can "
        "be large.",
        "",
        f"**{len(files)}** files ({langs}) · "
        f"**{funcs}** functions/methods · **{classes}** classes · "
        f"**{len(graph.edges)}** call edges "
        f"({len(graph.ambiguous)} ambiguous, "
        f"{len(graph.external)} external — see map.json)",
        "",
    ]
    if run_stats is not None:
        lines += [
            f"*Mapped {len(files)} files in {run_stats.elapsed_ms} ms "
            f"(cache: {run_stats.reused} reused / "
            f"{run_stats.parsed} parsed).*",
            "",
        ]
    return lines


def _toc(files: list[FileMap], links: _Links) -> list[str]:
    """Table of contents: production files by directory, tests collapsed.

    Test files (A1's path classification) move into a ``<details>``
    block GitHub renders collapsed, keeping the index focused on
    production code while plain viewers still see the full list.
    """
    prod = [fm for fm in files if not classify.is_test_path(fm.path)]
    tests = [fm for fm in files if classify.is_test_path(fm.path)]
    lines = ["## Contents", ""]
    lines += _toc_entries(prod, links)
    if tests:
        summary_tag = f"<summary>tests ({len(tests)} files)</summary>"
        lines += [f"<details>{summary_tag}", ""]
        lines += _toc_entries(tests, links)
        lines += ["</details>", ""]
    return lines


def _toc_entries(files: list[FileMap], links: _Links) -> list[str]:
    """Per-directory TOC bullets, with minor files collapsed to one line."""
    by_dir: dict[str, list[FileMap]] = {}
    for fm in files:
        by_dir.setdefault(_dir_of(fm.path), []).append(fm)
    lines: list[str] = []
    for directory, dir_files in by_dir.items():
        lines.append(f"- **{directory}/**")
        minor: list[str] = []
        for fm in dir_files:
            if _is_minor(fm):
                minor.append(fm.path.rsplit("/", 1)[-1])
            else:
                lines.append(_toc_bullet(fm, links))
        if minor:
            names = ", ".join(f"`{m}`" for m in minor)
            lines.append(f"  - *also present:* {names}")
    if lines:
        lines.append("")
    return lines


def _toc_bullet(fm: FileMap, links: _Links) -> str:
    """One file's TOC bullet: link, symbol count, and purpose line.

    The redundant ``(parse error)`` marker is dropped — the Overview's
    parse-error list already carries it — and zero-symbol files show no
    count.
    """
    base = fm.path.rsplit("/", 1)[-1]
    count = len(fm.symbols)
    suffix = f" ({count} symbols)" if count else ""
    purpose = ""
    if fm.doc and not fm.error:
        purpose = f" — {oneline(fm.doc, 80)}"
    return f"  - [`{base}`]({links.href(fm.path)}){suffix}{purpose}"


def _file_section(
    fm: FileMap,
    graph: CallGraph,
    symbols_by_id: dict[str, Symbol],
    ambiguous_by_caller: dict[str, list[tuple[str, int]]],
    links: _Links,
    order: str = "path",
) -> list[str]:
    """One ``##`` section per file with all its symbols."""
    lines = [
        "---",
        "",
        f'## <a id="{links.anchor(fm.path)}"></a> `{fm.path}`',
        "",
    ]
    if fm.error:
        lines += [f"*{fm.language} — parse error: {fm.error}*", ""]
        return lines
    meta = f"{fm.language} · {len(fm.symbols)} symbols"
    if fm.doc:
        meta += f" — {oneline(fm.doc, 100)}"
    lines += [f"*{meta}*", ""]
    for sym in _ordered_symbols(fm, graph, order):
        lines += _symbol_block(
            sym, graph, symbols_by_id, ambiguous_by_caller, links
        )
    return lines


def _symbol_block(
    sym: Symbol,
    graph: CallGraph,
    symbols_by_id: dict[str, Symbol],
    ambiguous_by_caller: dict[str, list[tuple[str, int]]],
    links: _Links,
) -> list[str]:
    """Heading + relations for one symbol."""
    anchor = links.anchor(sym.id)
    lines = [
        f'### <a id="{anchor}"></a> `{signature(sym)}`',
        "",
        f"*{sym.kind} · lines {sym.start_line}-{sym.end_line}*",
        "",
    ]
    if sym.doc:
        lines += [oneline(sym.doc, 120), ""]
    relations = _relations(
        sym, graph, symbols_by_id, ambiguous_by_caller, links
    )
    if relations:
        lines += [*relations, ""]
    return lines


def _relations(
    sym: Symbol,
    graph: CallGraph,
    symbols_by_id: dict[str, Symbol],
    ambiguous_by_caller: dict[str, list[tuple[str, int]]],
    links: _Links,
) -> list[str]:
    """``calls`` / ``called by`` bullet lines for a symbol."""
    lines = []
    out_links = [
        _link(callee, symbols_by_id, links)
        for callee in graph.calls_out.get(sym.id, [])
    ]
    for name, count in ambiguous_by_caller.get(sym.id, []):
        out_links.append(f"`{name}` *(ambiguous: {count} candidates)*")
    if out_links:
        lines.append(f"- **calls:** {', '.join(out_links)}")
    in_links = [
        _link(caller, symbols_by_id, links)
        for caller in graph.calls_in.get(sym.id, [])
    ]
    if in_links:
        lines.append(f"- **called by:** {', '.join(in_links)}")
    return lines


def _link(sym_id: str, symbols_by_id: dict[str, Symbol], links: _Links) -> str:
    """Markdown link to a symbol, or a plain label for top level."""
    if sym_id.endswith(MODULE_CALLER_SUFFIX):
        path = sym_id[: -len(MODULE_CALLER_SUFFIX)]
        return f"top level of [`{path}`]({links.href(path)})"
    sym = symbols_by_id.get(sym_id)
    if sym is None:
        return f"`{sym_id}`"
    return f"[`{sym.qualname}`]({links.href(sym_id)})"
