"""Doc parsing: heading sections and the code references they hold."""

from pathlib import Path

from dekko.core.docs import (
    NOISE_NAMES,
    Candidate,
    DocIndex,
    DocSection,
    parse_doc,
    resolve_docs,
)
from dekko.core.model import Field, FileMap, Symbol


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


# --- resolution -----------------------------------------------------------


def _sym(
    path: str,
    qualname: str,
    start: int,
    end: int,
    kind: str = "function",
    suffix: str = "",
) -> Symbol:
    name = qualname.rsplit(".", 1)[-1]
    return Symbol(
        id=f"{path}::{qualname}{suffix}",
        name=name,
        qualname=qualname,
        kind=kind,
        path=path,
        language="python",
        start_line=start,
        end_line=end,
    )


def _repo(tmp_path: Path) -> list[FileMap]:
    """A small mapped tree on disk plus its extracted symbols."""
    sources = {
        "src/pkg/walker.py": 40,
        "src/pkg/index.py": 60,
        "src/pkg/other.py": 20,
        "src/pkg/__init__.py": 1,
    }
    for rel, count in sources.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x = 1\n" * count)
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/guide.md").write_text("# Guide\n")
    return [
        FileMap(
            "src/pkg/walker.py",
            "python",
            symbols=[_sym("src/pkg/walker.py", "discover", 5, 30)],
        ),
        FileMap(
            "src/pkg/index.py",
            "python",
            symbols=[
                _sym("src/pkg/index.py", "MapIndex", 1, 50, "class"),
                _sym("src/pkg/index.py", "MapIndex.load", 10, 20, "method"),
                _sym("src/pkg/index.py", "MapIndex.build", 22, 40, "method"),
                _sym("src/pkg/index.py", "Config.save", 52, 55, "method"),
                _sym("src/pkg/index.py", "run", 56, 57),
            ],
        ),
        FileMap(
            "src/pkg/other.py",
            "python",
            symbols=[
                _sym("src/pkg/other.py", "Config.save", 1, 5, "method"),
                _sym("src/pkg/other.py", "render", 6, 8),
                _sym("src/pkg/other.py", "render", 9, 12, suffix="#2"),
            ],
        ),
        FileMap("src/pkg/__init__.py", "python"),
    ]


def _resolve(
    tmp_path: Path, text: str, doc: str = "docs/guide.md"
) -> DocIndex:
    files = _repo(tmp_path)
    sections, cands = parse_doc(doc, text)
    return resolve_docs(tmp_path, sections, cands, files, [doc])


def _links(index: DocIndex) -> set[tuple[str, str, str]]:
    return {(link.text, link.tier, link.target) for link in index.links}


def test_paths_resolve_from_the_doc_and_from_the_root(tmp_path: Path) -> None:
    text = "# S\n[a](../src/pkg/walker.py) and `src/pkg/index.py`\n"

    assert _links(_resolve(tmp_path, text)) == {
        ("../src/pkg/walker.py", "EXTRACTED", "src/pkg/walker.py"),
        ("src/pkg/index.py", "EXTRACTED", "src/pkg/index.py"),
    }


def test_a_path_line_targets_the_innermost_symbol(tmp_path: Path) -> None:
    index = _resolve(tmp_path, "# S\n`src/pkg/index.py:15`\n")

    (link,) = index.links
    assert link.target == "src/pkg/index.py::MapIndex.load"
    assert link.target_line == 15


def test_a_path_line_outside_any_symbol_targets_the_file(
    tmp_path: Path,
) -> None:
    index = _resolve(tmp_path, "# S\n`src/pkg/walker.py:35`\n")

    assert _links(index) == {
        ("src/pkg/walker.py:35", "EXTRACTED", "src/pkg/walker.py")
    }


def test_a_partial_path_matches_a_unique_suffix(tmp_path: Path) -> None:
    index = _resolve(tmp_path, "# S\nsee `pkg/walker.py` and `walker.py`\n")

    assert {t for _, _, t in _links(index)} == {"src/pkg/walker.py"}


def test_qualified_names(tmp_path: Path) -> None:
    text = "# S\n`MapIndex.load` and `pkg.walker.discover` and `pkg.index`\n"

    assert _links(_resolve(tmp_path, text)) == {
        ("MapIndex.load", "EXTRACTED", "src/pkg/index.py::MapIndex.load"),
        ("pkg.walker.discover", "EXTRACTED", "src/pkg/walker.py::discover"),
        ("pkg.index", "EXTRACTED", "src/pkg/index.py"),
    }


