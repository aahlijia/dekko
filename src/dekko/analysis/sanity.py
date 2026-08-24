"""``dekko sanity <target>``: cross-check a callers/uses result against
a targeted grep sweep.

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
from contextlib import redirect_stdout
from dataclasses import dataclass
from pathlib import Path

from dekko.analysis import query
from dekko.classify import is_test_path
from dekko.core import languages
from dekko.core.walker import DEFAULT_EXCLUDE_DIRS
from dekko.render.mapfile import MapIndex
from dekko.storage.cache import CACHE_DIR
from dekko.textutil import fit_to_budget

EXIT_OK = 0
# A genuinely broken invocation (grep unavailable/timed out/errored) —
# distinct from a "clean run that happens to have grep-only misses",
# which still exits 0 (advisory, not an error; see ``run()``).
EXIT_GREP_FAILED = 2
EXIT_NOT_FOUND = query.EXIT_NOT_FOUND
EXIT_AMBIGUOUS = query.EXIT_AMBIGUOUS


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
    "comment mention near the symbol's own definition — not a call site"
)
CAUSE_IMPORT_STATEMENT = (
    "import/require statement naming the symbol — not a call site"
)
CAUSE_UNEXPLAINED = "unexplained miss — inspect manually"

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
) -> str:
    """Name the likely cause of one grep-only hit.

    A pure function over one grep-matched line plus its context — no
    repo/grep I/O, so it's directly testable in isolation. Checked in
    the order ``dekko-verify/SKILL.md`` lists its blind spots: a
    qualified-call syntax match is checked first (it's visible in the
    line itself and the most specific signal available), then whether
    the line is a bare import/require statement naming the symbol
    (round 21 Track B3 — the dominant "grep-only" shape on any
    import-heavy codebase, same "visible in the line itself" precedence
    as the qualified-call check), then whether the hit is a
    doc-comment/docstring line sitting near the symbol's own definition
    mentioning its bare name (not a call at all), then whether the file
    is in a language dekko can't parse at all, then whether it's a test
    file excluded by ``sanity``'s own default filtering, then whether
    the target name is short/generic enough that dekko's count should
    be read as directional rather than exact. A line matching none of
    these is reported as "unexplained" rather than forcing a guess that
    doesn't fit — matching the plan's own "false confidence from the
    classifier itself" caution.

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

    Returns:
        One of the ``CAUSE_*`` constants.
    """
    if _looks_qualified_call(snippet, bare_name):
        return CAUSE_QUALIFIED_CALL
    if _looks_like_import_statement(snippet, bare_name):
        return CAUSE_IMPORT_STATEMENT
    if near_own_definition and looks_like_comment:
        return CAUSE_COMMENT_MENTION
    if unsupported_language:
        return CAUSE_UNSUPPORTED_LANGUAGE
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
    """
    doc = _run_query_json(index, "callers", sym_target)
    hits: list[tuple[str, int]] = []
    for entry in doc.get("results", []):
        sites = entry.get("sites") or [entry["line"]]
        hits.extend((entry["path"], ln) for ln in sites)
    return hits, list(doc.get("module_level", []))


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
) -> tuple[list[dict], int]:
    """Cap one report bucket by row count then token budget.

    Mirrors ``query._fit_entries``: each bucket (matches/dekko-only/
    grep-only) gets ``budget`` applied independently, the same way
    every existing ``query`` relation action already applies ``budget``
    to its own single result set rather than splitting one budget
    across unrelated calls.

    Returns:
        ``(kept_rows, total_before_capping)``.
    """
    serialized = [json.dumps(r) for r in rows]
    kept, _meter = fit_to_budget(serialized, budget, limit)
    return rows[: len(kept)], len(rows)


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


