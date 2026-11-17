"""Read side of the doc index: which sections name code, and doc rot.

Pure lookups and ranking over a loaded ``MapIndex``; ``query`` renders
them for ``query docs``/``query refs``, ``docs --stale`` for rot, and
``contextpack``/``workset``/``query symbol`` for their ``docs:`` lines.
"""

import re
from dataclasses import dataclass

from dekko.core.docs import AMBIGUOUS, EXTRACTED, INFERRED, DocSection
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
    text = re.sub(r"[^\w\- ]", "", heading.strip().lower())
    return text.replace(" ", "-")


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
