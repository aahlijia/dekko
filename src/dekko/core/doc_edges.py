"""Links from one doc to another: paths, site routes and wikilinks.

The doc index resolves code references in ``core.docs``; this module
answers the narrower question of which doc (and which heading in it) a
link means. It returns a verdict and leaves recording it to the caller,
so the code-link rules and these never see each other's candidates.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import unquote

from dekko.classify import is_test_path

if TYPE_CHECKING:
    from dekko.core.docs import Candidate, DocSection

EXTRACTED = "EXTRACTED"
AMBIGUOUS = "AMBIGUOUS"

# More homes than this for one wikilink name tells a reader nothing.
_MAX_CANDIDATES = 5

# What a route or an extensionless ``:doc:`` target may be on disk, in
# the order a docs site tries them.
_PAGE_FORMS = (
    ".md",
    ".mdx",
    ".rst",
    "/index.md",
    "/index.mdx",
    "/index.rst",
    "/README.md",
)

_DOC_SUFFIX_RE = re.compile(r"\.(?:md|mdx|rst)$", re.IGNORECASE)


@dataclass(frozen=True)
class DocEdge:
    """A link from a doc section to another doc.

    Attributes:
        section: Index of the linking section in the doc index.
        line: 1-based line of the link.
        text: The link as written (path, route or wikilink name), with
            its ``#anchor`` when it has one.
        kind: ``link``, ``route`` or ``wiki``.
        tier: ``EXTRACTED`` or ``AMBIGUOUS``.
        target: The doc linked to; empty when ``AMBIGUOUS``.
        target_section: Index of the heading the anchor names, or
            ``None`` when the link names no heading.
        candidates: The docs an ``AMBIGUOUS`` wikilink could mean.
    """

    section: int
    line: int
    text: str
    kind: str
    tier: str
    target: str
    target_section: int | None = None
    candidates: tuple[str, ...] = ()


@dataclass(frozen=True)
class Broken:
    """A doc link that reaches nothing: ``missing`` or ``no-heading``."""

    text: str
    why: str


def slug(heading: str) -> str:
    """GitHub's anchor for a heading: lowercase, dashes for spaces."""
    text = re.sub(r"[^\w\- ]", "", heading.strip().lower())
    return text.replace(" ", "-")


@dataclass
class DocTargets:
    """Every indexed doc and its headings, for resolving links to them.

    Attributes:
        root: Repository root, to tell a missing route from another
            site's.
        docs: Every indexed doc path.
        stems: Lowercased file stem → docs with it, for wikilinks.
        bare: Path without extension → doc, for wikilinks with a path.
        route_roots: Folders a site route may be rooted at: the repo
            root (``""``) and each top-level folder holding docs.
        anchors: Doc → anchor → section index (explicit ids and
            GitHub slugs, numbered for repeats).
        titles: Doc → lowercased heading text → section index.
    """

    root: Path
    docs: set[str]
    stems: dict[str, list[str]] = field(default_factory=dict)
    bare: dict[str, str] = field(default_factory=dict)
    route_roots: list[str] = field(default_factory=list)
    anchors: dict[str, dict[str, int]] = field(default_factory=dict)
    titles: dict[str, dict[str, int]] = field(default_factory=dict)


def build_targets(
    root: Path, sections: list[DocSection], doc_paths: list[str]
) -> DocTargets:
    """Index the docs and their headings once per resolution pass."""
    targets = DocTargets(root=root, docs=set(doc_paths))
    tops = sorted({p.split("/", 1)[0] for p in doc_paths if "/" in p})
    targets.route_roots = ["", *tops]
    for path in doc_paths:
        stem = _DOC_SUFFIX_RE.sub("", path)
        targets.bare[stem] = path
        name = stem.rsplit("/", 1)[-1].lower()
        targets.stems.setdefault(name, []).append(path)
    for at, section in enumerate(sections):
        if section.level == 0:
            continue
        _add_heading(targets, at, section)
    return targets


def _add_heading(targets: DocTargets, at: int, section: DocSection) -> None:
    anchors = targets.anchors.setdefault(section.path, {})
    if section.anchor:
        anchors.setdefault(section.anchor, at)
    base = slug(section.heading)
    name, n = base, 0
    while name in anchors:
        n += 1
        name = f"{base}-{n}"
    anchors[name] = at
    titles = targets.titles.setdefault(section.path, {})
    titles.setdefault(section.heading.strip().lower(), at)


def resolve(
    targets: DocTargets, at: int, doc: str, cand: Candidate
) -> DocEdge | Broken | None:
    """What a link candidate means as a doc link.

    Args:
        targets: The indexed docs and their headings.
        at: Index of the section holding the link.
        doc: Path of the doc holding the link.
        cand: The candidate.

    Returns:
        An edge, a broken link, or ``None`` when the candidate is not a
        link to a doc (code rules take it from there) or is one that
        resolves to nothing worth reporting.
    """
    verdict = _verdict(targets, at, doc, cand)
    if isinstance(verdict, Broken) and is_test_path(doc):
        # Test fixtures plant broken links on purpose.
        return None
    return verdict


