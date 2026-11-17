"""Doc parsing: heading sections and the code references they hold."""

from dekko.core.docs import Candidate, DocSection, parse_doc


def _sections(path: str, text: str) -> list[DocSection]:
    return parse_doc(path, text)[0]


def _cands(path: str, text: str) -> list[Candidate]:
    return [c for group in parse_doc(path, text)[1] for c in group]


def _shapes(path: str, text: str) -> set[tuple[str, str, str]]:
    return {(c.text, c.kind, c.shape) for c in _cands(path, text)}


# --- sections -------------------------------------------------------------


def test_atx_headings_make_flat_sections() -> None:
    text = "# Title\nintro\n## Usage ##\nrun it\n### Deep\nx\n"

    assert _sections("README.md", text) == [
        DocSection("README.md", "Title", 1, 1, 2),
        DocSection("README.md", "Usage", 2, 3, 4),
        DocSection("README.md", "Deep", 3, 5, 6),
    ]


def test_text_before_the_first_heading_is_a_preamble() -> None:
    text = "Some intro.\n\n# First\nbody\n"

    sections = _sections("docs/guide.md", text)

    assert sections[0] == DocSection("docs/guide.md", "guide.md", 0, 1, 2)
    assert sections[1] == DocSection("docs/guide.md", "First", 1, 3, 4)


def test_setext_headings() -> None:
    text = "Title\n=====\n\nSub\n---\nbody\n"

    assert [(s.heading, s.level, s.line) for s in _sections("a.md", text)] == [
        ("Title", 1, 1),
        ("Sub", 2, 4),
    ]


def test_a_rule_after_a_blank_line_is_not_a_heading() -> None:
    text = "# Top\npara\n\n---\nmore\n"

    assert [s.heading for s in _sections("a.md", text)] == ["Top"]


def test_headings_inside_fences_are_ignored() -> None:
    text = "# Real\n```python\n# not a heading\n```\n"

    assert [s.heading for s in _sections("a.md", text)] == ["Real"]


def test_front_matter_is_skipped() -> None:
    text = "---\ntitle: x\n---\n# Doc\nbody\n"

    sections = _sections("a.md", text)

    assert [(s.heading, s.line) for s in sections] == [("Doc", 4)]


def test_rst_levels_follow_first_appearance() -> None:
    text = (
        "=====\nTitle\n=====\n\nIntro\n\nPart\n----\n\nText\n\n"
        "Sub\n~~~\n\nOther\n-----\n"
    )

    got = [(s.heading, s.level) for s in _sections("guide.rst", text)]

    assert got == [("Title", 1), ("Part", 2), ("Sub", 3), ("Other", 2)]


def test_rst_underline_shorter_than_title_is_not_a_heading() -> None:
    text = "Long title here\n---\n"

    assert [s.level for s in _sections("a.rst", text)] == [0]


def test_crlf_input() -> None:
    text = "# One\r\nuse `MapIndex`\r\n"

    assert _sections("a.md", text)[0].heading == "One"
    assert ("MapIndex", "span", "bare") in _shapes("a.md", text)


# --- candidates -----------------------------------------------------------


def test_code_span_shapes() -> None:
    text = (
        "# S\n"
        "`MapIndex` and `MapIndex.load()` and `tsl::Status`\n"
        "and `src/dekko/core/walker.py:120` and `map(x)`\n"
    )

    shapes = _shapes("a.md", text)

    assert ("MapIndex", "span", "bare") in shapes
    assert ("MapIndex.load", "span", "qualified") in shapes
    assert ("tsl::Status", "span", "qualified") in shapes
    assert ("src/dekko/core/walker.py:120", "span", "path") in shapes
    assert ("map", "span", "bare") in shapes


def test_a_code_span_with_spaces_yields_its_qualified_tokens() -> None:
    text = "# S\n`x = MapIndex.load(root)` then `dekko query docs`\n"

    shapes = _shapes("a.md", text)

    assert ("MapIndex.load", "span", "qualified") in shapes
    assert not any(shape == "bare" for _, _, shape in shapes)


def test_double_backtick_spans() -> None:
    text = "# S\n`` a `tick` `` and ``Config.load``\n"

    assert ("Config.load", "span", "qualified") in _shapes("a.md", text)


