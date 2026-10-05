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
subprocess by bare name (the shared classification depends only on
facts about the bare name, ``_name_inputs``; what depends on which
symbol sharing it is checked runs per symbol in
``_apply_target_facts``, the same two functions ``run()`` calls, so
the two modes classify identically). The repo is walked once for the
whole sweep, and each name's grep runs over only the files that can
hold it (``_plan_sweep``), with the same rows ``_run_grep`` returns.
It reports a triage
summary (an aggregate cause histogram plus the symbols with an
unexplained miss) rather than a full per-symbol dump. Callers mode
only — see ``run_all()``'s own docstring.

In callers mode, a grep-only hit that calls the name can also be
classified ``CAUSE_LIKELY_EXTERNAL_COLLISION`` when the target is a
method, no other repo-defined symbol shares its bare name, the shared
ladder left the row unexplained or generic (a same-named local or a
test file's own helper keeps its own cause), and neither the hit's
own line nor its file's top-of-file
imports mention the target's declaring type — the cheap, no-type-
inference proxy for "this is almost certainly an unrelated external-
library method sharing the name, not a real caller" (e.g. Java/AssertJ
``.isTrue()`` colliding with a repo-defined ``isTrue`` method). See
``_receiver_mismatch()``.

This is a spot check, not a re-verification — see the module's own
``EXIT_OK``-always-on-a-clean-run contract in ``run()``'s docstring.
The blind-spot causes are heuristic pattern-matches on a grep-only
line's syntax, not a re-derivation of the resolver's actual reasoning,
so every cause is worded as a *likely* explanation, matching
``dekko-verify``'s own cautious framing.

Two design decisions worth calling out:

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
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from dataclasses import dataclass
from pathlib import Path

from dekko import repo_ops
from dekko.analysis import ambiguous, query
from dekko.analysis import unused as unused_mod
from dekko.classify import is_test_path
from dekko.core import languages
from dekko.core.model import TYPE_KINDS, ReadSite, Symbol
from dekko.core.walker import DEFAULT_EXCLUDE_DIRS
from dekko.render.mapfile import MapIndex
from dekko.source import read_lines
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
# refactor needed to reuse it. ``.dekko/`` (the cache/output dir) is added on
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
# This cap used to truncate silently -- cline showed ``matches (24) +
# grep_only (4976) == 5000`` exactly, with every "dekko-only" row past
# the cap a truncation artifact rather than a real resolver
# disagreement. ``_run_grep`` now reports whether the cap was hit
# (``GrepSweepResult.truncated``) so ``run()`` can disclose it and stop
# reporting a false-confidence "dekko-only" count under truncation — see
# ``run()``'s own handling.
_MAX_GREP_LINES = 5000

# A single-line 26 MB cache/data file that grep's own ``-I`` binary-skip
# heuristic didn't catch produced 26 MB of terminal output from one
# command. A real source line is never remotely this long -- a raw grep
# line past this many characters is definitionally a binary/data blob,
# not code worth reporting as a hit at all (see ``_run_grep``'s
# pathological-line guard).
_PATHOLOGICAL_LINE_CHARS = 10_000

