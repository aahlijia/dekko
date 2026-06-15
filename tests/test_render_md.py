"""MAP.md rendering: the agent-steering header and purpose lines."""

from conftest import RepoFactory

from dekko.model import CallGraph, FileMap, Symbol
from dekko.render_md import render_markdown


def test_map_has_agent_steering_header(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo({"a.py": "def f():\n    return 1\n"})
    text = (root / ".dekko" / "MAP.md").read_text()
    assert "**Agents:**" in text
    assert "dekko summary" in text
    assert "dekko query | context | affected" in text


def _file_with_doc(doc: str | None, sym_doc: str | None) -> FileMap:
    sym = Symbol(
        id="cache.py::run",
        name="run",
        qualname="run",
        kind="function",
        path="cache.py",
        language="python",
        start_line=1,
        end_line=2,
        doc=sym_doc,
    )
    return FileMap(path="cache.py", language="python", symbols=[sym], doc=doc)


def test_purpose_lines_present_when_doc_set() -> None:
    fm = _file_with_doc("Incremental extraction cache.", "Run the extraction.")
    text = render_markdown([fm], CallGraph(), "demo")
    # TOC entry carries the file purpose.
    assert "(1 symbols) — Incremental extraction cache." in text
    # File section meta line carries the file purpose.
    assert "*python · 1 symbols — Incremental extraction cache.*" in text
    # Symbol block carries the symbol doc as plain text.
    assert "Run the extraction." in text


def _toc_entry(text: str, prefix: str) -> str:
    """The TOC bullet for a file, scoped to the ``## Contents`` section.

    Scoping avoids matching overview bullets (e.g. the largest-files
    list) that share the ``- [`name`` shape.
    """
    toc = text.split("## Contents")[1].split("---")[0]
    return next(ln for ln in toc.splitlines() if ln.strip().startswith(prefix))


def test_purpose_lines_absent_when_doc_none() -> None:
    fm = _file_with_doc(None, None)
    text = render_markdown([fm], CallGraph(), "demo")
    # Bare meta line, no placeholder purpose noise.
    assert "*python · 1 symbols*" in text
    # TOC entry has no purpose suffix.
    assert "—" not in _toc_entry(text, "- [`cache")


def test_purpose_line_truncated_for_long_doc() -> None:
    long_doc = "x" * 200
    fm = _file_with_doc(long_doc, None)
    text = render_markdown([fm], CallGraph(), "demo")
    assert "…" in text
    # TOC entry stays a single line within the 80-char budget.
    assert "…" in _toc_entry(text, "- [`cache")


def test_purpose_line_suppressed_on_parse_error() -> None:
    fm = FileMap(
        path="bad.py",
        language="python",
        error="syntax error",
        doc="Should not appear.",
    )
    text = render_markdown([fm], CallGraph(), "demo")
    # TOC entry is a bare link: no leaked doc, and no redundant
    # "(parse error)" marker — the Overview's parse-error list carries
    # it now (B6 item 4).
    toc_line = _toc_entry(text, "- [`bad")
    assert "Should not appear." not in toc_line
    assert "(parse error)" not in toc_line
    # The error itself surfaces in the Overview and the file section.
    overview = text.split("## Contents")[0]
    assert "syntax error" in overview
    assert "*python — parse error: syntax error*" in text
