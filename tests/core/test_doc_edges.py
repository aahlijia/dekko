"""Links between docs: anchors, heading ids, wikilinks and site routes."""

from pathlib import Path

from dekko.analysis.docs import why_text
from dekko.core.docs import (
    Candidate,
    DocIndex,
    DocSection,
    build_doc_index,
    parse_doc,
)


def _sections(path: str, text: str) -> list[DocSection]:
    return parse_doc(path, text)[0]


def _cands(path: str, text: str) -> list[Candidate]:
    return [c for group in parse_doc(path, text)[1] for c in group]


def _links(path: str, text: str) -> set[tuple[str, str, str, str]]:
    """``(text, kind, anchor, label)`` of every link-like candidate."""
    return {
        (c.text, c.kind, c.anchor, c.label)
        for c in _cands(path, text)
        if c.kind in ("link", "wiki", "route")
    }


# --- anchors and heading ids ----------------------------------------------


def test_a_link_keeps_its_heading_anchor() -> None:
    text = "# A\nsee [setup](guide/setup.md#install)\n"

    assert _links("README.md", text) == {
        ("guide/setup.md", "link", "install", "setup")
    }


def test_a_line_anchor_stays_a_line_reference() -> None:
    text = "# A\n[code](src/app.py#L10-L20)\n"

    assert _links("README.md", text) == {
        ("src/app.py:10-20", "link", "", "code")
    }


def test_a_same_doc_anchor_is_a_link_with_no_path() -> None:
    text = "# A\njump to [usage](#usage)\n## Usage\n"

    assert _links("README.md", text) == {("", "link", "usage", "usage")}


def test_an_explicit_heading_id_is_kept_apart_from_the_heading() -> None:
    text = "# Edit Predictions {#edit-predictions}\nx\n## Plain\ny\n"

    sections = _sections("docs/ai.md", text)

    assert [(s.heading, s.anchor) for s in sections] == [
        ("Edit Predictions", "edit-predictions"),
        ("Plain", ""),
    ]


def test_a_heading_id_with_classes_is_read_too() -> None:
    text = "## Keys {#keys .unnumbered}\n"

    assert _sections("a.md", text)[0].anchor == "keys"


# --- wikilinks ------------------------------------------------------------


def test_wikilink_forms() -> None:
    text = (
        "# Note\n"
        "[[Plain]] and [[folder/Deep|the deep note]]\n"
        "[[Topic#Setup]] [[Topic#Setup|setup]] ![[diagram]]\n"
    )

    assert _links("vault/note.md", text) == {
        ("Plain", "wiki", "", ""),
        ("folder/Deep", "wiki", "", "the deep note"),
        ("Topic", "wiki", "Setup", ""),
        ("Topic", "wiki", "Setup", "setup"),
        ("diagram", "wiki", "", ""),
    }


def test_wikilinks_inside_code_are_ignored() -> None:
    text = (
        "# Note\n"
        "`[[Inline]]` is code\n"
        "```bash\n"
        "if [[ -f x ]]; then echo; fi\n"
        "[[Fenced]]\n"
        "```\n"
    )

    assert not {c for c in _cands("note.md", text) if c.kind == "wiki"}


def test_a_bash_test_in_prose_is_not_a_wikilink() -> None:
    text = "# Note\nrun if [[ -f x ]] holds\n"

    assert not {c for c in _cands("note.md", text) if c.kind == "wiki"}


def test_a_wikilink_path_is_not_also_a_prose_path() -> None:
    text = "# Note\nsee [[notes/other.md]]\n"

    kinds = {(c.text, c.kind) for c in _cands("note.md", text)}

    assert kinds == {("notes/other.md", "wiki")}


def test_wikilinks_are_markdown_only() -> None:
    text = "Title\n=====\n\nsee [[Other]]\n"

    assert not {c for c in _cands("guide.rst", text) if c.kind == "wiki"}


# --- site routes ----------------------------------------------------------


def test_an_extensionless_absolute_link_is_a_route() -> None:
    text = "# A\n[YOLO](/features/yolo-mode) and [cfg](/cli/config/#flags)\n"

    assert _links("docs/index.mdx", text) == {
        ("/features/yolo-mode", "route", "", "YOLO"),
        ("/cli/config", "route", "flags", "cfg"),
    }


def test_an_absolute_link_with_an_extension_stays_a_link() -> None:
    text = "# A\n![logo](/assets/logo.png) [g](/docs/guide.md)\n"

    assert _links("README.md", text) == {
        ("/assets/logo.png", "link", "", "logo"),
        ("/docs/guide.md", "link", "", "g"),
    }