# An unconditional length cap on any snippet that does make it into a
# rendered/serialized row -- independent of, and a lower bar than,
# ``_PATHOLOGICAL_LINE_CHARS`` (that guard drops a hit entirely; this
# one just keeps an ordinary-but-long real source line from bloating the
# report). Applied at render/serialize time in ``_grep_row``, never
# during classification -- ``classify_miss`` is always called with the
# hit's full, untruncated snippet.
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
# A build script is skipped by design, so a call written in one is real
# and still never in the map. Reading that line's shape instead gave a
# different wrong answer per shape: a bare call was "unexplained", a
# receiver call a "resolver blind spot", a common name "an unrelated
# external-library method".
CAUSE_BUILD_SCRIPT = (
    "build script: dekko does not index build scripts, so a call here "
    "never becomes an edge"
)
# A file in a language dekko parses that the map still doesn't hold:
# skipped as too large or generated, or excluded. Dekko has no call
# sites there because it never read the file, so "unexplained" was the
# wrong answer for an index fact. zed: 120 of one symbol's 407
# unexplained rows sat in a single 1.3 MB test file.
CAUSE_NOT_MAPPED = (
    "file not in the map (too large, generated, or excluded) — dekko "
    "never parsed it; see dekko map --max-file-size"
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
# A comment line is never a call site wherever it sits, so the old "only
# near the definition or in the file header" gate protected nothing. The
# zone survives in the wording instead, so a row still says how much
# context backs it.
CAUSE_COMMENT_ELSEWHERE = (
    "comment mention — not a call site (a comment line, away from the "
    "symbol's definition)"
)
CAUSE_IMPORT_STATEMENT = (
    "import/require statement naming the symbol — not a call site"
)
CAUSE_TYPE_ANNOTATION = (
    "type position (annotation, generic argument, construction, or "
    "enum payload) — not a call site"
)
# Tier 1: the map already holds this exact
# (path, line) as a value-reference edge to the target. Not a guess.
CAUSE_VALUE_REFERENCE = (
    "passed or stored as a value, not called — dekko has this as a "
    "reference (see: dekko query callers <target>, 'referenced (not "
    "called)')"
)
# Tier 2: a line-shape match in a language whose spec has no
# ``reference_query``. The label says it is a shape match and names
# what dekko can't see, per this module's "admit what you don't know"
# rule.
CAUSE_VALUE_REFERENCE_UNRESOLVED = (
    "looks passed or stored as a value, not called (line-shape match) "
    "— dekko records no value references for this file's language; a "
    "known blind spot, not a call the resolver missed"
)
CAUSE_LOCAL_BINDING_OR_LITERAL = (
    "matches an unrelated local variable/parameter declaration, an "
    "object key/field, or a string literal value — not a reference to "
    "the target"
)
# JS/TS, Java and Kotlin (see ``_JS_SHAPE_GRAMMARS`` and
# ``_looks_like_jvm_string_mention``): once string and template
# text is blanked, the name is gone from the line. The biggest
# unexplained shape on the TS repos measured (claude-code: 2,116 of
# 4,319 rows), mostly log messages naming a class.
CAUSE_STRING_MENTION = (
    "mention inside a string or template text — not a call site"
)
# Tier 1, JS/TS: the map records a property read of this name at this
# exact line (``MapIndex.reads_by_name``), and the line has no bare call
# of it.
CAUSE_PROPERTY_READ = (
    "a property read of a same-named field (`x.name`), not a call — "
    "dekko records it as a read"
)
CAUSE_LIKELY_EXTERNAL_COLLISION = (
    "likely an unrelated external-library method sharing this bare "
    "name — no other repo-defined candidate exists, and neither this "
    "line nor this file's imports mention the target's declaring type"
)
CAUSE_CROSS_FILE_COLLISION = (
    "call-shaped reference to a different, same-named declaration "
    "elsewhere in the repo — not a miss on the target"
)
# Tier 1: the map records this exact (path, line) as a call site or a
# value reference of a *different* symbol with the target's bare name
# (``_attributed_sites``). The shape rule above only sees a hit inside
# a sibling's own file, so a call from any third file to a reused
# helper name (claude-code's ``errorMessage``: 367 rows on one target)
# fell through to "unexplained". The map's attribution is exact to
# the line, and the row names it (``resolved_to``), so the label says
# what dekko decided rather than that it decided right.
CAUSE_RESOLVED_ELSEWHERE = (
    "dekko attributes this line to a different, same-named declaration "
    "(see resolved_to) — a miss only if that attribution is wrong"
)
# Tier 1, per constructor target: the line constructs the target's
# class, and the map put the construction on the class because its
# arguments fit more than one constructor equally. The class is not a
# rival declaration, so ``CAUSE_RESOLVED_ELSEWHERE`` misread it.
CAUSE_CONSTRUCTOR_TIE = (
    "a construction whose arguments fit 2+ constructors of the class — "
    "recorded on the class, not on one overload (see: dekko query "
    "callers <class>)"
)
# Tier 1, per constructor target: the map picked another overload of
# the same class for this construction.
CAUSE_SIBLING_CONSTRUCTOR = (
    "resolved to another constructor of the same class (see resolved_to)"
)
CAUSE_UNEXPLAINED = "unexplained miss — inspect manually"
# Tier 2: a value-position use of a same-named local declared earlier
# in the enclosing function (``const errorMessage = ...`` four lines
# above ``error: errorMessage,``). Measured on the 100 most-flagged
# non-type claude-code targets: 759 of 4,104 unexplained rows (18.5%),
# led by ``errorMessage``, ``count``, ``action``. Applied as a
# post-pass that can only upgrade an "unexplained" row, never override
# a specific cause.
# The cause string carries no line number on purpose: ``--all``
# aggregates by cause, and a per-line variant fragmented cline's sweep
# into 110 one-row buckets. The declaration line rides on the row
# instead (``decl_line`` in JSON, appended in text) via
# ``_shadow_decl_lines``.
CAUSE_SHADOWING_LOCAL = (
    "use of a same-named local declared earlier in an enclosing scope "
    "— not a reference to the target; scope heuristic, not a parse"
)
# Tier 1: the hit's innermost enclosing symbol is an interface, type
# alias or enum. Nothing inside one is a call: member signatures,
# ``readonly count?: number``, generic parameter defaults, and any
# comment or string in there. Not class/struct/trait bodies, which
# hold code. The largest leftover bucket on the TS repos measured
# (570 rows on claude-code, 1,911 on cline).
CAUSE_TYPE_CONTEXT = (
    "inside an interface, type-alias or enum body — a type context, "
    "never a call site"
)
# Tier 1, per target: a call-shaped line inside the target's own
# span. ``resolver._record_edge`` drops ``caller_id == target_id`` on
# purpose, so the missing edge is the resolver's design, not a miss.
CAUSE_SELF_RECURSION = (
    "recursive call inside the symbol's own body — dekko records no "
    "self edges by design"
)
# Tier 1, per target: the hit's file imports the bare name from a
# source the map could not place in the repo (``module_external``) or
# from a repo file other than the target's (``module_edge_names``).
# The resolver bound the name to that import; the line cannot reach
# the target. The import's module or path rides on the row as
# ``bound_to``, like ``resolved_to``.
# Tier 1, per callable target: the name sits, without a call, inside
# the target's own span: the function passing itself
# (``setTimeout(doRefresh, ..)`` inside ``doRefresh``), a same-named
# field (``self.blame`` inside ``fn blame``), its name in a message.
# None of them can be a caller the map missed: even a real self
# reference is never an edge.
CAUSE_SELF_MENTION = (
    "mention inside the symbol's own body, not a call — never a missed "
    "caller (dekko records no self edges by design)"
)
CAUSE_IMPORT_BOUND_ELSEWHERE = (
    "this file imports a different declaration of the name (see "
    "bound_to) — the resolver bound the name to that import, not the "
    "target"
)
# Tier 1, per target: ``heritage_lines`` holds this exact line as an
# ``extends``/``implements`` clause naming the target. A clause the
# map resolved to a same-named sibling reads as resolved elsewhere
# instead (``_attributed_sites`` carries heritage sites too).
CAUSE_HERITAGE = (
    "heritage clause (extends/implements) the map records — not a call "
    "site; see: dekko query subtypes <target>"
)
# The remaining shape causes name a line whose only mention of the
# target is in a place code never calls from. Each refuses a line
# that also calls the name bare, so a real missed call never hides
# behind one.
CAUSE_TRAILING_COMMENT = (
    "comment mention — not a call site (a trailing comment after code "
    "on the line)"
)
CAUSE_JSX_TEXT = "JSX text content — literal text, not a call site"
CAUSE_SIGNATURE = (
    "a method or function signature with this name (abstract, interface "
    "member or overload) — a declaration, not a call site"
)
# Tier 2 sibling of ``CAUSE_PROPERTY_READ``: the ``x.name`` shape with
# no ``(`` after it, on a line the map records no read for.
CAUSE_PROPERTY_ACCESS_SHAPE = (
    "a property access of a same-named member (`x.name`), not a call — "
    "line-shape match; the map records no read at this line"
)
# Java and Kotlin, type targets only: the line names the type and no
# occurrence of the name could run a constructor
# (``_JVM_CONSTRUCTION_TEMPLATES``).
# A Java constructor can't run without ``Name(``, ``Name<..>(`` or
# ``Name::new``, so a missed construction never lands here. spring-boot:
# 17,589 of 18,349 unexplained rows were this shape.
CAUSE_TYPE_MENTION = (
    "names the type without constructing it (declaration, parameter "
    "or return type, generic argument, static member access, cast or "
    "class literal) — not a call site"
)
# Every cause at or below ``_classify_miss_remaining``: the rungs the
# per-target tier-1 facts (self-recursion, import bound elsewhere) sit
# above. A row under one of these is re-decided by them; a row with a
# more specific cause (a comment line that names ``appendFileSync``)
# keeps it.
_REMAINING_CAUSES = frozenset(
    {
        CAUSE_UNSUPPORTED_LANGUAGE,
        CAUSE_VALUE_REFERENCE_UNRESOLVED,
        CAUSE_CROSS_FILE_COLLISION,
        CAUSE_LIKELY_EXTERNAL_COLLISION,
        CAUSE_TEST_FILTER,
        CAUSE_GENERIC_NAME,
        CAUSE_UNEXPLAINED,
    }
)

# Generous top-of-file import/using-block scan window for
# ``_receiver_mismatch``'s cheap textual proxy check -- see that
# function's own docstring.
_TYPE_REFERENCE_WINDOW_LINES = 60

# --- unused-mode reference-shape classification ------------------------
#
# ``classify_unused_reference`` answers a different question than
# ``classify_miss`` above: not "why didn't dekko count this as a call,"
# but "does this grep hit indicate a reference dekko's zero-evidence
# claim doesn't already account for."
SHAPE_CALL = "call"
SHAPE_DECLARATION = "declaration"
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
# three named shapes plus the general "any non-call mention"
# catch-all, without hand-rolling a query per grammar the way
# reference_query fixes necessarily do.
_SPREAD_TEMPLATE = r"\.\.\.\s*{name}\b"
_TYPEOF_TEMPLATE = r"\btypeof\s+{name}\b"
_SUBSCRIPT_TEMPLATE = r"{name}\s*\["
_BARE_CALL_TEMPLATE = r"\b{name}\s*\("

# A C/C++ header-only forward declaration/prototype: an optional
# ``extern``, one or more return-type-shaped tokens (a macro like
# ``TF_CAPI_EXPORT``, the actual return type, any ``*`` pointer stars),
# the name, a parameter list, and a bare trailing ``;`` with nothing
# else on the line -- no body, so it's a declaration, not an
# invocation. Checked before ``_BARE_CALL_TEMPLATE``
# (``TF_CloseDeprecatedSession``'s one grep-only hit on tensorflow, a
# plain ``extern`` prototype in ``c_api.h``, was tagged ``[call]`` --
# syntactically indistinguishable from a real call by the bare-call
# template alone). A genuine call statement on its own line (``foo(a,
# b);``) never has a type-token prefix before the name, which is the
# discriminator this template relies on.
_DECLARATION_TEMPLATE = (
    r"^(?:extern\s+)?(?:[A-Za-z_][A-Za-z0-9_]*[\s*]+)+"
    r"{name}\s*\([^;]*\)\s*;\s*$"
)


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
      ``SHAPE_DECLARATION`` (a C/C++ header-only forward declaration/
      prototype -- return type, name, params, bare trailing ``;``, no
      body) is checked before ``SHAPE_CALL``, since a prototype is
      syntactically indistinguishable from a bare call by the call
      template alone. ``SHAPE_CALL`` (bare
      ``name(`` or qualified ``x.name(``/``x::name(``) is checked
      before spread/typeof/subscript, since ``...name()`` (spreading a
      call's *result*) is a genuine call site first and a spread
      second — the more actionable classification wins. ``SHAPE_OTHER``
      is the catch-all for every
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
    if re.search(_DECLARATION_TEMPLATE.format(name=name), snippet.strip()):
        return "reference", SHAPE_DECLARATION
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
# ``Type.method(`` qualified call. These usually resolve, but one
# routed through a re-export or alias the resolver doesn't follow can
# still drop, which ``dekko-verify/SKILL.md`` names as a cause to check.
# Built per-hit (the bare name varies), not module-level.
_QUALIFIED_CALL_TEMPLATE = r"[A-Za-z_][A-Za-z0-9_]*(?:\.|::){name}\s*\("


def _is_generic_name(name: str, is_known_collision_name: bool = False) -> bool:
    """Whether ``name`` is short/common enough, or has *measurably*
    collided repo-wide in this specific map, to warrant a directional
    caution.

    Two independent, additive signals: the curated word list (and
    length shortcut) catches conventionally-generic names even in a
    small/synthetic repo where they happen not to collide yet;
    ``is_known_collision_name`` (from ``ambiguous.collision_names``)
    catches any name -- regardless of curation -- that has *actually*
    collided 2+ ways somewhere in this repo's own call graph (on cline,
    ``delete``/``resolve``/``close``/``invoke``/``dispose``/``clear``/
    ``error`` all reproduced this exact collision shape while absent
    from the curated list). Additive by
    design: this can only ever add cases to ``CAUSE_GENERIC_NAME``,
    never remove one the curated list already caught, so no existing
    passing test can regress.
    """
    return (
        len(name) <= _GENERIC_NAME_MAX_LEN
        or name.lower() in _GENERIC_NAMES
        or is_known_collision_name
    )


def _looks_qualified_call(snippet: str, bare_name: str) -> bool:
    """Whether a grep-matched line looks like a qualified call site."""
    pattern = re.compile(
        _QUALIFIED_CALL_TEMPLATE.format(name=re.escape(bare_name))
    )
    return pattern.search(snippet) is not None


# The single most common "grep-only" shape on any import-heavy codebase
# (~300+ of claude-code's grep-only bucket, 8 of claude-buddy's) is a
# bare import/require statement naming the target — correctly excluded
# from dekko's own callers count (an import binds a name, it doesn't
# call it), but with no dedicated ``classify_miss()`` cause before this,
# every one fell through to ``CAUSE_UNEXPLAINED``. Each template is
# anchored at the line start (after stripping leading whitespace) so a
# line that merely mentions "import" mid-sentence (prose, a different
# identifier) never false-positives -- a real import/require statement's
# keyword always opens the (stripped) line.
_ESM_NAMED_IMPORT_TEMPLATE = (
    r"^import\s+(?:type\s+)?\{{[^}}]*\b{name}\b[^}}]*\}}\s*from\s+['\"]"
)
_ESM_DEFAULT_IMPORT_TEMPLATE = (
    r"^import\s+(?:\*\s+as\s+)?{name}\s+from\s+['\"]"
)
_PY_FROM_IMPORT_TEMPLATE = r"^from\s+\S+\s+import\s+.*\b{name}\b"
# Java's ``import a.b.C;`` (and ``import static a.b.C.field;``) has no
# equivalent among the three templates above -- none of them match a
# semicolon-terminated, dot-qualified import with no braces/``from``
# keyword. Requires the bare name to be the final dotted segment
# immediately before the terminating ``;`` -- same "visible in the line
# itself" discipline as every other template here. An optional
# ``static `` modifier is allowed between ``import`` and the qualified
# path (a Java static import of a field/method, not a type import)
# without a separate template, since the shape is otherwise identical.
_JAVA_IMPORT_TEMPLATE = r"^import\s+(?:static\s+)?[\w.]*\.{name}\s*;"
# Kotlin shares Java's ``import a.b.C`` shape closely enough to warrant
# its own narrow template rather than loosening the Java one (which
# would risk false-positiving on Java's own semicolon-required style):
# no terminating ``;``, and an optional trailing ``as Alias`` rename.
_KOTLIN_IMPORT_TEMPLATE = r"^import\s+[\w.]*\.{name}(?:\s+as\s+\w+)?\s*$"
# ``export { X } from './x.js'`` / ``export type { X } from`` re-exports
# a name without calling it: the same binding shape as a named import.
# The ``from`` clause is optional: an alias-only re-export of a local
# (``export { a as b };``) binds a name the same way.
_ESM_REEXPORT_TEMPLATE = (
    r"^export\s+(?:type\s+)?\{{[^}}]*\b{name}\b[^}}]*\}}\s*"
    r"(?:from\s+['\"]|;?\s*$)"
)
_IMPORT_LINE_TEMPLATES = (
    _ESM_NAMED_IMPORT_TEMPLATE,
    _ESM_REEXPORT_TEMPLATE,
    _ESM_DEFAULT_IMPORT_TEMPLATE,
    _PY_FROM_IMPORT_TEMPLATE,
    _JAVA_IMPORT_TEMPLATE,
    _KOTLIN_IMPORT_TEMPLATE,
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


# A TS/JS type-position mention (``import type Output from
# './output.js'``, ``output: Output``, ``Foo<Output>``) is neither an
# import-*statement* shape (none of ``_IMPORT_LINE_TEMPLATES`` match a
# parameter-type annotation) nor a call -- it fell straight to
# CAUSE_UNEXPLAINED (78% of claude-code's sample bucket). Deliberately
# scoped to curly-brace/TS-shaped grammars only, gated via
# ``_grammar_for_path`` the same way ``_COMMENT_PREFIXES_BY_GRAMMAR`` is
# -- Python's own ``x: Output`` annotation syntax looks identical but
# the evidence is TS-specific; other languages' type-position syntax
# would need their own follow-up evidence before extending this check
# there (same "ship the shape that's evidenced" precedent as the
# Java/Kotlin import template split above).
_TYPE_ANNOTATION_GRAMMARS = frozenset(
    {"typescript", "tsx", "javascript", "rust"}
)
_TS_IMPORT_TYPE_TEMPLATE = r"^import\s+type\s+.*\b{name}\b"
# The colon half matches ``identifier: identifier``, which in TS is a
# type annotation, an object-literal value (``error: errorMessage,``), a
# ternary else-branch or a ``case`` label -- a regex can't tell them
# apart, but the *target's kind* can: a function's name after a colon is
# never a type annotation. So the colon half is consulted only for a
# type target; the generic-argument and ``import type`` shapes are
# unambiguous syntax whatever the target is and stay ungated. Measured
# on the 100 most-flagged non-type claude-code targets: 145 of 186 "type
# position" rows were the colon half firing on a value. Those now fall
# to "unexplained", on purpose -- a wrong explanation closes an
# investigation that should stay open.
# `x: Output`, not `x: Output()`
_TS_TYPE_COLON_TEMPLATE = r":\s*{name}\b(?!\s*\()"
# `Foo<Output>`, `Foo<Output, Bar>`
_TS_TYPE_GENERIC_TEMPLATE = r"<\s*{name}\s*[,>]"
_TS_TYPE_POSITION_TEMPLATE = (
    f"{_TS_TYPE_COLON_TEMPLATE}|{_TS_TYPE_GENERIC_TEMPLATE}"
)
# Rust's type-position idioms have no TS equivalent and need their own
# templates, even with "rust" now admitted to _TYPE_ANNOTATION_GRAMMARS
# -- Rust's field/parameter shape (`field: Type`) is syntactically
# identical to TS's `x: Output` and already falls out of
# _TS_TYPE_POSITION_TEMPLATE's first half for free once the grammar gate
# is open.
#
# `impl Trait for Type` / `impl<T> Trait<T> for Type<T>` -- an `impl`
# header naming the type being implemented for, not a call or a value
# reference. TS has no equivalent shape.
_RUST_IMPL_FOR_TEMPLATE = r"^impl(?:<[^>]*>)?\s+.+\bfor\s+{name}\b"
# Rust turbofish (`Type::<Concrete>`, `func::<Type>()`) -- distinct
# from TS's bare `<Output>` generic-argument shape by its leading
# `::`. Anchored on the `::` prefix for precision, even though the
# existing generic-argument template would likely also match the
# bare `<Type>` substring once Rust is grammar-gated in.
_RUST_TURBOFISH_TEMPLATE = r"::<\s*{name}\s*>"
# Real-repo verification against zed's `NavHistory`/`BufferFontSize`:
# the `impl...for` template alone left the plain *inherent* impl block
# (`impl NavHistory { ... }`, `impl<T> NavHistory<T> { ... }` -- no
# trailing `for Trait`) unclassified, even though `impl NavHistory {` is
# exactly the motivating line. Anchored the same way as
# `_RUST_IMPL_FOR_TEMPLATE` (start of line, optional generic parameter
# list) but requires the name immediately after, not after a `for`.
_RUST_IMPL_TEMPLATE = r"^impl(?:<[^>]*>)?\s+{name}\b"
# A function's return-type position (`-> Type`, `-> &Type`, `-> &mut
# Type`) has no TS equivalent needing this shape (TS's colon-based
# return-type syntax, `): Type {`, already falls out of the existing
# colon template for free) -- Rust's `->` arrow syntax needs its own.
# Also evidenced directly by zed (`pub fn nav_history(&self) ->
# &NavHistory {`). Same negative-lookahead discipline as the colon
# template, to avoid misclassifying `-> Type()` (a call whose result is
# the return value) as a type annotation -- not a real Rust shape (a
# function's return type is never itself a call expression), but kept
# for defense-in-depth consistency with every other template in this
# module. The lookahead also excludes a trailing `::` (a qualified
# call/path via a leading reference, e.g. `&Type::method()`) for the
# same defense-in-depth reason, even though a return type is never
# itself `-> Type::method()`.
_RUST_RETURN_TYPE_TEMPLATE = r"->\s*&?(?:mut\s+)?{name}\b(?!\s*\(|::)"
# A reference-type mention anywhere in the line (`&Type`, `&mut Type`)
# -- covers shapes the colon/return-type templates above don't anchor
# to, e.g. a nested parameter type inside a higher-order function
# signature (zed: `cb: &mut dyn FnMut(&mut NavHistory, &mut App) ->
# Option<NavigationEntry>,`, where `&mut NavHistory` sits inside a
# `FnMut(...)` parameter list, not directly after a top-level colon).
# Gated with the same call-site negative lookahead as every other
# template here, since `&Type(args)` (a reference to a
# freshly-constructed tuple struct) is a real call, not a bare type
# mention -- also excludes a trailing `::` so `&Type::method()` (a
# qualified call on a reference) isn't misclassified either, even though
# in practice dekko's own resolver already attributes such a call as a
# match before classify_miss ever sees it.
_RUST_REF_TYPE_TEMPLATE = r"&(?:mut\s+)?{name}\b(?!\s*\(|::)"
# Two Rust shapes that name a *type* with no call involved. Both are
# consulted only when the target itself is a type (``target_is_type``):
# for a function target ``name {`` is a block after an expression and
# ``(name)`` is a value handed to a call, which is value-reference
# territory, not this bucket's.
#
# Struct literal or struct pattern: ``let loc = AbortMessageLocation {``.
_RUST_STRUCT_LITERAL_TEMPLATE = r"\b{name}\s*\{{"
# ...but never a declaration or impl header, which also put ``Name {``
# on the line. Own-definition lines are already excluded upstream and
# ``impl`` headers match their own template above; this guard is for a
# same-named declaration dekko didn't index (a ``macro_rules!`` body).
_RUST_DECL_HEADER_TEMPLATE = (
    r"\b(?:struct|enum|union|impl|trait|mod)\s+(?:<[^>]*>\s*)?{name}\b"
)
# Enum-variant payload or tuple-struct field: ``Variant(Name),``,
# ``struct Wrapper(pub Name);``, ``Variant(Other, Name)``. The leading
# CamelCase identifier is what separates a payload *declaration* from
# ``items.map(Name)``, where a tuple-struct constructor travels as a
# function value -- that one is a real (if indirect) use, left alone.
_RUST_TUPLE_PAYLOAD_TEMPLATE = (
    r"\b[A-Z]\w*\s*\((?:[^()]*,)?\s*(?:pub(?:\([^)]*\))?\s+)?"
    r"{name}\s*[,)]"
)


def _looks_like_rust_type_construction(stripped: str, name: str) -> bool:
    """Whether a stripped Rust line names the type ``name`` (already
    ``re.escape``d) as a struct literal/pattern or a tuple payload."""
    if re.search(_RUST_TUPLE_PAYLOAD_TEMPLATE.format(name=name), stripped):
        return True
    if re.search(_RUST_DECL_HEADER_TEMPLATE.format(name=name), stripped):
        return False
    return (
        re.search(_RUST_STRUCT_LITERAL_TEMPLATE.format(name=name), stripped)
        is not None
    )


def _looks_like_type_annotation(
    snippet: str, bare_name: str, path: str, *, target_is_type: bool
) -> bool:
    """Whether a grep-matched line uses ``bare_name`` in a TS/JS or
    Rust type position (an ``import type`` statement, a parameter/
    variable type annotation, a generic type argument, an ``impl``
    header naming the type (with or without a trailing ``for Trait``),
    a return-type arrow, a reference type, or turbofish) rather than as
    a call or value reference.

    ``_TS_TYPE_POSITION_TEMPLATE``'s negative lookahead after the name
    exists specifically to avoid misclassifying ``x: someFunc()`` (a
    call whose result is being assigned/typed) as a type annotation --
    mirrors this module's existing "accept the gap, don't guess wrong"
    discipline. Scoped to ``_TYPE_ANNOTATION_GRAMMARS`` via
    ``_grammar_for_path`` -- always ``False`` outside those grammars,
    never a guess. The Rust-specific templates are additionally gated
    on the grammar being Rust specifically, since their syntax
    (``impl``, ``->``, ``&``, ``::<...>``) never appears in TS/JS
    source the same way.

    ``target_is_type`` additionally admits Rust struct literals and
    enum/tuple-struct payloads -- see
    ``_looks_like_rust_type_construction`` -- and is what admits the
    ``x: Name`` colon shape at all (see
    ``_TS_TYPE_COLON_TEMPLATE``). Keyword-only and required so every
    caller decides it explicitly.
    """
    grammar = _grammar_for_path(path)
    if grammar not in _TYPE_ANNOTATION_GRAMMARS:
        return False
    name = re.escape(bare_name)
    stripped = snippet.strip()
    if re.search(_TS_IMPORT_TYPE_TEMPLATE.format(name=name), stripped):
        return True
    if re.search(_TS_TYPE_GENERIC_TEMPLATE.format(name=name), stripped):
        return True
    if target_is_type and re.search(
        _TS_TYPE_COLON_TEMPLATE.format(name=name), stripped
    ):
        return True
    if grammar == "rust" and any(
        re.search(template.format(name=name), stripped)
        for template in (
            _RUST_IMPL_FOR_TEMPLATE,
            _RUST_IMPL_TEMPLATE,
            _RUST_TURBOFISH_TEMPLATE,
            _RUST_RETURN_TYPE_TEMPLATE,
            _RUST_REF_TYPE_TEMPLATE,
        )
    ):
        return True
    return (
        grammar == "rust"
        and target_is_type
        and _looks_like_rust_type_construction(stripped, name)
    )


_JVM_GRAMMARS = frozenset({"java", "kotlin"})
# A string or char literal on one line. Kotlin's ``"""raw"""`` blanks
# as three adjacent strings, which is the same result.
_JVM_LITERAL = re.compile(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)\'')
# Kotlin string templates hold code: ``"${Name(1)}"`` constructs.
_KOTLIN_TEMPLATE_TEMPLATE = r"\$(?:\{{[^}}]*\b{name}\b|{name}\b)"
# Up to three levels of ``<..>`` type arguments, no parentheses inside.
_JVM_GENERICS = (
    r"(?:<[^<>()]*(?:<[^<>()]*(?:<[^<>()]*>[^<>()]*)*>[^<>()]*)*>)?"
)
# Every shape that can run a constructor, or declare one dekko didn't
# index: ``Name(``, ``Name<..>(``, ``Name::new``, ``Name<..>::new``. An
# annotation (``@Name(..)``) only passes arguments and never constructs.
_JVM_CONSTRUCTION_TEMPLATES = (
    r"(?<!@)\b{name}\s*" + _JVM_GENERICS + r"\s*\(",
    r"\b{name}\s*" + _JVM_GENERICS + r"\s*::\s*new\b",
)
# Kotlin adds a constructor reference (``::Name``) and a trailing-lambda
# construction (``Name { .. }``). A ``:`` or ``>`` right before the name
# is a return or super type ahead of a body (``): Name {``), not one.
_KOTLIN_CONSTRUCTION_TEMPLATES = (
    r"::\s*{name}\b",
    r"(?<![:>])(?<![:>]\s)\b{name}\s*" + _JVM_GENERICS + r"\s*\{{",
)


def _jvm_code_only(snippet: str) -> str:
    """A Java/Kotlin line with string and char literals blanked and a
    trailing ``//`` comment cut."""
    code = _JVM_LITERAL.sub('""', snippet)
    cut = code.find("//")

    return code if cut < 0 else code[:cut]


def _looks_like_jvm_construction(
    code: str, bare_name: str, grammar: str
) -> bool:
    """Whether any occurrence of ``bare_name`` in ``code`` (literals
    already blanked) could construct the type or declare a
    constructor."""
    name = re.escape(bare_name)
    templates = _JVM_CONSTRUCTION_TEMPLATES
    if grammar == "kotlin":
        templates += _KOTLIN_CONSTRUCTION_TEMPLATES

    return any(re.search(t.format(name=name), code) for t in templates)


def _looks_like_jvm_type_mention(
    snippet: str, bare_name: str, path: str, *, target_is_type: bool
) -> bool:
    """Whether a Java/Kotlin line names the type ``bare_name`` without
    constructing it: a declaration, parameter or return type, a generic
    argument, a static member, a cast, ``instanceof`` or a class
    literal.

    The safety property is the construction check: a Java constructor
    can't run without one of ``_JVM_CONSTRUCTION_TEMPLATES``' shapes,
    so a real missed construction is never explained away here. Only
    for a type target (``target_is_type``, which counts the type's own
    constructors as the type), never for a method sharing the name.
    """
    grammar = _grammar_for_path(path)
    if not target_is_type or grammar not in _JVM_GRAMMARS:
        return False
    if grammar == "kotlin" and re.search(
        _KOTLIN_TEMPLATE_TEMPLATE.format(name=re.escape(bare_name)), snippet
    ):
        return False
    code = _jvm_code_only(snippet)

    return _word(bare_name).search(code) is not None and not (
        _looks_like_jvm_construction(code, bare_name, grammar)
    )


def _looks_like_jvm_string_mention(
    snippet: str, bare_name: str, path: str
) -> bool:
    """Whether a Java/Kotlin line names ``bare_name`` only inside string
    or char literals (``"org.example.JarLauncher"``). Neither language
    runs code out of a string, except a Kotlin template, which is code
    and refuses the match."""
    grammar = _grammar_for_path(path)
    if grammar not in _JVM_GRAMMARS:
        return False
    name = re.escape(bare_name)
    if grammar == "kotlin" and re.search(
        _KOTLIN_TEMPLATE_TEMPLATE.format(name=name), snippet
    ):
        return False
    word = _word(bare_name)

    return (
        word.search(snippet) is not None
        and word.search(_JVM_LITERAL.sub('""', snippet)) is None
    )


# A same-bare-name local variable/parameter declaration (``const warn:
# string[] = []``) or a bare quoted string literal value (``status ===
# "warn"``) in an unrelated file share the target's bare name without
# referencing it at all -- 32% of claude-buddy's unexplained bucket. A
# full declaration-vs-reference check would require real parsing (out of
# scope for this module's pattern-match-on-the-grep-line philosophy, per
# the module docstring) -- this is a scoped, regex-based partial fix
# covering the two concrete shapes observed, not every possible
# "unrelated local binding" shape (a JSX prop, a destructured parameter,
# an object-literal key -- extend reactively if one of those surfaces as
# a concrete new bucket).
# ``(?:mut\s+)?`` admits Rust's ``let mut name =``; JS has no ``mut``.
_LOCAL_DECL_TEMPLATE = r"^(?:const|let|var)\s+(?:mut\s+)?{name}\s*[:=]"
_STRING_LITERAL_TEMPLATE = r'["\']{name}["\']'
# A `catch (error) { ... }` parameter binding -- the caught exception
# name shares the target's bare name but is a fresh local binding, not a
# reference to it. The optional leading `}` covers the common `} catch
# (error) {` brace-placement style (the previous block's closer on the
# same line as `catch`).
_CATCH_BINDING_TEMPLATE = r"^\}}?\s*catch\s*\(\s*{name}\b"
# A bare interface/type field declaration (`error?: string;` /
# `error: string;`) inside an object/interface body -- same "local
# binding, not a reference" shape as the const/let/var case, just
# without a keyword prefix. Anchored to avoid matching a real
# assignment/comparison expression that happens to start with the
# name followed by ":" in an unrelated context (e.g. a ternary) --
# requires the line, stripped, to consist of just `name` then
# `:`/`?:` then a type and a terminator, mirroring this module's
# existing "visible in the line itself" anchoring discipline.
_INTERFACE_FIELD_TEMPLATE = r"^{name}\??\s*:\s*\S"


def _looks_like_local_binding_or_literal(snippet: str, bare_name: str) -> bool:
    """Whether a grep-matched line is an unrelated local variable/
    parameter declaration naming ``bare_name``, a ``catch`` binding, a
    bare interface/type field declaration, or a bare quoted string
    literal equal to it -- none of these are a reference to the
    target.

    Must only ever be consulted after ``_looks_qualified_call``/
    ``_looks_like_import_statement`` have already run (both are checked
    unconditionally at the top of ``classify_miss``'s ladder before
    this function's result is ever threaded in), so a real call whose
    string *argument* happens to equal ``bare_name`` (e.g.
    ``someFunc("warn")`` when ``warn`` is itself the target) is never
    reached by a qualified-call check that would have explained it
    differently -- see this function's own test coverage for the
    "ordering caveat" this precedence is meant to guard.
    """
    name = re.escape(bare_name)
    stripped = snippet.strip()
    if re.search(_LOCAL_DECL_TEMPLATE.format(name=name), stripped):
        return True
    if re.search(_CATCH_BINDING_TEMPLATE.format(name=name), stripped):
        return True
    if re.search(_INTERFACE_FIELD_TEMPLATE.format(name=name), stripped):
        return True
    # Only treat a bare quoted match as a literal, not a substring of a
    # longer string -- requires the quote characters to be the
    # immediately adjacent characters, same discipline as every other
    # anchored template in this module.
    return (
        re.search(_STRING_LITERAL_TEMPLATE.format(name=name), stripped)
        is not None
    )


# Three JS/TS line shapes that name the target without calling it:
# the name only inside string/template text, a recorded property read,
# and an object key/field/typed parameter (``{ ok: [], warn: [] }``,
# ``(action: string) => void``). JS/TS only: Python f-strings and dict
# keys, Go map-literal keys and Kotlin/Ruby interpolation give "inside
# a string" or ``name:`` other meanings, and the evidence was TS.
_JS_SHAPE_GRAMMARS = frozenset({"typescript", "tsx", "javascript"})
# A bare call of the name, not a method call on something else. A line
# holding one is never explained by these shapes, so a real missed call
# can't hide behind a same-line string or key.
_JS_BARE_CALL_TEMPLATE = r"(?<![.\w]){name}\s*\("
# ``name:`` / ``name?:`` right after ``{``, ``,``, ``(``, ``;`` or the
# line start, never ``name::``.
_JS_KEY_TEMPLATE = r"(?:^|(?<=[{{,(;]))\s*{name}\s*\??:(?!:)"


@dataclass(frozen=True)
class _JsShapes:
    """Which non-call JS/TS shapes explain one grep hit's line."""

    string_mention: bool = False
    property_read: bool = False
    key_or_field: bool = False


def _skip_quoted(line: str, i: int) -> int:
    """Index just past the ``'``/``"`` string that opens at ``i``."""
    quote, j = line[i], i + 1
    while j < len(line) and line[j] != quote:
        j += 2 if line[j] == "\\" else 1
    return j + 1


def _skip_braces(line: str, j: int) -> int:
    """Index just past the ``}`` closing the ``${`` body that starts at
    ``j`` (brace-depth aware, one line only), or the line's end."""
    depth = 1
    while j < len(line) and depth:
        depth += {"{": 1, "}": -1}.get(line[j], 0)
        j += 1
    return j


def _template_code(line: str, i: int) -> tuple[str, int]:
    """The ``${...}`` bodies of the template literal opening at ``i``,
    each with its own strings blanked (``${x ? "a-b" : ""}`` used to
    keep the quoted text and let ``a`` match).

    Returns:
        The kept code (bodies, space-separated) and the index just past
        the closing backtick, or the line's end for an unclosed one.
    """
    kept: list[str] = []
    j = i + 1
    while j < len(line) and line[j] != "`":
        if line[j] == "\\":
            j += 2
        elif line.startswith("${", j):
            k = _skip_braces(line, j + 2)
            kept.append(_js_code_only(line[j + 2 : k - 1]))
            j = k
        else:
            j += 1
    return " " + " ".join(kept) + " ", j + 1


def _js_code_only(line: str, *, keep_comments: bool = False) -> str:
    """``line`` with JS string, template and comment text blanked.

    Quoted strings become ``""``; a template literal keeps only its
    ``${...}`` bodies (brace-depth aware, strings inside them blanked
    too), so a template holding ``${warn()}`` still shows the call. A
    ``/* ... */`` becomes a space and a ``//`` ends the line, unless
    ``keep_comments`` (the trailing-comment check wants the comment
    text intact and the rest blanked). One line at a time: the
    continuation line of a multi-line template has no backtick and
    reads as code; ``_js_line_state`` is what knows better.
    """
    out: list[str] = []
    i = 0
    while i < len(line):
        c = line[i]
        if line.startswith("//", i):
            if keep_comments:
                out.append(line[i:])
            break
        if line.startswith("/*", i) and not keep_comments:
            end = line.find("*/", i + 2)
            out.append(" ")
            i = len(line) if end < 0 else end + 2
        elif c in "'\"":
            out.append('""')
            i = _skip_quoted(line, i)
        elif c == "`":
            code, i = _template_code(line, i)
            out.append(f'"{code}"')
        else:
            out.append(c)
            i += 1
    return "".join(out)


# A string that is nothing but a call expression: ``eval("warn()")``,
# ``setTimeout("tick()", 1)``, ``new Function("return f()")``. No space
# before ``(``: prose like ``"Project (.claude/agents/)"`` has one, and
# code in a string doesn't.
_CALL_ONLY_TEXT = re.compile(r"^\s*(?:new\s+)?[\w.$]+\(.*\)\s*;?\s*$")


def _string_contents(line: str) -> list[str]:
    """The text of every quoted string and template literal on
    ``line``, template ``${...}`` bodies left out."""
    out: list[str] = []
    i = 0
    while i < len(line):
        c = line[i]
        if c in "'\"":
            j = _skip_quoted(line, i)
            out.append(line[i + 1 : j - 1])
            i = j
        elif c == "`":
            buf: list[str] = []
            j = i + 1
            while j < len(line) and line[j] != "`":
                if line[j] == "\\":
                    j += 2
                elif line.startswith("${", j):
                    j = _skip_braces(line, j + 2)
                else:
                    buf.append(line[j])
                    j += 1
            out.append("".join(buf))
            i = j + 1
        else:
            i += 1
    return out


def _call_written_in_text(line: str, name: str) -> bool:
    """Whether some string on ``line`` is *nothing but* a call naming
    ``name`` (already ``re.escape``d): the ``eval("name()")`` shape, a
    real reference the resolver can't see. A string that merely
    contains ``name(`` among other text (a template of generated code,
    a log line) is string text; the old any-``name(``-in-the-line test
    kept every such row visible.
    """
    return any(
        _CALL_ONLY_TEXT.match(text) is not None
        and re.search(rf"\b{name}\(", text) is not None
        for text in _string_contents(line)
    )


def _bash_code_only(line: str) -> str:
    """``line`` with shell-quoted text blanked: ``'...'`` verbatim,
    ``"..."`` with backslash escapes. jq filters and ``POOLS=("...")``
    literals read as string text."""
    out: list[str] = []
    i = 0
    while i < len(line):
        c = line[i]
        if c == "'":
            j = line.find("'", i + 1)
            out.append("''")
            i = len(line) if j < 0 else j + 1
        elif c == '"':
            j = i + 1
            while j < len(line) and line[j] != '"':
                j += 2 if line[j] == "\\" else 1
            out.append('""')
            i = j + 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _js_shapes(
    snippet: str,
    bare_name: str,
    path: str,
    is_read_site: bool,
) -> _JsShapes:
    """Classify ``snippet`` against the three JS/TS non-call shapes.

    Bash gets the string shape alone: it is Tier-2 but yields call
    links (claude-buddy has four ``.sh`` callers), so "declarations-only
    language" would be false, and a name that survives only inside
    shell quotes (a jq filter) is string text.

    Args:
        snippet: The grep-matched line.
        bare_name: The bare identifier being searched for.
        path: The hit's file; any other file gets no shape.
        is_read_site: Whether the map records a property read of
            ``bare_name`` at this line (``_read_sites``).

    Returns:
        The shapes that hold. At most one matters: ``classify_miss``
        checks them in field order.
    """
    grammar = _grammar_for_path(path)
    name = re.escape(bare_name)
    word = re.compile(rf"\b{name}\b")
    stripped = snippet.strip()
    if grammar == "bash":
        return _JsShapes(
            string_mention=word.search(_bash_code_only(stripped)) is None
        )
    if grammar not in _JS_SHAPE_GRAMMARS:
        return _JsShapes()
    code = _js_code_only(stripped)
    if not word.search(code):
        return _JsShapes(
            string_mention=not _call_written_in_text(stripped, name)
        )
    bare_call = re.search(_JS_BARE_CALL_TEMPLATE.format(name=name), code)
    if bare_call:
        return _JsShapes()
    rest = re.sub(_JS_KEY_TEMPLATE.format(name=name), " ", code)
    return _JsShapes(
        property_read=is_read_site,
        key_or_field=rest != code and not word.search(rest),
    )


# --- file state and the leftover line shapes ------------------------------

# Per-run caches for the checks that read a hit's file: the lines
# themselves and, per JS/TS file, the lexer state at the start of each
# line. Keyed by ``(root, path)`` (a long-lived process can serve more
# than one root); cleared at the start of ``run()`` and ``run_all()``
# so an edited file is re-read on the next run. Filled from worker
# threads under ``--all``: a dict write of one immutable value is
# atomic in CPython, and two threads that compute the same file agree.
_file_lines: dict[tuple[str, str], list[str]] = {}
_file_states: dict[tuple[str, str], list[str]] = {}


def _reset_file_caches() -> None:
    _file_lines.clear()
    _file_states.clear()
    _shadow_decl_lines.clear()


def _cached_lines(root: Path, path: str) -> list[str]:
    key = (str(root), path)
    lines = _file_lines.get(key)
    if lines is None:
        lines = read_lines(root, path)
        _file_lines[key] = lines

    return lines


_STATE_CODE = "code"
_STATE_TEMPLATE = "template"
_STATE_BLOCK = "block"


def _lex_block(text: str, i: int) -> tuple[str, int]:
    if text.startswith("*/", i):
        return _STATE_CODE, i + 2

    return _STATE_BLOCK, i + 1


def _lex_template(text: str, i: int) -> tuple[str, int]:
    c = text[i]
    if c == "\\":
        return _STATE_TEMPLATE, i + 2
    if c == "`":
        return _STATE_CODE, i + 1
    if text.startswith("${", i):
        return _STATE_TEMPLATE, _skip_braces(text, i + 2)

    return _STATE_TEMPLATE, i + 1


def _lex_code(text: str, i: int) -> tuple[str, int]:
    c = text[i]
    if c in "'\"":
        return _STATE_CODE, _skip_quoted(text, i)
    if c == "`":
        return _STATE_TEMPLATE, i + 1
    if text.startswith("//", i):
        return _STATE_CODE, len(text)
    if text.startswith("/*", i):
        return _STATE_BLOCK, i + 2

    return _STATE_CODE, i + 1


_LEX_STEP = {
    _STATE_CODE: _lex_code,
    _STATE_TEMPLATE: _lex_template,
    _STATE_BLOCK: _lex_block,
}


def _js_line_states(lines: list[str]) -> list[str]:
    """The lexer state at the *start* of each line of a JS/TS file:
    ``code``, ``template`` (inside a backtick literal) or ``block``
    (inside ``/* */``).

    Three states and one pass over the file. Quoted strings end at
    their line; template literals (with brace-aware ``${}`` bodies,
    skipped unlexed on their own line) and block comments carry
    across lines; ``//`` ends the line in code state. No JSX awareness:
    see ``_js_line_state`` for the desync guard.
    """
    states: list[str] = []
    state = _STATE_CODE
    for text in lines:
        states.append(state)
        i = 0
        while i < len(text):
            state, i = _LEX_STEP[state](text, i)
    return states


def _js_line_state(root: Path, path: str, line: int) -> str:
    """``code``/``template``/``block`` at the start of ``path:line``.

    A line-at-a-time rule cannot see that a line sits inside a template
    literal or a ``/* */`` block opened above, which is what a prompt's
    continuation line or a JSDoc body without ``*`` prefixes is. Cached
    per file for the run.

    **Desync guard.** A backtick or an unbalanced quote inside JSX
    text, or a regex literal, can leave the lexer in the wrong state
    for the rest of the file. A file that does not end in ``code``
    state is treated as all-``code``: no state-based cause anywhere in
    it (11 of 1,902 JS/TS files on claude-code, files *about* shells).
    A desync that closes again before the file ends is the remaining
    risk; the value-position rule and the map's tier-1 references
    bound what it can hide.
    """
    key = (str(root), path)
    states = _file_states.get(key)
    if states is None:
        states = _js_line_states([*_cached_lines(root, path), ""])
        if states[-1] != _STATE_CODE:
            states = [_STATE_CODE] * len(states)
        _file_states[key] = states

    return states[line - 1] if line <= len(states) else _STATE_CODE


# ``.tsx``/``.jsx`` carry JSX; ``.ts`` cannot.
_JSX_GRAMMARS = frozenset({"tsx", "javascript"})
# Grammars whose ``#`` opens a comment outside quotes, for the
# trailing-comment shape; the C family is the ``//`` set.
_HASH_COMMENT_GRAMMARS = frozenset({"python", "bash", "ruby", "zsh"})
_SLASH_COMMENT_GRAMMARS = frozenset(
    {"rust", "c", "cpp", "go", "java", "kotlin", "swift", "scala", "csharp"}
)
# Symbol kinds whose body is a type context: nothing inside one is a
# call. Not ``class``/``struct``/``trait``, which hold code.
_TYPE_CONTEXT_KINDS = frozenset({"interface", "type_alias", "enum"})
# A method call on an expression result (``foo().name(``, ``a[0].name(``,
# ``x!.name(``, ``x?.name(``) or a line-start ``.name(`` continuation of
# a chain: the same blind spot as the identifier-receiver form
# (``_QUALIFIED_CALL_TEMPLATE``), a receiver whose type the resolver
# does not know. Any grammar.
_EXPRESSION_CALL_TEMPLATE = r"[)\]!?]\s*\.\s*{name}\s*\(|^\.\s*{name}\s*\("
# ``x.name`` / ``x?.name`` / ``x!.name`` / ``foo().name`` with no ``(``
# after the name.
_PROPERTY_ACCESS_TEMPLATE = r"[\w)\]]\s*[?!]?\.\s*{name}\b(?!\s*\()"
# TS modifiers that may precede a method signature.
_TS_MEMBER_MODIFIERS = (
    r"(?:(?:public|protected|private|static|abstract|readonly|async|"
    r"override|declare|export|get|set)\s+)*"
)
# A bare member line of a multi-line ``export {`` list, one specifier.
_EXPORT_LIST_MEMBER_TEMPLATE = (
    r"^\s*(?:type\s+)?(?:\w+\s+as\s+)?{name}(?:\s+as\s+\w+)?\s*,?\s*$"
)
_EXPORT_LIST_OPENER = re.compile(r"^export\s+(?:type\s+)?\{\s*$")
_EXPORT_LIST_ANY_MEMBER = re.compile(
    r"^(?:type\s+)?(?:\w+\s+as\s+)?\w+(?:\s+as\s+\w+)?\s*,?$"
)
# How far up a member line of an ``export {`` list may sit from its
# opener (cline's ``sdk/packages/core/src/index.ts`` is a 300-line
# export list).
_EXPORT_LIST_SCAN_LINES = 600
# A multi-line destructuring's opener (``const {`` / ``const [`` at the
# end of a line) and one member line of it (``name,`` / ``key: name,``
# / ``name = default,``), bounded to 40 member lines.
_DESTRUCTURE_OPENER = re.compile(r"\b(?:const|let|var)\s*[{\[]\s*$")
_DESTRUCTURE_NESTED_OPENER = re.compile(r"^\s*\w+\s*:\s*[{\[]\s*$")
_DESTRUCTURE_MEMBER = re.compile(
    r"^\s*(?:\.\.\.)?\w+(?:\s*:\s*[\w{}\[\]\s,]+)?(?:\s*=\s*[^,]+)?\s*,?\s*$"
)
_DESTRUCTURE_MEMBER_TEMPLATE = (
    r"^\s*(?:\.\.\.)?(?:\w+\s*:\s*)?{name}\s*(?:=\s*[^,]+)?,?\s*$"
)
_DESTRUCTURE_SCAN_LINES = 40


@dataclass(frozen=True)
class _LineShapes:
    """The leftover non-call shapes of one grep hit's line, each a
    rung of ``classify_miss``. JS/TS only except where noted.

    Attributes:
        template_text: The line starts inside a template literal and
            the name is gone once the template text is blanked.
        block_comment: The line starts inside a ``/* */`` block and
            the name is gone once the comment text is blanked.
        expression_call: A method call on an expression result or a
            chain continuation (``_EXPRESSION_CALL_TEMPLATE``); any
            grammar.
        type_shape: A type position the anchored templates miss
            (``_looks_like_js_type_shape``).
        trailing_comment: The name appears only after a ``//``, inside
            a ``/* */`` or after a ``#`` following code
            (``_looks_like_trailing_comment``); JS/TS, the ``#``
            grammars and the C family.
        jsx_text: JSX text content between ``>`` and ``<``.
        property_access: ``x.name`` with no call, the shape twin of
            a recorded property read.
        signature: An abstract/overload/interface-member signature.
        declaration: A declaration line binding the name
            (destructuring, arrow/function parameter, ``for``-of,
            ``readonly name: T``, a multi-line destructuring member,
            a JSX attribute ``name=``).
        export_member: A member line of a multi-line ``export {``
            list (an import-statement shape, like the single-line
            re-export).
    """

    template_text: bool = False
    block_comment: bool = False
    expression_call: bool = False
    type_shape: bool = False
    trailing_comment: bool = False
    jsx_text: bool = False
    property_access: bool = False
    signature: bool = False
    declaration: bool = False
    export_member: bool = False


def _word(bare_name: str) -> re.Pattern:
    return re.compile(rf"\b{re.escape(bare_name)}\b")


def _split_trailing_comment(code: str) -> tuple[str, str]:
    """``(body, comment)`` for a JS/TS line with strings blanked and
    comment text kept (``_js_code_only(..., keep_comments=True)``)."""
    body, comment = code, ""
    cut = body.find("//")
    if cut >= 0:
        body, comment = body[:cut], body[cut:]
    parts: list[str] = []

    def keep(m: re.Match) -> str:
        parts.append(m.group(0))
        return " "

    body = re.sub(r"/\*.*?\*/", keep, body)
    if "/*" in body:
        cut = body.index("/*")
        parts.append(body[cut:])
        body = body[:cut]

    return body, " ".join([comment, *parts])


def _looks_like_trailing_comment(
    snippet: str, bare_name: str, path: str
) -> bool:
    """Whether the name appears only in a comment that follows code
    on the line (``since: number; // timestamp of last mood change``).

    With strings blanked first, so a ``//`` inside a URL string does
    not split the line and a name inside a string does not count as
    the comment's. JS/TS, the ``#`` grammars and the C family; the
    name must be in the comment part and absent from the code part.
    """
    grammar = _grammar_for_path(path)
    word = _word(bare_name)
    stripped = snippet.strip()
    if grammar in _JS_SHAPE_GRAMMARS:
        body, comment = _split_trailing_comment(
            _js_code_only(stripped, keep_comments=True)
        )
    elif grammar in _HASH_COMMENT_GRAMMARS:
        body, _, comment = _bash_code_only(stripped).partition("#")
    elif grammar in _SLASH_COMMENT_GRAMMARS:
        body, _, comment = _DOUBLE_QUOTED.sub('""', stripped).partition("//")
    else:
        return False

    return word.search(comment) is not None and word.search(body) is None


def _looks_like_js_type_shape(
    code: str, bare_name: str, *, target_is_type: bool
) -> bool:
    """The JS/TS type positions ``_looks_like_type_annotation``'s
    anchored templates miss, over the string-blanked line.

    For any target: a heritage clause (``extends``/``implements``, the
    shape fallback for a clause the map did not resolve), a union or
    generic member (``<Name |``, ``| Name``, ``, Name>``), and
    ``typeof Name`` (a type query names the value, never calls it).
    For a type target only: a return type ``): ...Name...`` (no space
    before the colon, which a ternary ``) : x`` has), ``as``/
    ``satisfies Name``, a namespace-qualified ``x: ns.Name``, a
    function-type return ``) => Name``, a type-alias body ``type X =
    ... Name ...`` and an indexed-access type ``Name["key"]``.
    """
    name = re.escape(bare_name)
    if re.search(rf"\b(?:extends|implements)\b[^{{=;]*\b{name}\b", code):
        return True
    if (
        re.search(rf"<\s*{name}\s*[|&>,\[]", code)
        or re.search(rf"(?<![|&])[|&](?![|&])\s*{name}\b(?!\s*\()", code)
        or re.search(rf",\s*{name}\s*>", code)
    ):
        return True
    if re.search(rf"\btypeof\s+{name}\b", code):
        return True
    if not target_is_type:
        return False

    return any(
        re.search(template, code)
        for template in (
            rf"\):\s*[^={{;]*\b{name}\b[^={{;]*(?:=>|\{{|;|,)?\s*$",
            rf"\b(?:as|satisfies)\s+{name}\b",
            rf"[:<|&,(]\s*(?:[\w$]+\.)+{name}\b(?!\s*\()",
            rf"\)\s*=>\s*{name}\b\s*(?:[,;)\]}}|]|$)",
            rf"^(?:export\s+)?type\s+\w+(?:<[^>]*>)?\s*=\s*[^;]*\b{name}\b",
            rf"(?<![.\w]){name}\s*\[\s*[\"']",
        )
    )


def _looks_like_signature(code: str, bare_name: str) -> bool:
    """An ``abstract``/``declare`` method signature, or an anchored
    ``name(params): Type`` / ``function name(params): Type`` line with
    no body after the return type (an overload or interface member)."""
    name = re.escape(bare_name)
    if re.search(rf"\b(?:abstract|declare)\b[^(]*\b{name}\s*\(", code):
        return True
    if re.search(
        rf"^{_TS_MEMBER_MODIFIERS}{name}\s*(?:<[^>]*>)?\([^)]*\)\s*:\s*"
        rf"[^={{;]+[;{{]?\s*$",
        code,
    ):
        return True

    return (
        re.search(
            rf"^(?:export\s+)?(?:async\s+)?function\s+{name}\s*(?:<[^>]*>)?"
            rf"\([^)]*\)\s*:\s*[^={{;]+;?\s*$",
            code,
        )
        is not None
    )


def _blank_braces(line: str) -> str:
    """``line`` with every ``{...}`` body blanked (depth aware)."""
    out: list[str] = []
    depth = 0
    for c in line:
        if c == "{":
            depth += 1
            out.append(" ")
        elif c == "}":
            depth = max(0, depth - 1)
            out.append(" ")
        else:
            out.append(c if depth == 0 else " ")
    return "".join(out)


def _looks_like_jsx_text(code: str, word: re.Pattern) -> bool:
    """The name sits only in JSX text content: between ``>`` and ``<``
    (or the line's end) once ``{...}`` bodies are blanked, and nowhere
    outside those segments. A text line with no tag on it is not
    claimed (that needs JSX-children state across lines)."""
    blanked = _blank_braces(code)
    segments = re.findall(r">([^<>]*)(?=<|$)", blanked)
    if not any(word.search(s) for s in segments):
        return False
    outside = re.sub(r">[^<>]*(?=<|$)", ">", blanked)

    return word.search(outside) is None


def _looks_like_jsx_attribute(
    code: str, bare_name: str, word: re.Pattern
) -> bool:
    """A JSX attribute ``name="..."``/``name={...}`` with no other
    mention on the line: an object key, the way ``{ name: ... }`` is.
    ``action={action}`` keeps a bare ``action`` and falls through to
    the shadow pass."""
    attribute = rf"(?:^|\s){re.escape(bare_name)}=(?:\"\"|\{{)"
    if not re.search(attribute, code):
        return False

    return word.search(re.sub(attribute, " ", code)) is None


def _destructuring_opener(
    lines: list[str], line: int, bare_name: str
) -> int | None:
    """Line of the ``const {``/``const [`` opener when ``line`` is a
    bare member line of a multi-line destructuring declaration naming
    ``bare_name``, else ``None``.

    Walks up through member lines and nested pattern openers only,
    bounded to ``_DESTRUCTURE_SCAN_LINES``. claude-code's ``src/`` is
    React-Compiler output (``const { action } = t0`` over several
    lines), which is why "props" are destructuring members there.
    """
    if not lines or line > len(lines):
        return None
    member = _DESTRUCTURE_MEMBER_TEMPLATE.format(name=re.escape(bare_name))
    if not re.match(member, lines[line - 1]):
        return None
    for ln in range(line - 1, max(0, line - _DESTRUCTURE_SCAN_LINES), -1):
        text = lines[ln - 1]
        if not text.strip():
            continue
        if _DESTRUCTURE_OPENER.search(text):
            return ln
        if _DESTRUCTURE_NESTED_OPENER.match(text):
            continue
        if not _DESTRUCTURE_MEMBER.match(text):
            return None

    return None


def _export_list_member(lines: list[str], line: int, bare_name: str) -> bool:
    """Whether ``line`` is a one-specifier member line of a multi-line
    ``export {``/``export type {`` list, walking up through member
    and comment lines only, bounded to ``_EXPORT_LIST_SCAN_LINES``."""
    if not lines or line > len(lines):
        return False
    member = _EXPORT_LIST_MEMBER_TEMPLATE.format(name=re.escape(bare_name))
    if not re.match(member, lines[line - 1]):
        return False
    for ln in range(line - 1, max(0, line - _EXPORT_LIST_SCAN_LINES), -1):
        text = lines[ln - 1].strip()
        if not text or text.startswith(("//", "/*", "*")):
            continue
        if _EXPORT_LIST_OPENER.match(text):
            return True
        if not _EXPORT_LIST_ANY_MEMBER.match(text):
            return False

    return False


def _looks_like_js_declaration(
    code: str, bare_name: str, lines: list[str], line: int
) -> bool:
    """A JS/TS declaration line that binds ``bare_name`` itself: a
    destructuring declaration, a bare ``let name``, a ``for``-of
    binding, an arrow or ``function`` parameter list, ``readonly
    name: T``, or a member line of a multi-line destructuring."""
    name = re.escape(bare_name)
    templates = (
        rf"^(?:export\s+)?(?:const|let|var)\s+[\[{{][^=]*\b{name}\b[^=]*[\]}}]\s*=",
        rf"^(?:const|let|var)\s+{name}\s*(?:;|$)",
        rf"^for\s*\(\s*(?:const|let|var)\s+(?:[{{\[][^=]*)?\b{name}\b",
        rf"(?<![\w.]){name}\s*=>",
        rf"[(,]\s*(?:\.\.\.)?(?:\{{[^}}]*)?\b{name}\b[^)]*\)\s*(?::\s*[^=]+)?=>",
        rf"\bfunction\b[^(]*\([^)]*\b{name}\b",
        rf"^(?:readonly\s+)?{name}\??\s*:\s*\S",
    )
    if any(re.search(t, code) for t in templates):
        return True

    return _destructuring_opener(lines, line, bare_name) is not None


def _js_line_shapes(
    root: Path,
    hit: "GrepHit",
    bare_name: str,
    grammar: str,
    *,
    target_is_type: bool,
) -> _LineShapes:
    """``_line_shapes`` for a JS/TS hit."""
    name = re.escape(bare_name)
    word = _word(bare_name)
    stripped = hit.snippet.strip()
    state = _js_line_state(root, hit.path, hit.line)
    if state == _STATE_TEMPLATE and not word.search(
        _js_code_only("`" + stripped)
    ):
        return _LineShapes(template_text=True)
    if state == _STATE_BLOCK and not word.search(
        _js_code_only("/*" + stripped)
    ):
        return _LineShapes(block_comment=True)
    code = _js_code_only(stripped)
    lines = _cached_lines(root, hit.path)
    jsx = grammar in _JSX_GRAMMARS
    bare_call = re.search(_JS_BARE_CALL_TEMPLATE.format(name=name), code)

    return _LineShapes(
        expression_call=(
            re.search(_EXPRESSION_CALL_TEMPLATE.format(name=name), code)
            is not None
        ),
        type_shape=_looks_like_js_type_shape(
            code, bare_name, target_is_type=target_is_type
        ),
        trailing_comment=_looks_like_trailing_comment(
            stripped, bare_name, hit.path
        ),
        jsx_text=jsx and _looks_like_jsx_text(code, word),
        property_access=(
            bare_call is None
            and re.search(_PROPERTY_ACCESS_TEMPLATE.format(name=name), code)
            is not None
        ),
        signature=_looks_like_signature(code, bare_name),
        declaration=(
            _looks_like_js_declaration(code, bare_name, lines, hit.line)
            or (jsx and _looks_like_jsx_attribute(code, bare_name, word))
        ),
        export_member=_export_list_member(lines, hit.line, bare_name),
    )


def _line_shapes(
    root: Path,
    hit: "GrepHit",
    bare_name: str,
    *,
    target_is_type: bool,
) -> _LineShapes:
    """The ``_LineShapes`` of one hit, by its file's grammar.

    JS/TS gets every shape (``_js_line_shapes``). Every other grammar
    gets the two whose meaning is the same everywhere: a method call
    on an expression result and a trailing comment (bash with its
    quotes blanked first, the C family with double-quoted text
    blanked). The rest of the module's shape rules stay JS/TS-only on
    purpose: strings and a bare name mean other things elsewhere.
    """
    grammar = _grammar_for_path(hit.path)
    if grammar in _JS_SHAPE_GRAMMARS:
        return _js_line_shapes(
            root, hit, bare_name, grammar, target_is_type=target_is_type
        )
    stripped = hit.snippet.strip()
    code = _bash_code_only(stripped) if grammar == "bash" else stripped

    return _LineShapes(
        expression_call=(
            re.search(
                _EXPRESSION_CALL_TEMPLATE.format(name=re.escape(bare_name)),
                code,
            )
            is not None
        ),
        trailing_comment=_looks_like_trailing_comment(
            stripped, bare_name, hit.path
        ),
    )


# Tier 2: a function handed around as a value in a language dekko
# extracts no reference edges for. Tier 1 (the map already recorded this
# exact line as a reference) is the caller's job and needs no regex;
# this is only the fallback for grammars whose
# ``LanguageSpec.reference_query`` is ``None``.
#
# One shape only: the **path-qualified** name, ``.map(Thing::as_str)``.
# The design also had a bare name in argument position
# (``register(my_fn)``) and a field init (``handler: my_fn,``). Both
# were built, measured on zed, and removed: of ~10,000 rows they
# moved, nearly all were same-named *locals* (``Some(buffer)``,
# ``(program, args)``, ``indent_guide(buffer_id, 1)``), because Rust
# names a getter after what it returns. A bare name can't be told from
# a local without scope analysis -- the read-side twin of the
# resolver's own shadowed-local bug. A ``::`` in front of the name is
# the one thing a local can never have.
#
# Allowlisted grammars, not "every grammar without a reference query":
# the path shape means this in Rust and C++; extend on evidence, same
# as ``_TYPE_ANNOTATION_GRAMMARS``.
_VALUE_REFERENCE_GRAMMARS = frozenset({"rust", "cpp"})
# Refuses a ``(`` after the name -- the whole safety property: a real
# missed *call* can never land here. Also refuses a trailing ``::``
# (turbofish call ``name::<T>(x)``, or ``name`` being a module) and
# ``!`` (macro invocation).
_VALUE_PATH_TEMPLATE = r"::\s*{name}\b(?!\s*(?:\(|::|!))"
# ``use a::b::name;`` / ``using ns::name;`` have the same shape and are
# imports. Rust/C++ have no entry in ``_IMPORT_LINE_TEMPLATES``, so
# this function has to stand down on them itself.
_USE_STATEMENT = re.compile(r"^(?:pub(?:\([^)]*\))?\s+)?use\b|^using\b")
# One row of a multi-line ``use a::{b, c::name};`` block: nothing but
# names, ``::`` paths, commas and braces.
_BARE_NAME_LIST_LINE = re.compile(r"^(?:[\w\s,{}*]|::)+;?$")
# zed, measured: a path inside a string (``"std::net::UdpSocket::bind"``,
# ``.expect("validated in BenchAppContext::build")``) or a lint path
# in an attribute (``#[warn(clippy::all)]``) has the shape and is not
# a value. Quoted text is blanked before matching; attribute lines are
# refused outright.
_DOUBLE_QUOTED = re.compile(r'"(?:[^"\\]|\\.)*"')
_ATTRIBUTE_LINE_PREFIXES = ("#[", "#![")


def _looks_like_value_reference(
    snippet: str, bare_name: str, path: str
) -> bool:
    """Whether a grep-matched line names ``bare_name`` as a
    path-qualified value (``Thing::name``) with no call after it.

    A line-shape heuristic, scoped to ``_VALUE_REFERENCE_GRAMMARS`` --
    always ``False`` elsewhere, including every language dekko *does*
    record reference edges for, where an unrecorded value reference is
    a real resolver miss and must stay visible as one. It can't say
    *which* ``name`` the path reaches (``Other::as_str`` matches a
    sanity run on ``Prompt::as_str``), only that the line is not a
    call; the cause text says "line-shape match" for that reason. The
    caller only consults this for a non-type target (see
    ``_classify_grep_hits``).
    """
    if _grammar_for_path(path) not in _VALUE_REFERENCE_GRAMMARS:
        return False
    stripped = snippet.strip()
    if stripped.startswith(_ATTRIBUTE_LINE_PREFIXES):
        return False
    if _USE_STATEMENT.search(stripped) or _BARE_NAME_LIST_LINE.match(stripped):
        return False
    code = _DOUBLE_QUOTED.sub('""', stripped)
    return (
        re.search(_VALUE_PATH_TEMPLATE.format(name=re.escape(bare_name)), code)
        is not None
    )


# How far above a hit to look for its ``use`` opener. Rust ``use``
# lists run longer than JS import lists (one ``use gpui::{...}`` per
# file, rustfmt-wrapped), so this is wider than
# ``_IMPORT_WINDOW_LINES``; a row past it stays "unexplained".
_USE_WINDOW_LINES = 80


def _looks_like_rust_use_line(
    root: Path, hit: "GrepHit", bare_name: str
) -> bool:
    """Whether ``hit``'s line is part of a Rust ``use`` declaration
    naming ``bare_name``: a whole ``use`` line, or one row of a
    multi-line ``use a::{...};`` list.

    Rust has no entry in ``_IMPORT_LINE_TEMPLATES``, and the multi-line
    member check only knows JS/TS ``import {`` blocks, so every Rust
    import fell through to "unexplained". A ``use`` declaration can't
    contain a call, so labelling one an import can't hide a missed
    call. Checked against tree-sitter over every line of zed's 1,923
    Rust files: no ``use`` line missed; the only lines wrongly claimed
    were ``use`` text inside string literals and macro bodies, which
    aren't calls either.

    A continuation row is claimed only if every line between it and
    the nearest ``use`` opener above is itself a bare name list, and no
    line in between (or the opener) closes the declaration with ``;``.
    """
    if _grammar_for_path(hit.path) != "rust":
        return False
    stripped = hit.snippet.strip()
    if not re.search(rf"\b{re.escape(bare_name)}\b", stripped):
        return False
    if _USE_STATEMENT.match(stripped):
        return True
    if not _BARE_NAME_LIST_LINE.match(stripped):
        return False
    lines = read_lines(root, hit.path)
    start = max(0, hit.line - 1 - _USE_WINDOW_LINES)
    for above in reversed(lines[start : hit.line - 1]):
        text = above.strip()
        if _USE_STATEMENT.match(text):
            return not text.endswith(";")
        if not _BARE_NAME_LIST_LINE.match(text) or text.endswith(";"):
            return False
    return False


# ``_looks_like_import_statement`` only catches the single-line ``import
# { X } from "...";`` shape -- _ESM_NAMED_IMPORT_TEMPLATE is anchored at
# line start and requires ``import``/``{``/``from`` all on the matched
# line. A multi-line destructured import (``import {\n  X,\n  Y,\n} from
# "...";``) puts the bare-name hit on a line containing only ``  X,`` --
# none of those tokens are on that line, so the anchored regex never
# matches and it fell through to CAUSE_UNEXPLAINED. This was the
# dominant "grep-only" shape in claude-buddy (6 of 8 flagged rows), not
# the edge case.
#
# Both halves were later widened. The opener also accepts ``export {`` /
# ``export type {`` (a barrel's re-export list -- on cline, those rows
# fell through to CAUSE_GENERIC_NAME, telling an agent a specific
# 30-character identifier was "a generic name") and a leading default
# binding (``import React, {``). And the member line no longer has to be
# exactly one name: claude-buddy packs several per line (``searchBuddy,
# renderBuddy, SPECIES,`` / ``type Species, type Rarity,``), which is
# why two one-name-per-line repos could not reproduce the gap.
_IMPORT_OPEN_BRACE = re.compile(
    r"^\s*(?:import|export)\s+(?:type\s+)?(?:[\w$]+\s*,\s*)?\{"
)
# One line of an import/export specifier list: only specifiers
# (optional ``type`` modifier, optional ``as`` alias) and commas.
# Anything call- or expression-shaped fails it, which is what keeps a
# widened member check from swallowing real references.
_IMPORT_MEMBER_LINE = re.compile(
    r"^\s*(?:(?:type\s+)?[\w$]+(?:\s+as\s+[\w$]+)?\s*(?:,\s*|$))+$"
)
# How many lines above a bare-name hit to scan for an unclosed
# ``import {`` block opener -- generous enough for a real multi-line
# destructured import list (which rarely runs past a couple dozen
# names) without scanning the whole file.
_IMPORT_WINDOW_LINES = 20


def _looks_like_multiline_import_member(
    root: Path, hit: "GrepHit", bare_name: str
) -> bool:
    """Whether ``hit``'s line is a specifier-list line (one or more
    ``name,`` members) inside a multi-line ``import { ... } from
    "...";`` or ``export { ... }`` block --
    ``_looks_like_import_statement`` only catches the single-line shape
    (on claude-buddy, 6 of 8 flagged rows are this multi-line shape, the
    dominant style there). Reads a small window of the hit's own file
    around its line -- the only file re-read this module does, kept
    small and best-effort (any read/decode failure returns ``False``,
    same fallback as the rest of this module's parsing).

    Scans backward from the hit line (nearest line first) for the
    first brace-relevant line -- an ``import {`` opener or a line
    containing ``}`` -- and answers based on *that* line alone, not
    "any opener/any closer anywhere in the window" (a flat any()/any()
    scan let an unrelated earlier import's closing ``}`` falsely "close"
    a still-open block sitting directly above the hit, as soon as *any*
    window line happened to contain a ``}`` regardless of which opener
    it actually belonged to). A ``}``-bearing line is checked before an
    opener match on the *same* line so a complete single-line import
    (``import { X } from 'y';``, which matches both patterns) is
    correctly treated as closed, not as a dangling opener.
    """
    if not _IMPORT_MEMBER_LINE.match(hit.snippet):
        return False  # not a specifier-list line at all -- cheap bail-out
    if not re.search(
        rf"(?<![\w$]){re.escape(bare_name)}(?![\w$])", hit.snippet
    ):
        return False
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
    above any one of their definitions).

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
# every Tier-2 grammar (languages.TIER2_GRAMMARS's values), plus the
# names ``_comment_style_for_path`` falls back to for a file dekko
# does not parse.
#
# Vue and Svelte are deliberately left unmapped: both are
# mixed-content SFC formats (an HTML-ish template plus embedded
# script/style blocks, each with its own comment convention), and a
# single-line-start prefix check has no way to know which embedded
# language a hit's line belongs to. ``_looks_like_comment_line`` falls
# back to False for them, a false negative (heuristic doesn't fire)
# never a false positive.
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
    # Not grammars dekko parses: the names ``_comment_style_for_path``
    # falls back to for a Groovy or Mojo file, a Gradle build script
    # or an OCaml interface file. A comment there is still a comment
    # to ``sanity --unused``.
    "groovy": _SLASH_STYLE,
    "gradle": _SLASH_STYLE,
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
    for anything ``is_supported()`` also rejects.
    """
    spec = languages.spec_for_path(path)
    if spec is not None:
        return spec.grammar
    return languages.tier2_grammar_for_path(path)


# Extensions in no registry whose comment syntax is known anyway. An
# OCaml interface file is not indexed, because it restates what its
# ``.ml`` defines, but a comment line in one is still a comment.
_UNREGISTERED_COMMENT_STYLES: dict[str, str] = {
    ".mli": "ocaml_interface",
}


def _comment_style_for_path(path: str) -> str:
    """The ``_COMMENT_PREFIXES_BY_GRAMMAR`` key for ``path``, or ``""``.

    ``_grammar_for_path`` first. A file dekko recognizes and doesn't
    parse has no grammar, so it falls back to the language name its
    registry gives it, then to ``_UNREGISTERED_COMMENT_STYLES``.
    """
    dot = path.rfind(".")
    unregistered = _UNREGISTERED_COMMENT_STYLES.get(path[dot:].lower(), "")
    return (
        _grammar_for_path(path)
        or languages.build_script_language(path)
        or languages.known_unsupported_language(path)
        or unregistered
    )


def _unindexed_cause(path: str) -> str | None:
    """The cause for a hit in a file dekko recognizes and never parses.

    A file fact, decided before any rung that reads the line's shape:
    those rungs guess why the resolver missed a call, and the resolver
    never saw this file. Limited to the two registries of recognized
    files, so a hit in a README keeps the ladder it had.

    Args:
        path: The hit's repo-relative path.

    Returns:
        ``CAUSE_BUILD_SCRIPT``, ``CAUSE_UNSUPPORTED_LANGUAGE``, or
        ``None`` for every other file.
    """
    if languages.build_script_language(path) is not None:
        return CAUSE_BUILD_SCRIPT
    if languages.known_unsupported_language(path) is not None:
        return CAUSE_UNSUPPORTED_LANGUAGE

    return None


def _looks_like_comment_line(snippet: str, path: str) -> bool:
    """Whether ``snippet``, considered alone, has the shape of a
    comment/docstring line in ``path``'s own grammar.

    Still pure/I/O-free -- ``path`` is only ever used as a string to
    resolve a grammar name (``_grammar_for_path``), never opened or
    read. Returns ``False`` for a path whose grammar isn't in
    ``_COMMENT_PREFIXES_BY_GRAMMAR`` (unsupported entirely, or one of
    the deliberately-unmapped Vue/Svelte SFC formats).

    This is the *only* gate on a comment cause (the
    near-definition/header zone now picks the wording, not the verdict),
    so two prefix matches that aren't comments are refused here: a PHP 8
    ``#[Attribute]`` line (``#`` opens a comment in PHP, ``#[`` opens an
    attribute, and an attribute can name a class), and a line whose
    leading ``/* ... */`` closes with real code after it.
    """
    grammar = _comment_style_for_path(path)
    prefixes = _COMMENT_PREFIXES_BY_GRAMMAR.get(grammar)
    if not prefixes:
        return False
    stripped = snippet.strip()
    if not stripped.startswith(prefixes):
        return False
    if grammar == "php" and stripped.startswith("#["):
        return False
    if stripped.startswith("/*") and "*/" in stripped:
        return not stripped.split("*/", 1)[1].strip()

    return True


# Bounded scan depth for the block-comment-continuation check below --
# same "generous but bounded" shape as ``_HEADER_SCAN_LINES``, sized to
# cover a long Javadoc/JSDoc ``@param``/``@return`` run without an
# unbounded read.
_BLOCK_COMMENT_SCAN_LINES = 40


def _looks_like_block_comment_continuation(root: Path, hit: "GrepHit") -> bool:
    """Whether ``hit``'s line is a ``/* ... */`` block-comment
    continuation row (a Javadoc/JSDoc-style `` * text`` line), not real
    code -- on tensorflow, a `` *
    {@link ANeuralNetworksEvent_wait},`` line plainly inside a ``/**
    ... */`` block was labelled ``[unexplained miss]`` because a bare
    ``*`` prefix is deliberately absent from every C-style family in
    ``_COMMENT_PREFIXES_BY_GRAMMAR`` -- see that table's own comment on
    why: a gofmt/rustfmt/clang-format-wrapped ``* Helper(x-1)``
    multiplication/dereference continuation line has the exact same
    shape and can sit right next to a real definition, so a bare prefix
    match alone can't tell the two apart.

    That trap only exists *inside* an already-open comment block,
    though -- real code can open a ``/*`` and leave it unclosed across
    a line boundary, but it can never do so as an ongoing multi-line
    *expression* the way a `` * Helper(x-1)`` continuation implies
    (that reading requires the ``/*`` to already be a comment). So
    this adds the missing evidence a bare prefix check can't see on
    its own -- an unclosed ``/*`` above the hit, found before any
    ``*/`` -- rather than loosening ``_looks_like_comment_line``'s
    prefix check itself.

    Args:
        root: Repo root, to re-read the hit's own file (same
            "small file re-read, best-effort" pattern as
            ``_in_leading_header_comment``).
        hit: The grep hit to classify.

    Returns:
        ``True`` only when the hit's grammar uses ``/* */`` block
        comments, the stripped line starts with ``*`` but isn't a bare
        ``*/`` close, a ``*=`` compound-assignment, or a ``**``
        (kwargs-unpack/exponent/double-pointer) line, AND a bounded
        backward scan from the hit finds an unclosed ``/*`` before any
        ``*/``. Any read failure, or scanning past the bound without
        finding an opener, returns ``False`` -- best-effort, never a
        guess.
    """
    prefixes = _COMMENT_PREFIXES_BY_GRAMMAR.get(
        _comment_style_for_path(hit.path)
    )
    if not prefixes or "/*" not in prefixes:
        return False
    stripped = hit.snippet.strip()
    if not stripped.startswith("*"):
        return False
    if stripped == "*/" or stripped.startswith(("*=", "**")):
        return False
    try:
        lines = (
            (root / hit.path)
            .read_text(encoding="utf-8", errors="replace")
            .splitlines()
        )
    except OSError:
        return False
    start = max(0, hit.line - 1 - _BLOCK_COMMENT_SCAN_LINES)
    for ln in reversed(lines[start : hit.line - 1]):
        if "*/" in ln:
            return False
        if "/*" in ln:
            return True
    return False


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
    looks_like_type_annotation: bool = False,
    looks_like_local_binding_or_literal: bool = False,
    in_leading_header_comment: bool = False,
    is_known_collision_name: bool = False,
    is_recorded_reference: bool = False,
    looks_like_value_reference: bool = False,
    not_mapped: bool = False,
    looks_like_string_mention: bool = False,
    is_recorded_read: bool = False,
    in_template_text: bool = False,
    in_block_comment: bool = False,
    looks_like_expression_call: bool = False,
    in_type_context: bool = False,
    looks_like_trailing_comment: bool = False,
    looks_like_jsx_text: bool = False,
    looks_like_property_access: bool = False,
    looks_like_signature: bool = False,
    looks_like_type_mention: bool = False,
) -> str:
    """Name the likely cause of one grep-only hit.

    A pure function over one grep-matched line plus its context — no
    repo/grep I/O, so it's directly testable in isolation (the checks
    that need file I/O or a repo-wide symbol-table lookup, such as
    ``_looks_like_multiline_import_member``, are computed by the caller
    and passed in rather than given to this function directly, to keep
    that contract). Checked in the order ``dekko-verify/SKILL.md`` lists
    its blind spots: a qualified-call syntax match is checked first
    (it's visible in the line itself and the most specific signal
    available), then whether the line is a bare import/require statement
    naming the symbol (the dominant "grep-only" shape on any
    import-heavy codebase, same "visible in the line itself" precedence
    as the qualified-call check), then whether the line is a bare member
    of a multi-line destructured import block (the residual gap in the
    single-line check above), then whether the line uses the name in a
    TS/JS type position -- an ``import type`` statement, a
    parameter/variable type annotation, or a generic type argument
    (neither an import statement nor a call, but just as unambiguous
    once matched; see ``_looks_like_type_annotation``), then whether the
    line is an unrelated local variable/parameter declaration or a bare
    string literal equal to the name (a same-bare-name local binding or
    string value in an unrelated file references nothing; see
    ``_looks_like_local_binding_or_literal``), then whether the hit is a
    comment/docstring line either sitting near the symbol's own
    definition or inside its file's uninterrupted leading header comment
    block (a module-header comment naming several exports can sit far
    from any one of their definitions; not a call at all either way),
    then whether the file is in a language dekko can't parse at all,
    then whether it's a test file excluded by ``sanity``'s own default
    filtering, then whether the target name is short/generic enough
    that dekko's count should be read as directional rather than exact. A line
    matching none of these is reported as "unexplained" rather than
    forcing a guess that doesn't fit — avoiding false confidence from
    the classifier itself.

    A later change reordered that in three places, and the paragraph
    above predates it. The comment check now runs right after the import
    checks (above the type and string-literal checks) and fires for
    *any* comment line: the near-definition/header zone only picks
    between ``CAUSE_COMMENT_MENTION`` and ``CAUSE_COMMENT_ELSEWHERE``.
    ``is_recorded_reference`` comes next, exact and index-backed.
    ``looks_like_value_reference``, a shape heuristic, sits low: after
    the unsupported-language check and before the test filter. The two
    rungs that depend on which same-named symbol is the target (a call
    in a file declaring a sibling, a receiver that shows no sign of the
    target's type) left this ladder for ``_apply_target_facts``, so
    both ``sanity`` modes classify a line from the same facts.

    Args:
        snippet: The grep-matched line's text.
        bare_name: The bare identifier being searched for.
        is_test_file: Whether the hit is test code: its path
            (``classify.is_test_path``) or an enclosing symbol the
            extractor flagged ``Symbol.test`` (a Rust inline ``mod
            tests``) -- the same predicate ``MapIndex.without_tests``
            drops callers by. Not ``classify.is_test_file``, which is
            narrower.
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
        looks_like_type_annotation: Whether the hit line uses the name
            in a TS/JS type position rather than as a call or value
            reference (``_looks_like_type_annotation``).
        looks_like_local_binding_or_literal: Whether the hit line is an
            unrelated local variable/parameter declaration naming the
            symbol, or a bare quoted string literal equal to it
            (``_looks_like_local_binding_or_literal``).
        in_leading_header_comment: Whether the hit sits inside an
            uninterrupted comment/blank-line run starting at line 1 of
            its own file (``_in_leading_header_comment``) — the
            module-header-comment shape ``near_own_definition`` alone
            doesn't cover. Independent of, and OR'd with,
            ``near_own_definition`` in the ``CAUSE_COMMENT_MENTION``
            check below; never a guess made from inside this function.
        is_known_collision_name: Whether ``bare_name`` has measurably
            collided 2+ ways somewhere in this repo's own call graph
            (``ambiguous.collision_names``) -- an additive signal to
            ``_is_generic_name`` alongside its curated word list,
            computed once per ``run()``/``run_all()`` invocation by the
            caller.
        is_recorded_reference: Whether the map holds this hit's exact
            ``(path, line)`` as a value-reference edge to the target
            (``_reference_sites``). An index fact computed by the
            caller, never a guess made from inside this function.
        looks_like_value_reference: Whether the hit line has the shape
            of the name used as a value in a language dekko records no
            reference edges for (``_looks_like_value_reference``).
            Callers leave it ``False`` for a type target.
        not_mapped: Whether the hit's file is in a supported language
            but absent from the map (skipped or excluded). An index
            fact computed by the caller, and checked before every
            other rung: the shape rungs explain a resolver miss, and
            the resolver never saw this file.
        looks_like_string_mention: Whether a JS/TS, Java or Kotlin
            line names the target only inside string or template text
            (``_js_shapes``, ``_looks_like_jvm_string_mention``).
            Checked after the type rung.
        is_recorded_read: Whether the map records a property read of
            the name at this JS/TS line and the line has no bare call
            of it (``_js_shapes``). Checked after the string rung.
        in_template_text: Whether the JS/TS line starts inside a
            template literal opened above and the name is gone once
            that text is blanked (``_js_line_state``). Checked right
            after ``not_mapped``: the line is string text whatever
            its own shape says, and prompt prose and generated-code
            templates hold ``name(`` all the time, so the
            ``eval("name()")`` exception does not apply.
        in_block_comment: Whether the JS/TS line starts inside a
            ``/* */`` block opened above and the name is gone once the
            comment text is blanked. Checked with ``in_template_text``.
        looks_like_expression_call: Whether the line calls the name
            on an expression result or as a chain continuation
            (``_EXPRESSION_CALL_TEMPLATE``). Same cause and blind spot
            as the identifier-receiver form; checked after the import
            rungs.
        in_type_context: Whether the hit's innermost enclosing symbol
            is an interface, type alias or enum (``_enclosing_chain``).
            An index fact; checked after the recorded-reference rung
            (a fact about the *target* beats one about the line) and
            below the comment rungs (a comment inside an interface body
            still reads as a comment).
        looks_like_trailing_comment: Whether the name appears only in
            a comment that follows code on the line
            (``_looks_like_trailing_comment``). Checked after the type
            rung, before the string rung.
        looks_like_jsx_text: Whether the name sits only in JSX text
            content (``_looks_like_jsx_text``). Checked after the
            string rung.
        looks_like_property_access: Whether the line has the ``x.name``
            shape with no call and the map records no read there
            (``_PROPERTY_ACCESS_TEMPLATE``). Checked after the recorded
            read.
        looks_like_signature: Whether the line is an abstract, overload
            or interface-member signature (``_looks_like_signature``).
            Checked before the local-binding rung.
        looks_like_type_mention: Whether a Java/Kotlin line names a
            type target with no construction-shaped occurrence of it
            (``_looks_like_jvm_type_mention``). Checked with the type
            annotation rung.

    Returns:
        One of the ``CAUSE_*`` constants.
    """
    # First: every rung below reads the line's shape to guess why the
    # resolver missed it, and a file the map never parsed was never in
    # front of the resolver. "Qualified call -- resolver blind spot"
    # would be false there.
    if not_mapped:
        return CAUSE_NOT_MAPPED
    # Second: a line that starts inside a template literal or a block
    # comment opened above has no shape of its own to read.
    if in_template_text:
        return CAUSE_STRING_MENTION
    if in_block_comment:
        return CAUSE_COMMENT_ELSEWHERE
    binding = _binding_cause(
        snippet,
        bare_name,
        looks_like_import_member=looks_like_import_member,
        looks_like_expression_call=looks_like_expression_call,
    )
    if binding is not None:
        return binding
    # The comment test sits above the type and
    # string-literal checks. A comment is a comment whatever it quotes
    # (``# ... "tfrun" commands ...`` used to get the literal label).
    # It stays below the anchored qualified-call/import checks, which
    # can't match prose.
    if looks_like_comment:
        if near_own_definition or in_leading_header_comment:
            return CAUSE_COMMENT_MENTION
        return CAUSE_COMMENT_ELSEWHERE
    non_call = _non_call_cause(
        is_recorded_reference=is_recorded_reference,
        in_type_context=in_type_context,
        looks_like_type_annotation=looks_like_type_annotation,
        looks_like_type_mention=looks_like_type_mention,
        looks_like_trailing_comment=looks_like_trailing_comment,
        looks_like_string_mention=looks_like_string_mention,
        looks_like_jsx_text=looks_like_jsx_text,
        is_recorded_read=is_recorded_read,
        looks_like_property_access=looks_like_property_access,
        looks_like_signature=looks_like_signature,
        looks_like_local_binding_or_literal=(
            looks_like_local_binding_or_literal
        ),
    )
    if non_call is not None:
        return non_call
    return _classify_miss_remaining(
        bare_name,
        is_test_file=is_test_file,
        unsupported_language=unsupported_language,
        tests_excluded=tests_excluded,
        looks_like_value_reference=looks_like_value_reference,
        is_known_collision_name=is_known_collision_name,
    )


def _binding_cause(
    snippet: str,
    bare_name: str,
    *,
    looks_like_import_member: bool,
    looks_like_expression_call: bool,
) -> str | None:
    """``classify_miss``'s call-and-import rungs: a qualified call, an
    import/require or re-export statement, a member line of a
    multi-line import or export list, a call on an expression result.
    Split out to keep ``classify_miss`` under the complexity ceiling.
    """
    if _looks_qualified_call(snippet, bare_name):
        return CAUSE_QUALIFIED_CALL
    if _looks_like_import_statement(snippet, bare_name):
        return CAUSE_IMPORT_STATEMENT
    if looks_like_import_member:
        return CAUSE_IMPORT_STATEMENT
    if looks_like_expression_call:
        return CAUSE_QUALIFIED_CALL
    return None


def _non_call_cause(
    *,
    is_recorded_reference: bool,
    in_type_context: bool,
    looks_like_type_annotation: bool,
    looks_like_type_mention: bool,
    looks_like_trailing_comment: bool,
    looks_like_string_mention: bool,
    looks_like_jsx_text: bool,
    is_recorded_read: bool,
    looks_like_property_access: bool,
    looks_like_signature: bool,
    looks_like_local_binding_or_literal: bool,
) -> str | None:
    """``classify_miss``'s middle rungs: the line names the target
    without calling it. Index facts first, then line shapes; ``None``
    when none holds. Split out to keep ``classify_miss`` under the
    complexity ceiling.
    """
    if is_recorded_reference:
        return CAUSE_VALUE_REFERENCE
    if in_type_context:
        return CAUSE_TYPE_CONTEXT
    if looks_like_type_annotation:
        return CAUSE_TYPE_ANNOTATION
    if looks_like_type_mention:
        return CAUSE_TYPE_MENTION
    if looks_like_trailing_comment:
        return CAUSE_TRAILING_COMMENT
    if looks_like_string_mention:
        return CAUSE_STRING_MENTION
    if looks_like_jsx_text:
        return CAUSE_JSX_TEXT
    return _member_shape_cause(
        is_recorded_read=is_recorded_read,
        looks_like_property_access=looks_like_property_access,
        looks_like_signature=looks_like_signature,
        looks_like_local_binding_or_literal=(
            looks_like_local_binding_or_literal
        ),
    )


def _member_shape_cause(
    *,
    is_recorded_read: bool,
    looks_like_property_access: bool,
    looks_like_signature: bool,
    looks_like_local_binding_or_literal: bool,
) -> str | None:
    """The tail of ``_non_call_cause``: a recorded property read, its
    shape twin, a signature line, a declaration or literal."""
    if is_recorded_read:
        return CAUSE_PROPERTY_READ
    if looks_like_property_access:
        return CAUSE_PROPERTY_ACCESS_SHAPE
    if looks_like_signature:
        return CAUSE_SIGNATURE
    if looks_like_local_binding_or_literal:
        return CAUSE_LOCAL_BINDING_OR_LITERAL
    return None


def _classify_miss_remaining(
    bare_name: str,
    *,
    is_test_file: bool,
    unsupported_language: bool,
    tests_excluded: bool,
    looks_like_value_reference: bool,
    is_known_collision_name: bool = False,
) -> str:
    """The back half of ``classify_miss``'s ladder -- split out purely
    to keep ``classify_miss`` itself under this module's McCabe
    complexity ceiling as the ladder has grown; not a
    separately meaningful unit on its own, so it isn't independently
    documented/tested beyond what ``classify_miss``'s own test suite
    already exercises through the public function.
    """
    if unsupported_language:
        return CAUSE_UNSUPPORTED_LANGUAGE
    if looks_like_value_reference:
        return CAUSE_VALUE_REFERENCE_UNRESOLVED
    if tests_excluded and is_test_file:
        return CAUSE_TEST_FILTER
    if _is_generic_name(bare_name, is_known_collision_name):
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
    cmd = _grep_command(bare_name)
    command_text = " ".join(cmd)
    stdout, error = _run_grep_process(root, cmd)
    if error is not None:
        return GrepSweepResult([], command_text, error)

    return _parse_grep_lines(stdout.splitlines(), command_text)


def _grep_command(bare_name: str) -> list[str]:
    """The single-target sweep's grep command for ``bare_name``."""
    cmd = ["grep", "-rn", "-I", "-w", "-F"]
    for d in _grep_exclude_dirs():
        cmd += ["--exclude-dir", d]
    cmd += ["--", bare_name, "."]
    return cmd


def _run_grep_process(root: Path, cmd: list[str]) -> tuple[str, str | None]:
    """Run one grep under ``root``: ``(stdout, error)``.

    ``error`` is ``None`` on exit 0 or 1 (grep's "no match") and a
    message when grep is missing, times out, or exits with an error.
    """
    try:
        result = subprocess.run(
            cmd,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=_GREP_TIMEOUT,
        )
    except FileNotFoundError:
        return "", "'grep' not found on this system"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "", f"grep sweep failed: {exc}"
    if result.returncode not in (0, 1):
        detail = result.stderr.strip() or f"exit {result.returncode}"
        return "", f"grep sweep failed: {detail}"

    return result.stdout, None


def _parse_grep_lines(
    raw_lines: list[str | None],
    command_text: str,
) -> GrepSweepResult:
    """Turn raw ``path:line:text`` grep lines into a sweep result.

    Applies both safety caps: only the first ``_MAX_GREP_LINES`` raw
    lines count, and a line longer than ``_PATHOLOGICAL_LINE_CHARS`` is
    counted, not kept. ``None`` stands for a line already known to be
    that long (the ``--all`` sweep's in-process rows).
    """
    truncated = len(raw_lines) > _MAX_GREP_LINES
    hits: list[GrepHit] = []
    skipped_pathological = 0
    for raw in raw_lines[:_MAX_GREP_LINES]:
        if raw is None or len(raw) > _PATHOLOGICAL_LINE_CHARS:
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


# ``sanity --all`` once ran ``_run_grep`` per name: one full walk of the
# repo per name, 2,000 walks by default, minutes on a large repo. The
# sweep below walks once to learn which files can hold each name, then
# runs the same whole-word grep over only those files. grep still
# decides every row, and the files go in grep's own walk order, so the
# rows, their order and where the line cap cuts all match ``_run_grep``.

# Bytes of file paths per narrowed grep invocation, well under ARG_MAX.
_GREP_ARG_BYTES = 200_000
_TOKEN = re.compile(rb"[A-Za-z0-9_]+")
_IDENTIFIER = re.compile(rb"[A-Za-z0-9_]+\Z")

# A name's place in one file: ``None`` means hand the file to grep;
# a list holds the rows already matched in-process (long-line files).
_FileRows = list[str | None] | None


@dataclass(frozen=True)
class _SweepPlan:
    """Which files each swept name can match, in grep's walk order.

    Attributes:
        files_by_name: Per name, ``(path, rows)`` in walk order; see
            ``_FileRows``.
        error: ``None``, or why the one walk could not run.
    """

    files_by_name: dict[str, list[tuple[str, _FileRows]]]
    error: str | None = None


def _grep_file_list(root: Path) -> tuple[list[str], str | None]:
    """Every file a per-name grep could print from, in its walk order.

    grep's own traversal with the same excludes and the same ``-I``
    binary rule, listing each text file that has at least one line.
    """
    cmd = ["grep", "-rlI"]
    for d in _grep_exclude_dirs():
        cmd += ["--exclude-dir", d]
    cmd += ["--", "", "."]
    stdout, error = _run_grep_process(root, cmd)
    if error is not None:
        return [], error

    return stdout.split("\n")[:-1], None


def _has_long_line(data: bytes) -> bool:
    # A byte count is never below the decoded character count, so this
    # can only send an extra file in-process, never miss one grep would
    # report as an over-long line.
    if len(data) <= _PATHOLOGICAL_LINE_CHARS - 200:
        return False
    return max(map(len, data.split(b"\n"))) > _PATHOLOGICAL_LINE_CHARS - 200


def _word_patterns(names: list[str]) -> dict[bytes, re.Pattern]:
    """Whole-word patterns for names that are not plain identifiers."""
    return {
        b: re.compile(
            rb"(?<![A-Za-z0-9_])" + re.escape(b) + rb"(?![A-Za-z0-9_])"
        )
        for b in (n.encode() for n in names)
        if not _IDENTIFIER.match(b)
    }


def _names_in(
    text: bytes,
    plain: frozenset[bytes],
    odd: dict[bytes, re.Pattern],
    *,
    whole_word: bool,
) -> set[bytes]:
    hit = set(_TOKEN.findall(text)) & plain
    for b, rx in odd.items():
        if rx.search(text) if whole_word else b in text:
            hit.add(b)
    return hit


def _long_file_rows(
    path: str,
    data: bytes,
    plain: frozenset[bytes],
    odd: dict[bytes, re.Pattern],
) -> dict[bytes, list[str | None]]:
    """Match a file with an over-long line in-process, once for all names.

    Each matched line becomes the raw line grep would print, split the
    way ``_run_grep`` splits grep's output; a piece past the
    pathological cap is kept as ``None`` so it is counted, not stored.
    """
    rows: dict[bytes, list[str | None]] = defaultdict(list)
    for number, line in enumerate(data.split(b"\n"), 1):
        hit = _names_in(line, plain, odd, whole_word=True)
        if not hit:
            continue
        raw = f"{path}:{number}:" + line.decode("utf-8", "replace")
        pieces = [
            p if len(p) <= _PATHOLOGICAL_LINE_CHARS else None
            for p in raw.splitlines()
        ]
        for b in hit:
            rows[b].extend(pieces)
    return rows


def _plan_sweep(root: Path, names: list[str]) -> _SweepPlan:
    """Walk once and read each file once to place every name's files.

    The token test can only over-include a file (grep then finds
    nothing in it), never leave out one grep would match.
    """
    files, error = _grep_file_list(root)
    if error is not None:
        return _SweepPlan({}, error)

    plain = frozenset(
        n.encode() for n in names if _IDENTIFIER.match(n.encode())
    )
    odd = _word_patterns(names)
    by_name: dict[str, list[tuple[str, _FileRows]]] = defaultdict(list)
    for path in files:
        try:
            data = (root / path).read_bytes()
        except OSError:
            continue
        if _has_long_line(data):
            for b, rows in _long_file_rows(path, data, plain, odd).items():
                by_name[b.decode()].append((path, rows))
            continue
        for b in _names_in(data, plain, odd, whole_word=False):
            by_name[b.decode()].append((path, None))

    return _SweepPlan(dict(by_name))


def _run_grep_planned(
    root: Path,
    bare_name: str,
    files: list[tuple[str, _FileRows]],
) -> GrepSweepResult:
    """``_run_grep``'s result for ``bare_name``, grepping only ``files``.

    ``command_text`` stays the single-target command, the one a reader
    can rerun. Stops grepping once the line cap is past.
    """
    command_text = " ".join(_grep_command(bare_name))
    raw_lines: list[str | None] = []
    batch: list[str] = []
    size = 0

    def flush() -> str | None:
        nonlocal size
        if not batch:
            return None
        cmd = ["grep", "-nHI", "-w", "-F", "--", bare_name, *batch]
        batch.clear()
        size = 0
        stdout, error = _run_grep_process(root, cmd)
        raw_lines.extend(stdout.splitlines())
        return error

    for path, rows in files:
        if len(raw_lines) > _MAX_GREP_LINES:
            break
        if rows is None:
            batch.append(path)
            size += len(path) + 1
            if size < _GREP_ARG_BYTES:
                continue
        error = flush()
        if error is not None:
            return GrepSweepResult([], command_text, error)
        if rows is not None:
            raw_lines.extend(rows)
    error = flush()
    if error is not None:
        return GrepSweepResult([], command_text, error)

    return _parse_grep_lines(raw_lines, command_text)


def _receiver_mismatch(
    root: Path,
    hit: GrepHit,
    declaring_type: str,
    declaring_path: str | None = None,
) -> bool:
    """Whether nothing in ``hit``'s own line or its file's top-of-file
    import/using block textually mentions ``declaring_type`` — the
    cheap, no-type-inference proxy for "this call's receiver almost
    certainly isn't the target's type" (the target's declaring type is
    recognizably unrelated to the call site's surrounding class/import
    list).

    Deliberately the same cost/precision tier as
    ``_looks_like_multiline_import_member`` — a small, bounded,
    best-effort re-read of the hit's own file, ``False`` on any I/O
    failure — not a real import-resolution pass: no alias tracking, no
    type inference, no wildcard-import handling. It answers "is there
    *any* textual sign," not "is this definitely unrelated": false
    positives here are low-cost and false negatives are the accepted,
    safe-direction failure mode.

    Args:
        root: Repo root, for the one bounded file re-read.
        hit: The grep-only candidate hit being checked.
        declaring_type: The target's declaring type's own simple name
            (the last segment of its container symbol's qualname).
        declaring_path: The declaring type's own file, when known. A
            file never imports its own class, so checking that file's
            import block for ``declaring_type`` always (spuriously)
            comes up empty — this misfired on same-file comments
            referring to the correct target, reporting "likely an
            unrelated external-library method" when the real candidate
            was declared in that exact file. When ``hit.path ==
            declaring_path``, the import-block check is skipped and only
            the hit's own line is checked.

    Returns:
        ``True`` when neither the hit's own line nor the first
        ``_TYPE_REFERENCE_WINDOW_LINES`` lines of its file mention
        ``declaring_type``; ``False`` otherwise (including on any file
        read failure — matches the module's existing
        I/O-failure-is-always-``False`` contract).
    """
    if declaring_type in hit.snippet:
        return False  # the type name is right there on the line
    if declaring_path is not None and hit.path == declaring_path:
        return False  # own declaring file -- imports itself, not a mismatch
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


def _reference_sites(
    index: MapIndex, symbols: list[Symbol]
) -> frozenset[tuple[str, int]]:
    """Every ``(path, line)`` where the map records one of ``symbols``
    used as a value rather than called (tier 1).

    Read off ``MapIndex.referenced_in``/``ref_lines``, the same tables
    ``dekko query uses`` answers from -- but **only edges the
    referencing file could actually have made** (``_can_see``). The
    design called this tier "exact"; measuring it on claude-code said
    otherwise. Reference resolution has no notion of a shadowing
    local, so ``const count = ...; if (count >= 3)`` is recorded as a
    reference to an unrelated ``utils/array.ts::count``: 876 of 1,507
    candidate rows there (58%) sat in files that neither define nor
    import the name. Labelling those "dekko has this as a reference"
    would bless the resolver's mistake from the one command meant to
    catch it. An edge that fails the visibility test contributes
    nothing here, so its row keeps whatever cause it had before.

    Since 0.43.69 the resolver vetoes those edges itself
    (``resolver._ref_target_visible``), so on a fresh map this filter
    rarely removes anything. It stays for maps built by an older
    dekko, and costs one dict lookup per edge.

    Module-scope references are included: their caller is the
    ``path::<module>`` pseudo-id, which never enters ``symbols_by_id``,
    so the path comes off the id itself (same fallback as
    ``mapfile._prod_id``). Empty for any language whose spec has no
    ``reference_query``.
    """
    sites: set[tuple[str, int]] = set()
    for sym in symbols:
        for caller in index.referenced_in.get(sym.id, []):
            caller_sym = index.symbols_by_id.get(caller)
            path = (
                caller_sym.path
                if caller_sym is not None
                else caller.split("::", 1)[0]
            )
            if not _can_see(index, path, sym):
                continue
            sites.update(
                (path, ln) for ln in index.ref_lines.get((caller, sym.id), [])
            )
    return frozenset(sites)


def _caller_path(index: MapIndex, caller: str) -> str:
    """The file a caller id lives in; a ``path::<module>`` pseudo-id
    never enters ``symbols_by_id``, so the path comes off the id."""
    caller_sym = index.symbols_by_id.get(caller)
    if caller_sym is not None:
        return caller_sym.path

    return caller.split("::", 1)[0]


def _attributed_sites(
    index: MapIndex, bare_name: str
) -> dict[tuple[str, int], set[str]]:
    """Every ``(path, line)`` the map attributes a use of ``bare_name``
    to, and to which symbol(s).

    A call site of any symbol sharing the name (``calls_in`` +
    ``edge_lines``, the tables ``query callers`` answers from) or a
    visible value reference of one (``referenced_in`` + ``ref_lines``,
    gated by ``_can_see`` like ``_reference_sites``). Read off the
    same, tests-filtered index the dekko-side callers set is built
    from, so under the default ``--no-tests`` a test-file site is not
    a site here and keeps its test-filter cause.

    Args:
        index: The query index.
        bare_name: The bare name being cross-checked.

    Returns:
        ``(path, line)`` → the ids of the same-named symbols the map
        attributes that line to. Usually one; two when one line calls
        the name twice or a class and its constructor share it.
    """
    sites: dict[tuple[str, int], set[str]] = {}
    for sym in index.symbols_by_name.get(bare_name, []):
        for caller in index.calls_in.get(sym.id, []):
            path = _caller_path(index, caller)
            for ln in index.edge_lines.get((caller, sym.id), []):
                sites.setdefault((path, ln), set()).add(sym.id)
        for caller in index.referenced_in.get(sym.id, []):
            path = _caller_path(index, caller)
            if not _can_see(index, path, sym):
                continue
            for ln in index.ref_lines.get((caller, sym.id), []):
                sites.setdefault((path, ln), set()).add(sym.id)
        # A heritage clause the map resolved to a same-named sibling
        # is attributed to it the same way, so ``extends Name`` on the
        # wrong ``Name`` reads as resolved elsewhere, not as a shape.
        for loc in _heritage_sites(index, sym):
            sites.setdefault(loc, set()).add(sym.id)

    return sites


def _heritage_sites(index: MapIndex, sym: Symbol) -> set[tuple[str, int]]:
    """Every ``(path, line)`` the map records as an ``extends``/
    ``implements`` clause naming ``sym`` (``heritage_in`` +
    ``heritage_lines``, the tables ``query subtypes`` answers from)."""
    sites: set[tuple[str, int]] = set()
    for sub in index.heritage_in.get(sym.id, []):
        path = _caller_path(index, sub)
        sites.update(
            (path, ln) for ln in index.heritage_lines.get((sub, sym.id), [])
        )

    return sites


def _import_bound_to(
    index: MapIndex, path: str, bare_name: str, target_path: str
) -> str | None:
    """Where ``path`` binds ``bare_name`` by import, when that is not
    the target's own file: the module string of an import the map
    could not place in the repo (``module_external``), or the repo
    path a module edge carrying the name leads to
    (``module_edge_names``). ``None`` when the file imports no such
    binding, or only from the target's file or from a barrel that
    re-exports the name out of it: that import is the target's own
    declaration, so it explains nothing and the row stays a miss.
    """
    external = set(index.module_external.get(path, []))
    suffix = "/" + bare_name
    for imp in index.imports_by_path.get(path, []):
        if imp.name != bare_name:
            continue
        module = (
            imp.source[: -len(suffix)]
            if imp.source.endswith(suffix)
            else imp.source
        )
        if module in external:
            return module
    for (importer, imported), names in index.module_edge_names.items():
        if (
            importer == path
            and imported != target_path
            and bare_name in names
            and not _reexported_from(index, imported, bare_name, target_path)
        ):
            return imported

    return None


# How many barrels deep a name is followed, like the resolver's own
# re-export walk.
_BARREL_DEPTH = 8


def _reexported_from(
    index: MapIndex, start: str, bare_name: str, target_path: str
) -> bool:
    """Whether ``start`` passes ``bare_name`` on from ``target_path``.

    Follows module edges out of ``start`` that carry the name or a
    star (``export { X } from``, ``export * from``, and a file's own
    import of the name, which it may export again).

    Args:
        index: The query index.
        start: The repo file an import of the name leads to.
        bare_name: The imported name.
        target_path: The file declaring the symbol being checked.

    Returns:
        True when ``target_path`` is reached within ``_BARREL_DEPTH``
        hops.
    """
    seen = {start}
    frontier = [start]
    for _ in range(_BARREL_DEPTH):
        reached: list[str] = []
        for path in frontier:
            for nxt in index.module_deps_out.get(path, []):
                names = index.module_edge_names.get((path, nxt), [])
                if nxt in seen or not (bare_name in names or "*" in names):
                    continue
                if nxt == target_path:
                    return True
                seen.add(nxt)
                reached.append(nxt)
        frontier = reached
        if not frontier:
            break

    return False


# A bare call of the name: at the line start, after a non-identifier
# character, or after a spread. Not ``x.name(``.
_SELF_CALL_TEMPLATE = r"(?:^|[^.\w]|\.\.\.){name}\s*\("


def _is_self_recursion(
    symbols_by_path: dict[str, list[Symbol]],
    sym: Symbol,
    loc: tuple[str, int],
    snippet: str,
) -> bool:
    """Whether ``loc`` is a bare call of ``sym``'s name inside
    ``sym``'s own span. The resolver records no self edges by design
    (``resolver._record_edge``), so the missing edge is not a miss."""
    path, line = loc
    chain = _enclosing_chain(symbols_by_path.get(path, []), line)
    if not any(s.id == sym.id for s in chain):
        return False
    code = (
        _js_code_only(snippet.strip())
        if _grammar_for_path(path) in _JS_SHAPE_GRAMMARS
        else snippet
    )

    return (
        re.search(_SELF_CALL_TEMPLATE.format(name=re.escape(sym.name)), code)
        is not None
    )


# The trailing rungs the cross-file-collision fact used to outrank in
# the shared ladder; it re-decides only these.
_BELOW_COLLISION_CAUSES = frozenset(
    {CAUSE_TEST_FILTER, CAUSE_GENERIC_NAME, CAUSE_UNEXPLAINED}
)
# The rows the receiver-mismatch fact may re-decide: never a test file's
# own helper (the test filter's) or a same-named local.
_RECEIVER_MISMATCH_CAUSES = frozenset({CAUSE_GENERIC_NAME, CAUSE_UNEXPLAINED})
# The receiver check reads "the file never names the declaring type" as
# "the receiver isn't that type", which holds only where an import
# names a type. A C/C++ ``#include`` names a file, so a real
# ``shape->AddDim(..)`` miss read as a library method; Rust hits were
# closures and locals. Each grammar maps to its family: a hit in
# another family (a Python docstring naming a C++ method) is never a
# call of the target at all, whatever its receiver.
_RECEIVER_FAMILIES = {
    "java": "jvm",
    "kotlin": "jvm",
    "scala": "jvm",
    "typescript": "js",
    "tsx": "js",
    "javascript": "js",
    "python": "python",
    "csharp": "csharp",
}


def _in_own_span(
    symbols_by_path: dict[str, list[Symbol]],
    sym: Symbol,
    loc: tuple[str, int],
) -> bool:
    """Whether ``loc`` sits inside ``sym``'s own span."""
    chain = _enclosing_chain(symbols_by_path.get(loc[0], []), loc[1])
    return any(s.id == sym.id for s in chain)


def _calls_name(snippet: str, bare_name: str) -> bool:
    """Whether the line calls ``bare_name``, bare or on a receiver."""
    return (
        re.search(
            _BARE_CALL_TEMPLATE.format(name=re.escape(bare_name)), snippet
        )
        is not None
    )


def _looks_call_shaped(snippet: str, bare_name: str) -> bool:
    """A qualified or bare call of ``bare_name`` on the line."""
    return (
        _looks_qualified_call(snippet, bare_name)
        or re.search(
            _BARE_CALL_TEMPLATE.format(name=re.escape(bare_name)), snippet
        )
        is not None
    )


def _same_receiver_family(target_path: str, hit_path: str) -> bool:
    """Whether a hit's file is in the target's language family and that
    family's imports name types (``_RECEIVER_FAMILIES``)."""
    family = _RECEIVER_FAMILIES.get(_grammar_for_path(target_path) or "")

    return family is not None and family == _RECEIVER_FAMILIES.get(
        _grammar_for_path(hit_path) or ""
    )


@dataclass(frozen=True)
class _TargetFacts:
    """What the per-target rungs need to know about the target."""

    sym: Symbol
    other_decl_files: frozenset[str]
    declaring_type: str | None


def _target_facts(index: MapIndex, sym: Symbol) -> _TargetFacts:
    """The target-dependent inputs: the files of every *other*
    same-named declaration, and the gated declaring type."""
    return _TargetFacts(
        sym=sym,
        other_decl_files=frozenset(
            s.path
            for s in index.symbols_by_name.get(sym.name, [])
            if s.path != sym.path or s.start_line != sym.start_line
        ),
        declaring_type=_resolve_declaring_type(index, sym),
    )


def _self_cause(
    index: MapIndex, sym: Symbol, loc: tuple[str, int], snippet: str
) -> str | None:
    """A recursive call, or (callable targets) a mention, inside the
    target's own span. A type's span holds methods that are callers of
    their own, so a type gets only the call rung, as before."""
    if _is_self_recursion(index.symbols_by_path, sym, loc, snippet):
        return CAUSE_SELF_RECURSION
    if sym.kind not in TYPE_KINDS and _in_own_span(
        index.symbols_by_path, sym, loc
    ):
        return CAUSE_SELF_MENTION

    return None


def _shape_target_cause(
    root: Path,
    facts: _TargetFacts,
    loc: tuple[str, int],
    snippet: str,
    cause: str,
) -> str | None:
    """The two line-shape rungs that depend on the target: a call in a
    file declaring a same-named sibling, then a method call whose
    receiver shows no sign of the target's declaring type."""
    name = facts.sym.name
    if (
        cause in _BELOW_COLLISION_CAUSES
        and loc[0] in facts.other_decl_files
        and _looks_call_shaped(snippet, name)
    ):
        return CAUSE_CROSS_FILE_COLLISION
    if (
        facts.declaring_type is not None
        and cause in _RECEIVER_MISMATCH_CAUSES
        and _same_receiver_family(facts.sym.path, loc[0])
        and _calls_name(snippet, name)
        and _receiver_mismatch(
            root,
            GrepHit(path=loc[0], line=loc[1], snippet=snippet),
            facts.declaring_type,
            facts.sym.path,
        )
    ):
        return CAUSE_LIKELY_EXTERNAL_COLLISION

    return None


def _apply_target_facts(
    causes: dict[tuple[str, int], str],
    index: MapIndex,
    sym: Symbol | None,
    locs: Iterable[tuple[str, int]],
    snippets: dict[tuple[str, int], str],
    root: Path,
) -> dict[tuple[str, int], str]:
    """The per-target rungs, applied after the resolved-elsewhere
    relabel: a heritage clause naming the target (overrides any shape
    cause), then, on rows the shared ladder left in
    ``_REMAINING_CAUSES``, in order: a recursive call or mention inside
    the target's own body, an import binding the name elsewhere, a
    call in a file declaring a same-named sibling, and a method call
    whose receiver shows no sign of the target's type.

    Per target because the same line is a plain call for a same-named
    sibling; both modes run exactly this. ``sym`` is ``None`` in
    ``--usages`` mode, which has no target symbol and gets nothing.

    Args:
        causes: The classified rows, relabelled in place.
        index: The query index.
        sym: The target symbol, or ``None``.
        locs: The grep-only ``(path, line)`` locations.
        snippets: ``loc`` → the grep line's text.
        root: Repo root, for the receiver check's file read.

    Returns:
        ``loc`` → the import source a row was bound to, for every row
        given ``CAUSE_IMPORT_BOUND_ELSEWHERE`` (``_grep_row``'s
        ``bound_to``).
    """
    bound: dict[tuple[str, int], str] = {}
    if sym is None:
        return bound
    locs = list(locs)
    for loc in _heritage_sites(index, sym):
        if loc in causes and causes[loc] != CAUSE_NOT_MAPPED:
            causes[loc] = CAUSE_HERITAGE
    facts = _target_facts(index, sym)
    for loc in locs:
        cause = causes.get(loc)
        if cause not in _REMAINING_CAUSES:
            continue
        snippet = snippets.get(loc, "")
        own = _self_cause(index, sym, loc, snippet)
        if own is not None:
            causes[loc] = own
            continue
        where = _import_bound_to(index, loc[0], sym.name, sym.path)
        if where is not None:
            causes[loc] = CAUSE_IMPORT_BOUND_ELSEWHERE
            bound[loc] = where
            continue
        shaped = _shape_target_cause(root, facts, loc, snippet, cause)
        if shaped is not None:
            causes[loc] = shaped

    return bound


def _resolved_elsewhere(
    sites: dict[tuple[str, int], set[str]],
    own_id: str,
    locs: Iterable[tuple[str, int]],
) -> dict[tuple[str, int], list[str]]:
    """The grep-only ``locs`` the map attributes to a symbol other than
    ``own_id``, each with the sibling ids it names.

    The target's own call sites are matches and never grep-only, but
    its own *reference* sites are grep-only rows with the tier-1
    value-reference cause, and ``--all`` classifies one shared row
    set per bare name for every symbol sharing it, so this is decided
    per target, after the shared classification, never inside it.

    Args:
        sites: ``_attributed_sites`` for the bare name.
        own_id: The target symbol's id.
        locs: The grep-only ``(path, line)`` locations to check.

    Returns:
        ``loc`` → sorted sibling ids, for every loc that qualifies. A
        row in it takes ``CAUSE_RESOLVED_ELSEWHERE`` over whatever the
        shape ladder said: the ladder's rungs guess why the resolver
        missed the line, and the map says it didn't.
    """
    out: dict[tuple[str, int], list[str]] = {}
    for loc in locs:
        ids = sites.get(loc)
        # A line the map also attributes to the target itself (a value
        # reference of it beside a call of a sibling) keeps the
        # target's own tier-1 reference cause: that is the answer to
        # "why is this not a call of the target", not the sibling.
        if ids and own_id not in ids:
            out[loc] = sorted(ids)

    return out


def _caller_covers(
    index: MapIndex, callers: set[str], loc: tuple[str, int]
) -> bool:
    """Whether one of ``callers`` holds ``loc``: a symbol whose span
    contains the line, or the module pseudo-id of its file."""
    path, line = loc
    for caller in callers:
        caller_sym = index.symbols_by_id.get(caller)
        if caller_sym is None:
            if _caller_path(index, caller) == path:
                return True
        elif (
            caller_sym.path == path
            and caller_sym.start_line <= line <= caller_sym.end_line
        ):
            return True

    return False


def _resolved_elsewhere_causes(
    index: MapIndex,
    sym: Symbol,
    resolved: dict[tuple[str, int], list[str]],
) -> dict[tuple[str, int], str]:
    """The cause for each ``_resolved_elsewhere`` row of ``sym``.

    A construction is recorded on the class and on the overload its
    arguments pick, so for a constructor target the class is not a
    rival declaration. A row the map gave only to the class, from a
    caller holding one of ``sym``'s overload ties, is a tie; a row the
    map gave to the class and another of its constructors went to that
    sibling. Anything else keeps ``CAUSE_RESOLVED_ELSEWHERE``.

    Args:
        index: The query index.
        sym: The target symbol.
        resolved: ``_resolved_elsewhere``'s result for it.

    Returns:
        ``loc`` → cause, for every row in ``resolved``.
    """
    out = dict.fromkeys(resolved, CAUSE_RESOLVED_ELSEWHERE)
    cls = query.constructed_class(index, sym) if resolved else None
    if cls is None:
        return out

    siblings = {c.id for c in query.constructors_of(index, cls)} - {sym.id}
    tie_callers = {caller for caller, _ in index.ambiguous_in.get(sym.id, [])}
    for loc, ids in resolved.items():
        others = set(ids) - {cls.id}
        if cls.id not in ids:
            continue
        if others and others <= siblings:
            out[loc] = CAUSE_SIBLING_CONSTRUCTOR
        elif not others and _caller_covers(index, tie_callers, loc):
            out[loc] = CAUSE_CONSTRUCTOR_TIE

    return out


def _apply_resolved_elsewhere(
    causes: dict[tuple[str, int], str],
    index: MapIndex,
    attributed: dict[tuple[str, int], set[str]],
    sym: Symbol | None,
    locs: Iterable[tuple[str, int]],
) -> dict[tuple[str, int], list[str]]:
    """Relabel the grep-only rows the map attributes elsewhere, in place,
    and return the sibling ids per row for ``_grep_row``'s
    ``resolved_to``. ``sym`` is ``None`` in ``--usages`` mode, which
    has no target and gets nothing."""
    if sym is None:
        return {}
    resolved = _resolved_elsewhere(attributed, sym.id, locs)
    causes.update(_resolved_elsewhere_causes(index, sym, resolved))

    return resolved


@dataclass(frozen=True)
class _NameInputs:
    """The facts about a bare name that both ``sanity`` modes classify
    its grep hits from; see ``_classify_grep_hits`` for each field."""

    own_def_locs: frozenset[tuple[str, int]]
    ref_sites: frozenset[tuple[str, int]]
    target_kinds: frozenset[str]
    read_sites: frozenset[tuple[str, int]]
    is_known_collision_name: bool


def _name_inputs(
    index: MapIndex, bare_name: str, collision_names: frozenset[str]
) -> _NameInputs:
    """Build the shared classification inputs for ``bare_name``.

    One function for ``run()`` and the ``--all`` sweep, so the two
    can't hand the classifier different facts.

    Args:
        index: The query index.
        bare_name: The bare name.
        collision_names: ``ambiguous.collision_names(index)``.

    Returns:
        The inputs.
    """
    symbols = index.symbols_by_name.get(bare_name, [])

    return _NameInputs(
        own_def_locs=frozenset((s.path, s.start_line) for s in symbols),
        ref_sites=_reference_sites(index, symbols),
        target_kinds=_name_kinds(index, bare_name),
        read_sites=_read_sites(index, bare_name),
        is_known_collision_name=bare_name in collision_names,
    )


def _name_kinds(index: MapIndex, bare_name: str) -> frozenset[str]:
    """The kinds of every symbol named ``bare_name``, a type's own
    constructors counted as the type.

    A Java, C++ or Kotlin class and its constructors share a name, so
    without this every class with an explicit constructor was a mixed
    ``{class, method}`` name and every type-shape rule stood down on it.

    Args:
        index: The query index.
        bare_name: The bare name.

    Returns:
        The normalised kind set; empty when no symbol has the name.
    """
    symbols = index.symbols_by_name.get(bare_name, [])
    owner_kind: dict[str, str] = {}
    for sym in symbols:
        if sym.kind in TYPE_KINDS:
            for ctor in query.constructors_of(index, sym):
                owner_kind[ctor.id] = sym.kind

    return frozenset(owner_kind.get(s.id, s.kind) for s in symbols)


def _read_sites(index: MapIndex, bare_name: str) -> frozenset[tuple[str, int]]:
    """Every ``(path, line)`` where the map records a property read of
    ``bare_name`` (``MapIndex.reads_by_name``; JS/TS only).

    The reader's path comes off its symbol, else off the id's
    ``path::`` prefix for a module-level reader, the way
    ``_reference_sites`` takes it.
    """
    sites: set[tuple[str, int]] = set()
    for site in index.reads_by_name.get(bare_name, []):
        reader = index.symbols_by_id.get(site.reader)
        path = (
            reader.path
            if reader is not None
            else site.reader.split("::", 1)[0]
        )
        sites.update((path, ln) for ln in site.lines)
    return frozenset(sites)


# Languages where a sibling file in the same directory shares a
# package/namespace and needs no import to name the target.
_SAME_DIR_PACKAGE_GRAMMARS = frozenset({"go", "java", "kotlin"})


def _can_see(index: MapIndex, path: str, sym: Symbol) -> bool:
    """Whether code in ``path`` could name ``sym`` at all: same file,
    an import binding the symbol's own name or its outermost declaring
    type (``Src`` for ``Src::getSource``), or a same-directory sibling
    in a package-scoped language.

    An index fact (``MapIndex.imports_by_path``), no file I/O. Errs
    toward ``False``: a namespace import (``import * as u``) or a
    Python ``import mod`` doesn't bind the name and isn't credited.
    """
    if path == sym.path:
        return True
    visible = {sym.name, sym.qualname.split(".", 1)[0]}
    if any(imp.name in visible for imp in index.imports_by_path.get(path, [])):
        return True
    return (
        _grammar_for_path(path) in _SAME_DIR_PACKAGE_GRAMMARS
        and Path(path).parent == Path(sym.path).parent
    )


@dataclass(frozen=True)
class _MapScope:
    """What the *unfiltered* map says about a hit's file.

    ``run()`` and ``run_all()`` compare against ``index.without_tests()``
    by default, and that view has already deleted the two facts the
    classifier needs here: which symbols are test code by the
    extractor's flag rather than by path, and which files the map holds
    at all (``without_tests`` also drops test paths from
    ``languages_by_path``). So both are taken from the full index, once
    per run.

    Attributes:
        test_spans: Path -> ``(start_line, end_line)`` of every symbol
            flagged ``Symbol.test`` in a file whose path isn't already
            test code (a Rust inline ``mod tests``). Empty for every
            language whose extractor sets no such flag.
        mapped_paths: Every file the map holds, symbols or not.
    """

    test_spans: dict[str, tuple[tuple[int, int], ...]]
    mapped_paths: frozenset[str]

    def is_test_line(self, path: str, line: int) -> bool:
        """Whether ``path:line`` is test code by path or enclosing span."""
        if is_test_path(path):
            return True

        return any(a <= line <= b for a, b in self.test_spans.get(path, ()))

    def is_unmapped(self, path: str) -> bool:
        """Whether a supported-language ``path`` is absent from the map."""
        return path not in self.mapped_paths and languages.is_supported(path)


def _map_scope(index: MapIndex) -> _MapScope:
    """Build a ``_MapScope`` from the full (test-inclusive) ``index``."""
    spans: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for sym in index.symbols_by_id.values():
        if sym.test and not is_test_path(sym.path):
            spans[sym.path].append((sym.start_line, sym.end_line))

    return _MapScope(
        test_spans={path: tuple(v) for path, v in spans.items()},
        mapped_paths=frozenset(index.languages_by_path),
    )


def _classify_grep_hits(
    hits: list[GrepHit],
    bare_name: str,
    root: Path,
    *,
    own_def_locs: frozenset[tuple[str, int]],
    tests_excluded: bool,
    is_known_collision_name: bool = False,
    ref_sites: frozenset[tuple[str, int]] = frozenset(),
    target_kinds: frozenset[str] = frozenset(),
    symbols_by_path: dict[str, list[Symbol]] | None = None,
    scope: _MapScope | None = None,
    read_sites: frozenset[tuple[str, int]] = frozenset(),
) -> dict[tuple[str, int], str]:
    """Classify every grep hit for ``bare_name`` outside
    ``own_def_locs``, once.

    The shared stage of both modes: every input is a fact about the
    bare name (``_name_inputs``), never about which same-named symbol
    is asked about, so ``run()`` and ``run_all()`` get the same causes
    for the same hits. Whatever depends on the target is decided after
    this, per target, in ``_apply_resolved_elsewhere`` and
    ``_apply_target_facts``. This is the whole point of ``--all``: it
    exists to catch a classification regression, and two diverging
    paths could pass a sweep cleanly on exactly that kind of bug.

    Args:
        hits: Raw grep hits for ``bare_name`` (``sweep.hits``, before
            any dekko-side matching/diffing).
        bare_name: The bare identifier being searched for.
        root: Repo root, for ``_looks_like_multiline_import_member``'s
            one small file re-read.
        own_def_locs: Every same-bare-named symbol's own definition
            line — excluded from classification entirely.
        tests_excluded: Whether the dekko-side query being compared
            against excluded test files by default.
        is_known_collision_name: Whether ``bare_name`` is a member of
            ``ambiguous.collision_names(query_index)``.
        ref_sites: ``_reference_sites`` for every symbol sharing
            ``bare_name``. A hit at one of these locations is
            ``is_recorded_reference``; one that is a sibling's is
            relabelled per target afterwards. Empty by default, and
            always in ``--usages`` mode.
        target_kinds: ``_name_kinds`` for ``bare_name``: a type's own
            constructors count as the type. Gates the shape
            heuristics in opposite directions, both conservatively on
            a mixed group: Rust construction/payload shapes and the
            Java/Kotlin type mention need *every* kind in
            ``TYPE_KINDS``, the value-reference shape needs *none* of
            them to be. Empty (the default, and ``--usages`` mode)
            switches both off.
        symbols_by_path: The query index's symbols per file, for the
            enclosing-symbol and same-named-local checks.
        scope: The unfiltered map's test spans and file set (see
            ``_MapScope``). ``None`` falls back to path-only test
            classification and never reports a file as unmapped.
        read_sites: ``_read_sites`` for ``bare_name``: a JS/TS hit at
            one of these with no bare call is a property read. Empty
            by default, and always in ``--usages`` mode, where a
            ``this.handler`` passed along could be the very reference
            that mode is looking for.

    Returns:
        ``(path, line) -> CAUSE_*`` for every hit not in
        ``own_def_locs``. A caller diffs its own dekko-side hit set
        against this map's keys to get its own matches/dekko-only/
        grep-only split; the map's values are the pre-computed cause
        for every location that turns out to be grep-only.
    """
    target_is_type = bool(target_kinds) and target_kinds <= TYPE_KINDS
    allow_value_shape = bool(target_kinds) and not (target_kinds & TYPE_KINDS)
    causes: dict[tuple[str, int], str] = {}
    for h in hits:
        loc = (h.path, h.line)
        if loc in own_def_locs:
            continue
        unindexed = _unindexed_cause(h.path)
        if unindexed is not None:
            causes[loc] = unindexed
            continue
        looks_like_value = allow_value_shape and _looks_like_value_reference(
            h.snippet, bare_name, h.path
        )
        # No textual "does this file shadow the name" guard any more
        # (``_file_shadows_name``, 0.43.68 to 0.43.69). It switched
        # tier 1 off for a whole file because no regex can tell which
        # scope a line sits in. Since 0.43.70 the extractor can: a
        # shadowing local never becomes an edge, so an edge that is in
        # the map (and passed ``_can_see``) is one to trust.
        is_recorded_reference = loc in ref_sites
        js = _js_shapes(h.snippet, bare_name, h.path, loc in read_sites)
        shapes = _line_shapes(
            root, h, bare_name, target_is_type=target_is_type
        )
        chain = _enclosing_chain(
            symbols_by_path.get(h.path, []) if symbols_by_path else [], h.line
        )
        causes[loc] = classify_miss(
            h.snippet,
            bare_name,
            is_test_file=(
                scope.is_test_line(h.path, h.line)
                if scope is not None
                else is_test_path(h.path)
            ),
            unsupported_language=not languages.is_supported(h.path),
            tests_excluded=tests_excluded,
            near_own_definition=any(
                h.path == p and abs(h.line - ln) <= _COMMENT_PROXIMITY_LINES
                for p, ln in own_def_locs
            ),
            looks_like_comment=(
                _looks_like_comment_line(h.snippet, h.path)
                or _looks_like_block_comment_continuation(root, h)
            ),
            looks_like_import_member=(
                _looks_like_multiline_import_member(root, h, bare_name)
                or _looks_like_rust_use_line(root, h, bare_name)
                or shapes.export_member
            ),
            # ``.map(Prompt::as_str)`` also matches the TS-shaped
            # ``: name`` type template (on the second colon of ``::``)
            # and used to be labelled "type annotation" for a function
            # target. The path-value reading wins.
            looks_like_type_annotation=(
                not looks_like_value
                and (
                    _looks_like_type_annotation(
                        h.snippet,
                        bare_name,
                        h.path,
                        target_is_type=target_is_type,
                    )
                    or shapes.type_shape
                )
            ),
            looks_like_local_binding_or_literal=(
                js.key_or_field
                or _looks_like_local_binding_or_literal(h.snippet, bare_name)
                or shapes.declaration
            ),
            looks_like_type_mention=_looks_like_jvm_type_mention(
                h.snippet, bare_name, h.path, target_is_type=target_is_type
            ),
            looks_like_string_mention=(
                js.string_mention
                or _looks_like_jvm_string_mention(h.snippet, bare_name, h.path)
            ),
            is_recorded_read=js.property_read,
            in_template_text=shapes.template_text,
            in_block_comment=shapes.block_comment,
            looks_like_expression_call=shapes.expression_call,
            in_type_context=bool(chain)
            and chain[0].kind in _TYPE_CONTEXT_KINDS,
            looks_like_trailing_comment=shapes.trailing_comment,
            looks_like_jsx_text=shapes.jsx_text,
            looks_like_property_access=shapes.property_access,
            looks_like_signature=shapes.signature,
            in_leading_header_comment=(
                _looks_like_comment_line(h.snippet, h.path)
                and _in_leading_header_comment(root, h)
            ),
            is_known_collision_name=is_known_collision_name,
            is_recorded_reference=is_recorded_reference,
            looks_like_value_reference=looks_like_value,
            not_mapped=scope is not None and scope.is_unmapped(h.path),
        )
    if symbols_by_path:
        _explain_shadowing_locals(
            causes, hits, bare_name, root, own_def_locs, symbols_by_path
        )
    return causes


# ``(path, line)`` of a grep hit → the line of the same-named local
# that explains it. Filled by ``_explain_shadowing_locals``, read by
# ``_grep_row``. Process-global rather than threaded through the
# half-dozen call layers between them: a hit's location is unique
# within a run and the value is purely presentational.
_shadow_decl_lines: dict[tuple[str, int], int] = {}


# --- tier 2: same-named locals ----------------------------------------

# JS/TS only, like every other shape rule in this module: Python's
# scoping would mostly work too, but the evidence was TS.
_SHADOW_GRAMMARS = frozenset({"typescript", "tsx", "javascript"})
# The shapes that bind a name in JS/TS: a ``const``/``let``/``var``
# (plain or destructured), a ``catch`` parameter, an arrow parameter
# (``name =>``, ``(x, name) =>``, ``({ name }) =>``, ``([a, name]) =>``),
# a ``function`` parameter, a ``for``-of/in binding.
_SHADOW_DECL_TEMPLATE = (
    r"\b(?:const|let|var)\s+(?:{name}\b|[{{\[][^=]*\b{name}\b[^=]*[}}\]]\s*=)"
    r"|\bcatch\s*\(\s*{name}\b"
    r"|(?<![\w.]){name}\s*=>"
    r"|[(,]\s*(?:\.\.\.)?(?:\{{[^}}]*)?\b{name}\b[^)]*\)\s*(?::\s*[^=]+)?=>"
    r"|\bfunction\b[^(]*\([^)]*\b{name}\b"
    r"|\bfor\s*\(\s*(?:const|let|var)\s+(?:[{{\[][^=]*)?\b{name}\b"
)
# How far above a hit with no enclosing symbol the scan may go before
# the first indent-0 line stops it.
_SHADOW_SCAN_LINES = 400
# ``x.name(``: a method call on something else is never a local.
_METHOD_CALL_TEMPLATE = r"[\w)\]]\s*[?!]?\.\s*{name}\s*\("


def _enclosing_chain(symbols: list[Symbol], line: int) -> list[Symbol]:
    """Every symbol whose line range contains ``line``, innermost
    first (by span length; a tie keeps definition order)."""
    return sorted(
        (s for s in symbols if s.start_line <= line <= s.end_line),
        key=lambda s: s.end_line - s.start_line,
    )


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _binds_as_parameter(chain: list[Symbol], bare_name: str) -> Symbol | None:
    """The enclosing symbol whose parameter list binds ``bare_name``:
    a plain parameter, an optional one (``name?``), or a name inside a
    destructured pattern (the extractor stores ``{ slots, cursor }``
    as one parameter whose name is the pattern text)."""
    word = _word(bare_name)
    for s in chain:
        for p in s.params:
            pname = p.name.rstrip("?")
            if pname == bare_name or (
                pname[:1] in ("{", "[") and word.search(pname)
            ):
                return s

    return None


def _shadowing_decl_line(
    root: Path,
    hit: GrepHit,
    bare_name: str,
    chain: list[Symbol],
    own_def_locs: frozenset[tuple[str, int]],
) -> int | None:
    """Line of the same-named binding in scope for ``hit``, or
    ``None``.

    Scope is every enclosing symbol, then the file's top level: the
    scan walks up from the hit to the *outermost* enclosing symbol's
    start (an inner arrow sees the outer function's locals). With no
    enclosing symbol at all (an inline callback that is not an indexed
    symbol, or module-level code inside a block) it stops at the first
    indent-0 line above, after checking it: the enclosing top-level
    statement. Guards, each closing a specific way this could lie:

    - A parameter of an enclosing symbol is index-backed and in scope
      for the whole body: no regex, no indent rule.
    - A declaration's indent must be ``<=`` the hit's: a ``const``
      nested deeper than the use can't be in scope for it. A member
      line of a multi-line destructuring is compared on its opener,
      since members sit deeper than the use.
    - A declaration found by the scan must not be the target's own
      definition line (a nested recursive ``const visit = ...`` matched
      itself in the first probe): the nearest binding is the target,
      so the hit refers to it. A *parameter* declared on that line is
      a fresh binding all the same (``command(command: string)`` and
      ``${command}`` in its body), so the parameter check is not
      guarded. The caller refuses a ``x.name(`` method-call line.
    """
    param_owner = _binds_as_parameter(chain, bare_name)
    if param_owner is not None:
        return param_owner.start_line
    lines = _cached_lines(root, hit.path)
    if not lines or hit.line > len(lines):
        return None
    decl = re.compile(_SHADOW_DECL_TEMPLATE.format(name=re.escape(bare_name)))
    hit_indent = _indent(lines[hit.line - 1])
    lowest = (
        chain[-1].start_line
        if chain
        else max(1, hit.line - _SHADOW_SCAN_LINES)
    )
    for ln in range(hit.line - 1, lowest - 1, -1):
        text = lines[ln - 1]
        if not text.strip():
            continue
        found = _destructuring_opener(lines, ln, bare_name)
        if found is not None and _indent(lines[found - 1]) > hit_indent:
            found = None
        if found is None and _indent(text) <= hit_indent:
            found = ln if decl.search(_js_code_only(text)) else None
        if found is not None:
            return None if (hit.path, found) in own_def_locs else found
        if not chain and _indent(text) == 0:
            return None

    return None


def _explain_shadowing_locals(
    causes: dict[tuple[str, int], str],
    hits: list[GrepHit],
    bare_name: str,
    root: Path,
    own_def_locs: frozenset[tuple[str, int]],
    symbols_by_path: dict[str, list[Symbol]],
) -> None:
    """Upgrade ``CAUSE_UNEXPLAINED`` rows that use a same-named local.

    JS/TS hits whose enclosing scope binds ``bare_name`` above the
    hit (``_shadowing_decl_line``). Never rewrites a row that already
    carries a specific cause. A bare call ``name(`` with a declaration
    of ``name`` in scope calls that binding by the language's own
    rule, and the resolver vetoes exactly this edge on the same
    evidence (the shadowed-local veto), so it is explained too; a
    call-shaped line with no declaration in scope stays a miss, and a
    ``x.name(`` method call is never a local.
    """
    method_call = re.compile(
        _METHOD_CALL_TEMPLATE.format(name=re.escape(bare_name))
    )
    for h in hits:
        loc = (h.path, h.line)
        if causes.get(loc) != CAUSE_UNEXPLAINED:
            continue
        if _grammar_for_path(h.path) not in _SHADOW_GRAMMARS:
            continue
        if method_call.search(h.snippet):
            continue
        chain = _enclosing_chain(symbols_by_path.get(h.path, []), h.line)
        decl_line = _shadowing_decl_line(
            root, h, bare_name, chain, own_def_locs
        )
        if decl_line is not None:
            causes[loc] = CAUSE_SHADOWING_LOCAL
            _shadow_decl_lines[loc] = decl_line


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

    ``sym_target`` is an already-resolved ``Symbol``'s id (see
    ``run()``), which ``query.resolve_target`` looks up before any
    other reading, so it re-resolves to exactly that symbol.

    ``module_level`` entries carry per-site lines when the map
    recorded them; those fold straight into
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
    qualified external call, e.g. ``get`` for ``requests.get(...)``; see
    ``mapfile._callee_base``), never a qualified string. There is
    nothing to extract, since the target grammar ``uses`` already
    accepts is exactly one bare grep-able token. ``module_level_paths``
    is always empty — ``uses`` results carry no
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


def _grep_row(
    hit: GrepHit,
    cause: str | None = None,
    resolved_to: list[str] | None = None,
    bound_to: str | None = None,
) -> dict:
    row = {
        "file": hit.path,
        "line": hit.line,
        "snippet": _cap_snippet(hit.snippet.strip()),
    }
    if cause is not None:
        row["cause"] = cause
    decl = _shadow_decl_lines.get((hit.path, hit.line))
    if decl is not None and cause == CAUSE_SHADOWING_LOCAL:
        row["decl_line"] = decl
    if resolved_to:
        row["resolved_to"] = resolved_to
    if bound_to is not None and cause == CAUSE_IMPORT_BOUND_ELSEWHERE:
        row["bound_to"] = bound_to

    return row


def _fit_rows(
    rows: list[dict], budget: int | None, limit: int
) -> tuple[list[dict], Meter]:
    """Cap one report bucket by row count then token budget.

    Mirrors ``query._fit_entries`` exactly -- including returning the
    full ``Meter``, not just a bare total, so ``sanity --json`` can
    disclose truncation the same way every other budget-capped
    ``--json`` command already does (``sanity --json`` used to silently
    cap its row arrays at ``DEFAULT_REPORT_LIMIT`` with no
    ``meta``/``truncated`` disclosure anywhere in the output, unlike
    ``query --json``'s existing contract).

    Returns:
        ``(kept_rows, meter)``.
    """
    serialized = [json.dumps(r) for r in rows]
    kept, meter = fit_to_budget(serialized, budget, limit)
    return rows[: len(kept)], meter


# Disclosure notes -- printed (text mode) or attached (JSON mode)
# whenever ``_run_grep``'s safety caps actually fired, so a reader isn't
# left to (re-)discover on their own that the sweep was incomplete.
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


def _excluded_declarations_note(count: int) -> str:
    """The banner disclosing declaration lines dropped from the grep
    sweep before bucketing.

    Found independently on all five language families tested: a symbol's
    own declaration line -- and every other same-bare-named symbol's --
    is filtered out of the sweep before the matches/grep-only split,
    because a declaration is not a call site and never was a miss to
    explain. That exclusion is correct, but it used to be *silent*, so
    ``matches + grep-only`` never summed to the hit count of the very
    ``grep:`` command printed one line above it. The gap equalled the
    number of colliding same-bare-name declaration lines, which on an
    overload-heavy repo is never zero, and an agent reconciling the two
    numbers by hand found an unexplained shortfall every time.
    Disclosing the count makes the report self-reconciling: matches +
    grep-only + excluded == the swept hit total.
    """
    plural = "" if count == 1 else "s"
    return (
        f"{count} declaration line{plural} excluded from the buckets "
        "below (the target's own definition and any same-bare-named "
        "symbol's) -- a declaration is not a call site, so it is not a "
        "miss to explain. Counted here so matches + grep-only + "
        "excluded reconciles with the grep command's own hit total."
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
    excluded_declarations: int = 0,
    receiver_mismatch_note: str | None = None,
    receiver_mismatch_declaring_type: str | None = None,
    receiver_mismatch_count: int | None = None,
) -> dict:
    """Assemble ``sanity --json``'s output document.

    ``meta`` mirrors ``query --json``'s existing truncation-disclosure
    contract byte-for-byte (one ``Meter.as_dict()`` per bucket) so a
    consumer already handling ``query``'s ``meta`` shape needs no new
    parsing to detect a capped ``sanity`` bucket (the row arrays used to
    be silently capped at ``DEFAULT_REPORT_LIMIT`` with nothing in the
    JSON disclosing it). ``counts`` is kept exactly as-is alongside
    ``meta`` -- purely additive, so any existing consumer parsing
    ``counts`` keeps working unmodified.

    ``receiver_mismatch_note``/``_declaring_type``/``_count`` are
    present only when ``run()``'s receiver-mismatch heuristic actually
    flagged at least one grep-only hit (mirrors ``dekko_only_note``'s
    "only present when relevant" contract).
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
            # Without this, matches + grep_only silently
            # failed to sum to the printed grep command's own hit
            # count -- see ``_excluded_declarations_note``.
            "excluded_declarations": excluded_declarations,
            "grep_hits_swept": (
                matches_meter.total
                + grep_only_meter.total
                + excluded_declarations
            ),
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
    if excluded_declarations:
        doc["excluded_declarations_note"] = _excluded_declarations_note(
            excluded_declarations
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
            cause = row["cause"]
            if "decl_line" in row:
                cause = f"{cause} (declared at line {row['decl_line']})"
            if "resolved_to" in row:
                targets = ", ".join(row["resolved_to"])
                cause = f"{cause} (resolved to {targets})"
            if "bound_to" in row:
                cause = f"{cause} (bound to {row['bound_to']})"
            print(f"    {loc}  [{cause}]")
            print(f"      {row['snippet']}")
        else:
            print(f"    {loc}")
    if total > len(rows):
        print(f"    ... +{total - len(rows)} more")


def _group_grep_only_by_file(
    rows: list[dict],
) -> list[tuple[str, int, Counter[str]]]:
    """Group a bucket's *full* row set by file, largest cluster first.

    Grouping must run over every row the sweep found, not whatever
    survived ``--limit``/``--budget`` fitting first. On zed, ``sanity
    ... --group-by-file`` with the default ``--limit 200`` never showed
    either of the two largest real clusters (139 hits in ``shadow.rs``,
    125 in ``list.rs``) because neither file's individual rows made it
    into the first 200 -- exactly the clustering the flag exists to
    surface. Callers cap the *groups* this returns, not the rows that
    went into them.

    Args:
        rows: Every row in the bucket, unfitted.

    Returns:
        ``(file, file_total, causes)`` tuples, largest ``file_total``
        first; ties keep the files' first-appearance order (stable
        sort over dict-insertion order).
    """
    by_file: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        by_file[row["file"]][row.get("cause", "(no cause)")] += 1
    groups = [
        (file, sum(causes.values()), causes)
        for file, causes in by_file.items()
    ]
    groups.sort(key=lambda g: g[1], reverse=True)
    return groups


def _fit_file_groups(
    groups: list[tuple[str, int, Counter[str]]],
    budget: int | None,
    limit: int,
) -> tuple[list[tuple[str, int, Counter[str]]], Meter]:
    """Cap file groups by count then token budget.

    Mirrors ``_fit_rows``, applied to the per-file group summaries
    ``--group-by-file`` prints instead of individual rows -- so
    ``--limit``/``--budget`` bound how many *files* are shown, the
    same knob the rest of the report already uses, not a second
    row-count cap layered on top of grouping.

    Args:
        groups: Every file group, largest cluster first (see
            ``_group_grep_only_by_file``).
        budget: Approximate token budget for the printed groups, or
            ``None`` for unbounded.
        limit: Maximum number of groups to keep.

    Returns:
        ``(kept_groups, meter)`` -- ``meter.total`` is the group
        count, not the row count, so its footer speaks in groups.
    """
    serialized = [
        json.dumps({"file": f, "count": c, "causes": dict(causes)})
        for f, c, causes in groups
    ]
    kept, meter = fit_to_budget(serialized, budget, limit)
    return groups[: len(kept)], meter


def _print_bucket_by_file(
    title: str,
    rows: list[dict],
    *,
    budget: int | None,
    limit: int,
) -> None:
    """Roll up a bucket's full row set by file, largest cluster first.

    Groups ``rows`` in full, then applies ``--limit``/``--budget`` to
    the number of *file groups* printed -- see
    ``_group_grep_only_by_file`` and ``_fit_file_groups`` for why
    (grouping over an already row-truncated bucket hid the very
    clustering this flag exists to show).

    Args:
        title: Bucket label (``"grep-only"``).
        rows: The bucket's full, unfitted rows -- every hit the sweep
            classified into this bucket, not just the rows that would
            survive ``--limit``/``--budget`` applied to rows directly.
        budget: Token budget applied to the printed groups.
        limit: Maximum number of file groups to print.
    """
    print(f"  {title}: {len(rows)} (grouped by file)")
    groups = _group_grep_only_by_file(rows)
    kept_groups, meter = _fit_file_groups(groups, budget, limit)
    for file, file_total, causes in kept_groups:
        print(f"    {file}: {file_total}")
        for cause, count in causes.most_common():
            marker = "   <-- look here" if cause == CAUSE_UNEXPLAINED else ""
            print(f"      {count:>4}  {cause}{marker}")
    if meter.omitted:
        plural = "" if meter.omitted == 1 else "s"
        print(
            f"    ... +{meter.omitted} more file group{plural} "
            "(outside --limit/budget)"
        )


def _print_text(
    action: str,
    target: str,
    bare_name: str,
    grep_command: str,
    matches: tuple[list[dict], Meter],
    dekko_only: tuple[list[dict], Meter],
    grep_only: tuple[list[dict], Meter],
    grep_only_rows: list[dict],
    module_level: list[str],
    *,
    grep_truncated: bool = False,
    skipped_pathological: int = 0,
    excluded_declarations: int = 0,
    receiver_mismatch_note: str | None = None,
    group_by_file: bool = False,
    budget: int | None = None,
    limit: int = DEFAULT_REPORT_LIMIT,
) -> None:
    """Render ``run()``'s text report.

    Args:
        grep_only: The grep-only bucket, already fit to
            ``--limit``/``--budget`` by row count -- used for the flat
            (non-grouped) rendering, unchanged from before grouping
            learned to run over the full row set.
        grep_only_rows: The grep-only bucket's *full*, unfitted rows --
            used only when ``group_by_file`` is set, so grouping runs
            over every hit before ``--limit``/``--budget`` caps the
            number of file groups instead of the number of rows (see
            ``_print_bucket_by_file``).
    """
    print(f"dekko sanity: '{target}' ({action}) vs. grep '{bare_name}'")
    print(f"  grep: {grep_command}")
    if grep_truncated:
        print(f"  note: {_TRUNCATION_NOTE}")
    if skipped_pathological:
        print(f"  note: {_pathological_skip_note(skipped_pathological)}")
    if excluded_declarations:
        print(f"  note: {_excluded_declarations_note(excluded_declarations)}")
    if receiver_mismatch_note:
        print(f"  note: {receiver_mismatch_note}")
    _print_bucket_text("matches", *matches)
    if grep_truncated:
        print("  dekko-only: inconclusive (grep sweep truncated)")
    else:
        _print_bucket_text("dekko-only", *dekko_only)
    if group_by_file:
        _print_bucket_by_file(
            "grep-only", grep_only_rows, budget=budget, limit=limit
        )
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
    excluded_declarations: int = 0,
    property_reads: list[ReadSite] | None = None,
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
        "flagged_by_unused": True,
        "unused_status": unused_mod.STATUS_FLAGGED,
        "grep_command": grep_command,
        "grep_truncated": sweep.truncated,
        "grep_skipped_pathological": sweep.skipped_pathological,
        "reference_hits": rows,
        "counts": {
            "reference_hits": meter.total,
            "filtered_noise": noise_count,
            # Same silent-exclusion gap the callers/uses path had -- see
            # ``_excluded_declarations_note``.
            "excluded_declarations": excluded_declarations,
            "grep_hits_swept": (
                meter.total + noise_count + excluded_declarations
            ),
        },
        "meta": {"reference_hits": meter.as_dict()},
        "generic_name_caution": generic_name_caution,
    }
    if excluded_declarations:
        doc["excluded_declarations_note"] = _excluded_declarations_note(
            excluded_declarations
        )
    if sweep.truncated:
        doc["reference_hits_note"] = _TRUNCATION_NOTE
    if sweep.skipped_pathological:
        doc["grep_skipped_pathological_note"] = _pathological_skip_note(
            sweep.skipped_pathological
        )
    if property_reads:
        doc["property_reads"] = {
            "count": sum(len(s.lines) for s in property_reads),
            "sites": _property_read_locations(property_reads),
            "note": _property_reads_note(property_reads),
        }
    return doc


# How many property-read sites ``sanity --unused`` names in its note
# before saying "and N more".
_PROPERTY_READ_EXAMPLES = 3


def _property_read_locations(sites: list[ReadSite]) -> list[str]:
    """``path:line`` for the first few read sites, file order."""
    out: list[str] = []
    for site in sites:
        path = site.reader.split("::", 1)[0]
        out.extend(f"{path}:{line}" for line in site.lines)
        if len(out) >= _PROPERTY_READ_EXAMPLES:
            break
    return out[:_PROPERTY_READ_EXAMPLES]


def _property_reads_note(sites: list[ReadSite]) -> str:
    """The one line that says a flagged name is read as a property.

    The read is why ``unused`` marks the row ``[dispatch?]``, and it
    is not a call: a getter or handler reached through the object it
    belongs to. Said here so the grep hits below, which the buckets
    can only label ``[other]``, have an explanation next to them.
    """
    total = sum(len(s.lines) for s in sites)
    shown = ", ".join(_property_read_locations(sites))
    more = total - min(total, _PROPERTY_READ_EXAMPLES)
    tail = f" and {more} more" if more > 0 else ""
    return (
        f"dekko evidence (property reads): {total} site(s) read this "
        f"name as a property, e.g. {shown}{tail} -- a read, not a "
        "call: dispatch evidence only, which is why unused marks the "
        "row [dispatch?], never proof it is alive"
    )


def _print_unused_evidence(
    has_evidence: bool, property_reads: list[ReadSite] | None
) -> None:
    """The "what dekko itself knows" lines of the ``--unused`` report."""
    evidence = (
        "none -- this is why it was flagged" if not has_evidence else "present"
    )
    print(f"  dekko evidence (calls_in/referenced_in): {evidence}")
    if property_reads:
        print(f"  {_property_reads_note(property_reads)}")


def _not_flagged_reason(
    status: unused_mod.UnusedStatus,
    sym: Symbol,
    fan_in: int,
    referenced_by: int,
) -> str:
    """Say, in words, why ``dekko unused`` doesn't list ``sym``."""
    if status.reason == unused_mod.STATUS_CALL_BLIND:
        return (
            f"dekko extracts no calls for {sym.language}, so `dekko "
            "unused` does not evaluate its symbols at all"
        )
    if status.reason == unused_mod.STATUS_ROOT:
        return (
            "it is a root, which is never dead code by definition "
            "(exported, decorated, `main`, a test, re-exported, a "
            "std-trait impl, an object-literal member handed to an "
            "external call, or matched by --roots)"
        )
    if fan_in or referenced_by:
        return f"it is in use (fan-in {fan_in}, referenced-by {referenced_by})"

    return (
        "it is kept alive by evidence other than a direct call: a used "
        "member, a subtype, a type-position use, or an overload sharing "
        "its qualified name"
    )


