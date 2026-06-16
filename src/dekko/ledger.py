"""Session ledger: what is already in the agent's context (Pillar C).

The hooks layer (and the ``dekko ledger`` command) needs to know what an
agent has *already* loaded this session — so dekko can stop re-spending
tokens on context the model already holds (FR-C2 dedup) and adapt its
budget to what remains (FR-C3). Per the design's resolved decisions, the
single source of truth is the Claude Code **transcript** (the session
JSONL that every hook receives via ``transcript_path``): dekko persists
no authoritative session state of its own, so this module is a pure,
best-effort *projection* over that file.

Reconstructing from the transcript — rather than from dekko's own
emission log — is what makes dedup honest: it sees the files the agent
read *directly* with ``Read``, which dekko never emitted. Two transcript
signals carry most of the weight:

* a whole-file ``Read`` puts every symbol of that file in context (a
  partial read, only the symbols whose definitions fall in the read
  line-range);
* an assistant turn's ``message.usage`` reports the *real* context-window
  occupancy (``input_tokens`` + cached input), which is a far better
  budget signal than estimating tokens from text — used when present,
  with a chars-based estimate as the fallback.

Every record is parsed defensively: an unknown ``type``, a malformed
line, or a missing field is skipped, never raised, so a transcript schema
change degrades the ledger to *empty* rather than *wrong* (NFR-3, the R1
guard).
"""

import json
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from .mapfile import MapIndex
from .textutil import estimate_tokens

EXIT_OK = 0
EXIT_NO_TRANSCRIPT = 6

# Files listed in the text view, ranked by symbols-in-context.
_MAX_LISTED_FILES = 20

# Substring that marks a tool call as one of dekko's own MCP tools, so its
# emissions can be attributed to dekko rather than counted as the agent
# reading source directly.
_DEKKO_TOOL_MARK = "dekko"

# Input keys a dekko tool might carry a file/symbol target under.
_TARGET_KEYS = ("target", "file_path", "path")


@dataclass
class FileState:
    """What the session knows about one file's presence in context.

    Attributes:
        path: Repo-relative POSIX path.
        fully_read: A whole-file ``Read`` was observed (every symbol is in
            context); ``False`` for partial reads or dekko-only mentions.
        symbols_seen: Symbol ids known to be in context for this file.
        dekko_emitted: dekko surfaced this file (vs. the agent reading it).
    """

    path: str
    fully_read: bool = False
    symbols_seen: set[str] = field(default_factory=set)
    dekko_emitted: bool = False


@dataclass
class LedgerView:
    """A projection of the session transcript into context state.

    Attributes:
        session_id: The session this view was built for, when known.
        consumed_tokens: Best estimate of the current context-window
            occupancy (real usage when the transcript reports it).
        files: Per-file context state, keyed by repo-relative path.
        turns: Number of assistant turns observed.
    """

    session_id: str = ""
    consumed_tokens: int = 0
    files: dict[str, FileState] = field(default_factory=dict)
    turns: int = 0

    @property
    def symbols(self) -> set[str]:
        """Every symbol id in context, across all files."""
        out: set[str] = set()
        for state in self.files.values():
            out |= state.symbols_seen
        return out

    def has_symbol(self, sym_id: str) -> bool:
        """Whether a symbol is already in context (FR-C2 dedup check)."""
        return any(sym_id in s.symbols_seen for s in self.files.values())

    def has_file(self, path: str) -> bool:
        """Whether a file has been read or surfaced this session."""
        return path in self.files

    def remaining(self, session_budget: int, floor: int = 0) -> int:
        """Budget left under ``session_budget`` (never below ``floor``)."""
        return max(floor, session_budget - self.consumed_tokens)

    def as_dict(self) -> dict:
        """Structured view for JSON output."""
        fully = sum(1 for s in self.files.values() if s.fully_read)
        return {
            "session_id": self.session_id,
            "consumed_tokens": self.consumed_tokens,
            "turns": self.turns,
            "files": len(self.files),
            "files_fully_read": fully,
            "symbols": len(self.symbols),
        }