def test_a_url_is_not_a_route() -> None:
    text = "# A\n[site](https://example.com/features/x) [p](//cdn.x/y)\n"

    assert not {c for c in _cands("a.md", text) if c.kind == "route"}


def test_an_mdx_href_route_is_a_route() -> None:
    text = '# A\n<Card title="Rules" href="/features/cline-rules">x</Card>\n'

    assert _links("docs/index.mdx", text) == {
        ("/features/cline-rules", "route", "", "")
    }


def test_an_href_to_a_file_is_left_alone() -> None:
    text = '# A\n<a href="src/app.py">app</a>\n'

    assert not {c for c in _cands("a.md", text) if c.kind == "route"}


def test_an_rst_doc_role_is_a_route() -> None:
    text = (
        "Title\n=====\n\n"
        "see :doc:`install` and :doc:`the guide </guide/start>`\n"
    )

    assert _links("docs/index.rst", text) == {
        ("install", "route", "", ""),
        ("/guide/start", "route", "", ""),
    }


# --- resolution -----------------------------------------------------------

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def _index(name: str) -> DocIndex:
    root = _FIXTURES / name
    docs = sorted(
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.suffix in (".md", ".mdx", ".rst")
    )
    return build_doc_index(root, docs, [])


def _edges(index: DocIndex) -> set[tuple[str, str, str, str]]:
    """``(source doc, target doc, target heading, tier)`` per edge."""
    out = set()
    for e in index.edges:
        heading = (
            index.sections[e.target_section].heading
            if e.target_section is not None
            else ""
        )
        target = e.target or "|".join(e.candidates)
        out.add((index.sections[e.section].path, target, heading, e.tier))
    return out


def _dangling(index: DocIndex) -> set[tuple[str, str, str]]:
    return {
        (index.sections[d.section].path, d.text, d.why) for d in index.dangling
    }


def test_wikilinks_resolve_by_path_stem_and_heading() -> None:
    edges = _edges(_index("docvault"))

    assert edges == {
        ("Home.md", "Projects/Alpha.md", "", "EXTRACTED"),
        ("Home.md", "Beta.md", "", "EXTRACTED"),
        ("Home.md", "Projects/Alpha.md", "Goals", "EXTRACTED"),
        ("Home.md", "diagram.md", "", "EXTRACTED"),
        ("Home.md", "a/index.md|b/index.md", "", "AMBIGUOUS"),
        ("Projects/Alpha.md", "Home.md", "", "EXTRACTED"),
        ("Beta.md", "Projects/Alpha.md", "Goals", "EXTRACTED"),
        ("Orphan.md", "Home.md", "", "EXTRACTED"),
    }


def test_broken_wikilinks_and_headings_dangle() -> None:
    assert _dangling(_index("docvault")) == {
        ("Home.md", "Missing Note", "missing"),
        ("Beta.md", "Projects/Alpha#Nope", "no-heading"),
    }


def test_doc_links_and_routes_resolve_with_their_headings() -> None:
    edges = _edges(_index("docsite"))

    assert edges == {
        ("README.md", "docs/guide/setup.md", "", "EXTRACTED"),
        ("docs/SUMMARY.md", "docs/index.mdx", "", "EXTRACTED"),
        ("docs/SUMMARY.md", "docs/guide/setup.md", "", "EXTRACTED"),
        ("docs/SUMMARY.md", "docs/features/rules.mdx", "", "EXTRACTED"),
        ("docs/SUMMARY.md", "docs/api/index.md", "", "EXTRACTED"),
        ("docs/index.mdx", "docs/features/rules.mdx", "", "EXTRACTED"),
        ("docs/index.mdx", "docs/features/rules.mdx", "Custom", "EXTRACTED"),
        ("docs/index.mdx", "docs/api/index.md", "", "EXTRACTED"),
        ("docs/guide/setup.md", "docs/index.mdx", "", "EXTRACTED"),
        (
            "docs/guide/setup.md",
            "docs/features/rules.mdx",
            "Example",
            "EXTRACTED",
        ),
    }


def test_a_repeated_heading_is_reached_by_its_numbered_slug() -> None:
    index = _index("docsite")

    edge = next(
        e
        for e in index.edges
        if index.sections[e.section].path == "docs/guide/setup.md"
        and e.target_section is not None
    )

    assert index.sections[edge.target_section].line == 9


