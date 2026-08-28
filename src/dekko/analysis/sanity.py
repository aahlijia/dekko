"""``dekko sanity <target>``: cross-check a callers/uses/unused result
against a targeted grep sweep.

Automates the ``dekko-verify`` skill
(``integrations/claude/skills/dekko-verify/SKILL.md``), which is pure
guidance today — an agent has to both recognize a call-graph result as
"suspiciously low" *and* remember to actually run the one-grep sanity
check it recommends. Both are judgment calls that get skipped under
task pressure. ``sanity`` makes the check deterministic: run the same
``callers``/``uses`` query dekko would answer with, run one scoped
``grep -rn <bare-name>`` across the repo (excluding the same
directories ``dekko map`` already excludes — see
``core.walker.DEFAULT_EXCLUDE_DIRS``), diff the two hit sets by
``(file, line)``, and for every line grep found that dekko's answer
didn't, name the likely cause from ``dekko-verify``'s own documented
blind-spot list rather than leaving the agent to re-derive it.

A third mode, ``--unused <name>``, cross-checks the opposite kind of
result: a ``dekko unused`` "flagged dead" verdict, which is built
entirely from ``calls_in``/``referenced_in`` table lookups and never
looks at the file's raw text. This mode runs the same grep sweep and
reports every hit found outside the symbol's own definition, an
import/require statement, or a comment as "reference evidence" the
call-graph tables didn't already explain away — see
``classify_unused_reference`` and ``_run_unused_check``.

A fourth mode, ``--all`` (``run_all()``), removes the human selection
bias from the whole exercise: instead of one hand-picked ``target``,
it runs the same callers/grep cross-check over *every* in-repo symbol
with nonzero ``MapIndex.calls_in`` fan-in, deduping the expensive grep
subprocess by bare name (classification depends only on
``(root, bare_name)``, never on which symbol sharing that name is
being checked — see ``_classify_grep_hits``, extracted so ``run()``
and ``run_all()`` provably classify identically), and reports a triage
summary (an aggregate cause histogram plus the symbols with an
unexplained miss) rather than a full per-symbol dump. Callers mode
only — see ``run_all()``'s own docstring and
``.features/plans/round23/24-sanity-all-sweep.md`` for the full design
and its ``--usages``-sweep-mode/MCP-exposure open questions.

In callers mode (single-target ``run()`` only, not ``--all``), a
grep-only hit can also be classified ``CAUSE_LIKELY_EXTERNAL_COLLISION``
when the target is a method, no other repo-defined symbol shares its
bare name, and neither the hit's own line nor its file's top-of-file
imports mention the target's declaring type — the cheap, no-type-
inference proxy for "this is almost certainly an unrelated external-
library method sharing the name, not a real caller" (e.g. Java/AssertJ
``.isTrue()`` colliding with a repo-defined ``isTrue`` method). See
``_receiver_mismatch()`` and
``.features/plans/round23/25-sanity-receiver-mismatch-cue.md`` for the
full design.

This is a spot check, not a re-verification — see the module's own
``EXIT_OK``-always-on-a-clean-run contract in ``run()``'s docstring.
The blind-spot causes are heuristic pattern-matches on a grep-only
line's syntax, not a re-derivation of the resolver's actual reasoning,
so every cause is worded as a *likely* explanation, matching
``dekko-verify``'s own cautious framing.

Two design decisions worth calling out (see
``.features/plans/integrations/05-dekko-sanity-command.md`` for the
full design-question list this resolves):

- **Test filtering defaults to excluded, not included.** The plain
  CLI (``dekko query callers``) defaults to *including* tests unless
  ``--no-tests`` is passed. The MCP ``get_callers``/``find_usages``
  tools default the other way (tests excluded unless
  ``include_tests=true``) — and it's *that* default the
  ``dekko-verify`` blind-spot list is calibrated to ("get_callers/
  get_callees used their default --no-tests filter"). Since
  ``sanity`` exists to automate exactly that skill, it mirrors the
  MCP default: tests are excluded from the internal dekko-side query
  unless ``--include-tests`` is passed, so a grep-only hit inside a
  test file has a real, meaningful "likely filtered by default"
  explanation to give.
- **The internal dekko-side query never truncates.** ``sanity``'s own
  ``--limit``/``--budget`` cap the *rendered report* (the
  matches/dekko-only/grep-only rows actually printed), never the
  comparison data gathered to build it — the underlying
  ``query.run()`` call always uses a very large internal limit/budget
  so a real dekko hit is never misclassified as a "grep-only miss"
  purely because it fell outside a small default page size.
"""

import io
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from dataclasses import dataclass
from pathlib import Path

from dekko import repo_ops
from dekko.analysis import query
from dekko.classify import is_test_path
from dekko.core import languages
from dekko.core.model import TYPE_KINDS, Symbol
from dekko.core.walker import DEFAULT_EXCLUDE_DIRS
from dekko.render.mapfile import MapIndex
from dekko.storage.cache import CACHE_DIR
from dekko.textutil import Meter, fit_to_budget

EXIT_OK = 0
# A genuinely broken invocation (grep unavailable/timed out/errored) —
# distinct from a "clean run that happens to have grep-only misses",
# which still exits 0 (advisory, not an error; see ``run()``).
EXIT_GREP_FAILED = 2
EXIT_NOT_FOUND = query.EXIT_NOT_FOUND
EXIT_AMBIGUOUS = query.EXIT_AMBIGUOUS
# ``--all --fail-on-unexplained``'s CI-gate exit code -- next free code
# after EXIT_GREP_FAILED. See ``run_all()``.
EXIT_UNEXPLAINED_FOUND = 3

# Safety cap on how many unique bare names ``sanity --all`` will sweep
# -- analogous to ``_MAX_GREP_LINES``'s "hard ceiling on work done,
# independent of report caps" pattern. Overridable via ``--max-names``.
_MAX_SWEEP_NAMES = 2000

# ``--all``'s own default ``--jobs`` -- deliberately *not* ``map``'s
# sequential-by-default 1 (see module docstring's ``--all`` section):
# the sweep's whole purpose is a batch/triage run where wall-clock
# time matters and each unit of work (one read-only grep subprocess)
# is independent and side-effect-free.
_ALL_JOBS_DEFAULT = 4


# Directories the grep sweep must skip so its grep-only bucket isn't
# dominated by noise ``dekko map`` itself never even considers.
# ``DEFAULT_EXCLUDE_DIRS`` (noise dirs + vendored dirs) is already a
# public module-level constant in ``core.walker`` — no prerequisite
# refactor needed to reuse it, resolving the plan's "exclude-pattern
# reuse" open question. ``.dekko/`` (the cache/output dir) is added on
# top since it isn't itself noise/vendored source but must never be
# grepped either.
def _grep_exclude_dirs() -> tuple[str, ...]:
    """Directory names the grep sweep excludes, sorted for determinism."""
    return tuple(sorted(DEFAULT_EXCLUDE_DIRS | {CACHE_DIR}))


_GREP_TIMEOUT = 30
# Safety cap on raw grep output lines processed — a maximally generic
# bare name (see ``_GENERIC_NAMES``) on a huge repo could otherwise
# return an unbounded number of matches; this is a hard ceiling on
# work done, independent of the report's own --limit/--budget caps.
# Round 21 Track B1: this cap used to truncate silently -- cline's
# repro showed ``matches (24) + grep_only (4976) == 5000`` exactly,
# with every "dekko-only" row past the cap a truncation artifact
# rather than a real resolver disagreement. ``_run_grep`` now reports
# whether the cap was hit (``GrepSweepResult.truncated``) so ``run()``
# can disclose it and stop reporting a false-confidence "dekko-only"
# count under truncation — see ``run()``'s own handling.
_MAX_GREP_LINES = 5000

# Round 21 Track B2: a single-line 26 MB cache/data file that grep's
# own ``-I`` binary-skip heuristic didn't catch (claude-code.md §2.2)
# produced 26 MB of terminal output from one command. A real source
# line is never remotely this long -- a raw grep line past this many
# characters is definitionally a binary/data blob, not code worth
# reporting as a hit at all (see ``_run_grep``'s pathological-line
# guard).
_PATHOLOGICAL_LINE_CHARS = 10_000

# Round 21 Track B2: an unconditional length cap on any snippet that
# does make it into a rendered/serialized row -- independent of, and a
# lower bar than, ``_PATHOLOGICAL_LINE_CHARS`` (that guard drops a hit
# entirely; this one just keeps an ordinary-but-long real source line
# from bloating the report). Applied at render/serialize time in
# ``_grep_row``, never during classification -- ``classify_miss`` is
# always called with the hit's full, untruncated snippet.
_SNIPPET_MAX_CHARS = 240

# Effectively-unbounded caps for the internal dekko-side callers/uses
# query this module runs to build its own comparison set — see the
# module docstring's second design note. Sized well past any
# realistic real-world result set rather than using a true sentinel,
# since ``query.run`` treats ``budget=None`` as "fall back to
# DEFAULT_RELATION_BUDGET", not "unbounded".
_INTERNAL_LIMIT = 1_000_000
_INTERNAL_BUDGET = 10**9

# Default cap on rendered report rows per bucket (matches/dekko-only/
# grep-only) — independent of the internal fetch's own caps above.
DEFAULT_REPORT_LIMIT = 200

# --- blind-spot classification -------------------------------------

CAUSE_QUALIFIED_CALL = (
    "cross-package/qualified call — known resolver blind spot"
)
CAUSE_UNSUPPORTED_LANGUAGE = (
    "unparsed-language file — dekko can't parse this file at all"
)
CAUSE_TEST_FILTER = (
    "likely filtered by default --no-tests; re-run with --include-tests"
)
CAUSE_GENERIC_NAME = (
    "generic name in a dense repo; treat dekko's count as directional, "
    "not exact"
)
CAUSE_COMMENT_MENTION = (
    "comment mention — not a call site (near the symbol's own "
    "definition, or in its file's leading header comment)"
)
CAUSE_IMPORT_STATEMENT = (
    "import/require statement naming the symbol — not a call site"
)
CAUSE_LIKELY_EXTERNAL_COLLISION = (
    "likely an unrelated external-library method sharing this bare "
    "name — no other repo-defined candidate exists, and neither this "
    "line nor this file's imports mention the target's declaring type"
)
CAUSE_UNEXPLAINED = "unexplained miss — inspect manually"

# Generous top-of-file import/using-block scan window for
# ``_receiver_mismatch``'s cheap textual proxy check -- see that
# function's own docstring.
_TYPE_REFERENCE_WINDOW_LINES = 60

# --- unused-mode reference-shape classification ------------------------
#
# ``classify_unused_reference`` answers a different question than
# ``classify_miss`` above: not "why didn't dekko count this as a call,"
# but "does this grep hit indicate a reference dekko's zero-evidence
# claim doesn't already account for." See ``sanity --unused``'s design
# doc (``.features/plans/round23/23-sanity-unused-variant.md``) for the
# full rationale.
SHAPE_CALL = "call"
SHAPE_SPREAD = "spread"
SHAPE_TYPEOF = "typeof"
SHAPE_SUBSCRIPT = "subscript"
SHAPE_OTHER = "other"

# Deliberately plain regex over the raw grep line, same philosophy as
# every other check in this module ("heuristic pattern-matches on a
# grep-only line's syntax, not a re-derivation of the resolver's
# actual reasoning" — see the module docstring). Not AST-aware, not
# per-language — these four shapes are common enough across
# curly-brace languages that one shared heuristic set covers the
# report's three named cases plus the general "any non-call mention"
# catch-all, without hand-rolling a query per grammar the way
# reference_query fixes necessarily do.
_SPREAD_TEMPLATE = r"\.\.\.\s*{name}\b"
_TYPEOF_TEMPLATE = r"\btypeof\s+{name}\b"
_SUBSCRIPT_TEMPLATE = r"{name}\s*\["
_BARE_CALL_TEMPLATE = r"\b{name}\s*\("


