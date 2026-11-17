"""Doc sections and the code references they hold.

Markdown and reStructuredText are read line by line: just enough to find
headings, fenced or literal code blocks, inline code, and links. Each
heading opens a section; each section collects reference candidates
that a later pass resolves against the map. Names are only taken from
code (inline code and code blocks), never from prose, so ordinary words
such as "run" or "new" can't turn into links.
"""

import bisect
import re
from dataclasses import dataclass, field

from dekko.core import languages, tier2
from dekko.core.walker import DOC_EXTENSIONS


@dataclass(frozen=True)
class DocSection:
    """One heading's span of a doc file.

    Attributes:
        path: Repo-relative path of the doc.
        heading: Heading text; the file name for the text before the
            first heading.
        level: Heading level (1 = top); 0 for the text before the first
            heading.
        line: First line of the heading (1-based).
        end_line: Last line before the next heading, or the file's end.
    """

    path: str
    heading: str
    level: int
    line: int
    end_line: int


@dataclass(frozen=True)
class Candidate:
    """A possible code reference, before resolution.

    Attributes:
        line: 1-based line in the doc.
        text: The reference: a name (``MapIndex.load``, ``Pack``) or a
            path with an optional ``:LINE`` or ``:START-END``.
        kind: Where it was found: ``span`` (inline code), ``fence``
            (a code block), ``link`` (a link target) or ``prose``.
        shape: ``path``, ``qualified`` (dotted or ``::`` name) or
            ``bare`` (one identifier).
        label: A link's text, empty for everything else.
    """

    line: int
    text: str
    kind: str
    shape: str
    label: str = field(default="", compare=False)


# Shell sessions name commands and files, not the repo's symbols, so a
# block tagged with one of these gives paths only, even where the shell
# is a language dekko maps.
_SHELL_TAGS = frozenset(
    {
        "bash", "sh", "shell", "zsh", "fish", "console", "shell-session",
        "shellsession", "powershell", "ps1", "pwsh", "cmd", "bat", "text",
        "txt", "plain", "output",
    }
)  # fmt: skip

_TAG_ALIASES = frozenset(
    {
        "py", "python3", "pycon", "js", "jsx", "mjs", "cjs", "ts", "rs",
        "golang", "c++", "cc", "cxx", "h", "hpp", "kt", "kts", "rb", "cs",
        "ex", "exs", "hs", "ml", "pl", "scala", "lua", "zig", "swift",
    }
)  # fmt: skip

_NAME_TAGS = (
    frozenset(languages.SPEC_BY_NAME)
    | frozenset(tier2.TIER2_SPECS)
    | _TAG_ALIASES
) - _SHELL_TAGS

_IDENT = r"[A-Za-z_]\w*"
_QUALIFIED_RE = re.compile(
    rf"(?<![\w.:/-]){_IDENT}(?:(?:\.|::){_IDENT})+(?![\w/(-]*/)"
)
_NAME_RE = re.compile(rf"[~!@]?({_IDENT}(?:(?:\.|::){_IDENT})*)(?:\(.*\))?")
_PATH_RE = re.compile(
    r"(?<![\w./@+-])"
    r"((?:\.{1,2}/)*(?:[\w.@+-]+/)*[\w.@+-]*\w)"
    r"(?::(\d+)(?:-(\d+))?)?"
    r"(?![\w/])"
)
_URL_RE = re.compile(r"[A-Za-z][\w+.-]*://\S+|\S+@\S+\.\w+")
_MD_LINK_RE = re.compile(r"!?\[([^\]]*)\]\(\s*<?([^)\s>]+)>?(?:\s+[^)]*)?\)")
_MD_REFDEF_RE = re.compile(r"^\s{0,3}\[([^\]]+)\]:\s*<?(\S+?)>?(?:\s.*)?$")
_RST_LINK_RE = re.compile(r"`([^`<]*?)\s*<([^>`]+)>`__?")
_RST_LITERAL_RE = re.compile(r"``(.+?)``")
_RST_ROLE_RE = re.compile(r":[\w-]+(?::[\w-]+)?:`([^`]+)`")
_RST_INTERPRETED_RE = re.compile(r"(?<![`\w])`([^`]+)`(?!_)")
_MD_SPAN_RE = re.compile(r"(`+)(.+?)(?<!`)\1(?!`)")
_SCHEME_RE = re.compile(r"^[A-Za-z][\w+.-]*:")
_ANCHOR_LINE_RE = re.compile(r"^L(\d+)(?:-L?(\d+))?$")

