"""Doc sections and the code references they hold.

Markdown and reStructuredText are read line by line: just enough to find
headings, fenced or literal code blocks, inline code, and links. Each
heading opens a section; each section collects reference candidates
that a later pass resolves against the map. Names are only taken from
code (inline code and code blocks), never from prose, so ordinary words
such as "run" or "new" can't turn into links.
"""

import bisect
import posixpath
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from dekko.core import languages, tier2
from dekko.core import resolver as resolver_mod
from dekko.core.model import TYPE_KINDS, FileMap, Symbol
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
    code = [m.span() for m in _MD_SPAN_RE.finditer(line)]
    links = [
        m
        for m in _MD_LINK_RE.finditer(line)
        if not any(start <= m.start() < end for start, end in code)
    ]
    for m in links:
        cands += _link_candidate(m.group(2), m.group(1), num)
    rest = line
    for m in reversed(links):
        rest = rest[: m.start()] + " " + rest[m.end() :]
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


# --- resolution -----------------------------------------------------------

EXTRACTED = "EXTRACTED"
INFERRED = "INFERRED"
AMBIGUOUS = "AMBIGUOUS"

# A name with more homes than this tells a reader nothing; it is dropped
# rather than listed.
MAX_CANDIDATES = 5

# A bare name shorter than this is unique only by luck.
MIN_BARE_LENGTH = 4

# Names too generic to read as a reference to this repo's code even
# when the repo happens to define one: language builtins and ambient
# globals the resolver already treats as noise, plus a few method names
# every codebase has.
NOISE_NAMES = (
    resolver_mod.AMBIENT_GLOBAL_NAMES
    | resolver_mod.BUILTIN_METHOD_NAMES
    | frozenset({"clone", "iter_mut"})
)

# Members every type has through the language itself (``Foo.class``,
# ``Foo.prototype``), never a missing member.
_LANGUAGE_MEMBERS = frozenset(
    {"class", "new", "prototype", "constructor", "this", "super", "self"}
)

# Index files that stand for their directory as a module, by extension.
_PACKAGE_STEMS = {
    "py": "/__init__",
    "rs": "/mod",
    "js": "/index",
    "jsx": "/index",
    "mjs": "/index",
    "ts": "/index",
    "tsx": "/index",
}


@dataclass(frozen=True)
class DocLink:
    """A resolved reference from a doc section to code.

    Attributes:
        section: Index of the section in the doc index's ``sections``.
        line: 1-based line in the doc.
        text: The reference as normalized by parsing.
        kind: ``span``, ``fence``, ``link`` or ``prose``.
        tier: ``EXTRACTED``, ``INFERRED`` or ``AMBIGUOUS``.
        target: Symbol id or file path; empty when ``AMBIGUOUS``.
        target_line: The line a ``path:LINE`` reference named, if any.
        candidates: The symbol ids or paths an ``AMBIGUOUS`` reference
            could mean.
    """

    section: int
    line: int
    text: str
    kind: str
    tier: str
    target: str
    target_line: int | None = None
    candidates: tuple[str, ...] = ()


@dataclass(frozen=True)
class DocDangling:
    """A doc reference that no longer points at anything.

    Attributes:
        section: Index of the section in ``sections``.
        line: 1-based line in the doc.
        text: The reference as normalized by parsing.
        kind: ``span``, ``fence`` or ``link``.
        why: ``missing`` (no such path), ``past-end`` (line past the
            file's end), ``no-member`` (a known type or module lacks the
            member) or ``moved`` (a link's line no longer holds the
            symbol its text names).
        now: For ``moved``, where that symbol is now (``path:line``).
    """

    section: int
    line: int
    text: str
    kind: str
    why: str
    now: str | None = None


@dataclass
class DocIndex:
    """Every indexed doc section with its resolved and dangling refs."""

    sections: list[DocSection] = field(default_factory=list)
    links: list[DocLink] = field(default_factory=list)
    dangling: list[DocDangling] = field(default_factory=list)