def classify_unused_reference(
    snippet: str,
    bare_name: str,
    *,
    path: str,
) -> tuple[str, str | None]:
    """Classify one grep hit for a symbol ``dekko unused`` flagged dead.

    Returns ``(bucket, detail)``:

    - ``("noise", CAUSE_IMPORT_STATEMENT)`` / ``("noise",
      CAUSE_COMMENT_MENTION)`` — the hit is explained by something
      already accounted for elsewhere (an import naming the symbol
      doesn't call it; a comment mentioning it isn't code). Not
      reported as reference evidence.
    - ``("reference", SHAPE_*)`` — everything else: a real, non-noise
      mention of the bare name outside its own definition.
      ``SHAPE_CALL`` (bare ``name(`` or qualified ``x.name(``/
      ``x::name(``) is checked before spread/typeof/subscript, since
      ``...name()`` (spreading a call's *result*) is a genuine call
      site first and a spread second — the more actionable
      classification wins. ``SHAPE_OTHER`` is the catch-all for every
      other bare mention (assignment RHS, argument, destructuring
      element, array/object member, JSX prop, etc.) — this is
      deliberately not scoped to just the three named shapes, so a
      reference pattern no language's ``reference_query`` covers *yet*
      still surfaces as "reference evidence found," not silently
      dropped for not matching a known template. This is the
      generality behind "a general safety net beyond any one
      language-specific detection fix."

    Unlike ``classify_miss``, comment detection here is unconditional
    (no ``near_own_definition`` gate) — in this mode a comment
    mentioning the bare name *anywhere* in the repo is still just a
    comment, not usage evidence, regardless of where it sits relative
    to the symbol's own definition.

    Args:
        snippet: The grep-matched line's text.
        bare_name: The bare identifier being searched for.
        path: The hit's repo-relative path (for comment-style lookup).

    Returns:
        ``(bucket, detail)`` where ``bucket`` is ``"noise"`` or
        ``"reference"`` and ``detail`` is a ``CAUSE_*`` or ``SHAPE_*``
        constant.
    """
    if _looks_like_import_statement(snippet, bare_name):
        return "noise", CAUSE_IMPORT_STATEMENT
    if _looks_like_comment_line(snippet, path):
        return "noise", CAUSE_COMMENT_MENTION
    name = re.escape(bare_name)
    if _looks_qualified_call(snippet, bare_name) or re.search(
        _BARE_CALL_TEMPLATE.format(name=name), snippet
    ):
        return "reference", SHAPE_CALL
    if re.search(_SPREAD_TEMPLATE.format(name=name), snippet):
        return "reference", SHAPE_SPREAD
    if re.search(_TYPEOF_TEMPLATE.format(name=name), snippet):
        return "reference", SHAPE_TYPEOF
    if re.search(_SUBSCRIPT_TEMPLATE.format(name=name), snippet):
        return "reference", SHAPE_SUBSCRIPT
    return "reference", SHAPE_OTHER


# A name this short is common enough on its own (loop variables aside,
# real identifiers this short — "id", "map", "new" — collide constantly
# in a dense repo) to warrant the same "treat as directional" caution
# dekko-verify gives its own longer curated examples below.
_GENERIC_NAME_MAX_LEN = 3
# dekko-verify's own named examples ("new", "then", "map", "iter_mut")
# plus a few more of the same shape: short, high-frequency method/
# function names that collide across unrelated types in any
# sufficiently large codebase.
_GENERIC_NAMES = frozenset(
    {
        "new",
        "then",
        "map",
        "iter_mut",
        "get",
        "set",
        "run",
        "add",
        "init",
        "next",
        "main",
        "build",
        "parse",
        "load",
        "save",
        "start",
        "stop",
        "send",
        "call",
        "exec",
        "apply",
        "open",
        "close",
        "update",
        "remove",
        "create",
        "delete",
        "write",
        "read",
    }
)

# Matches an identifier immediately followed by ``.name(`` or
# ``::name(`` — the shape of a Go ``pkg.Func(``, a C++
# ``namespace::func(``/``Type::method(``, or a Java/Python
# ``Type.method(`` qualified call, all of which
# ``dekko-verify/SKILL.md`` names as the resolver's known blind spot.
# Built per-hit (the bare name varies), not module-level.
_QUALIFIED_CALL_TEMPLATE = r"[A-Za-z_][A-Za-z0-9_]*(?:\.|::){name}\s*\("


def _is_generic_name(name: str) -> bool:
    """Whether ``name`` is short/common enough to warrant a directional
    caution — dekko-verify's "dense-repo common short method name"
    case."""
    return len(name) <= _GENERIC_NAME_MAX_LEN or name.lower() in _GENERIC_NAMES


def _looks_qualified_call(snippet: str, bare_name: str) -> bool:
    """Whether a grep-matched line looks like a qualified call site."""
    pattern = re.compile(
        _QUALIFIED_CALL_TEMPLATE.format(name=re.escape(bare_name))
    )
    return pattern.search(snippet) is not None


# Round 21 Track B3: the single most common "grep-only" shape on any
# import-heavy codebase (~300+ of claude-code's grep-only bucket, 8 of
# claude-buddy's) is a bare import/require statement naming the
# target — correctly excluded from dekko's own callers count (an
# import binds a name, it doesn't call it), but with no dedicated
# ``classify_miss()`` cause before this, every one fell through to
# ``CAUSE_UNEXPLAINED``. Each template is anchored at the line start
# (after stripping leading whitespace) so a line that merely mentions
# "import" mid-sentence (prose, a different identifier) never
# false-positives -- a real import/require statement's keyword always
# opens the (stripped) line.
_ESM_NAMED_IMPORT_TEMPLATE = (
    r"^import\s+(?:type\s+)?\{{[^}}]*\b{name}\b[^}}]*\}}\s*from\s+['\"]"
)
_ESM_DEFAULT_IMPORT_TEMPLATE = (
    r"^import\s+(?:\*\s+as\s+)?{name}\s+from\s+['\"]"
)
_PY_FROM_IMPORT_TEMPLATE = r"^from\s+\S+\s+import\s+.*\b{name}\b"
_IMPORT_LINE_TEMPLATES = (
    _ESM_NAMED_IMPORT_TEMPLATE,
    _ESM_DEFAULT_IMPORT_TEMPLATE,
    _PY_FROM_IMPORT_TEMPLATE,
)
# CJS ``require(...)`` doesn't bind its target the way ESM/Python
# imports do (it's an ordinary call expression, e.g. ``const { NAME }
# = require('x')`` or ``const NAME = require('x')``) — checked
# separately: "is this a require(...) call, and does the line mention
# the bare name outside the call's own module-path argument" rather
# than one combined regex, since the destructuring/assignment shape in
# front of ``require(...)`` varies too much for one anchored template.
# The module-path argument itself is excluded from the name search
# below (not just the whole line checked as-is) so a require of a
# module whose *path* happens to contain the bare name (e.g.
# ``require('./target')`` binding to some other local name) doesn't
# false-positive the way a naive whole-line search would.
_REQUIRE_CALL = re.compile(r"\brequire\(\s*['\"][^'\"]+['\"]\s*\)")


def _looks_like_import_statement(snippet: str, bare_name: str) -> bool:
    """Whether a grep-matched line is a bare import/require statement
    naming ``bare_name`` — not a call or reference to it."""
    stripped = snippet.strip()
    name = re.escape(bare_name)
    for template in _IMPORT_LINE_TEMPLATES:
        if re.search(template.format(name=name), stripped):
            return True
    match = _REQUIRE_CALL.search(stripped)
    if match:
        outside_call = stripped[: match.start()] + stripped[match.end() :]
        return re.search(rf"\b{name}\b", outside_call) is not None
    return False


# Round 22 claude-buddy.md §2.4: ``_looks_like_import_statement`` only
# catches the single-line ``import { X } from "...";`` shape --
# _ESM_NAMED_IMPORT_TEMPLATE is anchored at line start and requires
# ``import``/``{``/``from`` all on the matched line. A multi-line
# destructured import (``import {\n  X,\n  Y,\n} from "...";``) puts
# the bare-name hit on a line containing only ``  X,`` -- none of
# those tokens are on that line, so the anchored regex never matches
# and it fell through to CAUSE_UNEXPLAINED. This was the dominant
# "grep-only" shape in that repo (6 of 8 flagged rows), not the edge
# case.
_IMPORT_OPEN_BRACE = re.compile(r"^\s*import\s+(?:type\s+)?\{")
# How many lines above a bare-name hit to scan for an unclosed
# ``import {`` block opener -- generous enough for a real multi-line
# destructured import list (which rarely runs past a couple dozen
# names) without scanning the whole file.
_IMPORT_WINDOW_LINES = 20


def _looks_like_multiline_import_member(
    root: Path, hit: "GrepHit", bare_name: str
) -> bool:
    """Whether ``hit``'s line is a bare ``name,``/``name`` member
    inside a multi-line destructured ``import { ... } from "...";``
    block -- ``_looks_like_import_statement`` only catches the
    single-line shape (round 22 claude-buddy.md §2.4: 6 of 8 flagged
    rows in that repo are this multi-line shape, the dominant style
    there). Reads a small window of the hit's own file around its
    line -- the only file re-read this module does, kept small and
    best-effort (any read/decode failure returns ``False``, same
    fallback as the rest of this module's parsing).

    Scans backward from the hit line (nearest line first) for the
    first brace-relevant line -- an ``import {`` opener or a line
    containing ``}`` -- and answers based on *that* line alone, not
    "any opener/any closer anywhere in the window" (round 23
    claude-buddy.md §2.1: a flat any()/any() scan let an unrelated
    earlier import's closing ``}`` falsely "close" a still-open block
    sitting directly above the hit, as soon as *any* window line
    happened to contain a ``}`` regardless of which opener it actually
    belonged to). A ``}``-bearing line is checked before an opener
    match on the *same* line so a complete single-line import
    (``import { X } from 'y';``, which matches both patterns) is
    correctly treated as closed, not as a dangling opener.
    """
    stripped = hit.snippet.strip().rstrip(",")
    if stripped != bare_name:
        return False  # not a bare "name," line at all -- cheap bail-out
    try:
        lines = (
            (root / hit.path)
            .read_text(encoding="utf-8", errors="replace")
            .splitlines()
        )
    except OSError:
        return False
    start = max(0, hit.line - 1 - _IMPORT_WINDOW_LINES)
    window = lines[start : hit.line - 1]  # lines strictly above the hit
    for ln in reversed(window):
        if "}" in ln:
            return False  # nearest brace event is a close
        if _IMPORT_OPEN_BRACE.match(ln):
            return True  # nearest brace event is an unclosed opener
    return False  # no opener at all within the window


# Bounded scan depth for the leading-header-comment check — matches
# _TYPE_REFERENCE_WINDOW_LINES's "generous but bounded" precedent. A
# hit farther into the file than this can't be part of an
# uninterrupted from-line-1 comment run in any file worth trusting the
# heuristic on, so it's treated as "not a header mention" rather than
# triggering an unbounded read.
_HEADER_SCAN_LINES = 60