def iter_records(transcript_path: Path) -> Iterator[dict]:
    """Yield parseable JSON records from a transcript, skipping junk.

    Best-effort: a missing file yields nothing; a malformed line or a
    non-object record is skipped silently (the R1 schema-drift guard).

    Args:
        transcript_path: Path to the session JSONL.

    Yields:
        Each record that parses to a JSON object.
    """
    try:
        text = transcript_path.read_text()
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(record, dict):
            yield record


def _rel_path(file_path: str, root: Path) -> str | None:
    """Normalize a read target to a repo-relative POSIX path, or None."""
    if not isinstance(file_path, str) or not file_path:
        return None
    p = Path(file_path)
    if not p.is_absolute():
        return p.as_posix()
    try:
        return p.relative_to(root).as_posix()
    except ValueError:
        return None


def _content_blocks(record: dict) -> list:
    """The message content blocks of a record, or an empty list."""
    message = record.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    return content if isinstance(content, list) else []


def _is_direct(block: dict) -> bool:
    """Whether a tool_use was issued by the main agent (not a subagent)."""
    caller = block.get("caller")
    if isinstance(caller, dict):
        return caller.get("type", "direct") == "direct"
    return True


def _usage_total(message: dict) -> int | None:
    """Context-window occupancy from an assistant turn's usage, if any."""
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None
    return (
        int(usage.get("input_tokens", 0) or 0)
        + int(usage.get("cache_read_input_tokens", 0) or 0)
        + int(usage.get("cache_creation_input_tokens", 0) or 0)
    )


def _read_symbols(
    state: FileState, inp: dict, index: MapIndex
) -> None:
    """Fold a ``Read``'s file/line-range into a file's seen symbols."""
    syms = index.symbols_by_path.get(state.path, [])
    offset = inp.get("offset")
    limit = inp.get("limit")
    if offset is None and limit is None:
        state.fully_read = True
        state.symbols_seen |= {s.id for s in syms}
        return
    lo = int(offset) if isinstance(offset, int) else 1
    hi = lo + int(limit) if isinstance(limit, int) else None
    for s in syms:
        if s.start_line >= lo and (hi is None or s.start_line < hi):
            state.symbols_seen.add(s.id)


def _apply_read(
    view: LedgerView, inp: dict, index: MapIndex, root: Path
) -> None:
    """Register a ``Read`` tool call against the ledger."""
    rel = _rel_path(inp.get("file_path", ""), root)
    if rel is None:
        return
    state = view.files.setdefault(rel, FileState(rel))
    _read_symbols(state, inp, index)


def _apply_dekko(view: LedgerView, inp: dict, root: Path) -> None:
    """Attribute a dekko tool call's target file as dekko-emitted."""
    for key in _TARGET_KEYS:
        rel = _rel_path(inp.get(key, ""), root)
        if rel is not None:
            view.files.setdefault(rel, FileState(rel)).dekko_emitted = True
            return


def _apply_tool_use(
    view: LedgerView, block: dict, index: MapIndex, root: Path
) -> None:
    """Route one tool_use block to the right ledger update."""
    if not _is_direct(block):
        return
    name = block.get("name", "")
    inp = block.get("input")
    if not isinstance(inp, dict):
        return
    if name == "Read":
        _apply_read(view, inp, index, root)
    elif _DEKKO_TOOL_MARK in name.lower():
        _apply_dekko(view, inp, root)


def build_view(
    transcript_path: Path, index: MapIndex, root: Path
) -> LedgerView:
    """Project a session transcript into a :class:`LedgerView`.

    Walks the JSONL once, accumulating files/symbols in context from
    ``Read`` calls and the real token tally from assistant ``usage``
    (falling back to an estimate over message/result text when no usage
    is reported). Defensive throughout: unparseable or unexpected records
    are skipped, so the worst case is an empty view, never an exception.

    Args:
        transcript_path: Path to the session JSONL.
        index: Loaded map index, for file→symbol attribution.
        root: Repository root, for relativizing absolute read paths.

    Returns:
        The reconstructed ledger view.
    """
    view = LedgerView()
    peak_usage: int | None = None
    est_fallback = 0
    for record in iter_records(transcript_path):
        sid = record.get("sessionId")
        if isinstance(sid, str) and sid:
            view.session_id = sid
        if record.get("type") == "assistant":
            view.turns += 1
            message = record.get("message")
            if isinstance(message, dict):
                total = _usage_total(message)
                # Peak, not last: context occupancy only grows within a
                # turn, so the high-water mark is robust to a trailing
                # in-progress turn whose usage is still zero/partial (the
                # transcript is read while it is being written). It also
                # fails safe — overstating consumed tokens only makes the
                # budget-aware pushes terser, never over-eager.
                if total:
                    peak_usage = max(peak_usage or 0, total)
        for block in _content_blocks(record):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                _apply_tool_use(view, block, index, root)
            else:
                est_fallback += _block_estimate(block)
    view.consumed_tokens = (
        peak_usage if peak_usage is not None else est_fallback
    )
    return view