def _verdict(
    targets: DocTargets, at: int, doc: str, cand: Candidate
) -> DocEdge | Broken | None:
    if cand.kind == "wiki":
        return _wiki(targets, at, doc, cand)
    if cand.kind == "route":
        return _route(targets, at, doc, cand)
    if cand.kind != "link":
        return None
    if not cand.text:
        return _own_anchor(targets, doc, cand)
    return _link(targets, at, doc, cand)


def _link(
    targets: DocTargets, at: int, doc: str, cand: Candidate
) -> DocEdge | Broken | None:
    base = posixpath.dirname(doc)
    options = [
        posixpath.normpath(posixpath.join(base, cand.text)),
        posixpath.normpath(cand.text.lstrip("/")),
    ]
    hit = next((p for p in options if p in targets.docs), None)
    if hit is None:
        return None
    return _edge(targets, at, cand, [hit], cand.anchor)


def _own_anchor(
    targets: DocTargets, doc: str, cand: Candidate
) -> Broken | None:
    if _section_for(targets, doc, cand.anchor, wiki=False) is not None:
        return None
    return Broken(f"#{cand.anchor}", "no-heading")


def _route(
    targets: DocTargets, at: int, doc: str, cand: Candidate
) -> DocEdge | Broken | None:
    route = cand.text
    if route.startswith("/"):
        bases = [
            posixpath.normpath(posixpath.join(r, route.lstrip("/")))
            for r in targets.route_roots
        ]
    else:
        bases = [
            posixpath.normpath(posixpath.join(posixpath.dirname(doc), route))
        ]
    hits = list(
        dict.fromkeys(
            base + form
            for base in bases
            for form in _PAGE_FORMS
            if base + form in targets.docs
        )
    )
    if len(hits) == 1:
        return _edge(targets, at, cand, hits, cand.anchor)
    if not hits and _route_should_exist(targets, route):
        return Broken(_with_anchor(route, cand.anchor), "missing")
    return None


def _route_should_exist(targets: DocTargets, route: str) -> bool:
    """A route whose first folder this repo serves pages from.

    One-segment routes (``/blog``) are as likely another app on the
    same site as a page this repo forgot, so they never count.
    """
    parts = route.strip("/").split("/")
    if len(parts) < 2 or not route.startswith("/"):
        return False
    roots = [targets.root / r for r in targets.route_roots]
    if any((r / route.strip("/")).exists() for r in roots):
        # A repo folder or file, linked the way GitHub renders it.
        return False
    return any((r / parts[0]).is_dir() for r in roots)


def _wiki(
    targets: DocTargets, at: int, doc: str, cand: Candidate
) -> DocEdge | Broken | None:
    name = cand.text
    ext = posixpath.splitext(name)[1]
    if ext and not _DOC_SUFFIX_RE.search(name):
        # An embedded image or attachment, not a note.
        return None
    stem = _DOC_SUFFIX_RE.sub("", name)
    found = _wiki_targets(targets, doc, stem)
    if found:
        if len(found) > _MAX_CANDIDATES:
            return None
        return _edge(targets, at, cand, found, cand.anchor, wiki=True)
    return Broken(_with_anchor(name, cand.anchor), "missing")


def _wiki_targets(targets: DocTargets, doc: str, stem: str) -> list[str]:
    """Docs a wikilink name means: by path, then by file stem."""
    if "/" in stem:
        beside = posixpath.normpath(
            posixpath.join(posixpath.dirname(doc), stem)
        )
        for key in (posixpath.normpath(stem.lstrip("/")), beside):
            if key in targets.bare:
                return [targets.bare[key]]
        tail = "/" + stem.lstrip("/")
        return sorted(p for k, p in targets.bare.items() if k.endswith(tail))
    return sorted(targets.stems.get(stem.lower(), []))


def _edge(
    targets: DocTargets,
    at: int,
    cand: Candidate,
    found: list[str],
    anchor: str,
    *,
    wiki: bool = False,
) -> DocEdge | Broken:
    text = _with_anchor(cand.text, anchor)
    if len(found) > 1:
        return DocEdge(
            at, cand.line, text, cand.kind, AMBIGUOUS, "", None, tuple(found)
        )
    section = None
    if anchor:
        section = _section_for(targets, found[0], anchor, wiki=wiki)
        if section is None:
            return Broken(text, "no-heading")
    return DocEdge(
        at, cand.line, text, cand.kind, EXTRACTED, found[0], section
    )


def _section_for(
    targets: DocTargets, doc: str, anchor: str, *, wiki: bool
) -> int | None:
    """The section of ``doc`` an anchor names, if any."""
    wanted = unquote(anchor).strip()
    anchors = targets.anchors.get(doc, {})
    for key in (wanted, wanted.lower(), slug(wanted)):
        if key in anchors:
            return anchors[key]
    if wiki:
        return targets.titles.get(doc, {}).get(wanted.lower())
    return None


def _with_anchor(text: str, anchor: str) -> str:
    return f"{text}#{anchor}" if anchor else text