_ATX_RE = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*#*[ \t]*$")
_SETEXT_RE = re.compile(r"^ {0,3}(=+|-+)[ \t]*$")
_FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})\s*([^`\s]*)")
_RST_ADORN_CHARS = "=-~^\"'`*+#:._<>!$%&,;/?@[]\\|{}()"
_RST_DIRECTIVE_RE = re.compile(
    r"^(\s*)\.\.\s+(?:code-block|code|sourcecode)::\s*(\S*)"
)


def parse_doc(
    path: str, text: str
) -> tuple[list[DocSection], list[list[Candidate]]]:
    """Split a doc into heading sections and collect their candidates.

    Args:
        path: Repo-relative path; ``.rst`` selects reStructuredText,
            anything else Markdown.
        text: The file's text.

    Returns:
        The sections in file order, and for each section the reference
        candidates found inside it (same order).
    """
    lines = text.splitlines()
    if path.lower().endswith(".rst"):
        body = 0
        headings, cands = _scan_rst(lines)
    else:
        body = _front_matter_end(lines)
        headings, cands = _scan_markdown(lines, body)
    sections = _sections(path, headings, lines, body)
    return sections, _group(sections, cands)


# --- sections -------------------------------------------------------------


def _sections(
    path: str,
    headings: list[tuple[int, int, str]],
    lines: list[str],
    body: int,
) -> list[DocSection]:
    """Turn ``(line, level, text)`` headings into flat sections.

    ``body`` is the index of the first line after any front matter,
    which never makes a preamble section on its own.
    """
    total = len(lines)
    starts = [h[0] for h in headings]
    first = starts[0] if starts else total + 1
    out: list[DocSection] = []
    if any(line.strip() for line in lines[body : first - 1]):
        name = path.rsplit("/", 1)[-1]
        out.append(DocSection(path, name, 0, body + 1, first - 1))
    for i, (line, level, heading) in enumerate(headings):
        end = starts[i + 1] - 1 if i + 1 < len(starts) else total
        out.append(DocSection(path, heading, level, line, end))
    return out


def _group(
    sections: list[DocSection], cands: list[Candidate]
) -> list[list[Candidate]]:
    """Bucket candidates into the section whose lines hold them."""
    groups: list[list[Candidate]] = [[] for _ in sections]
    starts = [s.line for s in sections]
    for cand in cands:
        at = bisect.bisect_right(starts, cand.line) - 1
        if at >= 0:
            groups[at].append(cand)
    return groups


# --- markdown -------------------------------------------------------------


def _front_matter_end(lines: list[str]) -> int:
    """Index of the first line after a leading ``---`` block (or 0)."""
    if not lines or lines[0].strip() != "---":
        return 0
    for i in range(1, len(lines)):
        if lines[i].strip() in ("---", "..."):
            return i + 1
    return 0


def _scan_markdown(
    lines: list[str], body: int
) -> tuple[list[tuple[int, int, str]], list[Candidate]]:
    """Headings and candidates of a Markdown doc."""
    headings: list[tuple[int, int, str]] = []
    cands: list[Candidate] = []
    fence: tuple[str, str] | None = None
    underline = -1
    for i in range(body, len(lines)):
        line, num = lines[i], i + 1
        if fence is not None:
            if _closes_fence(line, fence[0]):
                fence = None
            else:
                cands += _fence_candidates(line, num, fence[1])
            continue
        opened = _FENCE_OPEN_RE.match(line)
        if opened:
            fence = (opened.group(1), _tag(opened.group(2)))
            continue
        if i == underline:
            continue
        heading = _md_heading(lines, i)
        if heading is not None:
            headings.append((num, heading[0], heading[1]))
            if heading[2]:
                underline = i + 1
        cands += _md_line_candidates(line, num)
    return headings, cands


def _closes_fence(line: str, opener: str) -> bool:
    stripped = line.strip()
    return (
        len(stripped) >= len(opener)
        and set(stripped) == {opener[0]}
        and len(line) - len(line.lstrip()) <= 3
    )


def _md_heading(lines: list[str], i: int) -> tuple[int, str, bool] | None:
    """``(level, text, is_setext)`` when line ``i`` opens a heading."""
    atx = _ATX_RE.match(lines[i])
    if atx:
        return len(atx.group(1)), (atx.group(2) or "").strip(), False
    line = lines[i]
    if i + 1 >= len(lines) or not line.strip():
        return None
    if _FENCE_OPEN_RE.match(line) or _SETEXT_RE.match(line):
        return None
    under = _SETEXT_RE.match(lines[i + 1])
    if not under or len(line) - len(line.lstrip()) > 3:
        return None
    return (1 if under.group(1)[0] == "=" else 2), line.strip(), True


def _md_line_candidates(line: str, num: int) -> list[Candidate]:
    """Links, inline code and prose paths on one Markdown line."""
    cands: list[Candidate] = []
    refdef = _MD_REFDEF_RE.match(line)
    if refdef:
        cands += _link_candidate(refdef.group(2), refdef.group(1), num)
        return cands
    for m in _MD_LINK_RE.finditer(line):
        cands += _link_candidate(m.group(2), m.group(1), num)
    rest = _MD_LINK_RE.sub(" ", line)
    for m in _MD_SPAN_RE.finditer(rest):
        cands += _span_candidates(m.group(2).strip(), num)
    rest = _MD_SPAN_RE.sub(" ", rest)
    cands += _path_candidates(rest, num, "prose")
    return cands


# --- reStructuredText -----------------------------------------------------


def _is_adornment(line: str) -> bool:
    stripped = line.rstrip()
    return (
        len(stripped) >= 2
        and stripped[0] in _RST_ADORN_CHARS
        and set(stripped) == {stripped[0]}
    )


def _rst_heading(
    lines: list[str], i: int
) -> tuple[tuple[str, bool], str, int] | None:
    """``(style, title, lines used)`` when line ``i`` opens a heading."""
    line = lines[i]
    if _is_adornment(line) and i + 2 < len(lines):
        title, under = lines[i + 1], lines[i + 2]
        if (
            title.strip()
            and under.rstrip() == line.rstrip()
            and len(line.rstrip()) >= len(title.strip())
        ):
            return (line[0], True), title.strip(), 3
    if not line.strip() or _is_adornment(line) or i + 1 >= len(lines):
        return None
    if line[0].isspace():
        return None
    under = lines[i + 1]
    if _is_adornment(under) and len(under.rstrip()) >= len(line.rstrip()):
        return (under[0], False), line.strip(), 2
    return None


def _scan_rst(
    lines: list[str],
) -> tuple[list[tuple[int, int, str]], list[Candidate]]:
    """Headings and candidates of a reStructuredText doc."""
    headings: list[tuple[int, int, str]] = []
    cands: list[Candidate] = []
    styles: list[tuple[str, bool]] = []
    block: tuple[int, str] | None = None
    i = 0
    while i < len(lines):
        line, num = lines[i], i + 1
        if block is not None:
            if line.strip() and _indent(line) <= block[0]:
                block = None
            else:
                cands += _fence_candidates(line, num, block[1])
                i += 1
                continue
        heading = _rst_heading(lines, i)
        if heading is not None:
            style, title, used = heading
            if style not in styles:
                styles.append(style)
            headings.append((num, styles.index(style) + 1, title))
            i += used
            continue
        block = _rst_block_start(line)
        cands += _rst_line_candidates(line, num)
        i += 1
    return headings, cands


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _rst_block_start(line: str) -> tuple[int, str] | None:
    """``(indent, tag)`` when ``line`` opens a code or literal block."""
    directive = _RST_DIRECTIVE_RE.match(line)
    if directive:
        return len(directive.group(1)), _tag(directive.group(2))
    if line.rstrip().endswith("::") and not line.lstrip().startswith(".."):
        return _indent(line), ""
    return None


def _rst_line_candidates(line: str, num: int) -> list[Candidate]:
    """Links, literals, roles and prose paths on one RST line."""
    cands: list[Candidate] = []
    for m in _RST_LINK_RE.finditer(line):
        cands += _link_candidate(m.group(2), m.group(1).strip(), num)
    rest = _RST_LINK_RE.sub(" ", line)
    for pattern in (_RST_LITERAL_RE, _RST_ROLE_RE, _RST_INTERPRETED_RE):
        for m in pattern.finditer(rest):
            cands += _span_candidates(_role_target(m.group(1)), num)
        rest = pattern.sub(" ", rest)
    cands += _path_candidates(rest, num, "prose")
    return cands


def _role_target(text: str) -> str:
    """A role's target: ``title <target>`` gives ``target``."""
    m = re.search(r"<([^>]+)>\s*$", text)
    return (m.group(1) if m else text).strip()


# --- candidate extraction -------------------------------------------------


def _tag(raw: str) -> str:
    """A code block's language tag, normalized (``{.python}`` → python)."""
    return raw.strip("{}").lstrip(".").split(",")[0].lower()


def _fence_candidates(line: str, num: int, tag: str) -> list[Candidate]:
    """Paths from any code block; names only from code-language blocks."""
    text = _URL_RE.sub(" ", line)
    cands = _path_candidates(text, num, "fence")
    if tag and tag not in _NAME_TAGS:
        return cands
    text = _PATH_RE.sub(_blank_paths, text)
    cands += [
        Candidate(num, m.group(0), "fence", "qualified")
        for m in _QUALIFIED_RE.finditer(text)
    ]
    return cands


def _span_candidates(content: str, num: int) -> list[Candidate]:
    """What one inline code span refers to."""
    if not content:
        return []
    path = _whole_path(content)
    if path is not None:
        return [Candidate(num, path, "span", "path")]
    name = _NAME_RE.fullmatch(content)
    if name:
        text = name.group(1)
        shape = "bare" if re.fullmatch(_IDENT, text) else "qualified"
        return [Candidate(num, text, "span", shape)]
    text = _URL_RE.sub(" ", content)
    cands = _path_candidates(text, num, "span")
    text = _PATH_RE.sub(_blank_paths, text)
    cands += [
        Candidate(num, m.group(0), "span", "qualified")
        for m in _QUALIFIED_RE.finditer(text)
    ]
    return cands


def _link_candidate(target: str, label: str, num: int) -> list[Candidate]:
    """A link target as a path candidate, or nothing for URLs/anchors."""
    target = target.strip()
    if not target or target.startswith("#") or _SCHEME_RE.match(target):
        return []
    target, _, anchor = target.split("?", 1)[0].partition("#")
    lines = _ANCHOR_LINE_RE.match(anchor)
    if lines:
        target += f":{lines.group(1)}"
        if lines.group(2):
            target += f"-{lines.group(2)}"
    return [Candidate(num, target, "link", "path", label)]


def _path_candidates(text: str, num: int, kind: str) -> list[Candidate]:
    """Path-shaped tokens in free text."""
    text = _URL_RE.sub(" ", text)
    return [
        Candidate(num, _with_lines(m), kind, "path")
        for m in _PATH_RE.finditer(text)
        if _looks_like_path(m.group(1))
    ]


def _whole_path(content: str) -> str | None:
    """The span as a path, when the whole span is one."""
    m = _PATH_RE.fullmatch(content)
    if m and _looks_like_path(m.group(1)):
        return _with_lines(m)
    return None


def _with_lines(m: re.Match[str]) -> str:
    text = m.group(1)
    if m.group(2):
        text += f":{m.group(2)}"
    if m.group(3):
        text += f"-{m.group(3)}"
    return text


def _looks_like_path(token: str) -> bool:
    """A token with a directory part, or a source or doc file name."""
    name = token.rsplit("/", 1)[-1]
    if name.startswith(".") and "/" not in token:
        return False
    if "/" in token:
        return bool(name) and not token.startswith("//")
    return languages.is_supported(name) or name.lower().endswith(
        DOC_EXTENSIONS
    )


def _blank_paths(m: re.Match[str]) -> str:
    """Blank out a path token so its dotted file name isn't a name."""
    return " " * len(m.group(0)) if _looks_like_path(m.group(1)) else m[0]