def _in_leading_header_comment(root: Path, hit: "GrepHit") -> bool:
    """Whether ``hit`` sits inside an uninterrupted comment/blank-line
    run starting at line 1 of its own file -- the "module summary"
    shape a doc-comment-proximity check alone can't catch (a header
    block naming several of the file's exports can sit dozens of lines
    above any one of their definitions; see
    ``.features/plans/round24/
    07-sanity-comment-mention-file-header-gap.md``).

    Deliberately stricter than a bare ``looks_like_comment`` check on
    the hit line alone: every line from 1 up to and including the hit
    line must be blank or comment-shaped, not just the hit line
    itself. This is what keeps the check safe without a proximity
    bound -- a false positive would require an unbroken comment run
    from the very top of the file, which the operator-continuation
    false-positive shape ``_COMMENT_PROXIMITY_LINES``'s own comment
    warns about (a wrapped ``* Helper(x-1)`` multiplication
    continuation) cannot produce on its own, since that shape only
    ever appears *inside* an already-open real comment block, itself
    only reachable via the same all-comment-since-line-1 run.

    Reads a small, bounded prefix of the hit's own file -- the same
    "small file re-read, best-effort" pattern as
    ``_looks_like_multiline_import_member`` and ``_receiver_mismatch``;
    any read/decode failure or a hit past ``_HEADER_SCAN_LINES``
    returns ``False`` (never a guess).
    """
    if hit.line > _HEADER_SCAN_LINES:
        return False
    try:
        lines = (
            (root / hit.path)
            .read_text(encoding="utf-8", errors="replace")
            .splitlines()
        )
    except OSError:
        return False
    for ln in lines[: hit.line]:
        if ln.strip() and not _looks_like_comment_line(ln, hit.path):
            return False
    return True


# How close a grep-only hit must be to the target's own definition
# line to be considered "near" it for doc-comment classification.
# Wide enough to cover a leading block/line comment stacked directly
# above a def (Go/Rust/Java/C/C++ doc-comment convention) and a
# same-line-opening Python docstring immediately below it, without
# being so wide it starts absorbing unrelated nearby code.
_COMMENT_PROXIMITY_LINES = 3

# Comment-marker "families" shared by multiple grammars. Each tuple is
# a set of line-start prefixes that -- within the *specific* grammars
# assigned that family below -- can only ever open a comment, never a
# real statement/expression/operator. Bare "*" is deliberately absent
# from every C-style family: a Javadoc/JSDoc "* @param ..." line is
# real, but so is a gofmt/rustfmt/clang-format line-wrapped
# "* Helper(x-1)" multiplication/dereference continuation right next
# to a definition -- the false-positive risk outweighs the narrow
# benefit, the same "accept the gap" trade-off this design already
# makes for multi-line docstring prose (see the module's design plan).
_SLASH_STYLE = ("//", "/*")
_HASH_STYLE = ("#",)
_DASH_STYLE = ("--",)
_SEMI_STYLE = (";",)
_PERCENT_STYLE = ("%",)
_BANG_STYLE = ("!",)
_PAREN_STAR_STYLE = ("(*",)
_QUOTE_STYLE = ('"',)
_PYTHON_DOCSTRING = ('"""', "'''")

# grammar name -> comment/docstring line-start prefixes. Covers every
# Tier-1 grammar (languages.TIER1_SPECS, via each spec's .grammar) and
# every Tier-2 grammar (languages.TIER2_GRAMMARS's values) *except*
# Vue and Svelte, deliberately left unmapped: both are mixed-content
# SFC formats (an HTML-ish template plus embedded script/style blocks,
# each with its own comment convention), and a single-line-start
# prefix check has no way to know which embedded language a hit's
# line belongs to. ``_grammar_for_path`` still resolves "vue"/
# "svelte" as a grammar name for these paths (``is_supported()``
# stays True, so ``unsupported_language`` is computed correctly
# elsewhere) -- ``_looks_like_comment_line`` just falls back to False
# for them, a false negative (heuristic doesn't fire) never a false
# positive.
#
# fsharp is intentionally SLASH_STYLE-only, not
# SLASH_STYLE + _PAREN_STAR_STYLE like ocaml/pascal: real F# code can
# reference the multiplication operator as a first-class value
# written ``(*)`` (e.g. ``List.reduce (*) xs``) -- an idiom OCaml's
# own lexer forbids for exactly this reason (OCaml requires
# ``( * )`` with spaces, because bare ``(*`` always opens a comment
# there), which is why ocaml/ocaml_interface keep
# ``_PAREN_STAR_STYLE`` safely but fsharp doesn't.
_COMMENT_PREFIXES_BY_GRAMMAR: dict[str, tuple[str, ...]] = {
    # Tier-1 (languages.TIER1_SPECS)
    "python": _HASH_STYLE + _PYTHON_DOCSTRING,
    "rust": _SLASH_STYLE,
    "c": _SLASH_STYLE,
    "cpp": _SLASH_STYLE,
    "javascript": _SLASH_STYLE,
    "typescript": _SLASH_STYLE,
    "tsx": _SLASH_STYLE,
    "go": _SLASH_STYLE,
    "java": _SLASH_STYLE,
    # Tier-2 (languages.TIER2_GRAMMARS), grouped by family
    "csharp": _SLASH_STYLE,
    "kotlin": _SLASH_STYLE,
    "swift": _SLASH_STYLE,
    "scala": _SLASH_STYLE,
    "dart": _SLASH_STYLE,
    "zig": _SLASH_STYLE,
    "gleam": _SLASH_STYLE,
    "groovy": _SLASH_STYLE,
    "solidity": _SLASH_STYLE,
    "d": _SLASH_STYLE,
    "hare": _SLASH_STYLE,
    "odin": _SLASH_STYLE,
    "haxe": _SLASH_STYLE,
    "fsharp": _SLASH_STYLE,
    "php": _SLASH_STYLE + _HASH_STYLE,
    "nix": _SLASH_STYLE + _HASH_STYLE,
    "pascal": _SLASH_STYLE + _PAREN_STAR_STYLE,
    "ruby": _HASH_STYLE,
    "perl": _HASH_STYLE,
    "r": _HASH_STYLE,
    "julia": _HASH_STYLE,
    "elixir": _HASH_STYLE,
    "nim": _HASH_STYLE,
    "bash": _HASH_STYLE,
    "zsh": _HASH_STYLE,
    "powershell": _HASH_STYLE,
    "crystal": _HASH_STYLE,
    "gdscript": _HASH_STYLE,
    "mojo": _HASH_STYLE,
    "starlark": _HASH_STYLE,
    "cmake": _HASH_STYLE,
    "tcl": _HASH_STYLE,
    "lua": _DASH_STYLE,
    "haskell": _DASH_STYLE,
    "elm": _DASH_STYLE,
    "ada": _DASH_STYLE,
    "sql": (*_DASH_STYLE, "/*"),
    "clojure": _SEMI_STYLE,
    "racket": _SEMI_STYLE,
    "scheme": _SEMI_STYLE,
    "commonlisp": _SEMI_STYLE,
    "elisp": _SEMI_STYLE,
    "erlang": _PERCENT_STYLE,
    "fortran": _BANG_STYLE,
    "ocaml": _PAREN_STAR_STYLE,
    "ocaml_interface": _PAREN_STAR_STYLE,
    "vim": _QUOTE_STYLE,
}


def _grammar_for_path(path: str) -> str | None:
    """The tree-sitter grammar name backing ``path``, Tier-1 or
    Tier-2.

    Mirrors ``languages.is_supported()``'s own Tier-1-then-Tier-2
    check, returning the grammar name itself instead of a bool, so
    ``_looks_like_comment_line`` can look up a grammar-specific
    marker set rather than guessing from one global list. ``None``
    for anything ``is_supported()`` also rejects, and for Vue/Svelte
    -- ``is_supported() == True`` for both, but they're deliberately
    left out of ``_COMMENT_PREFIXES_BY_GRAMMAR`` (see that table's
    comment).
    """
    spec = languages.spec_for_path(path)
    if spec is not None:
        return spec.grammar
    return languages.tier2_grammar_for_path(path)


def _looks_like_comment_line(snippet: str, path: str) -> bool:
    """Whether ``snippet``, considered alone, has the shape of a
    comment/docstring line in ``path``'s own grammar.

    Still pure/I/O-free -- ``path`` is only ever used as a string to
    resolve a grammar name (``_grammar_for_path``), never opened or
    read. Returns ``False`` for a path whose grammar isn't in
    ``_COMMENT_PREFIXES_BY_GRAMMAR`` (unsupported entirely, or one of
    the deliberately-unmapped Vue/Svelte SFC grammars).
    """
    prefixes = _COMMENT_PREFIXES_BY_GRAMMAR.get(_grammar_for_path(path) or "")
    if not prefixes:
        return False
    return snippet.strip().startswith(prefixes)


def classify_miss(
    snippet: str,
    bare_name: str,
    *,
    is_test_file: bool,
    unsupported_language: bool,
    tests_excluded: bool,
    near_own_definition: bool = False,
    looks_like_comment: bool = False,
    looks_like_import_member: bool = False,
    likely_unrelated_external: bool = False,
    in_leading_header_comment: bool = False,
) -> str:
    """Name the likely cause of one grep-only hit.

    A pure function over one grep-matched line plus its context — no
    repo/grep I/O, so it's directly testable in isolation (the two
    checks that need file I/O, ``_looks_like_multiline_import_member``
    and the receiver-mismatch heuristic behind
    ``likely_unrelated_external``, are computed by the caller and
    passed in rather than given to this function directly, to keep
    that contract). Checked in the order ``dekko-verify/SKILL.md``
    lists its blind spots: a qualified-call syntax match is checked
    first (it's visible in the line itself and the most specific
    signal available), then whether the line is a bare import/require
    statement naming the symbol (round 21 Track B3 — the dominant
    "grep-only" shape on any import-heavy codebase, same "visible in
    the line itself" precedence as the qualified-call check), then
    whether the line is a bare member of a multi-line destructured
    import block (round 22 claude-buddy.md §2.4 — the residual gap in
    the single-line check above), then whether the hit is a
    comment/docstring line either sitting near the symbol's own
    definition or inside its file's uninterrupted leading header
    comment block (round 24 07-sanity-comment-mention-file-header-gap.md
    — a module-header comment naming several exports can sit far from
    any one of their definitions; not a call at all either way), then
    whether the file is in a language dekko can't parse at all, then
    whether the caller's own receiver-mismatch heuristic flagged this
    hit as likely an unrelated external-library method sharing the
    target's bare name (round 23 spring-boot.md §4 — a same-named
    AssertJ/stdlib/third-party method colliding with the one
    repo-defined candidate; see ``_receiver_mismatch``), then whether
    it's a test file excluded by ``sanity``'s own default filtering,
    then whether the target name is short/generic enough that dekko's
    count should be read as directional rather than exact. A line
    matching none of these is reported as "unexplained" rather than
    forcing a guess that doesn't fit — matching the plan's own "false
    confidence from the classifier itself" caution.

    Args:
        snippet: The grep-matched line's text.
        bare_name: The bare identifier being searched for.
        is_test_file: Whether the hit's file is test code
            (``classify.is_test_path``).
        unsupported_language: Whether the hit's file is in a language
            dekko has no parser for (``languages.is_supported``).
        tests_excluded: Whether the dekko-side query this hit is being
            compared against excluded test files (``sanity``'s own
            default; see the module docstring).
        near_own_definition: Whether the hit sits in the same file as
            the target's own definition, within
            ``_COMMENT_PROXIMITY_LINES`` of it. Always ``False`` in
            ``--usages`` mode, where there is no in-repo definition to
            be near.
        looks_like_comment: Whether the hit line, taken alone, has the
            syntactic shape of a comment/docstring line in its file's
            grammar (``_looks_like_comment_line``).
        looks_like_import_member: Whether the hit line is a bare
            member of a multi-line destructured import block
            (``_looks_like_multiline_import_member``).
        in_leading_header_comment: Whether the hit sits inside an
            uninterrupted comment/blank-line run starting at line 1 of
            its own file (``_in_leading_header_comment``) — the
            module-header-comment shape ``near_own_definition`` alone
            doesn't cover. Independent of, and OR'd with,
            ``near_own_definition`` in the ``CAUSE_COMMENT_MENTION``
            check below; never a guess made from inside this function.
        likely_unrelated_external: Whether the caller's own
            receiver-mismatch gating (single-repo-candidate method
            target, resolvable declaring type) held for this run *and*
            ``_receiver_mismatch`` found no textual evidence of the
            declaring type in this hit's line or file. Always ``False``
            outside that gated scenario (see ``run()``'s own gating
            computation) — never a guess made from inside this
            function.

    Returns:
        One of the ``CAUSE_*`` constants.
    """
    if _looks_qualified_call(snippet, bare_name):
        return CAUSE_QUALIFIED_CALL
    if _looks_like_import_statement(snippet, bare_name):
        return CAUSE_IMPORT_STATEMENT
    if looks_like_import_member:
        return CAUSE_IMPORT_STATEMENT
    if looks_like_comment and (
        near_own_definition or in_leading_header_comment
    ):
        return CAUSE_COMMENT_MENTION
    if unsupported_language:
        return CAUSE_UNSUPPORTED_LANGUAGE
    if likely_unrelated_external:
        return CAUSE_LIKELY_EXTERNAL_COLLISION
    if tests_excluded and is_test_file:
        return CAUSE_TEST_FILTER
    if _is_generic_name(bare_name):
        return CAUSE_GENERIC_NAME
    return CAUSE_UNEXPLAINED