def test_rst_literals_and_roles_are_code_spans() -> None:
    text = (
        "T\n=\n\n``Config.load`` and :func:`~pkg.mod.run` and :class:`Pack`\n"
    )

    shapes = _shapes("a.rst", text)

    assert ("Config.load", "span", "qualified") in shapes
    assert ("pkg.mod.run", "span", "qualified") in shapes
    assert ("Pack", "span", "bare") in shapes


def test_untagged_and_mapped_language_fences_give_names_and_paths() -> None:
    text = (
        "# S\n```\nidx = MapIndex.load(root)\n```\n"
        "```python\nfrom x import y  # see src/a.py\nwalker.discover(r)\n```\n"
    )

    shapes = _shapes("a.md", text)

    assert ("MapIndex.load", "fence", "qualified") in shapes
    assert ("walker.discover", "fence", "qualified") in shapes
    assert ("src/a.py", "fence", "path") in shapes
    assert not any(s == "bare" for _, k, s in shapes if k == "fence")


def test_shell_fences_give_paths_only() -> None:
    text = "# S\n```bash\nuv.run tool src/app.py\n```\n"

    shapes = _shapes("a.md", text)

    assert shapes == {("src/app.py", "fence", "path")}


def test_other_tagged_fences_give_paths_only() -> None:
    text = '# S\n```json\n{"a.b": "docs/x.md"}\n```\n'

    assert _shapes("a.md", text) == {("docs/x.md", "fence", "path")}


def test_an_unclosed_fence_runs_to_the_end() -> None:
    text = "# S\n```python\nMapIndex.load(r)\n# Not heading\n"

    assert [s.heading for s in _sections("a.md", text)] == ["S"]
    assert ("MapIndex.load", "fence", "qualified") in _shapes("a.md", text)


def test_rst_code_blocks_are_fences() -> None:
    text = (
        "T\n=\n\n.. code-block:: python\n\n   MapIndex.load(root)\n\n"
        "Back to ``prose``.\n\nExample::\n\n   Pack.build()\n"
    )

    shapes = _shapes("a.rst", text)

    assert ("MapIndex.load", "fence", "qualified") in shapes
    assert ("Pack.build", "fence", "qualified") in shapes


def test_links_normalize_their_targets() -> None:
    text = (
        "# S\n"
        '[a](src/x.py) [b](../y.py#L10) [c](z.py#L5-L9 "title")\n'
        "[d](https://example.com/a.py) [e](mailto:x@y.z) [f](#local)\n"
        "[g](docs/cli.md#query) ![img](img/logo.png)\n"
        "[ref]: src/ref.py\n"
    )

    links = {c.text for c in _cands("a.md", text) if c.kind == "link"}

    assert links == {
        "src/x.py",
        "../y.py:10",
        "z.py:5-9",
        "docs/cli.md",
        "img/logo.png",
        "src/ref.py",
    }


def test_link_labels_are_kept() -> None:
    text = "# S\n[resolve](src/resolver.py#L12)\n"

    (link,) = [c for c in _cands("a.md", text) if c.kind == "link"]

    assert link.label == "resolve"
    assert link.shape == "path"


def test_rst_links() -> None:
    text = "T\n=\n\nSee `the walker <src/walker.py>`_ now.\n"

    links = [c for c in _cands("a.rst", text) if c.kind == "link"]

    assert [(c.text, c.label) for c in links] == [
        ("src/walker.py", "the walker")
    ]


def test_prose_gives_paths_but_never_names() -> None:
    text = (
        "# S\nEdit src/dekko/core/walker.py:30, then run MapIndex.load or "
        "resolve.\nAlso setup.py and e.g. this.\n"
    )

    shapes = _shapes("a.md", text)

    assert shapes == {
        ("src/dekko/core/walker.py:30", "prose", "path"),
        ("setup.py", "prose", "path"),
    }


def test_candidates_are_grouped_by_section_with_lines() -> None:
    text = "intro `Alpha`\n# One\n`Beta`\n# Two\n`Gamma`\n"

    groups = parse_doc("a.md", text)[1]

    assert [[(c.text, c.line) for c in g] for g in groups] == [
        [("Alpha", 1)],
        [("Beta", 3)],
        [("Gamma", 5)],
    ]


def test_a_bare_extension_is_not_a_path() -> None:
    text = "# S\nFiles ending in `.h` or `.py` count.\n"

    assert _shapes("a.md", text) == set()
