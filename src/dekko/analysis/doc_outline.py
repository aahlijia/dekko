"""Outline of a doc: its heading tree, with what each section links to.

``dekko outline <doc>`` is the prose counterpart of a code outline: one
row per heading, indented by level, with the line range it spans and
how many code references and links to other docs it holds. Under a
budget the deepest headings go first, so the shape survives. A
directory outline lists its docs after its code files.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field

from dekko.analysis import docs as docs_mod
from dekko.analysis.doc_graph import DocGraph
from dekko.core.docs import DocSection
from dekko.render.mapfile import MapIndex
from dekko.textutil import estimate_tokens, fit_to_budget

# A directory outline shows each doc this deep; the file outline has
# every level.
_DIR_LEVELS = 2


@dataclass
class DocOutline:
    """One doc's headings and the links each section holds.

    Attributes:
        path: Repo-relative doc path.
        sections: The doc's sections in file order.
        code_links: Code references per section (same order).
        doc_links: Links to other docs per section (same order).
    """

    path: str
    sections: list[DocSection] = field(default_factory=list)
    code_links: list[int] = field(default_factory=list)
    doc_links: list[int] = field(default_factory=list)

    @property
    def kind(self) -> str:
        """``rst`` or ``markdown``."""
        return "rst" if self.path.lower().endswith(".rst") else "markdown"


def build(
    index: MapIndex, path: str, ats: list[int] | None = None
) -> DocOutline:
    """The outline of one indexed doc.

    Args:
        index: Loaded map index.
        path: The doc.
        ats: The doc's section indexes, when the caller has grouped
            them already (outlining a folder groups them once).

    Returns:
        The doc's outline.
    """
    if ats is None:
        ats = [i for i, s in enumerate(index.doc_sections) if s.path == path]
    code = Counter(
        index.doc_links[i].section
        for i in index.doc_links_by_doc.get(path, [])
    )
    links = Counter(
        index.doc_edges[i].section for i in index.doc_edges_out.get(path, [])
    )
    return DocOutline(
        path=path,
        sections=[index.doc_sections[i] for i in ats],
        code_links=[code[i] for i in ats],
        doc_links=[links[i] for i in ats],
    )


def collect_dir(index: MapIndex, prefix: str) -> list[DocOutline]:
    """Outlines of every doc under a directory, most linked first."""
    norm = prefix.strip("/")
    paths = [
        p
        for p in docs_mod.doc_paths(index)
        if norm in ("", ".") or p.startswith(norm + "/")
    ]
    graph = DocGraph.from_index(index, tests=True)
    paths.sort(key=lambda p: (-len(graph.sources_of(p)), p))
    by_doc: dict[str, list[int]] = {}
    for at, section in enumerate(index.doc_sections):
        by_doc.setdefault(section.path, []).append(at)
    return [build(index, p, by_doc.get(p, [])) for p in paths]


def header(outline: DocOutline) -> str:
    """``outline: path  [kind]  N sections · N code links · N doc links``."""
    shown = [s for s in outline.sections if s.level > 0]
    return (
        f"outline: {outline.path}  [{outline.kind}]  "
        f"{_count(len(shown), 'section')} · "
        f"{_count(sum(outline.code_links), 'code link')} · "
        f"{_count(sum(outline.doc_links), 'doc link')}"
    )


def rows(outline: DocOutline, max_level: int | None = None) -> list[str]:
    """One row per heading, indented by level, down to ``max_level``."""
    out = []
    for section, code, links in _shown(outline):
        if max_level is None or section.level <= max_level:
            out.append(_row(section, code, links))
    return out


def render_text(outline: DocOutline, budget: int | None, limit: int) -> None:
    """Print one doc's outline, dropping the deepest headings first."""
    prefix = header(outline)
    shown = _shown(outline)
    picked = _shallowest_first(prefix, shown, budget, limit)
    kept, meter = fit_to_budget(picked, budget, limit, prefix=prefix)
    meter.total = len(shown)
    print(prefix)
    for row in kept:
        print(row)
    print(meter.footer())


def _shallowest_first(
    prefix: str,
    shown: list[tuple[DocSection, int, int]],
    budget: int | None,
    limit: int | None,
) -> list[str]:
    """Rows that fit, taken top level first, then printed in doc order.

    Every level-1 heading goes in before any level 2, and so on, so a
    long doc keeps its outer shape and as much of the next level as
    the budget allows.
    """
    rows_at = [_row(s, code, links) for s, code, links in shown]
    order = sorted(range(len(shown)), key=lambda i: (shown[i][0].level, i))
    used = estimate_tokens(prefix)
    chosen: list[int] = []
    for i in order:
        if limit is not None and len(chosen) >= limit:
            break
        cost = estimate_tokens(rows_at[i]) + 1
        if budget and chosen and used + cost > budget:
            break
        chosen.append(i)
        used += cost
    return [rows_at[i] for i in sorted(chosen)]


def json_entry(outline: DocOutline) -> dict:
    """The doc's outline as a JSON object."""
    return {
        "path": outline.path,
        "kind": outline.kind,
        "sections": [
            {
                "heading": s.heading,
                "level": s.level,
                "line": s.line,
                "end_line": s.end_line,
                "anchor": s.anchor,
                "code_links": code,
                "doc_links": links,
            }
            for s, code, links in _shown(outline)
        ],
    }


def render_json(
    target: str, outlines: list[DocOutline], budget: int | None
) -> None:
    """Print doc outlines as JSON, dropping whole docs past the budget."""
    entries = [json.dumps(json_entry(o)) for o in outlines]
    kept, meter = fit_to_budget(entries, budget, None)
    doc = {
        "target": target,
        "files": [],
        "docs": [json.loads(e) for e in kept],
        "meta": meter.as_dict(),
    }
    print(json.dumps(doc, indent=2))


def dir_rows(outlines: list[DocOutline]) -> list[str]:
    """Each doc as its header and its top headings, for a folder view."""
    out: list[str] = []
    for outline in outlines:
        out.append(header(outline))
        out += rows(outline, max_level=_DIR_LEVELS)
    return out


def _shown(outline: DocOutline) -> list[tuple[DocSection, int, int]]:
    """Sections under a heading; text before the first one is no row."""
    return [
        (s, code, links)
        for s, code, links in zip(
            outline.sections, outline.code_links, outline.doc_links
        )
        if s.level > 0
    ]


def _row(section: DocSection, code: int, links: int) -> str:
    indent = "  " * section.level
    counts = [f"{code} code"] if code else []
    counts += [f"{links} doc"] if links else []
    tail = f"  ({', '.join(counts)})" if counts else ""
    span = f"{section.line}-{section.end_line}"
    return f"{indent}{span}  {section.heading}{tail}"


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"