def _block_estimate(block: dict) -> int:
    """Rough token cost of a non-tool_use content block (fallback tally)."""
    content = block.get("content")
    if isinstance(content, str):
        return estimate_tokens(content)
    text = block.get("text")
    if isinstance(text, str):
        return estimate_tokens(text)
    return 0


def _encode_project(root: Path) -> str:
    """Claude Code's project-dir encoding of a repo path (best-effort)."""
    return str(root).replace("/", "-")


def find_transcript(root: Path, session: str | None = None) -> Path | None:
    """Locate a session transcript for ``root`` under ``~/.claude``.

    A convenience for the CLI when no explicit ``--transcript`` is given;
    best-effort and never raises. With ``session`` it resolves that exact
    session file; otherwise it returns the most recently modified
    transcript for the project.

    Args:
        root: Repository root (the project the session ran in).
        session: Explicit session id, or ``None`` for the latest.

    Returns:
        The transcript path, or ``None`` when none is found.
    """
    project = Path.home() / ".claude" / "projects" / _encode_project(root)
    if not project.is_dir():
        return None
    if session:
        candidate = project / f"{session}.jsonl"
        return candidate if candidate.is_file() else None
    transcripts = sorted(
        project.glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return transcripts[0] if transcripts else None


def render_text(view: LedgerView, budget: int | None) -> list[str]:
    """Render the ledger view as dense human/agent-readable lines."""
    sid = view.session_id or "unknown"
    head = (
        f"ledger · session {sid} · ~{view.consumed_tokens} tokens · "
        f"{view.turns} turns"
    )
    fully = sum(1 for s in view.files.values() if s.fully_read)
    lines = [
        head,
        f"files in context: {len(view.files)} ({fully} fully read)",
        f"symbols in context: {len(view.symbols)}",
    ]
    if budget is not None:
        lines.append(f"remaining vs {budget}: {view.remaining(budget)} tokens")
    ranked = sorted(
        view.files.values(),
        key=lambda s: (-len(s.symbols_seen), s.path),
    )
    for state in ranked[:_MAX_LISTED_FILES]:
        tags = []
        if state.fully_read:
            tags.append("read")
        if state.dekko_emitted:
            tags.append("dekko")
        suffix = (" · " + ", ".join(tags)) if tags else ""
        lines.append(
            f"  {state.path}  {len(state.symbols_seen)} syms{suffix}"
        )
    return lines


def run(
    root: Path,
    transcript: Path | None,
    session: str | None,
    budget: int | None,
    as_json: bool,
) -> int:
    """Inspect what the session has put in context (FR-C4).

    Args:
        root: Repository root (for the map and transcript discovery).
        transcript: Explicit transcript path, or ``None`` to discover one
            under ``~/.claude`` for ``root``.
        session: Session id to resolve when discovering, or ``None``.
        budget: Optional session budget, to report remaining tokens.
        as_json: Emit structured JSON instead of text.

    Returns:
        ``0`` ok, ``6`` when no transcript could be located.
    """
    from . import mapfile

    path = transcript or find_transcript(root, session)
    if path is None or not path.is_file():
        print(
            "dekko: no session transcript found (pass --transcript PATH)",
            file=sys.stderr,
        )
        return EXIT_NO_TRANSCRIPT
    index = mapfile.load_map(root) or MapIndex(root_label=root.name)
    view = build_view(path, index, root)
    if as_json:
        print(json.dumps(view.as_dict(), indent=2))
        return EXIT_OK
    for line in render_text(view, budget):
        print(line)
    return EXIT_OK