# --- grep sweep -------------------------------------------------------


@dataclass(frozen=True)
class GrepHit:
    """One matched grep line.

    Attributes:
        path: Repo-relative POSIX path.
        line: 1-based line number.
        snippet: The matched line's raw text.
    """

    path: str
    line: int
    snippet: str


@dataclass(frozen=True)
class GrepSweepResult:
    """One scoped grep sweep's outcome, plus its safety-cap disclosures.

    Attributes:
        hits: Matched lines that passed both safety caps.
        command_text: The grep command actually run — echoed in the
            report so a reader can rerun it by hand.
        error: ``None`` on success (including "zero matches", grep's
            own exit code 1); a message on a broken invocation.
            ``hits``/``command_text`` are best-effort when set.
        truncated: Whether raw grep output exceeded
            ``_MAX_GREP_LINES`` and was capped. A run with real
            matches beyond the cap makes the ``dekko-only`` bucket
            unreliable (grep may well have matched a line dekko
            "unexpectedly" lacks, just past the cutoff) — see
            ``run()``'s handling.
        skipped_pathological: Count of raw lines dropped for
            exceeding ``_PATHOLOGICAL_LINE_CHARS`` (a binary/data blob
            grep's own ``-I`` heuristic didn't catch, e.g. a
            single-line minified/cache file) — excluded from ``hits``
            entirely, counted toward neither a match nor a miss.
    """

    hits: list[GrepHit]
    command_text: str
    error: str | None
    truncated: bool = False
    skipped_pathological: int = 0


