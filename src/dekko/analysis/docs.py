"""Read side of the doc index: which sections name code, and doc rot.

Pure lookups and ranking over a loaded ``MapIndex``; ``query`` renders
them for ``query docs``/``query refs``, ``docs --stale`` for rot, and
``contextpack``/``workset``/``query symbol`` for their ``docs:`` lines.
"""

import json
import re
import sys
from dataclasses import dataclass

from dekko.core import doc_edges
from dekko.core.docs import (
    AMBIGUOUS,
    EXTRACTED,
    INFERRED,
    DocDangling,
    DocSection,
)
from dekko.render.mapfile import MapIndex

_TIER_RANK = {EXTRACTED: 0, INFERRED: 1, AMBIGUOUS: 2}

# Release notes cite removed names and old paths on purpose; they rank
# last and stay out of the stale report unless asked for.
_CHANGELOG_RE = re.compile(
    r"^(changelog|changes|release[-_ ]?notes|releases?|history|news)\b",
    re.IGNORECASE,
)

NO_DOC_INDEX = (
    "dekko: this map predates the doc index; run `dekko map` to index "
    "the repo's docs"
)


@dataclass(frozen=True)
class SectionHit:
    """One doc section that names a target.

    Attributes:
        section: The section.
        tier: The strongest tier among its references to the target.
        refs: The distinct reference texts, in document order.
        candidates: For an ``AMBIGUOUS`` hit, how many things the
            reference could mean.
    """

    section: DocSection
    tier: str
    refs: tuple[str, ...]
    candidates: int = 0


def is_changelog(path: str) -> bool:
    """Whether a doc is release notes, by its file name."""
    return bool(_CHANGELOG_RE.match(path.rsplit("/", 1)[-1]))


def sections_naming(index: MapIndex, targets: list[str]) -> list[SectionHit]:
    """Every section with a link to any of ``targets``, best first.

    Ranked by changelog last, then tier, then fewer links in the section
    (a focused section beats a catalog), then path and line.
    """
    by_section: dict[int, list[int]] = {}
    for target in targets:
        for i in index.doc_links_by_target.get(target, []):
            by_section.setdefault(index.doc_links[i].section, []).append(i)
    hits = [_hit(index, at, ids) for at, ids in by_section.items()]
    size = _links_per_section(index)
    return sorted(
        hits,
        key=lambda h: (
            is_changelog(h.section.path),
            _TIER_RANK[h.tier],
            size.get((h.section.path, h.section.line), 0),
            h.section.path,
            h.section.line,
        ),
    )


def _hit(index: MapIndex, at: int, link_ids: list[int]) -> SectionHit:
    links = [index.doc_links[i] for i in sorted(set(link_ids))]
    best = min(links, key=lambda link: _TIER_RANK[link.tier])
    return SectionHit(
        section=index.doc_sections[at],
        tier=best.tier,
        refs=tuple(dict.fromkeys(link.text for link in links)),
        candidates=len(best.candidates),
    )


def _links_per_section(index: MapIndex) -> dict[tuple[str, int], int]:
    counts: dict[tuple[str, int], int] = {}
    for link in index.doc_links:
        section = index.doc_sections[link.section]
        key = (section.path, section.line)
        counts[key] = counts.get(key, 0) + 1
    return counts


def top_hits(
    index: MapIndex, targets: list[str], n: int
) -> tuple[list[SectionHit], int]:
    """The ``n`` best sections naming ``targets``, and how many there are.

    Empty on a map that predates the doc index.
    """
    if not index.has_doc_index:
        return [], 0
    hits = sections_naming(index, targets)
    return hits[:n], len(hits)


def hit_label(hit: SectionHit) -> str:
    """``path:line heading``, the compact form a ``docs:`` line lists."""
    s = hit.section
    return f"{s.path}:{s.line} {s.heading}"


def docs_line(hits: list[SectionHit], total: int) -> str:
    """The ``docs:`` line body: labels, then ``+N more`` past them."""
    text = ", ".join(hit_label(h) for h in hits)
    if total > len(hits):
        text += f" +{total - len(hits)} more"
    return text


def file_targets(index: MapIndex, path: str) -> list[str]:
    """A file's own path, then every symbol defined in it."""
    return [path, *(s.id for s in index.symbols_by_path.get(path, []))]