_NOT_FLAGGED_ADVICE = (
    "--unused cross-checks a symbol `dekko unused` reported; for a low "
    "or surprising caller count use: dekko sanity <target>"
)


def _report_not_flagged(
    sym: Symbol,
    status: unused_mod.UnusedStatus,
    index: MapIndex,
    as_json: bool,
) -> int:
    """``sanity --unused`` on a symbol ``dekko unused`` never flagged.

    Reproduced on all seven test repos: this mode used to run its
    full grep sweep for any symbol and close with "flagged unused, but
    N call-shaped references found (possible resolver miss)", for
    ``SpringApplication.run`` (4,861 grep hits) as readily as for real
    dead code. The premise was never checked, and the sweep's volume
    is what made the false report look authoritative. So there is no
    sweep here: no flag means no verdict to cross-check, and saying so
    takes three lines and no grep.
    """
    fan_in = len(index.calls_in.get(sym.id) or [])
    referenced_by = len(index.referenced_in.get(sym.id) or [])
    reason = _not_flagged_reason(status, sym, fan_in, referenced_by)
    if as_json:
        empty = Meter(tokens=0, returned=0, total=0)
        doc = {
            "action": "sanity",
            "query_action": "unused",
            "target": f"{sym.path}:{sym.qualname}:{sym.start_line}",
            "bare_name": sym.name,
            "has_dekko_evidence": bool(fan_in or referenced_by),
            "flagged_by_unused": False,
            "unused_status": status.reason,
            "skipped": f"not flagged by dekko unused: {reason}",
            "advice": _NOT_FLAGGED_ADVICE,
            "grep_command": None,
            "grep_truncated": False,
            "grep_skipped_pathological": 0,
            "reference_hits": [],
            "counts": {
                "reference_hits": 0,
                "filtered_noise": 0,
                "excluded_declarations": 0,
                "grep_hits_swept": 0,
            },
            "meta": {"reference_hits": empty.as_dict()},
            "generic_name_caution": False,
        }
        print(json.dumps(doc, indent=2))
        return EXIT_OK

    print(f"dekko sanity --unused '{sym.name}' ({sym.path}:{sym.start_line})")
    print(f"  not flagged by `dekko unused`: {reason}")
    print(f"  no grep sweep run -- {_NOT_FLAGGED_ADVICE}")
    return EXIT_OK


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
    excluded_declarations: int = 0,
    property_reads: list[ReadSite] | None = None,
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
    if excluded_declarations:
        print(f"  note: {_excluded_declarations_note(excluded_declarations)}")
    # Only a symbol `dekko unused` really lists reaches this printer
    # (see ``_report_not_flagged``), so "flagged" below is a checked
    # fact, no longer an assumption. Evidence can still be "present"
    # here: an overload's callers are keyed to a sibling's id.
    _print_unused_evidence(has_evidence, property_reads)
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
    root_globs: tuple[str, ...] = (),
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

    status = unused_mod.unused_status(index, sym, root_globs)
    if not status.flagged:
        return _report_not_flagged(sym, status, index, as_json)

    bare_name = sym.name

    own_def_locs = frozenset(
        (s.path, s.start_line) for s in index.symbols_by_name.get(sym.name, [])
    )
    has_evidence = bool(index.calls_in.get(sym.id)) or bool(
        index.referenced_in.get(sym.id)
    )
    property_reads = index.reads_by_name.get(bare_name, [])

    sweep = _run_grep(root, bare_name)
    if sweep.error is not None:
        print(f"dekko: {sweep.error}", file=sys.stderr)
        return EXIT_GREP_FAILED

    hits = [h for h in sweep.hits if (h.path, h.line) not in own_def_locs]
    # Same silent-exclusion disclosure as the callers/uses path -- see
    # ``_excluded_declarations_note``.
    excluded_declarations = len(sweep.hits) - len(hits)
    reference_rows: list[dict] = []
    noise_count = 0
    for h in hits:
        bucket, detail = classify_unused_reference(
            h.snippet, bare_name, path=h.path
        )
        if bucket == "noise":
            noise_count += 1
            continue
        # classify_unused_reference's own comment check is
        # _looks_like_comment_line alone, which -- like classify_miss's
        # -- never recognizes a bare ``*`` block-comment continuation
        # line. Same re-read pattern as the multiline-import-member
        # check right below.
        if _looks_like_block_comment_continuation(root, h):
            noise_count += 1
            continue
        if _looks_like_multiline_import_member(root, h, bare_name):
            noise_count += 1
            continue
        row = _grep_row(h)
        row["shape"] = detail
        reference_rows.append(row)

    kept, meter = _fit_rows(reference_rows, budget, limit)
    # This single-target path never threaded
    # ``ambiguous.collision_names`` into `_is_generic_name` at all,
    # unlike `_run_all_sweeps`'s ``--all`` path (see its own docstring)
    # -- so a name like ``error``, absent from the curated
    # `_GENERIC_NAMES` list but measurably collision-prone in this
    # specific repo's own call graph, ran the full grep sweep into the
    # safety cap with no caution disclosed. Not a curated-list or
    # threshold gap after all: a wiring gap, one missed call site.
    is_known_collision_name = bare_name in ambiguous.collision_names(index)
    generic_caution = _is_generic_name(bare_name, is_known_collision_name)

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
            excluded_declarations=excluded_declarations,
            property_reads=property_reads,
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
        excluded_declarations=excluded_declarations,
        property_reads=property_reads,
    )
    return EXIT_OK


