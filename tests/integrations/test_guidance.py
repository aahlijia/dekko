"""Guard: dekko's agent guidance stays shared across MCP clients.

The bundled skills, the usage-policy block and the orientation preamble
are read by Claude Code and by Codex (which loads the same plugin
bundle). The design keeps one copy of each, worded so both agents can
follow it. These tests fail when an edit drifts back toward one client:
a skill outgrows Codex's frontmatter limits, a description loses a
phrase Claude already triggers on, or a client-only term appears with
no counterpart for the other agent.
"""

import re
from pathlib import Path

import pytest

from dekko.integrations import claude_md, orient

ROOT = Path(__file__).resolve().parent.parent.parent
SKILLS_DIR = ROOT / "integrations" / "claude" / "skills"
SKILL_FILES = sorted(SKILLS_DIR.glob("*/SKILL.md"))

# Codex's SKILL.md limits (codex-rs ``skills/src/parser.rs``).
CODEX_NAME_MAX = 64
CODEX_DESCRIPTION_MAX = 1024

# Phrases each skill's ``description`` carried before the wording went
# agent-neutral. The description is what makes Claude pick a skill, so
# neutral wording is added next to these, never swapped in for them.
TRIGGER_PHRASES: dict[str, tuple[str, ...]] = {
    "dekko-daemon": (
        "many bare `dekko` CLI commands via Bash",
        "several dekko CLI invocations in a row on the same repo",
        "reparsing map.json from scratch",
    ),
    "dekko-debug": (
        "a traceback, an exception type or message, a failing test's error",
        '"why does X raise"',
        '"who handles this error"',
        '"how does control get from A to B"',
        '"where is this env var read"',
    ),
    "dekko-delegate": (
        "Brief a subagent with dekko's map",
        "Agent/Task tool call, Workflow `agent()`, teammate message",
        "scripted `claude -p`",
        "explore, review, implement, or test",
    ),
    "dekko-notes": (
        "Read and write dekko's symbol-anchored notes",
        "every time you pull a symbol's context",
        "right after a non-obvious change",
        "right after any rename/move/signature edit",
    ),
    "dekko-orient": (
        "Use dekko's tools instead of grep/Read",
        "before any Grep/Glob/Read of a source file",
        "before a `grep`/`rg`/`find`/`cat` in Bash",
        '"find/locate/understand this symbol,"',
        '"who calls this,"',
        '"what will this change break,"',
        '"which tests should I run"',
    ),
    "dekko-refactor": (
        "List every site a rename, move, or signature change must touch",
        "about to rename a function, method, class or type",
        "rename or move a file or module",
        "change a parameter list or return type",
        "again after those edits, before running tests",
    ),
    "dekko-review-context": (
        "a PR description, a pre-merge summary",
        "(`/code-review`, `/review`, or an equivalent)",
    ),
    "dekko-verify": (
        "suspiciously low or zero call-graph result",
        'before concluding "no callers" or "dead code,"',
        "before deleting/renaming a symbol based on a zero-caller result",
        "an (external)/(unresolved) label",
    ),
}

# A client-only term that may appear only alongside its counterpart for
# the other agent, so neither reads guidance written for someone else.
PAIRED_TERMS: tuple[tuple[str, str], ...] = (
    ("mcp__plugin_dekko_dekko__", "mcp__dekko__"),
    ("claude -p", "codex exec"),
)

# Never valid in shared guidance: a Claude-only variable, or a tool name
# hard-coded with one client's prefix (write the bare name instead).
FORBIDDEN = (
    re.compile(r"CLAUDE_PROJECT_DIR"),
    re.compile(r"mcp__(?:plugin_dekko_)?dekko__[a-z]"),
)


def _frontmatter(path: Path) -> dict[str, str]:
    """The ``key: value`` pairs between a SKILL.md's ``---`` fences."""
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines and lines[0] == "---", f"{path} has no frontmatter"
    fields = {}
    for line in lines[1:]:
        if line == "---":
            return fields
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.strip()
    raise AssertionError(f"{path} frontmatter is never closed")


def _guidance_texts() -> list[tuple[str, str]]:
    """Every piece of shared guidance as ``(label, text)`` pairs."""
    texts = [
        (str(path.relative_to(ROOT)), path.read_text(encoding="utf-8"))
        for path in sorted(SKILLS_DIR.glob("*/*.md"))
    ]
    texts.append(("claude_md._POLICY_BODY", claude_md._POLICY_BODY))
    texts.append(("orient._PREAMBLE", orient._PREAMBLE))
    return texts


def test_every_skill_has_trigger_phrases_listed() -> None:
    names = {path.parent.name for path in SKILL_FILES}
    assert names == set(TRIGGER_PHRASES)


@pytest.mark.parametrize("path", SKILL_FILES, ids=lambda p: p.parent.name)
def test_frontmatter_fits_codex_limits(path: Path) -> None:
    fields = _frontmatter(path)
    assert fields.get("name") == path.parent.name
    assert len(fields["name"]) <= CODEX_NAME_MAX
    description = fields.get("description", "")
    assert description, f"{path} has an empty description"
    assert len(description) <= CODEX_DESCRIPTION_MAX


@pytest.mark.parametrize("path", SKILL_FILES, ids=lambda p: p.parent.name)
def test_description_keeps_claude_trigger_phrases(path: Path) -> None:
    description = _frontmatter(path)["description"]
    missing = [
        phrase
        for phrase in TRIGGER_PHRASES[path.parent.name]
        if phrase not in description
    ]
    assert not missing, f"{path.parent.name} dropped: {missing}"


@pytest.mark.parametrize(
    ("label", "text"),
    _guidance_texts(),
    ids=lambda v: v if "\n" not in v else "",
)
def test_client_only_terms_are_paired(label: str, text: str) -> None:
    for term, counterpart in PAIRED_TERMS:
        if term in text:
            assert counterpart in text, (
                f"{label} mentions {term!r} without {counterpart!r}"
            )


@pytest.mark.parametrize(
    ("label", "text"),
    _guidance_texts(),
    ids=lambda v: v if "\n" not in v else "",
)
def test_no_single_client_terms(label: str, text: str) -> None:
    for pattern in FORBIDDEN:
        match = pattern.search(text)
        assert match is None, f"{label} contains {match.group(0)!r}"
