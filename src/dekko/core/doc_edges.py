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
_EXT_RE = re.compile(r"\.[A-Za-z0-9]{1,8}$")
_HTML_ID_RE = re.compile(
    r"""<[a-zA-Z][^>]*?\b(?:id|name)\s*=\s*["']([^"']+)["']"""
)
_MD_LINK_TEXT_RE = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_HTML_TAG_RE = re.compile(r"<[^>]+>")

# Every browser scrolls to the page's top for ``#top``, with or without
# an element of that id.
_TOP = "top"

# Front matter that gives a page its own route: Docusaurus ``slug``,
# Jekyll ``permalink``.
_FRONT_ROUTE_RE = re.compile(r"""^(slug|permalink)\s*:\s*["']?([^"'\s#]+)""")


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


# Folders whose docs are test material (fixtures, test notes). Wider
# test-path rules (``spec/``, ``testing/``) would also take spec and
# test-plan folders, which are exactly the docs a reader wants.
_TEST_DOC_DIRS = frozenset({"test", "tests", "__tests__"})


def is_test_doc(path: str) -> bool:
    """Whether a doc lives in a test folder."""
    return any(part in _TEST_DOC_DIRS for part in path.split("/")[:-1])


def names_a_file(name: str) -> bool:
    """Whether a wikilink names a non-doc file: code, an image, a PDF.

    ``[[src/app.py]]`` or ``![[chart.png]]`` is a file reference, not a
    note; ``[[v1.2 notes]]`` has a dot but no extension.
    """
    ext = _EXT_RE.search(name.rsplit("/", 1)[-1])
    return ext is not None and not _DOC_SUFFIX_RE.search(name)


def slug(heading: str) -> str:
    """GitHub's anchor for a heading: lowercase, dashes for spaces.

    GitHub slugs the rendered heading, so a link in it counts by its
    text and HTML tags drop out.
    """
    text = _MD_LINK_TEXT_RE.sub(r"\1", heading)
    text = _HTML_TAG_RE.sub("", text)
    text = re.sub(r"[^\w\- ]", "", text.strip().lower())
    return text.replace(" ", "-")


def front_matter_routes(path: str, text: str) -> list[str]:
    """Routes a doc's front matter gives it, without their slashes.

    ``slug: /start`` and ``permalink: /about/team/`` are site routes; a
    relative ``slug: why-us`` sits beside the doc. Jekyll placeholders
    (``/:title/``) name no fixed route.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return []
    out: list[str] = []
    for line in lines[1:]:
        if line.strip() in ("---", "..."):
            return out
        m = _FRONT_ROUTE_RE.match(line)
        if m is None or ":" in m.group(2):
            continue
        value = m.group(2)
        if m.group(1) == "slug" and not value.startswith("/"):
            value = posixpath.join(posixpath.dirname(path), value)
        key = posixpath.normpath(value).strip("/")
        if key and key != ".":
            out.append(key)
    return []


def html_anchors(text: str) -> list[tuple[int, str]]:
    """``(line, id)`` for each HTML ``id``/``name`` attribute in a doc.

    ``<a id="readme-top"></a>`` and ``<h2 id="x">`` are link targets
    as real as a heading.
    """
    return [
        (num, m.group(1))
        for num, line in enumerate(text.splitlines(), 1)
        if "<" in line
        for m in _HTML_ID_RE.finditer(line)
    ]


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
        slugs: Route (no slashes) → docs whose front matter claims it.
    """

    root: Path
    docs: set[str]
    stems: dict[str, list[str]] = field(default_factory=dict)
    bare: dict[str, str] = field(default_factory=dict)
    route_roots: list[str] = field(default_factory=list)
    anchors: dict[str, dict[str, int]] = field(default_factory=dict)
    titles: dict[str, dict[str, int]] = field(default_factory=dict)
    slugs: dict[str, list[str]] = field(default_factory=dict)


def build_targets(
    root: Path,
    sections: list[DocSection],
    doc_paths: list[str],
    html_ids: dict[str, list[tuple[int, str]]] | None = None,
    routes: dict[str, list[str]] | None = None,
) -> DocTargets:
    """Index the docs and their headings once per resolution pass.

    Args:
        root: Repository root.
        sections: Every doc section, in index order.
        doc_paths: Every indexed doc.
        html_ids: Doc → ``(line, id)`` of its HTML anchors, each
            reaching the section that holds its line.
        routes: Doc → the routes its front matter gives it.

    Returns:
        The lookups resolution uses.
    """
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
    _add_html_ids(targets, sections, html_ids or {})
    for doc, keys in (routes or {}).items():
        for key in keys:
            targets.slugs.setdefault(key, []).append(doc)
    return targets


def _add_html_ids(
    targets: DocTargets,
    sections: list[DocSection],
    html_ids: dict[str, list[tuple[int, str]]],
) -> None:
    starts: dict[str, list[tuple[int, int]]] = {}
    for at, section in enumerate(sections):
        starts.setdefault(section.path, []).append((section.line, at))
    for doc, ids in html_ids.items():
        spans = starts.get(doc, [])
        for line, anchor in ids:
            held = [at for start, at in spans if start <= line]
            at = held[-1] if held else (spans[0][1] if spans else None)
            if at is not None:
                targets.anchors.setdefault(doc, {}).setdefault(anchor, at)


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
    if isinstance(verdict, Broken) and is_test_doc(doc):
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
    if cand.anchor.lower() == _TOP:
        return None
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
    pages = [
        base + form
        for base in bases
        for form in _PAGE_FORMS
        if base + form in targets.docs
    ]
    hits = list(dict.fromkeys([*pages, *_slug_hits(targets, route)]))
    if len(hits) == 1:
        return _edge(targets, at, cand, hits, cand.anchor)
    if hits:
        return None
    if route.startswith("/"):
        missing = _route_should_exist(targets, route)
    else:
        missing = not _on_disk(targets.root, bases[0])
    if not missing:
        return None
    return Broken(_with_anchor(route, cand.anchor), "missing")


def _slug_hits(targets: DocTargets, route: str) -> list[str]:
    """Docs whose front matter claims ``route``, under any route root."""
    if not route.startswith("/") or not targets.slugs:
        return []
    path = route.strip("/")
    hits: list[str] = []
    for root in targets.route_roots:
        if not root:
            hits += targets.slugs.get(path, [])
        elif path.startswith(root + "/"):
            hits += targets.slugs.get(path[len(root) + 1 :], [])
    return hits


def _on_disk(root: Path, base: str) -> bool:
    """Whether a relative Sphinx doc name is some file the doc index
    doesn't read (a notebook, a ``.txt`` page) or a folder."""
    where = root / base
    if where.exists():
        return True
    return where.parent.is_dir() and any(where.parent.glob(where.name + ".*"))


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
    if anchor and anchor.lower() != _TOP:
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