def _resolve_declaring_type(query_index: MapIndex, sym: Symbol) -> str | None:
    """The gated declaring-type simple name for ``run()``'s
    receiver-mismatch heuristic, or ``None`` when the gate doesn't
    hold.

    All four gating conditions must hold:

    1. ``sym.kind == "method"`` -- the heuristic is about a *receiver*
       relationship, which only makes sense for a method on some type.
       A free function/closure-local bare-name collision has no
       declaring type to check imports against (layer 1's denylist
       domain, not this heuristic's).
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
    root_globs: tuple[str, ...] = (),
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
    (a generic bare name on a large repo), the
    ``dekko-only`` bucket is reported as inconclusive (empty rows, a
    ``None``/absent count in JSON's ``counts.dekko_only``) rather than
    a false-confidence number — a location dekko resolved that the
    incomplete grep hit set doesn't cover may simply be past the
    cutoff, not a genuine resolver disagreement. ``matches``/
    ``grep_only`` are still reported (grep's own truncated view of
    them), alongside a ``grep_truncated``/``dekko_only_note`` (JSON)
    or a printed ``note:`` line (text) disclosing the cap was hit.
    Any raw grep line long enough to be a binary/data blob rather than
    real source is dropped from the sweep entirely
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
            instead of a flat row list. Groups the *full* grep-only
            bucket first, then applies ``limit``/``budget`` to the
            number of file groups printed, not to rows before grouping
            (see ``_print_bucket_by_file``).
            Text mode only, single-target only (no effect on ``--json``
            or ``--unused``, which already carries every row's
            ``file``/``cause`` for an external consumer to group).

    Returns:
        ``0`` on a completed comparison (regardless of findings),
        ``EXIT_NOT_FOUND``/``EXIT_AMBIGUOUS`` when ``target`` doesn't
        resolve to a unique symbol (callers/unused mode) or matches no
        external reference (``uses`` mode), ``EXIT_GREP_FAILED`` when
        the grep sweep itself couldn't run.
    """
    if unused:
        return _run_unused_check(
            index, target, root, limit, budget, as_json, root_globs
        )

    _reset_file_caches()
    query_index = index if include_tests else index.without_tests()
    # The target symbol, for the per-target tier-1 facts
    # (``_apply_target_facts``); ``None`` in ``--usages`` mode.
    target_sym: Symbol | None = None
    # The bare name's shared classification inputs, the same ones the
    # ``--all`` sweep builds. ``--usages`` mode has none: an external
    # base identifier has no in-repo declaration, reference edge or
    # kind.
    inputs = _NameInputs(
        own_def_locs=frozenset(),
        ref_sites=frozenset(),
        target_kinds=frozenset(),
        read_sites=frozenset(),
        is_known_collision_name=False,
    )
    # The receiver-mismatch gate's declaring type, for the one-per-run
    # banner; the rung itself runs in ``_apply_target_facts``.
    declaring_type: str | None = None
    # The map's own attribution of every use of the bare name, for the
    # tier-1 resolved-elsewhere cause (``_resolved_elsewhere``). Callers
    # mode only, like everything above.
    attributed: dict[tuple[str, int], set[str]] = {}

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
        inputs = _name_inputs(
            query_index, sym.name, ambiguous.collision_names(query_index)
        )
        declaring_type = _resolve_declaring_type(query_index, sym)
        attributed = _attributed_sites(query_index, sym.name)
        target_sym = sym
        try:
            dekko_hits, module_level = _dekko_hits_callers(query_index, sym.id)
        except _QueryFailedError as exc:
            return exc.code

    sweep = _run_grep(root, bare_name)
    if sweep.error is not None:
        print(f"dekko: {sweep.error}", file=sys.stderr)
        return EXIT_GREP_FAILED
    grep_command = sweep.command_text
    grep_hits = sweep.hits
    if inputs.own_def_locs:
        # The target's own definition line -- and every other
        # same-bare-named symbol's own definition line -- always
        # contains the bare name and would otherwise show up as a
        # spurious "grep-only" miss on every single callers check —
        # dekko's callers query correctly never treats a symbol's own
        # definition as a call site, so grep matching one isn't a miss
        # to explain, it's out of scope for a caller/uses cross-check
        # entirely.
        grep_hits = [
            h for h in grep_hits if (h.path, h.line) not in inputs.own_def_locs
        ]
    # Disclose how many raw hits that filter removed, so the
    # buckets below reconcile against the ``grep:`` command printed
    # above them -- see ``_excluded_declarations_note``.
    excluded_declarations = len(sweep.hits) - len(grep_hits)

    dekko_set = set(dekko_hits)
    grep_by_loc = {(h.path, h.line): h for h in grep_hits}

    matched_locs = sorted(dekko_set & set(grep_by_loc))
    dekko_only_locs = sorted(dekko_set - set(grep_by_loc))
    grep_only_hits = [
        h for h in grep_hits if (h.path, h.line) not in dekko_set
    ]
    causes = _classify_grep_hits(
        grep_hits,
        bare_name,
        root,
        tests_excluded=not include_tests,
        symbols_by_path=query_index.symbols_by_path,
        scope=_map_scope(index),
        **vars(inputs),
    )
    resolved = _apply_resolved_elsewhere(
        causes,
        query_index,
        attributed,
        target_sym,
        [(h.path, h.line) for h in grep_only_hits],
    )
    bound = _apply_target_facts(
        causes,
        query_index,
        target_sym,
        [(h.path, h.line) for h in grep_only_hits],
        {loc: h.snippet for loc, h in grep_by_loc.items()},
        root,
    )
    grep_only_rows = [
        _grep_row(
            h,
            causes[(h.path, h.line)],
            resolved.get((h.path, h.line)),
            bound.get((h.path, h.line)),
        )
        for h in grep_only_hits
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
            excluded_declarations=excluded_declarations,
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
        grep_only_rows,
        module_level,
        grep_truncated=sweep.truncated,
        skipped_pathological=sweep.skipped_pathological,
        excluded_declarations=excluded_declarations,
        receiver_mismatch_note=receiver_mismatch_note,
        group_by_file=group_by_file,
        budget=budget,
        limit=limit,
    )
    return EXIT_OK