def _dekko_only_report(
    dekko_only: tuple[list[dict], int], truncated: bool
) -> tuple[list[dict], int | None]:
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
    matches: tuple[list[dict], int],
    dekko_only_rows: list[dict],
    dekko_only_count: int | None,
    grep_only: tuple[list[dict], int],
    module_level: list[str],
) -> dict:
    """Assemble ``sanity --json``'s output document."""
    doc = {
        "action": "sanity",
        "query_action": query_action,
        "target": label,
        "bare_name": bare_name,
        "include_tests": include_tests,
        "grep_command": grep_command,
        "grep_truncated": sweep.truncated,
        "grep_skipped_pathological": sweep.skipped_pathological,
        "matches": matches[0],
        "dekko_only": dekko_only_rows,
        "grep_only": grep_only[0],
        "counts": {
            "matches": matches[1],
            "dekko_only": dekko_only_count,
            "grep_only": grep_only[1],
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
    return doc


def _print_bucket_text(title: str, rows: list[dict], total: int) -> None:
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


def _print_text(
    action: str,
    target: str,
    bare_name: str,
    grep_command: str,
    matches: tuple[list[dict], int],
    dekko_only: tuple[list[dict], int],
    grep_only: tuple[list[dict], int],
    module_level: list[str],
    *,
    grep_truncated: bool = False,
    skipped_pathological: int = 0,
) -> None:
    print(f"dekko sanity: '{target}' ({action}) vs. grep '{bare_name}'")
    print(f"  grep: {grep_command}")
    if grep_truncated:
        print(f"  note: {_TRUNCATION_NOTE}")
    if skipped_pathological:
        print(f"  note: {_pathological_skip_note(skipped_pathological)}")
    _print_bucket_text("matches", *matches)
    if grep_truncated:
        print("  dekko-only: inconclusive (grep sweep truncated)")
    else:
        _print_bucket_text("dekko-only", *dekko_only)
    _print_bucket_text("grep-only", *grep_only)
    if module_level:
        print(
            f"  dekko also reports {len(module_level)} module-level call "
            f"site(s) (no line info): {', '.join(sorted(module_level))}"
        )
    if grep_only[1] == 0 and not grep_truncated:
        print("  clean: no grep-only misses — spot check passed")


def run(
    index: MapIndex,
    target: str,
    root: Path,
    usages: bool = False,
    include_tests: bool = False,
    limit: int = DEFAULT_REPORT_LIMIT,
    budget: int | None = None,
    as_json: bool = False,
) -> int:
    """Cross-check a ``callers``/``uses`` result against a grep sweep.

    Always exits ``0`` on a clean run — a nonempty ``grep_only``
    bucket is a spot-check finding to relay, not itself an error
    condition (mirrors ``doctor``'s "reports, doesn't judge"
    contract). Only a genuinely broken invocation (target doesn't
    resolve, or the grep sweep itself couldn't run) exits nonzero.

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

    Returns:
        ``0`` on a completed comparison (regardless of findings),
        ``EXIT_NOT_FOUND``/``EXIT_AMBIGUOUS`` when ``target`` doesn't
        resolve to a unique symbol (callers mode) or matches no
        external reference (``uses`` mode), ``EXIT_GREP_FAILED`` when
        the grep sweep itself couldn't run.
    """
    query_index = index if include_tests else index.without_tests()
    own_def_loc: tuple[str, int] | None = None

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
        own_def_loc = (sym.path, sym.start_line)
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
    if own_def_loc is not None:
        # The target's own definition line always contains its bare
        # name and would otherwise show up as a spurious "grep-only"
        # miss on every single callers check — dekko's callers query
        # correctly never treats a symbol's own definition as a call
        # site, so grep matching it isn't a miss to explain, it's out
        # of scope for a caller/uses cross-check entirely.
        grep_hits = [h for h in grep_hits if (h.path, h.line) != own_def_loc]

    dekko_set = set(dekko_hits)
    grep_by_loc = {(h.path, h.line): h for h in grep_hits}

    matched_locs = sorted(dekko_set & set(grep_by_loc))
    dekko_only_locs = sorted(dekko_set - set(grep_by_loc))
    grep_only_hits = [
        h for h in grep_hits if (h.path, h.line) not in dekko_set
    ]
    tests_excluded = not include_tests
    grep_only_rows = [
        _grep_row(
            h,
            classify_miss(
                h.snippet,
                bare_name,
                is_test_file=is_test_path(h.path),
                unsupported_language=not languages.is_supported(h.path),
                tests_excluded=tests_excluded,
                near_own_definition=(
                    own_def_loc is not None
                    and h.path == own_def_loc[0]
                    and abs(h.line - own_def_loc[1])
                    <= _COMMENT_PROXIMITY_LINES
                ),
                looks_like_comment=_looks_like_comment_line(h.snippet, h.path),
            ),
        )
        for h in grep_only_hits
    ]
    match_rows = [_grep_row(grep_by_loc[loc]) for loc in matched_locs]
    dekko_only_rows = [_hit_row(*loc) for loc in dekko_only_locs]

    matches = _fit_rows(match_rows, budget, limit)
    dekko_only = _fit_rows(dekko_only_rows, budget, limit)
    grep_only = _fit_rows(grep_only_rows, budget, limit)

    dekko_only_rows_out, dekko_only_count = _dekko_only_report(
        dekko_only, sweep.truncated
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
            dekko_only_count=dekko_only_count,
            grep_only=grep_only,
            module_level=module_level,
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
    )
    return EXIT_OK
