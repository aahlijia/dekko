"""Links between docs: anchors, heading ids, wikilinks and site routes."""

from dekko.core.docs import Candidate, DocSection, parse_doc


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