def test_a_module_qualified_function(tmp_path: Path) -> None:
    index = _resolve(tmp_path, "# S\n```\nwalker.discover(root)\n```\n")

    assert _links(index) == {
        ("walker.discover", "EXTRACTED", "src/pkg/walker.py::discover")
    }


def test_a_qualified_name_with_two_homes_is_ambiguous(tmp_path: Path) -> None:
    (link,) = _resolve(tmp_path, "# S\n`Config.save`\n").links

    assert link.tier == "AMBIGUOUS"
    assert link.target == ""
    assert set(link.candidates) == {
        "src/pkg/index.py::Config.save",
        "src/pkg/other.py::Config.save",
    }


def test_an_overload_family_is_ambiguous(tmp_path: Path) -> None:
    (link,) = _resolve(tmp_path, "# S\n`render`\n").links

    assert link.tier == "AMBIGUOUS"
    assert len(link.candidates) == 2


def test_bare_names(tmp_path: Path) -> None:
    text = "# S\n`discover` and `run` and `MapIndex` and `missing_name`\n"

    assert _links(_resolve(tmp_path, text)) == {
        ("discover", "INFERRED", "src/pkg/walker.py::discover"),
        ("MapIndex", "INFERRED", "src/pkg/index.py::MapIndex"),
    }


def test_noise_names_are_never_bare_links(tmp_path: Path) -> None:
    assert "describe" in NOISE_NAMES
    files = _repo(tmp_path)
    files[2].symbols.append(_sym("src/pkg/other.py", "describe", 13, 14))
    sections, cands = parse_doc("a.md", "# S\n`describe`\n")

    index = resolve_docs(tmp_path, sections, cands, files, ["a.md"])

    assert index.links == []


def test_more_than_five_candidates_is_dropped(tmp_path: Path) -> None:
    files = _repo(tmp_path)
    for i in range(6):
        files[2].symbols.append(_sym(f"src/m{i}.py", "Shared.go_now", 1, 2))
    sections, cands = parse_doc("a.md", "# S\n`Shared.go_now`\n")

    index = resolve_docs(tmp_path, sections, cands, files, ["a.md"])

    assert index.links == []


def test_five_candidates_is_ambiguous(tmp_path: Path) -> None:
    files = _repo(tmp_path)
    for i in range(5):
        files[2].symbols.append(_sym(f"src/m{i}.py", "Shared.go_now", 1, 2))
    sections, cands = parse_doc("a.md", "# S\n`Shared.go_now`\n")

    (link,) = resolve_docs(tmp_path, sections, cands, files, ["a.md"]).links

    assert link.tier == "AMBIGUOUS"
    assert len(link.candidates) == 5


def test_unknown_and_non_code_targets_are_not_links(tmp_path: Path) -> None:
    text = (
        "# S\n`os.path.join` and [cfg](../pyproject.toml) and "
        "[guide](guide.md) and `self.value`\n"
    )

    index = _resolve(tmp_path, text)

    assert index.links == []
    assert index.dangling == []


def test_links_record_their_section(tmp_path: Path) -> None:
    index = _resolve(tmp_path, "# A\n`discover`\n# B\n`MapIndex.load`\n")

    assert [(link.section, link.line) for link in index.links] == [
        (0, 2),
        (1, 4),
    ]


# --- dangling -------------------------------------------------------------


def _dangling(index: DocIndex) -> set[tuple[str, str, str | None]]:
    return {(d.text, d.why, d.now) for d in index.dangling}


def test_a_missing_repo_path_dangles(tmp_path: Path) -> None:
    text = "# S\n[old](../src/pkg/gone.py) and `src/pkg/old.py`\n"

    assert _dangling(_resolve(tmp_path, text)) == {
        ("../src/pkg/gone.py", "missing", None),
        ("src/pkg/old.py", "missing", None),
    }


def test_paths_outside_the_repo_layout_do_not_dangle(tmp_path: Path) -> None:
    text = "# S\n`etc/hosts` and `install.sh` and [x](../../elsewhere.py)\n"

    assert _resolve(tmp_path, text).dangling == []