# --- --all sweep --------------------------------------------------
#
# A human-selected population of ``sanity <target>`` invocations has a
# real cost: a regression in ``classify_miss``'s own classification
# logic once sat in ``develop`` undetected, caught only because the
# tester happened to pick one symbol (out of dozens with nonzero fan-in)
# that exercised the buggy branch. ``run_all`` removes the selection
# bias: run the same cross-check over every in-repo symbol with nonzero
# fan-in instead of one hand-picked target.


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
    inputs: _NameInputs,
    *,
    tests_excluded: bool,
    symbols_by_path: dict[str, list[Symbol]] | None = None,
    scope: _MapScope | None = None,
    sweep: GrepSweepResult | None = None,
) -> tuple[GrepSweepResult, dict[tuple[str, int], str]]:
    """One grep + classify pass for ``bare_name``, shared across every
    symbol in its fan-in group — the sweep's whole cost-saving
    mechanism (see module docstring's ``--all`` paragraph): grep and
    classification cost drops from O(symbols with fan-in) to O(unique
    bare names among them). Sharing is sound because the shared stage
    reads only facts about the name (``_name_inputs``), the same ones
    ``run()`` reads; every target-dependent rung runs per symbol in
    ``_diff_symbol``.

    Args:
        root: Repo root, for the grep sweep and classifier's file I/O.
        bare_name: The bare identifier being swept.
        inputs: ``_name_inputs`` for ``bare_name``.
        tests_excluded: Whether the dekko-side query being compared
            against excluded test files by default.
        symbols_by_path: The query index's symbols per file.
        scope: The unfiltered map's test spans and file set, built once
            by ``run_all()``; see ``_MapScope``.
        sweep: The name's grep result when the caller already ran it
            (``--all``'s planned sweep); ``None`` runs ``_run_grep``.

    Returns:
        ``(sweep, causes)``. ``causes`` is empty when ``sweep.error``
        is set — a caller checks ``sweep.error`` before trusting an
        empty ``causes`` as "no grep-only hits" rather than "the sweep
        itself failed."
    """
    if sweep is None:
        sweep = _run_grep(root, bare_name)
    if sweep.error is not None:
        return sweep, {}
    causes = _classify_grep_hits(
        sweep.hits,
        bare_name,
        root,
        tests_excluded=tests_excluded,
        symbols_by_path=symbols_by_path,
        scope=scope,
        **vars(inputs),
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
    attributed: dict[tuple[str, int], set[str]] | None = None,
    snippets: dict[tuple[str, int], str] | None = None,
    *,
    root: Path,
) -> "_SymbolSweepResult | None":
    """Diff one symbol's own dekko-side callers hits against its bare
    name's shared classified grep hit set (``causes``).

    Mirrors ``run()``'s own matches/dekko-only/grep-only split, just
    keyed off ``causes`` (already computed once per bare name) instead
    of re-running ``_classify_grep_hits`` per symbol. ``attributed``
    (``_attributed_sites`` for the name, computed once per name by the
    caller) and ``snippets`` (the sweep's hit lines) are what let this
    one place, which knows which symbol the shared sweep is being
    diffed for, apply the resolved-elsewhere cause and the other
    per-target facts (``_apply_target_facts``) per symbol; the shared
    ``causes`` is never mutated.

    Returns:
        ``None`` if the symbol's own internal query unexpectedly fails
        to resolve (shouldn't happen for an already-enumerated fan-in
        symbol — ``callers`` can't fail the way ``uses`` can, see
        ``_QueryFailedError``'s own docstring — but this keeps one
        anomalous symbol from crashing the whole sweep).
    """
    try:
        dekko_hits, _module_level = _dekko_hits_callers(query_index, sym.id)
    except _QueryFailedError:
        return None
    dekko_set = set(dekko_hits)
    grep_locs = set(causes)
    matches = len(dekko_set & grep_locs)
    dekko_only = len(dekko_set - grep_locs)
    grep_only = grep_locs - dekko_set
    own = dict(causes)
    _apply_resolved_elsewhere(
        own, query_index, attributed or {}, sym, grep_only
    )
    _apply_target_facts(own, query_index, sym, grep_only, snippets or {}, root)
    grep_only_causes = [own[loc] for loc in grep_only]

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
    """Assemble ``sanity --all --json``'s output document.

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
    scope: _MapScope | None = None,
) -> tuple[
    dict[str, tuple[GrepSweepResult, dict[tuple[str, int], str]]],
    str | None,
]:
    """Run one grep+classify sweep per unique bare name in ``names``,
    sequentially or via a thread pool sized by ``workers`` — see
    ``run_all``'s own docstring for why threads, not processes.

    Each name greps only the files ``_plan_sweep`` placed it in. The
    second item is the plan's own error (the one walk failed), in
    which case no name is swept.

    ``ambiguous.collision_names(query_index)`` is computed exactly once
    here (not once per name in the loop below) and consulted per name
    from the already-built ``frozenset`` -- avoiding quadratic-ish
    re-computation across a large ``--all`` sweep, since
    ``collision_names`` itself is bounded by the map's own
    already-computed ambiguous-edge count, not by sweep size.
    """
    plan = _plan_sweep(root, names)
    if plan.error is not None:
        return {}, plan.error

    collision = ambiguous.collision_names(query_index)

    def _sweep_one(
        name: str,
    ) -> tuple[str, GrepSweepResult, dict[tuple[str, int], str]]:
        sweep, causes = _sweep_bare_name(
            root,
            name,
            _name_inputs(query_index, name, collision),
            tests_excluded=tests_excluded,
            symbols_by_path=query_index.symbols_by_path,
            scope=scope,
            sweep=_run_grep_planned(
                root, name, plan.files_by_name.get(name, [])
            ),
        )
        return name, sweep, causes

    sweeps: dict[str, tuple[GrepSweepResult, dict[tuple[str, int], str]]] = {}
    if workers <= 1 or len(names) <= 1:
        for name in names:
            _, sweep, causes = _sweep_one(name)
            sweeps[name] = (sweep, causes)
        return sweeps, None
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for name, sweep, causes in pool.map(_sweep_one, names):
            sweeps[name] = (sweep, causes)
    return sweeps, None


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
    root: Path,
) -> list[_SymbolSweepResult]:
    """Diff every fan-in symbol across ``names`` against its bare
    name's already-classified, shared grep sweep (see ``_diff_symbol``)."""
    results: list[_SymbolSweepResult] = []
    for name in names:
        sweep, causes = sweeps[name]
        attributed = _attributed_sites(query_index, name)
        snippets = {(h.path, h.line): h.snippet for h in sweep.hits}
        for sym in groups[name]:
            diffed = _diff_symbol(
                query_index, sym, causes, attributed, snippets, root=root
            )
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
    name. See the module docstring's ``--all`` paragraph.

    Callers mode only — there is
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
    _reset_file_caches()
    query_index = index if include_tests else index.without_tests()
    groups = _group_fan_in_symbols(query_index)
    all_names = sorted(groups)
    names_truncated = len(all_names) > max_names
    names = all_names[:max_names] if names_truncated else all_names

    workers = repo_ops.resolve_workers(jobs)
    sweeps, sweep_error = _run_all_sweeps(
        names,
        root,
        query_index,
        tests_excluded=not include_tests,
        workers=workers,
        scope=_map_scope(index),
    )
    if sweep_error is None:
        sweep_error = _first_sweep_error(names, sweeps)
    if sweep_error is not None:
        print(f"dekko: {sweep_error}", file=sys.stderr)
        return EXIT_GREP_FAILED

    results = _diff_all_symbols(query_index, groups, names, sweeps, root)

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