@dataclass
class _Tables:
    """Lookups over the mapped code, built once per resolution pass."""

    root: Path
    paths: set[str]
    docs: set[str]
    top_level: set[str]
    by_qualname_suffix: dict[str, list[Symbol]]
    by_name: dict[str, list[Symbol]]
    by_path_suffix: dict[str, list[str]]
    modules: dict[str, list[str]]
    symbols_in: dict[str, list[Symbol]]
    dir_exts: dict[str, set[str]]
    line_counts: dict[str, int] = field(default_factory=dict)


def build_doc_index(
    root: Path, doc_paths: list[str], files: list[FileMap]
) -> DocIndex:
    """Parse every doc and resolve its references against ``files``.

    Args:
        root: Repository root.
        doc_paths: Repo-relative docs, as ``walker.discover_docs`` found
            them.
        files: Every mapped code file.

    Returns:
        The doc index; a doc that can't be read is skipped.
    """
    sections: list[DocSection] = []
    candidates: list[list[Candidate]] = []
    read: list[str] = []
    for path in doc_paths:
        try:
            text = (root / path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        found, groups = parse_doc(path, text)
        sections += found
        candidates += groups
        read.append(path)
    return resolve_docs(root, sections, candidates, files, read)


def resolve_docs(
    root: Path,
    sections: list[DocSection],
    candidates: list[list[Candidate]],
    files: list[FileMap],
    doc_paths: list[str],
) -> DocIndex:
    """Resolve every section's candidates against the mapped code.

    Args:
        root: Repository root, for path existence and line counts.
        sections: Every doc section, in a stable order.
        candidates: Each section's candidates (same order).
        files: Every mapped code file.
        doc_paths: Every indexed doc, so doc-to-doc links are known.

    Returns:
        The sections with their links and dangling references.
    """
    tables = _tables(root, files, doc_paths)
    index = DocIndex(sections=list(sections))
    for at, (section, group) in enumerate(zip(sections, candidates)):
        for cand in group:
            _resolve_one(tables, at, section.path, cand, index)
    return index


def _tables(root: Path, files: list[FileMap], doc_paths: list[str]) -> _Tables:
    by_suffix: dict[str, list[Symbol]] = {}
    by_name: dict[str, list[Symbol]] = {}
    symbols_in: dict[str, list[Symbol]] = {}
    for fm in files:
        symbols_in[fm.path] = fm.symbols
        for sym in fm.symbols:
            by_name.setdefault(sym.name, []).append(sym)
            parts = sym.qualname.replace("::", ".").split(".")
            for start in range(len(parts) - 1):
                key = ".".join(parts[start:])
                by_suffix.setdefault(key, []).append(sym)
    paths = {fm.path for fm in files}
    top_level = {p.split("/", 1)[0] for p in (*paths, *doc_paths) if "/" in p}
    return _Tables(
        root=root,
        paths=paths,
        docs=set(doc_paths),
        top_level=top_level,
        by_qualname_suffix=by_suffix,
        by_name=by_name,
        by_path_suffix=_suffixes(paths, lambda p: p),
        modules=_suffixes(paths, _module_of),
        symbols_in=symbols_in,
        dir_exts=_dir_extensions((*paths, *doc_paths)),
    )


def _dir_extensions(paths: tuple[str, ...]) -> dict[str, set[str]]:
    """Each directory → the extensions of the indexed files right in it."""
    out: dict[str, set[str]] = {}
    for path in paths:
        parent, _, name = path.rpartition("/")
        if "." in name:
            out.setdefault(parent, set()).add(name.rsplit(".", 1)[1])
    return out


def _module_of(path: str) -> str:
    """A file's module path: no extension; a package index is its dir."""
    if "." not in path.rsplit("/", 1)[-1]:
        return path
    module, ext = path.rsplit(".", 1)
    stem = _PACKAGE_STEMS.get(ext)
    return module.removesuffix(stem) if stem else module


def _suffixes(
    paths: set[str], key: Callable[[str], str]
) -> dict[str, list[str]]:
    """Every ``/``-boundary suffix of ``key(path)`` → the paths."""
    out: dict[str, list[str]] = {}
    for path in sorted(paths):
        parts = key(path).split("/")
        for start in range(len(parts)):
            out.setdefault("/".join(parts[start:]), []).append(path)
    return out


def _resolve_one(
    t: _Tables, at: int, doc: str, cand: Candidate, index: DocIndex
) -> None:
    if cand.shape == "path":
        _resolve_path(t, at, doc, cand, index)
    elif cand.shape == "qualified":
        _resolve_qualified(t, at, cand, index)
    else:
        _resolve_bare(t, at, cand, index)


def _emit(
    index: DocIndex,
    at: int,
    cand: Candidate,
    targets: list[str],
    tier: str,
    target_line: int | None = None,
) -> None:
    """Record a link for one target, or an ambiguous one for several."""
    unique = list(dict.fromkeys(targets))
    if not unique or len(unique) > MAX_CANDIDATES:
        return
    ambiguous = len(unique) > 1
    index.links.append(
        DocLink(
            section=at,
            line=cand.line,
            text=cand.text,
            kind=cand.kind,
            tier=AMBIGUOUS if ambiguous else tier,
            target="" if ambiguous else unique[0],
            target_line=target_line,
            candidates=tuple(unique) if ambiguous else (),
        )
    )


def _dangle(
    index: DocIndex, at: int, cand: Candidate, why: str, now: str | None
) -> None:
    index.dangling.append(
        DocDangling(at, cand.line, cand.text, cand.kind, why, now)
    )


# --- paths ----------------------------------------------------------------


def _split_lines(text: str) -> tuple[str, int | None]:
    """``"a.py:10-20"`` → ``("a.py", 10)``; no line → ``None``."""
    m = re.fullmatch(r"(.*?):(\d+)(?:-\d+)?", text)
    if m:
        return m.group(1), int(m.group(2))
    return text, None


def _candidate_paths(doc: str, raw: str) -> list[str]:
    """Where a path could point: beside the doc, then from the root."""
    out = []
    if "/" in doc:
        out.append(
            posixpath.normpath(posixpath.join(doc.rsplit("/", 1)[0], raw))
        )
    out.append(posixpath.normpath(raw.lstrip("/")))
    return list(dict.fromkeys(out))


def _resolve_path(
    t: _Tables, at: int, doc: str, cand: Candidate, index: DocIndex
) -> None:
    raw, line = _split_lines(cand.text)
    options = _candidate_paths(doc, raw)
    hit = next((p for p in options if p in t.paths), None)
    if hit is not None:
        _emit_path(t, at, cand, [hit], line, index, EXTRACTED)
        return
    matches = _suffix_matches(t, raw)
    if matches and (len(matches) == 1 or cand.kind != "prose"):
        # A bare file name is a guess at which file it means.
        tier = EXTRACTED if "/" in raw else INFERRED
        _emit_path(t, at, cand, matches, line, index, tier)
        return
    if cand.kind in ("link", "span") and _is_missing(t, cand, options, raw):
        _dangle(index, at, cand, "missing", None)


def _suffix_matches(t: _Tables, raw: str) -> list[str]:
    """Mapped files a partial path or bare file name ends with."""
    if raw.startswith(".."):
        return []
    return t.by_path_suffix.get(posixpath.normpath(raw), [])


def _is_missing(
    t: _Tables, cand: Candidate, options: list[str], raw: str
) -> bool:
    """Whether an unresolved path names a file this repo should have.

    A bare file name, a path into an ignored or generated dir, or
    another project's layout is not this repo's rot. A link counts when
    its directory exists (read beside its doc, then from the root);
    inline code, read from the root only, counts when its directory
    already holds an indexed file of the same kind, the shape a renamed
    or deleted file leaves behind.
    """
    if "/" not in raw:
        return False
    if any(p in t.docs or (t.root / p).exists() for p in options):
        return False
    if cand.kind != "link":
        return _has_sibling_kind(t, options[-1])
    return any(
        not p.startswith("..")
        and p.split("/", 1)[0] in t.top_level
        and (t.root / p).parent.is_dir()
        for p in options
    )


def _has_sibling_kind(t: _Tables, path: str) -> bool:
    parent, _, name = path.rpartition("/")
    if "." not in name:
        return False
    return name.rsplit(".", 1)[1] in t.dir_exts.get(parent, set())


def _emit_path(
    t: _Tables,
    at: int,
    cand: Candidate,
    paths: list[str],
    line: int | None,
    index: DocIndex,
    tier: str,
) -> None:
    if len(paths) != 1 or line is None:
        _emit(index, at, cand, paths, tier)
        return
    path = paths[0]
    if line > _line_count(t, path):
        _dangle(index, at, cand, "past-end", None)
        return
    moved = _moved(t, cand, path, line)
    if moved is not None:
        _dangle(index, at, cand, "moved", moved)
        return
    target = _innermost(t.symbols_in.get(path, []), line)
    _emit(index, at, cand, [target.id if target else path], tier, line)


def _line_count(t: _Tables, path: str) -> int:
    if path not in t.line_counts:
        try:
            data = (t.root / path).read_bytes()
        except OSError:
            data = b""
        t.line_counts[path] = data.count(b"\n") + (
            1 if data and not data.endswith(b"\n") else 0
        )
    return t.line_counts[path]


def _innermost(symbols: list[Symbol], line: int) -> Symbol | None:
    """The smallest symbol whose line range holds ``line``."""
    holding = [s for s in symbols if s.start_line <= line <= s.end_line]
    return min(holding, key=lambda s: s.end_line - s.start_line, default=None)


def _moved(t: _Tables, cand: Candidate, path: str, line: int) -> str | None:
    """Where a link's named symbol is now, when its line no longer holds it."""
    if cand.kind != "link" or not cand.label:
        return None
    name = _NAME_RE.fullmatch(cand.label.strip("` "))
    if name is None:
        return None
    named = _symbols_named(t, name.group(1))
    in_file = [s for s in named if s.path == path]
    if len(in_file) != 1:
        return None
    sym = in_file[0]
    if sym.start_line <= line <= sym.end_line:
        return None
    return f"{path}:{sym.start_line}"


def _symbols_named(t: _Tables, text: str) -> list[Symbol]:
    key = text.replace("::", ".")
    if "." in key:
        return t.by_qualname_suffix.get(key, [])
    return t.by_name.get(key, [])


# --- names ----------------------------------------------------------------


def _resolve_qualified(
    t: _Tables, at: int, cand: Candidate, index: DocIndex
) -> None:
    key = cand.text.replace("::", ".")
    symbols = t.by_qualname_suffix.get(key, [])
    if symbols:
        _emit(index, at, cand, [s.id for s in symbols], EXTRACTED)
        return
    parts = key.split(".")
    modules = t.modules.get("/".join(parts), [])
    if modules:
        _emit(index, at, cand, modules, EXTRACTED)
        return
    members = _module_members(t, parts)
    if members:
        _emit(index, at, cand, [s.id for s in members], EXTRACTED)
        return
    owner = _known_type(t, parts)
    if owner is None:
        return
    line = next((f.line for f in owner.fields if f.name == parts[-1]), None)
    if line is not None:
        _emit(index, at, cand, [owner.id], EXTRACTED, line)
    elif cand.kind == "span" and parts[-1] not in _LANGUAGE_MEMBERS:
        _dangle(index, at, cand, "no-member", None)


def _module_members(t: _Tables, parts: list[str]) -> list[Symbol]:
    """``a.b.name`` as module ``a/b`` plus a symbol in it, longest first."""
    for split in range(len(parts) - 1, 0, -1):
        modules = t.modules.get("/".join(parts[:split]), [])
        member = ".".join(parts[split:])
        found = [
            s
            for path in modules
            for s in t.symbols_in.get(path, [])
            if s.qualname.replace("::", ".") == member
        ]
        if found:
            return found
    return []


def _known_type(t: _Tables, parts: list[str]) -> Symbol | None:
    """The one non-test in-repo type all but the last segment names.

    Test fixtures define throwaway types (``A``, ``Box``) whose names
    docs use for other things, so they never anchor a member check.
    """
    head = ".".join(parts[:-1])
    types = [
        s
        for s in _symbols_named(t, head)
        if s.kind in TYPE_KINDS and not s.test
    ]
    return types[0] if len(types) == 1 else None


def _resolve_bare(
    t: _Tables, at: int, cand: Candidate, index: DocIndex
) -> None:
    name = cand.text
    if len(name) < MIN_BARE_LENGTH or name in NOISE_NAMES:
        return
    symbols = t.by_name.get(name, [])
    _emit(index, at, cand, [s.id for s in symbols], INFERRED)