def doc_paths(index: MapIndex) -> list[str]:
    """Every indexed doc, in path order."""
    return sorted({s.path for s in index.doc_sections})


def match_doc(index: MapIndex, doc: str) -> list[str]:
    """Indexed docs equal to ``doc`` or ending in ``/doc``."""
    doc = doc.removeprefix("./")
    known = doc_paths(index)
    if doc in known:
        return [doc]
    return [p for p in known if p.endswith("/" + doc)]


def slug(heading: str) -> str:
    """GitHub's anchor for a heading: lowercase, dashes for spaces."""
    return doc_edges.slug(heading)


def sections_of(index: MapIndex, doc: str, heading: str | None) -> list[int]:
    """Section indexes of ``doc``, or of its sections named ``heading``.

    ``heading`` matches the heading text case-insensitively, or its
    GitHub-style slug.
    """
    wanted = heading.lower() if heading else None
    return [
        i
        for i, s in enumerate(index.doc_sections)
        if s.path == doc
        and (
            wanted is None
            or s.heading.lower() == wanted
            or slug(s.heading) == wanted
        )
    ]


# --- docs --stale -----------------------------------------------------------

EXIT_CLEAN = 0
EXIT_STALE = 1
EXIT_CANNOT_TELL = 2

_WHY_TEXT = {
    "missing": "missing",
    "past-end": "past end of file",
    "no-member": "no such member",
    "no-heading": "no such heading",
}


def why_text(why: str, now: str | None) -> str:
    """A dangling reference's reason, as a reader would say it."""
    return _WHY_TEXT.get(why) or f"moved: now {now}"


def run_stale(
    index: MapIndex,
    paths: list[str],
    include_changelogs: bool,
    as_json: bool,
) -> int:
    """Print every doc reference that no longer points at code.

    Args:
        index: Loaded map index.
        paths: Only docs at or under these repo-relative paths; empty
            for every doc.
        include_changelogs: Report release notes too; they cite removed
            names and old paths on purpose, so they are skipped by
            default.
        as_json: Emit JSON instead of text.

    Returns:
        0 when nothing is stale, 1 when something is, 2 when the map
        predates the doc index.
    """
    if not index.has_doc_index:
        print(NO_DOC_INDEX, file=sys.stderr)
        return EXIT_CANNOT_TELL
    rows = [d for d in index.doc_dangling if _wanted(index, d, paths)]
    skipped = 0
    if not include_changelogs:
        kept = [d for d in rows if not _in_changelog(index, d)]
        skipped, rows = len(rows) - len(kept), kept
    if as_json:
        stale = [_stale_json(index, d) for d in rows]
        print(json.dumps({"stale": stale, "count": len(stale)}, indent=2))
    else:
        _print_stale(index, rows)
    if skipped:
        print(
            f"dekko: note: {skipped} in changelogs not shown "
            "(--all shows them)",
            file=sys.stderr,
        )
    return EXIT_STALE if rows else EXIT_CLEAN


def _wanted(index: MapIndex, d: DocDangling, paths: list[str]) -> bool:
    if not paths:
        return True
    doc = index.doc_sections[d.section].path
    for raw in paths:
        prefix = raw.removeprefix("./").rstrip("/")
        if doc == prefix or doc.startswith(prefix + "/"):
            return True
    return False


def _in_changelog(index: MapIndex, d: DocDangling) -> bool:
    return is_changelog(index.doc_sections[d.section].path)


def _stale_json(index: MapIndex, d: DocDangling) -> dict:
    section = index.doc_sections[d.section]
    return {
        "path": section.path,
        "line": d.line,
        "heading": section.heading,
        "text": d.text,
        "why": d.why,
        "now": d.now,
    }


def _print_stale(index: MapIndex, rows: list[DocDangling]) -> None:
    """Group by doc: the doc's path, then one indented row per ref."""
    current = None
    for d in sorted(rows, key=lambda r: (_doc_of(index, r), r.line)):
        doc = _doc_of(index, d)
        if doc != current:
            print(doc)
            current = doc
        print(f"  {d.line}  {d.text}  ({why_text(d.why, d.now)})")


def _doc_of(index: MapIndex, d: DocDangling) -> str:
    return index.doc_sections[d.section].path
