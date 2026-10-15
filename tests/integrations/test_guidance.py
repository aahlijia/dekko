"""Guard rails for the agent guidance dekko ships.

One copy of the skills, the usage policy and the orientation preamble
serves every client (Claude Code, Codex, ...), so these tests keep that
text loadable by each client and free of wording only one of them can
act on. They also pin the trigger phrases each skill's ``description``
carried before it was made agent-neutral: a client decides whether to
use a skill from its description, so dropping a phrase can quietly make
a skill fire less often.
"""

from pathlib import Path

import pytest

from dekko.integrations import claude_md, orient

REPO_ROOT = Path(__file__).resolve().parents[2]
BUNDLE = REPO_ROOT / "integrations" / "claude"
SKILL_ROOTS = (BUNDLE / "skills", BUNDLE / "skills-codex")

# The plugin namespace Codex qualifies a plugin skill with (``dekko:x``).
PLUGIN_NAME = "dekko"
MAX_NAME = 64
MAX_QUALIFIED_NAME = 129

# Text that only makes sense on one client: a client-specific tool
# prefix, a client-only environment variable, or a behavior (the
# ``ask`` confirmation prompt) another client doesn't have.
CLIENT_ONLY_TOKENS = (
    "mcp__plugin_dekko_dekko__",
    "CLAUDE_PROJECT_DIR",
    "CLAUDE_PLUGIN_ROOT",
    "pause for confirmation",
)

# Phrases each description matched on before the wording went neutral.
# Additions next to them are fine; removing or rewording one is not.
TRIGGER_PHRASES: dict[str, tuple[str, ...]] = {
    "dekko-daemon": (
        "Start dekko's background daemon",
        "many bare `dekko` CLI commands via Bash",
        "several dekko CLI invocations in a row on the same repo",
        "a long refactor loop",
        "a batch of query/search/outline calls",
        "a hook/script context",
        "reparsing map.json from scratch",
    ),
    "dekko-debug": (
        "Work a stack trace, exception, or error report",
        "Trigger when given a traceback, an exception type or message, "
        "a failing test's error",
        '"why does X raise"',
        '"who handles this error"',
        '"how does control get from A to B"',
        '"where is this env var read"',
    ),
    "dekko-delegate": (
        "Brief a subagent with dekko's map before dispatching it",
        "Trigger before any Agent/Task tool call, Workflow `agent()`, "
        "teammate message, or scripted `claude -p` run",
        "that will explore, review, implement, or test in such a repo",
    ),
    "dekko-notes": (
        "Read and write dekko's symbol-anchored notes",
        "Trigger every time you pull a symbol's context (query_symbol, "
        "get_context_pack, dekko query/context)",
        "right after a non-obvious change",
        "right after any rename/move/signature edit",
    ),
    "dekko-orient": (
        "Use dekko's tools instead of grep/Read whenever a repo has a "
        ".dekko/ directory",
        "Trigger before any Grep/Glob/Read of a source file",
        "before a `grep`/`rg`/`find`/`cat` in Bash",
        '"find/locate/understand this symbol,"',
        '"what does this file/dir contain,"',
        '"who calls this,"',
        '"what will this change break,"',
        '"which tests should I run" impulse',
    ),
    "dekko-refactor": (
        "List every site a rename, move, or signature change must touch "
        "before editing",
        "Trigger when about to rename a function, method, class or type",
        "rename or move a file or module",
        "change a parameter list or return type",
        "move a symbol between files",
        "remove a symbol",
        "and again after those edits, before running tests",
    ),
    "dekko-review-context": (
        "Give a code-review or PR-description flow a structural head start",
        "Trigger when the user asks for a PR description, a pre-merge "
        "summary, or invokes a code-review flow (`/code-review`, "
        "`/review`, or an equivalent)",
    ),
    "dekko-verify": (
        "Sanity-check a suspiciously low or zero call-graph result from "
        "dekko (get_callers, get_callees, find_usages, impacted_tests, "
        "unused)",
        'before concluding "no callers" or "dead code,"',
        "Trigger whenever such a result looks surprising",
        "before deleting/renaming a symbol based on a zero-caller result",
        "when the repo mixes languages/has any unsupported-language files",
        "when an (external)/(unresolved) label lands on a name you're "
        "confident is first-party code",
    ),
}


def _skill_files() -> list[Path]:
    """Every ``SKILL.md`` under the skill roots that exist."""
    return sorted(
        path
        for root in SKILL_ROOTS
        if root.is_dir()
        for path in root.glob("*/SKILL.md")
    )


def _guidance_files() -> list[Path]:
    """Every Markdown file a client loads from a skill directory."""
    return sorted(
        path
        for root in SKILL_ROOTS
        if root.is_dir()
        for path in root.glob("*/*.md")
    )


def _frontmatter(path: Path) -> dict[str, str]:
    """The ``key: value`` pairs of a skill's leading ``---`` block.

    Skill frontmatter here is flat, one key per line, so a line split
    is enough; no YAML dependency is needed.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines and lines[0] == "---", f"{path} has no frontmatter"
    fields: dict[str, str] = {}
    for line in lines[1:]:
        if line == "---":
            return fields

        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.strip()

    pytest.fail(f"{path} frontmatter is never closed")


def _id(path: Path) -> str:
    """A short test id: the skill's directory and file name."""
    return f"{path.parent.name}/{path.name}"


def test_every_shipped_skill_is_found() -> None:
    names = {path.parent.name for path in _skill_files()}

    assert set(TRIGGER_PHRASES) <= names


@pytest.mark.parametrize("path", _skill_files(), ids=_id)
def test_skill_frontmatter_fits_every_client(path: Path) -> None:
    fields = _frontmatter(path)
    name = fields.get("name") or path.parent.name

    assert fields.get("description"), "description must be non-empty"
    assert len(name) <= MAX_NAME
    assert len(f"{PLUGIN_NAME}:{name}") <= MAX_QUALIFIED_NAME


@pytest.mark.parametrize("path", _guidance_files(), ids=_id)
def test_skill_text_has_no_client_only_tokens(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    found = [token for token in CLIENT_ONLY_TOKENS if token in text]

    assert not found, f"{_id(path)} names client-only text: {found}"


@pytest.mark.parametrize(
    "text",
    [claude_md._POLICY_BODY, orient._PREAMBLE],
    ids=["policy", "preamble"],
)
def test_standing_guidance_has_no_client_only_tokens(text: str) -> None:
    found = [token for token in CLIENT_ONLY_TOKENS if token in text]

    assert not found, f"client-only text: {found}"


@pytest.mark.parametrize(
    ("skill", "phrase"),
    [
        (skill, phrase)
        for skill, phrases in TRIGGER_PHRASES.items()
        for phrase in phrases
    ],
)
def test_description_keeps_its_trigger_phrases(
    skill: str,
    phrase: str,
) -> None:
    description = _frontmatter(BUNDLE / "skills" / skill / "SKILL.md")[
        "description"
    ]

    assert phrase in description