def test_prose_paths_never_dangle(tmp_path: Path) -> None:
    text = "# S\nSee src/pkg/gone.py for details.\n"

    index = _resolve(tmp_path, text)

    assert index.links == []
    assert index.dangling == []


def test_a_line_past_the_end_dangles(tmp_path: Path) -> None:
    index = _resolve(tmp_path, "# S\n`src/pkg/other.py:400`\n")

    assert index.links == []
    assert _dangling(index) == {("src/pkg/other.py:400", "past-end", None)}


def test_a_missing_member_of_a_known_class_dangles(tmp_path: Path) -> None:
    index = _resolve(tmp_path, "# S\n`MapIndex.lod` and `Thing.lod`\n")

    assert _dangling(index) == {("MapIndex.lod", "no-member", None)}


def test_a_member_check_skips_code_blocks(tmp_path: Path) -> None:
    index = _resolve(tmp_path, "# S\n```\nMapIndex.lod()\n```\n")

    assert index.dangling == []


def test_a_labeled_line_link_to_a_moved_symbol_dangles(tmp_path: Path) -> None:
    text = "# S\n[MapIndex.build](../src/pkg/index.py#L12)\n"

    index = _resolve(tmp_path, text)

    assert _dangling(index) == {
        ("../src/pkg/index.py:12", "moved", "src/pkg/index.py:22")
    }


def test_a_labeled_line_link_still_in_place_is_a_link(tmp_path: Path) -> None:
    text = "# S\n[`load`](../src/pkg/index.py#L12)\n"

    index = _resolve(tmp_path, text)

    assert index.dangling == []
    assert [link.target for link in index.links] == [
        "src/pkg/index.py::MapIndex.load"
    ]


def test_bare_names_never_dangle(tmp_path: Path) -> None:
    assert _resolve(tmp_path, "# S\n`removed_helper`\n").dangling == []


def test_a_file_name_alone_is_inferred(tmp_path: Path) -> None:
    index = _resolve(tmp_path, "# S\n`walker.py` and `pkg/walker.py`\n")

    assert _links(index) == {
        ("walker.py", "INFERRED", "src/pkg/walker.py"),
        ("pkg/walker.py", "EXTRACTED", "src/pkg/walker.py"),
    }


def test_missing_paths_in_code_blocks_do_not_dangle(tmp_path: Path) -> None:
    text = "# S\n```bash\ndekko outline src/app.py\n```\n"

    assert _resolve(tmp_path, text).dangling == []


def test_paths_under_unindexed_top_level_dirs_do_not_dangle(
    tmp_path: Path,
) -> None:
    (tmp_path / ".cache").mkdir()
    text = "# S\n`.cache/notes.json` and `vendor/lib/x.py`\n"

    assert _resolve(tmp_path, text).dangling == []


def test_a_field_of_a_known_type_links_to_the_type(tmp_path: Path) -> None:
    files = _repo(tmp_path)
    files[1].symbols[0].fields.append(Field("ambiguous_in", line=7))
    sections, cands = parse_doc("a.md", "# S\n`MapIndex.ambiguous_in`\n")

    index = resolve_docs(tmp_path, sections, cands, files, ["a.md"])

    assert index.dangling == []
    assert [(x.target, x.target_line) for x in index.links] == [
        ("src/pkg/index.py::MapIndex", 7)
    ]


def test_a_test_fixture_type_is_not_a_member_check_head(
    tmp_path: Path,
) -> None:
    files = _repo(tmp_path)
    fixture = _sym("tests/fixtures/a.py", "Widget", 1, 9, "class")
    fixture.test = True
    files[2].symbols.append(fixture)
    sections, cands = parse_doc(
        "a.md", "# S\n`Widget.gone` `MapIndex.class`\n"
    )

    index = resolve_docs(tmp_path, sections, cands, files, ["a.md"])

    assert index.dangling == []


def test_dot_slash_inline_code_is_not_read_beside_the_doc(
    tmp_path: Path,
) -> None:
    text = "# S\nimport `./x` or run `./gradlew`\n"

    assert _resolve(tmp_path, text).dangling == []


def test_a_missing_span_path_needs_a_sibling_of_its_kind(
    tmp_path: Path,
) -> None:
    text = "# S\n`src/screens/App.tsx` and `src/pkg/App.tsx` and `src/x.py`\n"

    assert _resolve(tmp_path, text).dangling == []