def _run_grep(root: Path, bare_name: str) -> GrepSweepResult:
    """Run the scoped grep sweep for ``bare_name`` under ``root``.

    Fixed-string (``-F``), whole-word (``-w`` — without it, a search
    for ``helper`` would also match ``helper_qualified_user`` as a
    substring, which is exactly the kind of noise a "targeted" grep
    sweep is supposed to avoid), binary-file-skipping (``-I``) match,
    one ``--exclude-dir`` per entry in ``_grep_exclude_dirs()`` — the
    same directories ``dekko map`` itself never walks into as source.
    Run with ``cwd=root`` and ``.`` as the search path (not the
    absolute ``root`` string) so grep's own output paths come back
    repo-relative, matching every path dekko's map already uses.

    Returns:
        A :class:`GrepSweepResult` — see its own docstring for what
        each field means, including the ``truncated``/
        ``skipped_pathological`` safety-cap disclosures.
    """
    cmd = ["grep", "-rn", "-I", "-w", "-F"]
    for d in _grep_exclude_dirs():
        cmd += ["--exclude-dir", d]
    cmd += ["--", bare_name, "."]
    command_text = " ".join(cmd)
    try:
        result = subprocess.run(
            cmd,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=_GREP_TIMEOUT,
        )
    except FileNotFoundError:
        return GrepSweepResult(
            [], command_text, "'grep' not found on this system"
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return GrepSweepResult([], command_text, f"grep sweep failed: {exc}")
    if result.returncode not in (0, 1):
        detail = result.stderr.strip() or f"exit {result.returncode}"
        return GrepSweepResult(
            [], command_text, f"grep sweep failed: {detail}"
        )
    raw_lines = result.stdout.splitlines()
    truncated = len(raw_lines) > _MAX_GREP_LINES
    hits: list[GrepHit] = []
    skipped_pathological = 0
    for raw in raw_lines[:_MAX_GREP_LINES]:
        if len(raw) > _PATHOLOGICAL_LINE_CHARS:
            skipped_pathological += 1
            continue
        path, _, rest = raw.partition(":")
        line_str, _, snippet = rest.partition(":")
        if not line_str.isdigit():
            continue
        if path.startswith("./"):
            path = path[2:]
        hits.append(GrepHit(path=path, line=int(line_str), snippet=snippet))
    return GrepSweepResult(
        hits,
        command_text,
        None,
        truncated=truncated,
        skipped_pathological=skipped_pathological,
    )


def _receiver_mismatch(root: Path, hit: GrepHit, declaring_type: str) -> bool:
    """Whether nothing in ``hit``'s own line or its file's top-of-file
    import/using block textually mentions ``declaring_type`` — the
    cheap, no-type-inference proxy for "this call's receiver almost
    certainly isn't the target's type" (round 23 spring-boot.md §4's
    own suggested heuristic: "target's declaring type recognizably
    unrelated to the call site's surrounding class/import list").

    Deliberately the same cost/precision tier as
    ``_looks_like_multiline_import_member`` — a small, bounded,
    best-effort re-read of the hit's own file, ``False`` on any I/O
    failure — not a real import-resolution pass: no alias tracking, no
    type inference, no wildcard-import handling. It answers "is there
    *any* textual sign," not "is this definitely unrelated" (see
    ``.features/plans/round23/25-sanity-receiver-mismatch-cue.md``'s
    "Risks / tradeoffs" section for why false positives here are
    low-cost and false negatives are the accepted, safe-direction
    failure mode).

    Args:
        root: Repo root, for the one bounded file re-read.
        hit: The grep-only candidate hit being checked.
        declaring_type: The target's declaring type's own simple name
            (the last segment of its container symbol's qualname).

    Returns:
        ``True`` when neither the hit's own line nor the first
        ``_TYPE_REFERENCE_WINDOW_LINES`` lines of its file mention
        ``declaring_type``; ``False`` otherwise (including on any file
        read failure — matches the module's existing
        I/O-failure-is-always-``False`` contract).
    """
    if declaring_type in hit.snippet:
        return False  # the type name is right there on the line
    try:
        lines = (
            (root / hit.path)
            .read_text(encoding="utf-8", errors="replace")
            .splitlines()
        )
    except OSError:
        return False
    window = lines[:_TYPE_REFERENCE_WINDOW_LINES]
    return not any(declaring_type in ln for ln in window)


def _classify_grep_hits(
    hits: list[GrepHit],
    bare_name: str,
    root: Path,
    *,
    own_def_locs: frozenset[tuple[str, int]],
    tests_excluded: bool,
    declaring_type: str | None = None,
) -> dict[tuple[str, int], str]:
    """Classify every grep hit for ``bare_name`` outside
    ``own_def_locs``, once.

    Extracted from ``run()``'s own per-hit classification block so
    ``run()`` (single target) and ``run_all()`` (the ``--all`` sweep's
    per-bare-name pass) provably run the *same* classification code,
    not two implementations that can drift apart — see the module
    docstring's ``--all`` paragraph. This is the whole point of the
    ``--all`` feature: it exists to catch a classification-logic
    regression like the round-23 ``_looks_like_multiline_import_
    member`` bug, and if this function's callers were allowed to
    diverge, a sweep could pass cleanly on exactly that kind of bug.

    Args:
        hits: Raw grep hits for ``bare_name`` (``sweep.hits``, before
            any dekko-side matching/diffing).
        bare_name: The bare identifier being searched for.
        root: Repo root, for ``_looks_like_multiline_import_member``'s
            and ``_receiver_mismatch``'s one small file re-read.
        own_def_locs: Every same-bare-named symbol's own definition
            line — excluded from classification entirely, matching
            ``run()``'s existing "not a call site to explain either
            way" treatment of a target's own definition.
        tests_excluded: Whether the dekko-side query being compared
            against excluded test files by default.
        declaring_type: The target's declaring type's own simple name,
            when ``run()``'s receiver-mismatch gating held for this
            run (single-repo-candidate method target with a resolvable
            declaring type) — ``None`` otherwise (the default, and
            always ``None`` from ``run_all()``'s sweep path, which
            doesn't compute this gating; see
            ``.features/plans/round23/25-sanity-receiver-mismatch-cue.md``).
            When set, each hit is additionally checked with
            ``_receiver_mismatch`` and the result threaded into
            ``classify_miss`` as ``likely_unrelated_external``.

    Returns:
        ``(path, line) -> CAUSE_*`` for every hit not in
        ``own_def_locs``. A caller diffs its own dekko-side hit set
        against this map's keys to get its own matches/dekko-only/
        grep-only split; the map's values are the pre-computed cause
        for every location that turns out to be grep-only.
    """
    causes: dict[tuple[str, int], str] = {}
    for h in hits:
        loc = (h.path, h.line)
        if loc in own_def_locs:
            continue
        causes[loc] = classify_miss(
            h.snippet,
            bare_name,
            is_test_file=is_test_path(h.path),
            unsupported_language=not languages.is_supported(h.path),
            tests_excluded=tests_excluded,
            near_own_definition=any(
                h.path == p and abs(h.line - ln) <= _COMMENT_PROXIMITY_LINES
                for p, ln in own_def_locs
            ),
            looks_like_comment=_looks_like_comment_line(h.snippet, h.path),
            looks_like_import_member=_looks_like_multiline_import_member(
                root, h, bare_name
            ),
            in_leading_header_comment=(
                _looks_like_comment_line(h.snippet, h.path)
                and _in_leading_header_comment(root, h)
            ),
            likely_unrelated_external=(
                declaring_type is not None
                and _receiver_mismatch(root, h, declaring_type)
            ),
        )
    return causes


# --- dekko-side comparison set ----------------------------------------


class _QueryFailedError(Exception):
    """Raised to unwind ``run()`` when the internal dekko query fails
    (not-found/ambiguous for ``uses`` — ``callers`` can't fail here
    since its target is already a resolved, disambiguated symbol)."""

    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


def _run_query_json(index: MapIndex, action: str, target: str) -> dict:
    """Run one query action, capturing its JSON doc instead of printing
    it — ``sanity`` composes its own report; dekko's own JSON is an
    intermediate value here, not the final output.

    Raises:
        _QueryFailedError: The query didn't resolve (its own error is
            already on stderr, via ``report_unresolved``/
            ``_run_uses_not_found`` — this just carries the exit code
            back up).
    """
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = query.run(
            index,
            action,
            target,
            as_json=True,
            limit=_INTERNAL_LIMIT,
            budget=_INTERNAL_BUDGET,
            sites=True,
            notes=False,
        )
    if code != query.EXIT_OK:
        raise _QueryFailedError(code)
    return json.loads(buf.getvalue())


def _dekko_hits_callers(
    index: MapIndex, sym_target: str
) -> tuple[list[tuple[str, int]], list[str]]:
    """``(site_hits, module_level_paths)`` for a resolved callers target.

    ``sym_target`` is a ``path:qualname:LINE`` string built from an
    already-resolved ``Symbol`` (see ``run()``) so this re-resolves to
    exactly the same candidate — the same disambiguation escape hatch
    ``note add``/``note rm`` already use for an overload set (resolves
    the plan's "overload/#N-suffixed target resolution" open
    question).

    ``module_level`` entries carry per-site lines when the map
    recorded them (round-23 §10 fix); those fold straight into
    ``hits`` alongside named-caller sites so a module-level call site
    with a known line matches grep like any other hit. Only entries
    with no recorded line (pre-v3 maps, or no site line captured)
    remain in the returned ``module_level_paths`` bucket.
    """
    doc = _run_query_json(index, "callers", sym_target)
    hits: list[tuple[str, int]] = []
    for entry in doc.get("results", []):
        sites = entry.get("sites") or [entry["line"]]
        hits.extend((entry["path"], ln) for ln in sites)
    module_level_bare: list[str] = []
    for m in doc.get("module_level", []):
        lines = m.get("lines")
        if lines:
            hits.extend((m["path"], ln) for ln in lines)
        else:
            module_level_bare.append(m["path"])
    return hits, module_level_bare


def _dekko_hits_uses(
    index: MapIndex, target: str
) -> tuple[list[tuple[str, int]], list[str]]:
    """``(site_hits, module_level_paths)`` for a ``uses`` target.

    ``target`` is used as-is — ``uses``/``find_usages`` already only
    ever operates on a bare base identifier (the trailing segment of a
    qualified external call, e.g. ``get`` for ``requests.get(...)``;
    see ``mapfile._callee_base``), never a qualified string. That
    resolves the plan's "bare-name extraction for --usages" open
    question: there is nothing to extract, since the target grammar
    ``uses`` already accepts is exactly one bare grep-able token.
    ``module_level_paths`` is always empty — ``uses`` results carry no
    module-level-pseudo-caller distinction the way callers/callees do.
    """
    doc = _run_query_json(index, "uses", target)
    hits: list[tuple[str, int]] = []
    for entry in doc.get("results", []):
        path = str(entry.get("caller", "")).split("::", 1)[0]
        hits.extend((path, ln) for ln in entry.get("lines") or [0])
    return hits, []


# --- report -------------------------------------------------------


def _hit_row(path: str, line: int) -> dict:
    return {"file": path, "line": line}


def _cap_snippet(snippet: str) -> str:
    """Cap a snippet to ``_SNIPPET_MAX_CHARS``, ellipsized when cut.

    Applied only here, at render/serialize time — ``classify_miss``
    and its helpers (``_looks_qualified_call``,
    ``_looks_like_comment_line``, ``_looks_like_import_statement``)
    always see a hit's full, untruncated ``snippet`` first, so
    truncation can never hide the very syntax a classification check
    is looking for.
    """
    if len(snippet) <= _SNIPPET_MAX_CHARS:
        return snippet
    return snippet[:_SNIPPET_MAX_CHARS] + "...(truncated)"


def _grep_row(hit: GrepHit, cause: str | None = None) -> dict:
    row = {
        "file": hit.path,
        "line": hit.line,
        "snippet": _cap_snippet(hit.snippet.strip()),
    }
    if cause is not None:
        row["cause"] = cause
    return row


def _fit_rows(
    rows: list[dict], budget: int | None, limit: int
) -> tuple[list[dict], Meter]:
    """Cap one report bucket by row count then token budget.

    Mirrors ``query._fit_entries`` exactly -- including returning the
    full ``Meter``, not just a bare total, so ``sanity --json`` can
    disclose truncation the same way every other budget-capped
    ``--json`` command already does (round 23 claude-code.md §2.1:
    ``sanity --json`` silently capped its row arrays at
    ``DEFAULT_REPORT_LIMIT`` with no ``meta``/``truncated`` disclosure
    anywhere in the output, unlike ``query --json``'s existing
    contract).

    Returns:
        ``(kept_rows, meter)``.
    """
    serialized = [json.dumps(r) for r in rows]
    kept, meter = fit_to_budget(serialized, budget, limit)
    return rows[: len(kept)], meter


# Round 21 Track B1/B2 disclosure notes -- printed (text mode) or
# attached (JSON mode) whenever ``_run_grep``'s safety caps actually
# fired, so a reader isn't left to (re-)discover on their own that the
# sweep was incomplete.
_TRUNCATION_NOTE = (
    f"grep sweep hit its {_MAX_GREP_LINES:,}-line safety cap; a "
    "dekko-resolved location the (incomplete) grep hit set doesn't "
    "cover may simply be past the cutoff, not a genuine resolver "
    "disagreement -- the dekko-only bucket below is reported as "
    "inconclusive rather than a count. matches/grep-only may also be "
    "undercounted."
)


def _pathological_skip_note(count: int) -> str:
    plural = "" if count == 1 else "s"
    return (
        f"{count} line{plural} skipped as pathological "
        f"(>{_PATHOLOGICAL_LINE_CHARS:,} characters, not real source) "
        "-- likely a minified/binary/cache-data blob grep's own -I "
        "check didn't catch"
    )


def _receiver_mismatch_note(
    bare_name: str, declaring_type: str, count: int
) -> str:
    """The one-per-run banner printed/attached when ``run()``'s
    receiver-mismatch heuristic flagged at least one grep-only hit —
    see ``_receiver_mismatch`` and the module docstring's paragraph on
    ``CAUSE_LIKELY_EXTERNAL_COLLISION``.
    """
    plural = "" if count == 1 else "s"
    return (
        f"'{bare_name}' is the only repo-defined symbol with this "
        f"bare name, but {count} grep-only hit{plural} below show no "
        f"reference to its declaring type ('{declaring_type}') in "
        "their file — these are likely calls to an unrelated "
        "external-library method sharing the name, not genuine "
        "callers of your target."
    )


def _dekko_only_report(
    dekko_only: tuple[list[dict], Meter], truncated: bool
) -> tuple[list[dict], Meter | None]:
    """Suppress the ``dekko-only`` bucket under a truncated grep sweep.

    See ``run()``'s own docstring and ``_TRUNCATION_NOTE`` for why: a
    truncated sweep can't rule out that grep would have matched a
    dekko-resolved location past its cutoff, so reporting a count here
    would be false confidence, not a finding.

    Returns:
        ``dekko_only`` unchanged when not truncated; ``([], None)``
        when it was.
    """
    if truncated:
        return [], None
    return dekko_only


def _build_json_doc(
    *,
    query_action: str,
    label: str,
    bare_name: str,
    include_tests: bool,
    grep_command: str,
    sweep: GrepSweepResult,
    matches: tuple[list[dict], Meter],
    dekko_only_rows: list[dict],
    dekko_only_meter: Meter | None,
    grep_only: tuple[list[dict], Meter],
    module_level: list[str],
    receiver_mismatch_note: str | None = None,
    receiver_mismatch_declaring_type: str | None = None,
    receiver_mismatch_count: int | None = None,
) -> dict:
    """Assemble ``sanity --json``'s output document.

    ``meta`` mirrors ``query --json``'s existing truncation-disclosure
    contract byte-for-byte (one ``Meter.as_dict()`` per bucket) so a
    consumer already handling ``query``'s ``meta`` shape needs no new
    parsing to detect a capped ``sanity`` bucket (round 23
    claude-code.md §2.1: the row arrays were silently capped at
    ``DEFAULT_REPORT_LIMIT`` with nothing in the JSON disclosing it).
    ``counts`` is kept exactly as-is alongside ``meta`` -- purely
    additive, so any existing consumer parsing ``counts`` keeps
    working unmodified.

    ``receiver_mismatch_note``/``_declaring_type``/``_count`` are
    present only when ``run()``'s receiver-mismatch heuristic actually
    flagged at least one grep-only hit (mirrors ``dekko_only_note``'s
    "only present when relevant" contract) — see
    ``.features/plans/round23/25-sanity-receiver-mismatch-cue.md``.
    """
    matches_rows, matches_meter = matches
    grep_only_rows, grep_only_meter = grep_only
    doc = {
        "action": "sanity",
        "query_action": query_action,
        "target": label,
        "bare_name": bare_name,
        "include_tests": include_tests,
        "grep_command": grep_command,
        "grep_truncated": sweep.truncated,
        "grep_skipped_pathological": sweep.skipped_pathological,
        "matches": matches_rows,
        "dekko_only": dekko_only_rows,
        "grep_only": grep_only_rows,
        "counts": {
            "matches": matches_meter.total,
            "dekko_only": (
                dekko_only_meter.total if dekko_only_meter else None
            ),
            "grep_only": grep_only_meter.total,
        },
        "meta": {
            "matches": matches_meter.as_dict(),
            "dekko_only": (
                dekko_only_meter.as_dict() if dekko_only_meter else None
            ),
            "grep_only": grep_only_meter.as_dict(),
        },
    }
    if sweep.truncated:
        doc["dekko_only_note"] = _TRUNCATION_NOTE
    if sweep.skipped_pathological:
        doc["grep_skipped_pathological_note"] = _pathological_skip_note(
            sweep.skipped_pathological
        )
    if module_level:
        doc["dekko_module_level"] = sorted(module_level)
    if receiver_mismatch_note:
        doc["receiver_mismatch_note"] = receiver_mismatch_note
        doc["receiver_mismatch_declaring_type"] = (
            receiver_mismatch_declaring_type
        )
        doc["receiver_mismatch_count"] = receiver_mismatch_count
    return doc


def _print_bucket_text(title: str, rows: list[dict], meter: Meter) -> None:
    total = meter.total
    print(f"  {title}: {total}")
    for row in rows:
        loc = f"{row['file']}:{row['line']}"
        if "cause" in row:
            print(f"    {loc}  [{row['cause']}]")
            print(f"      {row['snippet']}")
        else:
            print(f"    {loc}")
    if total > len(rows):
        print(f"    ... +{total - len(rows)} more")


def _print_bucket_by_file(title: str, rows: list[dict], meter: Meter) -> None:
    """Roll up a bucket's rows by file, largest cluster first.

    Args:
        title: Bucket label (``"grep-only"``).
        rows: The bucket's rendered rows (post ``--limit``/budget
            fitting — grouping still respects whatever rows survived
            fitting, same as ``_print_bucket_text``).
        meter: The bucket's cost meter, for the total-count header.
    """
    total = meter.total
    print(f"  {title}: {total} (grouped by file)")
    by_file: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        by_file[row["file"]][row.get("cause", "(no cause)")] += 1
    for file, causes in sorted(
        by_file.items(), key=lambda kv: sum(kv[1].values()), reverse=True
    ):
        file_total = sum(causes.values())
        print(f"    {file}: {file_total}")
        for cause, count in causes.most_common():
            marker = "   <-- look here" if cause == CAUSE_UNEXPLAINED else ""
            print(f"      {count:>4}  {cause}{marker}")
    if total > len(rows):
        print(f"    ... +{total - len(rows)} more (outside --limit/budget)")


def _print_text(
    action: str,
    target: str,
    bare_name: str,
    grep_command: str,
    matches: tuple[list[dict], Meter],
    dekko_only: tuple[list[dict], Meter],
    grep_only: tuple[list[dict], Meter],
    module_level: list[str],
    *,
    grep_truncated: bool = False,
    skipped_pathological: int = 0,
    receiver_mismatch_note: str | None = None,
    group_by_file: bool = False,
) -> None:
    print(f"dekko sanity: '{target}' ({action}) vs. grep '{bare_name}'")
    print(f"  grep: {grep_command}")
    if grep_truncated:
        print(f"  note: {_TRUNCATION_NOTE}")
    if skipped_pathological:
        print(f"  note: {_pathological_skip_note(skipped_pathological)}")
    if receiver_mismatch_note:
        print(f"  note: {receiver_mismatch_note}")
    _print_bucket_text("matches", *matches)
    if grep_truncated:
        print("  dekko-only: inconclusive (grep sweep truncated)")
    else:
        _print_bucket_text("dekko-only", *dekko_only)
    if group_by_file:
        _print_bucket_by_file("grep-only", *grep_only)
    else:
        _print_bucket_text("grep-only", *grep_only)
    if module_level:
        print(
            f"  dekko also reports {len(module_level)} module-level call "
            f"site(s) (no line info): {', '.join(sorted(module_level))}"
        )
    if grep_only[1].total == 0 and not grep_truncated:
        print("  clean: no grep-only misses — spot check passed")


# --- --unused mode -------------------------------------------------


def _build_unused_json_doc(
    *,
    sym: Symbol,
    bare_name: str,
    has_dekko_evidence: bool,
    grep_command: str,
    sweep: GrepSweepResult,
    reference_hits: tuple[list[dict], Meter],
    noise_count: int,
    generic_name_caution: bool,
) -> dict:
    """Assemble ``sanity --unused``'s JSON output document.

    Deliberately not the callers/uses ``matches``/``dekko_only``/
    ``grep_only`` three-bucket shape — there is no "dekko side" hit
    set to diff against in ``--unused`` mode, dekko's claim is just
    "zero," so forcing that shape here would invite a consumer to
    misread an empty ``dekko_only`` as meaningful. ``meta``/``counts``
    still follow the ``Meter.as_dict()``-based truncation-disclosure
    convention ``_build_json_doc`` already established.
    """
    rows, meter = reference_hits
    doc = {
        "action": "sanity",
        "query_action": "unused",
        "target": f"{sym.path}:{sym.qualname}:{sym.start_line}",
        "bare_name": bare_name,
        "has_dekko_evidence": has_dekko_evidence,
        "grep_command": grep_command,
        "grep_truncated": sweep.truncated,
        "grep_skipped_pathological": sweep.skipped_pathological,
        "reference_hits": rows,
        "counts": {
            "reference_hits": meter.total,
            "filtered_noise": noise_count,
        },
        "meta": {"reference_hits": meter.as_dict()},
        "generic_name_caution": generic_name_caution,
    }
    if sweep.truncated:
        doc["reference_hits_note"] = _TRUNCATION_NOTE
    if sweep.skipped_pathological:
        doc["grep_skipped_pathological_note"] = _pathological_skip_note(
            sweep.skipped_pathological
        )
    return doc


def _print_unused_text(
    label: str,
    bare_name: str,
    has_evidence: bool,
    grep_command: str,
    reference_hits: tuple[list[dict], Meter],
    noise_count: int,
    generic_name_caution: bool,
    *,
    grep_truncated: bool = False,
    skipped_pathological: int = 0,
) -> None:
    """Render ``sanity --unused``'s text report.

    ``noise_count`` isn't rendered directly (the report focuses on the
    signal — reference hits — the same choice ``grep_skipped_
    pathological`` already made over listing every dropped line), but
    is accepted for signature symmetry with ``_build_unused_json_doc``.
    """
    del noise_count
    rows, meter = reference_hits
    print(f"dekko sanity --unused '{bare_name}' ({label})")
    print(f"  grep: {grep_command}")
    if grep_truncated:
        print(f"  note: {_TRUNCATION_NOTE}")
    if skipped_pathological:
        print(f"  note: {_pathological_skip_note(skipped_pathological)}")
    evidence = (
        "none -- this is why it was flagged" if not has_evidence else "present"
    )
    print(f"  dekko evidence (calls_in/referenced_in): {evidence}")
    print(
        "  reference hits found outside definition/import/comment: "
        f"{meter.total}"
    )
    for row in rows:
        loc = f"{row['file']}:{row['line']}"
        print(f"    {loc}  [{row['shape']}]")
        print(f"      {row['snippet']}")
    if meter.total > len(rows):
        print(f"    ... +{meter.total - len(rows)} more")
    if generic_name_caution:
        print(f"  note: {CAUSE_GENERIC_NAME}")

    if meter.total == 0:
        print(
            "  clean: no reference evidence found outside definition "
            "-- flagged-unused looks correct"
        )
        return

    call_count = sum(1 for r in rows if r["shape"] == SHAPE_CALL)
    non_call_count = len(rows) - call_count
    parts = []
    if call_count:
        plural = "" if call_count == 1 else "s"
        parts.append(
            f"{call_count} call-shaped reference{plural} found "
            "(possible resolver miss)"
        )
    if non_call_count:
        plural = "" if non_call_count == 1 else "s"
        parts.append(f"{non_call_count} non-call reference{plural} found")
    summary = " and ".join(parts)
    print(f"  flagged unused, but {summary} -- verify before deleting")


def _run_unused_check(
    index: MapIndex,
    target: str,
    root: Path,
    limit: int,
    budget: int | None,
    as_json: bool,
) -> int:
    """``dekko sanity --unused <target>`` — see module docstring.

    Starts from dekko's own claim of zero ``calls_in``/``referenced_
    in`` evidence for the resolved symbol and asks whether a targeted
    grep sweep turns up anything outside the symbol's own definition,
    an import/require statement, or a comment — any such hit is a
    reference ``dekko unused`` didn't already explain away.

    Resolves against the full, unfiltered ``index`` (not ``index.
    without_tests()``) — matching ``dekko unused``'s own default,
    where a symbol called only from a test file is still "used."
    ``--include-tests`` is therefore a documented no-op in this mode;
    the caller never threads it through here.

    Returns:
        ``EXIT_OK`` on a completed check (regardless of findings —
        advisory, never a hard failure), ``EXIT_NOT_FOUND``/
        ``EXIT_AMBIGUOUS`` when ``target`` doesn't resolve to a unique
        symbol, ``EXIT_GREP_FAILED`` when the grep sweep itself
        couldn't run.
    """
    sym, candidates = query.resolve_target(index, target)
    if sym is None:
        return query.report_unresolved(target, candidates, index)
    bare_name = sym.name

    own_def_locs = frozenset(
        (s.path, s.start_line) for s in index.symbols_by_name.get(sym.name, [])
    )
    has_evidence = bool(index.calls_in.get(sym.id)) or bool(
        index.referenced_in.get(sym.id)
    )

    sweep = _run_grep(root, bare_name)
    if sweep.error is not None:
        print(f"dekko: {sweep.error}", file=sys.stderr)
        return EXIT_GREP_FAILED

    hits = [h for h in sweep.hits if (h.path, h.line) not in own_def_locs]
    reference_rows: list[dict] = []
    noise_count = 0
    for h in hits:
        bucket, detail = classify_unused_reference(
            h.snippet, bare_name, path=h.path
        )
        if bucket == "noise":
            noise_count += 1
            continue
        if _looks_like_multiline_import_member(root, h, bare_name):
            noise_count += 1
            continue
        row = _grep_row(h)
        row["shape"] = detail
        reference_rows.append(row)

    kept, meter = _fit_rows(reference_rows, budget, limit)
    generic_caution = _is_generic_name(bare_name)

    if as_json:
        doc = _build_unused_json_doc(
            sym=sym,
            bare_name=bare_name,
            has_dekko_evidence=has_evidence,
            grep_command=sweep.command_text,
            sweep=sweep,
            reference_hits=(kept, meter),
            noise_count=noise_count,
            generic_name_caution=generic_caution,
        )
        print(json.dumps(doc, indent=2))
        return EXIT_OK

    _print_unused_text(
        f"{sym.path}:{sym.start_line}",
        bare_name,
        has_evidence,
        sweep.command_text,
        (kept, meter),
        noise_count,
        generic_caution,
        grep_truncated=sweep.truncated,
        skipped_pathological=sweep.skipped_pathological,
    )
    return EXIT_OK


def _resolve_declaring_type(query_index: MapIndex, sym: Symbol) -> str | None:
    """The gated declaring-type simple name for ``run()``'s
    receiver-mismatch heuristic, or ``None`` when the gate doesn't
    hold.

    All four conditions from
    ``.features/plans/round23/25-sanity-receiver-mismatch-cue.md``'s
    "Gating" section must hold:

    1. ``sym.kind == "method"`` -- the heuristic is about a *receiver*
       relationship, which only makes sense for a method on some type.
       A free function/closure-local bare-name collision has no
       declaring type to check imports against (layer 1's denylist
       domain, not this heuristic's -- see the design doc).
    2. ``sym.qualname`` has a container segment (a bare ``"."``-free
       qualname, e.g. a free function, fails this and returns
       ``None``).
    3. The container qualname resolves to **exactly one** symbol in
       ``query_index.symbols_by_qualname`` whose ``kind`` is in
       ``TYPE_KINDS`` -- zero or multiple matches (an unusual qualname
       collision) means "don't guess," matching the module's own
       "false confidence from the classifier itself" caution.
    4. Exactly one repo-defined symbol shares ``sym.name`` --
       reuses the same list ``run()``'s own ``own_def_locs`` is built
       from (no new query).

    Returns:
        The declaring type's own simple name (the last segment of the
        container symbol's qualname, so a nested class like
        ``Outer.Inner`` compares against ``Inner`` -- the name that
        would actually appear in an import statement or receiver
        expression) when every condition holds; ``None`` otherwise.
    """
    if sym.kind != "method":
        return None
    container_qualname, sep, _ = sym.qualname.rpartition(".")
    if not sep:
        return None
    container_syms = [
        s
        for s in query_index.symbols_by_qualname.get(container_qualname, [])
        if s.kind in TYPE_KINDS
    ]
    if len(container_syms) != 1:
        return None
    if len(query_index.symbols_by_name.get(sym.name, [])) != 1:
        return None
    return container_syms[0].qualname.rsplit(".", 1)[-1]


def run(
    index: MapIndex,
    target: str,
    root: Path,
    usages: bool = False,
    unused: bool = False,
    include_tests: bool = False,
    limit: int = DEFAULT_REPORT_LIMIT,
    budget: int | None = None,
    as_json: bool = False,
    group_by_file: bool = False,
) -> int:
    """Cross-check a ``callers``/``uses``/``unused`` result against a
    grep sweep.

    Always exits ``0`` on a clean run — a nonempty ``grep_only``
    bucket (or, in ``--unused`` mode, a nonempty reference-hit set) is
    a spot-check finding to relay, not itself an error condition
    (mirrors ``doctor``'s "reports, doesn't judge" contract). Only a
    genuinely broken invocation (target doesn't resolve, or the grep
    sweep itself couldn't run) exits nonzero.

    When ``unused`` is set, dispatches to ``_run_unused_check`` before
    any of the callers/uses logic below runs — a separate mode with
    its own comparison shape (dekko's claim is "zero evidence," not a
    hit set to diff against grep's), not a branch spliced into the
    callers/uses body. ``usages``/``include_tests`` are not consulted
    in this mode: ``--unused`` is mutually exclusive with ``--usages``
    (enforced by the caller), and ``--include-tests`` is a documented
    no-op here (see ``_run_unused_check``'s own docstring).

    When the grep sweep itself hits its ``_MAX_GREP_LINES`` safety cap
    (round 21 Track B1 — a generic bare name on a large repo), the
    ``dekko-only`` bucket is reported as inconclusive (empty rows, a
    ``None``/absent count in JSON's ``counts.dekko_only``) rather than
    a false-confidence number — a location dekko resolved that the
    incomplete grep hit set doesn't cover may simply be past the
    cutoff, not a genuine resolver disagreement. ``matches``/
    ``grep_only`` are still reported (grep's own truncated view of
    them), alongside a ``grep_truncated``/``dekko_only_note`` (JSON)
    or a printed ``note:`` line (text) disclosing the cap was hit.
    Any raw grep line long enough to be a binary/data blob rather than
    real source (round 21 Track B2) is dropped from the sweep entirely
    and counted in ``grep_skipped_pathological`` instead of being
    reported as a hit or bloating the report.

    Args:
        index: Loaded map index (unfiltered — this function applies
            its own test-inclusion default; see the module docstring's
            first design note).
        target: Symbol target (callers mode) or bare external base
            identifier (``--usages`` mode).
        root: Repository root on disk, for the grep sweep.
        usages: Check ``uses <target>`` instead of ``callers
            <target>``.
        unused: Check whether a symbol ``dekko unused`` flagged dead
            has grep-visible reference evidence instead of running the
            callers/uses cross-check. Mutually exclusive with
            ``usages`` (enforced by the caller, not this function).
        include_tests: Include test files in the dekko-side query
            (default: excluded, matching the MCP ``get_callers``/
            ``find_usages`` tools' own default).
        limit: Max rendered rows per bucket (matches/dekko-only/
            grep-only) — never affects the internal comparison data
            itself (see the module docstring's second design note).
        budget: Approximate token budget, applied independently to
            each of the three report buckets (see ``_fit_rows``), or
            ``None`` for unbounded.
        as_json: Emit structured JSON instead of a text report.
        group_by_file: Roll up the grep-only bucket's text-mode
            rendering by file (count and cause breakdown per file)
            instead of a flat row list. Text mode only, single-target
            only (no effect on ``--json`` or ``--unused``, which
            already carries every row's ``file``/``cause`` for an
            external consumer to group).

    Returns:
        ``0`` on a completed comparison (regardless of findings),
        ``EXIT_NOT_FOUND``/``EXIT_AMBIGUOUS`` when ``target`` doesn't
        resolve to a unique symbol (callers/unused mode) or matches no
        external reference (``uses`` mode), ``EXIT_GREP_FAILED`` when
        the grep sweep itself couldn't run.
    """
    if unused:
        return _run_unused_check(index, target, root, limit, budget, as_json)

    query_index = index if include_tests else index.without_tests()
    own_def_locs: frozenset[tuple[str, int]] = frozenset()
    # The receiver-mismatch heuristic's declaring-type name -- set only
    # when every gating condition below holds (callers mode, a method
    # target, exactly one repo-defined candidate for its bare name, and
    # an unambiguous declaring-type lookup). ``None`` leaves
    # ``_classify_grep_hits`` in its existing, ungated behavior. See
    # ``.features/plans/round23/25-sanity-receiver-mismatch-cue.md``.
    declaring_type: str | None = None

    if usages:
        bare_name = target
        query_action = "uses"
        label = target
        try:
            dekko_hits, module_level = _dekko_hits_uses(query_index, target)
        except _QueryFailedError as exc:
            return exc.code
    else:
        sym, candidates = query.resolve_target(query_index, target)
        if sym is None:
            return query.report_unresolved(target, candidates, query_index)
        bare_name = sym.name
        query_action = "callers"
        label = sym.id
        # Every symbol sharing the target's bare name, not just the
        # target itself -- a same-bare-named symbol's own definition
        # line (e.g. an unrelated MetalRenderer.new_internal, when the
        # query target is Editor.new_internal) is just as much "not a
        # call site" as the target's own, and MapIndex.symbols_by_name
        # already indexes every symbol by bare name repo-wide (round
        # 22 zed.md §3.3).
        own_def_locs = frozenset(
            (s.path, s.start_line)
            for s in query_index.symbols_by_name.get(sym.name, [])
        )
        declaring_type = _resolve_declaring_type(query_index, sym)
        sym_target = f"{sym.path}:{sym.qualname}:{sym.start_line}"
        try:
            dekko_hits, module_level = _dekko_hits_callers(
                query_index, sym_target
            )
        except _QueryFailedError as exc:
            return exc.code

    sweep = _run_grep(root, bare_name)
    if sweep.error is not None:
        print(f"dekko: {sweep.error}", file=sys.stderr)
        return EXIT_GREP_FAILED
    grep_command = sweep.command_text
    grep_hits = sweep.hits
    if own_def_locs:
        # The target's own definition line -- and every other
        # same-bare-named symbol's own definition line -- always
        # contains the bare name and would otherwise show up as a
        # spurious "grep-only" miss on every single callers check —
        # dekko's callers query correctly never treats a symbol's own
        # definition as a call site, so grep matching one isn't a miss
        # to explain, it's out of scope for a caller/uses cross-check
        # entirely.
        grep_hits = [
            h for h in grep_hits if (h.path, h.line) not in own_def_locs
        ]

    dekko_set = set(dekko_hits)
    grep_by_loc = {(h.path, h.line): h for h in grep_hits}

    matched_locs = sorted(dekko_set & set(grep_by_loc))
    dekko_only_locs = sorted(dekko_set - set(grep_by_loc))
    grep_only_hits = [
        h for h in grep_hits if (h.path, h.line) not in dekko_set
    ]
    tests_excluded = not include_tests
    causes = _classify_grep_hits(
        grep_hits,
        bare_name,
        root,
        own_def_locs=own_def_locs,
        tests_excluded=tests_excluded,
        declaring_type=declaring_type,
    )
    grep_only_rows = [
        _grep_row(h, causes[(h.path, h.line)]) for h in grep_only_hits
    ]
    match_rows = [_grep_row(grep_by_loc[loc]) for loc in matched_locs]
    dekko_only_rows = [_hit_row(*loc) for loc in dekko_only_locs]

    matches = _fit_rows(match_rows, budget, limit)
    dekko_only = _fit_rows(dekko_only_rows, budget, limit)
    grep_only = _fit_rows(grep_only_rows, budget, limit)

    dekko_only_rows_out, dekko_only_meter = _dekko_only_report(
        dekko_only, sweep.truncated
    )

    receiver_mismatch_note = None
    receiver_mismatch_count = sum(
        1
        for h in grep_only_hits
        if causes[(h.path, h.line)] == CAUSE_LIKELY_EXTERNAL_COLLISION
    )
    if declaring_type is not None and receiver_mismatch_count:
        receiver_mismatch_note = _receiver_mismatch_note(
            bare_name, declaring_type, receiver_mismatch_count
        )

    if as_json:
        doc = _build_json_doc(
            query_action=query_action,
            label=label,
            bare_name=bare_name,
            include_tests=include_tests,
            grep_command=grep_command,
            sweep=sweep,
            matches=matches,
            dekko_only_rows=dekko_only_rows_out,
            dekko_only_meter=dekko_only_meter,
            grep_only=grep_only,
            module_level=module_level,
            receiver_mismatch_note=receiver_mismatch_note,
            receiver_mismatch_declaring_type=declaring_type,
            receiver_mismatch_count=(
                receiver_mismatch_count if receiver_mismatch_note else None
            ),
        )
        print(json.dumps(doc, indent=2))
        return EXIT_OK

    _print_text(
        query_action,
        label,
        bare_name,
        grep_command,
        matches,
        dekko_only,
        grep_only,
        module_level,
        grep_truncated=sweep.truncated,
        skipped_pathological=sweep.skipped_pathological,
        receiver_mismatch_note=receiver_mismatch_note,
        group_by_file=group_by_file,
    )
    return EXIT_OK


# --- --all sweep --------------------------------------------------
#
# Round 23's own claude-buddy evaluation shows the cost of a
# human-selected population of ``sanity <target>`` invocations: a real
# regression in ``classify_miss``'s own classification logic sat in
# ``develop`` for a full round undetected, caught only because the
# tester happened to pick one symbol (out of dozens with nonzero
# fan-in) that exercised the buggy branch. ``run_all`` removes the
# selection bias: run the same cross-check over every in-repo symbol
# with nonzero fan-in instead of one hand-picked target. See
# ``.features/plans/round23/24-sanity-all-sweep.md`` for the full
# design this section implements.


def _group_fan_in_symbols(query_index: MapIndex) -> dict[str, list[Symbol]]:
    """Bare name -> every symbol sharing it that has nonzero
    ``calls_in`` fan-in of its own.

    Built from ``sorted(query_index.symbols_by_name)`` so the returned
    dict already iterates in alphabetical-by-bare-name order — the
    stable, deterministic sweep order ``run_all``'s ``--max-names``
    truncation and reporting both rely on (a fixed subset under the
    cap, not a run-order-dependent one).
    """
    groups: dict[str, list[Symbol]] = {}
    for name in sorted(query_index.symbols_by_name):
        fan_in = [
            s
            for s in query_index.symbols_by_name[name]
            if query_index.calls_in.get(s.id)
        ]
        if fan_in:
            groups[name] = fan_in
    return groups


def _sweep_bare_name(
    root: Path,
    bare_name: str,
    *,
    own_def_locs: frozenset[tuple[str, int]],
    tests_excluded: bool,
) -> tuple[GrepSweepResult, dict[tuple[str, int], str]]:
    """One grep + classify pass for ``bare_name``, shared across every
    symbol in its fan-in group — the sweep's whole cost-saving
    mechanism (see module docstring's ``--all`` paragraph): grep and
    classification cost drops from O(symbols with fan-in) to O(unique
    bare names among them).

    Returns:
        ``(sweep, causes)``. ``causes`` is empty when ``sweep.error``
        is set — a caller checks ``sweep.error`` before trusting an
        empty ``causes`` as "no grep-only hits" rather than "the sweep
        itself failed."
    """
    sweep = _run_grep(root, bare_name)
    if sweep.error is not None:
        return sweep, {}
    causes = _classify_grep_hits(
        sweep.hits,
        bare_name,
        root,
        own_def_locs=own_def_locs,
        tests_excluded=tests_excluded,
    )
    return sweep, causes


@dataclass(frozen=True)
class _SymbolSweepResult:
    """One fan-in symbol's own diff against its bare name's shared,
    already-classified grep sweep.

    Attributes:
        target: ``path:qualname`` display label — re-runnable directly
            as ``dekko sanity <target>`` for the full single-target
            report.
        bare_name: The symbol's bare name.
        matches: Count of dekko-hit locations grep's sweep also found.
        dekko_only: Count of dekko-hit locations grep's sweep missed.
        grep_only_causes: One ``CAUSE_*`` string per grep-only hit
            this symbol's own dekko-side query result didn't already
            explain.
    """

    target: str
    bare_name: str
    matches: int
    dekko_only: int
    grep_only_causes: list[str]


def _diff_symbol(
    query_index: MapIndex,
    sym: Symbol,
    causes: dict[tuple[str, int], str],
) -> "_SymbolSweepResult | None":
    """Diff one symbol's own dekko-side callers hits against its bare
    name's shared classified grep hit set (``causes``).

    Mirrors ``run()``'s own matches/dekko-only/grep-only split, just
    keyed off ``causes`` (already computed once per bare name) instead
    of re-running ``_classify_grep_hits`` per symbol.

    Returns:
        ``None`` if the symbol's own internal query unexpectedly fails
        to resolve (shouldn't happen for an already-enumerated fan-in
        symbol — ``callers`` can't fail the way ``uses`` can, see
        ``_QueryFailedError``'s own docstring — but this keeps one
        anomalous symbol from crashing the whole sweep).
    """
    sym_target = f"{sym.path}:{sym.qualname}:{sym.start_line}"
    try:
        dekko_hits, _module_level = _dekko_hits_callers(
            query_index, sym_target
        )
    except _QueryFailedError:
        return None
    dekko_set = set(dekko_hits)
    grep_locs = set(causes)
    matches = len(dekko_set & grep_locs)
    dekko_only = len(dekko_set - grep_locs)
    grep_only_causes = [causes[loc] for loc in grep_locs - dekko_set]
    return _SymbolSweepResult(
        target=f"{sym.path}:{sym.qualname}",
        bare_name=sym.name,
        matches=matches,
        dekko_only=dekko_only,
        grep_only_causes=grep_only_causes,
    )


def _names_truncated_note(swept: int) -> str:
    return (
        f"--max-names cap reached: swept only the first {swept:,} "
        "unique bare names (alphabetical order); pass a higher "
        "--max-names to cover the rest"
    )


def _unexplained_count(causes: list[str]) -> int:
    return sum(1 for c in causes if c == CAUSE_UNEXPLAINED)


def _build_all_json_doc(
    *,
    symbols_swept: int,
    unique_names_swept: int,
    names_truncated: bool,
    jobs: int,
    aggregate_causes: Counter,
    flagged: list[_SymbolSweepResult],
    results: list[_SymbolSweepResult],
    limit: int,
    budget: int | None,
) -> dict:
    """Assemble ``sanity --all --json``'s output document — see the
    design doc's "Output shape" section for the schema this mirrors.

    ``symbols`` carries the full per-symbol breakdown (not just the
    flagged subset) for programmatic/CI use, same for ``flagged`` —
    both independently capped via ``_fit_rows`` (this module's usual
    row-count/token-budget guard) since either can grow large on a
    real repo; a ``*_meta`` sibling discloses truncation on each,
    matching the ``meta``-per-bucket convention ``_build_json_doc``
    already established for the single-target report.
    """
    flagged_rows = [
        {
            "target": r.target,
            "grep_only": len(r.grep_only_causes),
            "unexplained": _unexplained_count(r.grep_only_causes),
        }
        for r in flagged
    ]
    symbol_rows = [
        {
            "target": r.target,
            "bare_name": r.bare_name,
            "counts": {
                "matches": r.matches,
                "dekko_only": r.dekko_only,
                "grep_only": len(r.grep_only_causes),
            },
            "causes": dict(Counter(r.grep_only_causes)),
        }
        for r in results
    ]
    flagged_kept, flagged_meter = _fit_rows(flagged_rows, budget, limit)
    symbols_kept, symbols_meter = _fit_rows(symbol_rows, budget, limit)
    doc = {
        "action": "sanity_all",
        "symbols_swept": symbols_swept,
        "unique_names_swept": unique_names_swept,
        "names_truncated": names_truncated,
        "jobs": jobs,
        "aggregate_causes": dict(aggregate_causes),
        "flagged": flagged_kept,
        "symbols": symbols_kept,
        "meta": {
            "flagged": flagged_meter.as_dict(),
            "symbols": symbols_meter.as_dict(),
        },
    }
    if names_truncated:
        doc["names_truncated_note"] = _names_truncated_note(unique_names_swept)
    return doc


def _print_all_text(
    *,
    symbols_swept: int,
    unique_names_swept: int,
    names_truncated: bool,
    jobs: int,
    aggregate_causes: Counter,
    flagged: list[_SymbolSweepResult],
    limit: int,
) -> None:
    """Render ``sanity --all``'s text triage summary — see the design
    doc's "Output shape" section for the format this mirrors. Doesn't
    dump a per-symbol report the way single-target ``sanity`` does:
    the sweep's job is pointing at what to look at, not reproducing
    every row (re-run ``dekko sanity <target>`` on a flagged symbol for
    that)."""
    print(
        f"dekko sanity --all: swept {symbols_swept} symbols "
        f"({unique_names_swept} unique names), jobs={jobs}"
    )
    if names_truncated:
        print(f"  note: {_names_truncated_note(unique_names_swept)}")
    print()

    total_grep_only = sum(aggregate_causes.values())
    print(f"causes across {total_grep_only:,} grep-only hits:")
    if total_grep_only == 0:
        print("  (none)")
    for cause, count in aggregate_causes.most_common():
        marker = "   <-- look here" if cause == CAUSE_UNEXPLAINED else ""
        print(f"  {cause:<58} {count}{marker}")

    print()
    if not flagged:
        print(
            "clean: no unexplained grep-only misses across the sweep — "
            "spot check passed"
        )
        return

    print("flagged (nonzero unexplained misses), sorted by count:")
    shown = flagged[:limit]
    for r in shown:
        unexplained = _unexplained_count(r.grep_only_causes)
        print(
            f"  {r.target:<40} grep_only={len(r.grep_only_causes)} "
            f"(unexplained={unexplained})"
        )
    remaining = len(flagged) - len(shown)
    if remaining > 0:
        print(f"  ... ({remaining} more, --limit {len(flagged)} to see all)")
    print()
    print(
        "re-run `dekko sanity <target>` on any flagged symbol above for "
        "the full match/dekko-only/grep-only report."
    )


def _run_all_sweeps(
    names: list[str],
    root: Path,
    query_index: MapIndex,
    *,
    tests_excluded: bool,
    workers: int,
) -> dict[str, tuple[GrepSweepResult, dict[tuple[str, int], str]]]:
    """Run one grep+classify sweep per unique bare name in ``names``,
    sequentially or via a thread pool sized by ``workers`` — see
    ``run_all``'s own docstring for why threads, not processes.
    """

    def _sweep_one(
        name: str,
    ) -> tuple[str, GrepSweepResult, dict[tuple[str, int], str]]:
        own_def_locs = frozenset(
            (s.path, s.start_line)
            for s in query_index.symbols_by_name.get(name, [])
        )
        sweep, causes = _sweep_bare_name(
            root,
            name,
            own_def_locs=own_def_locs,
            tests_excluded=tests_excluded,
        )
        return name, sweep, causes

    sweeps: dict[str, tuple[GrepSweepResult, dict[tuple[str, int], str]]] = {}
    if workers <= 1 or len(names) <= 1:
        for name in names:
            _, sweep, causes = _sweep_one(name)
            sweeps[name] = (sweep, causes)
        return sweeps
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for name, sweep, causes in pool.map(_sweep_one, names):
            sweeps[name] = (sweep, causes)
    return sweeps


def _first_sweep_error(
    names: list[str],
    sweeps: dict[str, tuple[GrepSweepResult, dict[tuple[str, int], str]]],
) -> str | None:
    """The first per-name grep error encountered, in sweep order, or
    ``None`` if every name's sweep ran cleanly."""
    for name in names:
        sweep, _causes = sweeps[name]
        if sweep.error is not None:
            return sweep.error
    return None


def _diff_all_symbols(
    query_index: MapIndex,
    groups: dict[str, list[Symbol]],
    names: list[str],
    sweeps: dict[str, tuple[GrepSweepResult, dict[tuple[str, int], str]]],
) -> list[_SymbolSweepResult]:
    """Diff every fan-in symbol across ``names`` against its bare
    name's already-classified, shared grep sweep (see ``_diff_symbol``)."""
    results: list[_SymbolSweepResult] = []
    for name in names:
        _sweep, causes = sweeps[name]
        for sym in groups[name]:
            diffed = _diff_symbol(query_index, sym, causes)
            if diffed is not None:
                results.append(diffed)
    return results


def run_all(
    index: MapIndex,
    root: Path,
    *,
    include_tests: bool = False,
    jobs: int = _ALL_JOBS_DEFAULT,
    max_names: int = _MAX_SWEEP_NAMES,
    fail_on_unexplained: bool = False,
    limit: int = DEFAULT_REPORT_LIMIT,
    budget: int | None = None,
    as_json: bool = False,
) -> int:
    """``dekko sanity --all`` — sweep the same callers/grep cross-check
    ``run()`` runs for one target over every in-repo symbol with
    nonzero ``calls_in`` fan-in, deduping the grep subprocess by bare
    name. See the module docstring's ``--all`` paragraph and
    ``.features/plans/round23/24-sanity-all-sweep.md`` for the full
    design and rationale.

    Callers mode only (see the design doc's Scope section) — there is
    no ``usages``/``unused`` equivalent here; the caller (``cli.
    run_sanity``) is responsible for rejecting ``--all`` combined with
    ``--usages``/``--unused`` before this function is ever called.

    Grep subprocess sweeps run in a thread pool sized by
    ``repo_ops.resolve_workers(jobs)`` (``0`` = all cores) — threads,
    not processes, since each unit of work is "wait on one grep
    subprocess" (I/O-bound), avoiding the cost of pickling the
    already-loaded ``MapIndex`` across a process boundary the way
    ``dekko map --jobs`` needs to for its own (CPU-bound) parallel
    extraction.

    Args:
        index: Loaded map index (unfiltered — this function applies
            its own test-inclusion default, same as ``run()``).
        root: Repository root on disk, for each name's grep sweep.
        include_tests: Include test files in the dekko-side query and
            in fan-in grouping (default: excluded, matching ``run()``
            and the MCP ``get_callers`` default).
        jobs: Thread-pool size for the per-name grep sweeps (``0`` =
            all cores, ``1`` = sequential).
        max_names: Safety cap on unique bare names swept — names past
            this cap (alphabetical order) are not swept, and the
            truncation is disclosed rather than silently sweeping a
            partial, unlabeled subset.
        fail_on_unexplained: Exit ``EXIT_UNEXPLAINED_FOUND`` instead of
            ``EXIT_OK`` when the aggregate unexplained-cause count is
            nonzero — opt-in so a first `--all` run in CI can't
            surprise-break a pipeline.
        limit: Max rendered rows for the ``flagged`` list (text) and
            cap on the ``flagged``/``symbols`` arrays (JSON) — mirrors
            ``run()``'s own per-bucket ``--limit``.
        budget: Approximate token budget applied to the JSON
            ``flagged``/``symbols`` arrays, or ``None`` for unbounded.
        as_json: Emit structured JSON instead of the text triage
            summary.

    Returns:
        ``EXIT_OK`` on a completed sweep (regardless of findings,
        unless ``fail_on_unexplained``), ``EXIT_UNEXPLAINED_FOUND``
        when ``fail_on_unexplained`` is set and the aggregate
        unexplained count is nonzero, ``EXIT_GREP_FAILED`` when any
        name's grep sweep itself couldn't run.
    """
    query_index = index if include_tests else index.without_tests()
    groups = _group_fan_in_symbols(query_index)
    all_names = sorted(groups)
    names_truncated = len(all_names) > max_names
    names = all_names[:max_names] if names_truncated else all_names

    workers = repo_ops.resolve_workers(jobs)
    sweeps = _run_all_sweeps(
        names,
        root,
        query_index,
        tests_excluded=not include_tests,
        workers=workers,
    )
    sweep_error = _first_sweep_error(names, sweeps)
    if sweep_error is not None:
        print(f"dekko: {sweep_error}", file=sys.stderr)
        return EXIT_GREP_FAILED

    results = _diff_all_symbols(query_index, groups, names, sweeps)

    aggregate_causes: Counter = Counter()
    for r in results:
        aggregate_causes.update(r.grep_only_causes)

    flagged = [r for r in results if CAUSE_UNEXPLAINED in r.grep_only_causes]
    flagged.sort(
        key=lambda r: _unexplained_count(r.grep_only_causes), reverse=True
    )

    if as_json:
        doc = _build_all_json_doc(
            symbols_swept=len(results),
            unique_names_swept=len(names),
            names_truncated=names_truncated,
            jobs=workers,
            aggregate_causes=aggregate_causes,
            flagged=flagged,
            results=results,
            limit=limit,
            budget=budget,
        )
        print(json.dumps(doc, indent=2))
    else:
        _print_all_text(
            symbols_swept=len(results),
            unique_names_swept=len(names),
            names_truncated=names_truncated,
            jobs=workers,
            aggregate_causes=aggregate_causes,
            flagged=flagged,
            limit=limit,
        )

    if fail_on_unexplained and aggregate_causes[CAUSE_UNEXPLAINED] > 0:
        return EXIT_UNEXPLAINED_FOUND
    return EXIT_OK