def test_broken_doc_links_routes_and_anchors_dangle() -> None:
    assert _dangling(_index("docsite")) == {
        ("docs/index.mdx", "/features/gone", "missing"),
        ("docs/guide/setup.md", "../features/rules.mdx#nope", "no-heading"),
    }


def test_a_route_two_roots_hold_resolves_to_nothing(tmp_path: Path) -> None:
    for top in ("docs", "site"):
        (tmp_path / top / "guide").mkdir(parents=True)
        (tmp_path / top / "guide" / "start.md").write_text("# Start\n")
    (tmp_path / "README.md").write_text("# R\n[s](/guide/start)\n")
    docs = ["README.md", "docs/guide/start.md", "site/guide/start.md"]

    index = build_doc_index(tmp_path, docs, [])

    assert not index.edges
    assert not index.dangling


def test_a_one_segment_route_never_dangles(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# R\n[blog](/blog)\n")

    index = build_doc_index(tmp_path, ["README.md"], [])

    assert not index.edges
    assert not index.dangling


def test_a_relative_doc_role_resolves_beside_the_doc(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "index.rst").write_text(
        "Index\n=====\n\nsee :doc:`install`\n"
    )
    (tmp_path / "docs" / "install.rst").write_text("Install\n=======\n")

    index = build_doc_index(
        tmp_path, ["docs/index.rst", "docs/install.rst"], []
    )

    assert [e.target for e in index.edges] == ["docs/install.rst"]


def test_a_stem_shared_by_more_than_five_docs_is_dropped(
    tmp_path: Path,
) -> None:
    docs = []
    for n in range(6):
        (tmp_path / f"d{n}").mkdir()
        (tmp_path / f"d{n}" / "index.md").write_text("# I\n")
        docs.append(f"d{n}/index.md")
    (tmp_path / "Home.md").write_text("# H\n[[index]]\n")

    index = build_doc_index(tmp_path, ["Home.md", *docs], [])

    assert not index.edges
    assert not index.dangling


def test_an_embedded_image_is_not_a_doc_link(tmp_path: Path) -> None:
    (tmp_path / "Home.md").write_text("# H\n![[chart.png]]\n")

    index = build_doc_index(tmp_path, ["Home.md"], [])

    assert not index.edges
    assert not index.dangling


def test_a_wikilink_in_a_test_doc_never_dangles(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "notes.md").write_text("# N\n[[Nowhere]]\n")

    index = build_doc_index(tmp_path, ["tests/notes.md"], [])

    assert not index.dangling


def test_a_link_to_a_doc_is_not_a_code_link(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("# A\n[b](b.md)\n")
    (tmp_path / "b.md").write_text("# B\n")

    index = build_doc_index(tmp_path, ["a.md", "b.md"], [])

    assert not index.links
    assert [e.target for e in index.edges] == ["b.md"]


def test_a_missing_heading_reads_as_such() -> None:
    assert why_text("no-heading", None) == "no such heading"


def test_a_test_doc_never_reports_a_broken_doc_link(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "a.md").write_text(
        "# A\n[b](b.md#nope) [c](#gone)\n"
    )
    (tmp_path / "tests" / "b.md").write_text("# B\n")

    index = build_doc_index(tmp_path, ["tests/a.md", "tests/b.md"], [])

    assert not index.dangling
    assert [e.target for e in index.edges] == []


def test_a_route_to_a_repo_folder_is_not_missing(tmp_path: Path) -> None:
    (tmp_path / "crates" / "editor").mkdir(parents=True)
    (tmp_path / "CONTRIBUTING.md").write_text(
        "# C\n[editor](/crates/editor)\n"
    )

    index = build_doc_index(tmp_path, ["CONTRIBUTING.md"], [])

    assert not index.dangling


def test_anchors_a_browser_resolves_never_dangle(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text(
        '<a id="readme-top"></a>\n'
        "# Project\n\n"
        "[Back to top](#top) [again](#readme-top)\n"
        '<a name="custom-anchor"></a>\n'
        "[custom](#custom-anchor) [other](other.md#html-id)\n"
        "[guide](other.md#see-the-guide-here)\n"
    )
    (tmp_path / "other.md").write_text(
        '# Other\n\n<h2 id="html-id">Raw HTML</h2>\n\n'
        "## See [the guide](docs/guide.md) here\n"
    )

    index = build_doc_index(tmp_path, ["README.md", "other.md"], [])

    assert index.dangling == []
    targets = {
        (e.text, index.sections[e.target_section].heading) for e in index.edges
    }
    assert ("other.md#html-id", "Other") in targets
    assert (
        "other.md#see-the-guide-here",
        "See [the guide](docs/guide.md) here",
    ) in targets


def test_a_link_to_another_doc_s_top_has_no_heading(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("# A\n[b](b.md#top)\n")
    (tmp_path / "b.md").write_text("# B\n")

    index = build_doc_index(tmp_path, ["a.md", "b.md"], [])

    assert [(e.target, e.target_section) for e in index.edges] == [
        ("b.md", None)
    ]
    assert index.dangling == []


# --- Sphinx toctrees, encoded targets, front-matter routes ----------------


def test_toctree_entries_are_routes() -> None:
    text = (
        "Index\n=====\n\n"
        ".. toctree::\n"
        "   :maxdepth: 2\n"
        "   :caption: Contents\n"
        "\n"
        "   install\n"
        "   The guide </guide/start>\n"
        "   usage.rst\n"
        "   self\n"
        "   api/*\n"
        "   https://example.com/elsewhere\n"
        "\n"
        "After the tree, plain text.\n"
    )

    assert _links("docs/index.rst", text) == {
        ("install", "route", "", ""),
        ("/guide/start", "route", "", ""),
        ("usage", "route", "", ""),
    }


def test_a_myst_toctree_fence_lists_routes() -> None:
    text = (
        "# Index\n\n```{toctree}\n:maxdepth: 1\n\n"
        "install\nguide/start.md\n```\n"
    )

    assert _links("docs/index.md", text) == {
        ("install", "route", "", ""),
        ("guide/start", "route", "", ""),
    }


def test_toctree_entries_resolve_and_a_missing_one_dangles(
    tmp_path: Path,
) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "index.rst").write_text(
        "Index\n=====\n\n.. toctree::\n\n   install\n   gone\n"
    )
    (tmp_path / "docs" / "install.rst").write_text("Install\n=======\n")

    index = build_doc_index(
        tmp_path, ["docs/index.rst", "docs/install.rst"], []
    )

    assert [e.target for e in index.edges] == ["docs/install.rst"]
    assert [(d.text, d.why) for d in index.dangling] == [("gone", "missing")]


def test_a_relative_doc_role_with_no_doc_dangles(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "index.rst").write_text(
        "Index\n=====\n\nsee :doc:`instal`\n"
    )

    index = build_doc_index(tmp_path, ["docs/index.rst"], [])

    assert [(d.text, d.why) for d in index.dangling] == [("instal", "missing")]


def test_a_percent_encoded_link_resolves(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text(
        "# R\n[notes](my%20notes.md#caf%C3%A9)\n"
    )
    (tmp_path / "my notes.md").write_text("# Notes\n## Café\n")

    index = build_doc_index(tmp_path, ["README.md", "my notes.md"], [])

    assert [
        (e.target, index.sections[e.target_section].heading)
        for e in index.edges
    ] == [("my notes.md", "Café")]
    assert index.dangling == []


def test_a_percent_encoded_code_link_is_decoded() -> None:
    text = "# A\n[app](src/my%20app.py)\n"

    assert _links("README.md", text) == {("src/my app.py", "link", "", "app")}


def test_front_matter_slugs_and_permalinks_route_to_their_doc(
    tmp_path: Path,
) -> None:
    (tmp_path / "docs" / "guides").mkdir(parents=True)
    (tmp_path / "docs" / "guides" / "x.md").write_text(
        "---\ntitle: X\nslug: /getting-started\n---\n# X\n"
    )
    (tmp_path / "docs" / "guides" / "y.md").write_text(
        "---\nslug: why-us\n---\n# Y\n"
    )
    (tmp_path / "docs" / "z.md").write_text(
        "---\npermalink: /about/team/\n---\n# Z\n"
    )
    (tmp_path / "README.md").write_text(
        "# R\n[a](/docs/getting-started)\n[b](/docs/guides/why-us)\n"
        "[c](/about/team)\n"
    )
    docs = ["README.md", "docs/guides/x.md", "docs/guides/y.md", "docs/z.md"]

    index = build_doc_index(tmp_path, docs, [])

    assert sorted(e.target for e in index.edges) == [
        "docs/guides/x.md",
        "docs/guides/y.md",
        "docs/z.md",
    ]
    assert index.dangling == []
