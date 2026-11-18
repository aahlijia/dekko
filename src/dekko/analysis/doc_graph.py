"""The link graph over a repo's docs, shared by every doc-aware command.

Nodes are the indexed docs, edges the resolved links between them.
Nav files (a docs site's table of contents) link to everything, so they
are kept as entry points but left out of what measures how docs relate:
which are most linked, how they cluster, how one leads to another.
"""

from __future__ import annotations

import posixpath
import re
from collections import Counter
from dataclasses import dataclass, field

from dekko.classify import is_test_path
from dekko.core.doc_edges import EXTRACTED
from dekko.render.mapfile import MapIndex

# Table-of-contents files by name: mdBook, docsify, Jupyter Book.
NAV_NAMES = frozenset({"summary.md", "_sidebar.md", "_toc.md"})

# A page this mostly made of linked list items, with at least this
# many links out, is a hand-written table of contents.
_NAV_MIN_LINKS = 10
_NAV_LIST_SHARE = 0.8

# A doc that links out to this many others and is linked from at most
# one reads as a starting point.
_ENTRY_MIN_OUT = 5
_ENTRY_NAMES = frozenset({"readme", "index"})

_LIST_LINK_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+.*(?:\]\(|\[\[)")


@dataclass
class DocGraph:
    """Docs and the links between them.

    Attributes:
        docs: Every doc in the graph, in path order.
        nav: Docs that are tables of contents.
        edges: ``(from, to)`` → number of links, resolved ones only,
            self-links left out.
        orphans: Docs no other doc links to, in path order.
        entries: Docs a reader starts from: README and index files, nav
            files, and docs that link out widely but are seldom linked.
    """

    docs: list[str]
    nav: set[str] = field(default_factory=set)
    edges: dict[tuple[str, str], int] = field(default_factory=dict)
    orphans: list[str] = field(default_factory=list)
    entries: list[str] = field(default_factory=list)
    _sources: dict[str, set[str]] = field(default_factory=dict, repr=False)
    _targets: dict[str, set[str]] = field(default_factory=dict, repr=False)

    @classmethod
    def from_index(cls, index: MapIndex, *, tests: bool = False) -> DocGraph:
        """Build the graph from a loaded map.

        Args:
            index: The map, with its doc index.
            tests: Keep docs under test paths (left out by default).

        Returns:
            The doc graph.
        """
        docs = sorted(
            {
                s.path
                for s in index.doc_sections
                if tests or not is_test_path(s.path)
            }
        )
        graph = cls(docs=docs)
        graph.edges = _edges(index, set(docs))
        for a, b in graph.edges:
            graph._sources.setdefault(b, set()).add(a)
            graph._targets.setdefault(a, set()).add(b)
        graph.nav = {d for d in docs if _is_nav(index, d, graph.edges)}
        graph.orphans = [d for d in docs if not graph.sources_of(d)]
        graph.entries = [d for d in docs if graph._is_entry(d)]
        return graph

    @property
    def cluster_edges(self) -> dict[tuple[str, str], int]:
        """The edges that say how docs relate.

        A site-wide table of contents (one found by name, or one that
        links to half the docs or more) cuts across every topic, so its
        edges go. A section index (``languages.md`` listing the language
        pages) stays: it is what ties that section's pages together.
        """
        wide = {d for d in self.nav if self.is_site_wide(d)}
        return {
            pair: n
            for pair, n in self.edges.items()
            if pair[0] not in wide and pair[1] not in wide
        }

    def is_site_wide(self, doc: str) -> bool:
        """A table of contents for the whole site, not one section."""
        if doc not in self.nav:
            return False
        if posixpath.basename(doc).lower() in NAV_NAMES:
            return True
        return len(self.targets_of(doc)) * 2 >= len(self.docs)

    def sources_of(self, doc: str) -> set[str]:
        """Docs that link to ``doc``."""
        return self._sources.get(doc, set())

    def targets_of(self, doc: str) -> set[str]:
        """Docs ``doc`` links to."""
        return self._targets.get(doc, set())

    def most_linked(self, n: int) -> list[tuple[str, int]]:
        """The ``n`` docs the most other docs link to, nav links aside."""
        counts = Counter(
            b for a, b in self.edges if a not in self.nav and b not in self.nav
        )
        ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        return ranked[:n]

    def _is_entry(self, doc: str) -> bool:
        stem = posixpath.splitext(posixpath.basename(doc))[0].lower()
        if stem in _ENTRY_NAMES or doc in self.nav:
            return True
        return (
            len(self.targets_of(doc)) >= _ENTRY_MIN_OUT
            and len(self.sources_of(doc)) <= 1
        )


def _edges(index: MapIndex, docs: set[str]) -> dict[tuple[str, str], int]:
    out: Counter[tuple[str, str]] = Counter()
    for edge in index.doc_edges:
        source = index.doc_sections[edge.section].path
        if edge.tier != EXTRACTED or source == edge.target:
            continue
        if source in docs and edge.target in docs:
            out[(source, edge.target)] += 1
    return dict(out)


def _is_nav(
    index: MapIndex, doc: str, edges: dict[tuple[str, str], int]
) -> bool:
    if posixpath.basename(doc).lower() in NAV_NAMES:
        return True
    links_out = sum(n for (a, _), n in edges.items() if a == doc)
    if links_out < _NAV_MIN_LINKS:
        return False
    return _list_share(index, doc) >= _NAV_LIST_SHARE


def _list_share(index: MapIndex, doc: str) -> float:
    """Share of a doc's non-blank lines that are list items with a link."""
    if index.root_dir is None:
        return 0.0
    try:
        text = (index.root_dir / doc).read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return 0.0
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return 0.0
    return sum(bool(_LIST_LINK_RE.match(line)) for line in lines) / len(lines)
