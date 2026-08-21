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
_MAX_GREP_LINES = 5000

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


def classify_miss(
    snippet: str,
    bare_name: str,
    *,
    is_test_file: bool,
    unsupported_language: bool,
    tests_excluded: bool,
) -> str:
    """Name the likely cause of one grep-only hit.

    A pure function over one grep-matched line plus its context — no
    repo/grep I/O, so it's directly testable in isolation. Checked in
    the order ``dekko-verify/SKILL.md`` lists its blind spots: a
    qualified-call syntax match is checked first (it's visible in the
    line itself and the most specific signal available), then whether
    the file is in a language dekko can't parse at all, then whether
    it's a test file excluded by ``sanity``'s own default filtering,
    then whether the target name is short/generic enough that dekko's
    count should be read as directional rather than exact. A line
    matching none of these is reported as "unexplained" rather than
    forcing a guess that doesn't fit — matching the plan's own
    "false confidence from the classifier itself" caution.

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

    Returns:
        One of the ``CAUSE_*`` constants.
    """
    if _looks_qualified_call(snippet, bare_name):
        return CAUSE_QUALIFIED_CALL
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


def _run_grep(
    root: Path, bare_name: str
) -> tuple[list[GrepHit], str, str | None]:
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
        ``(hits, command_text, error)`` — ``error`` is ``None`` on
        success (including "zero matches", grep's own exit code 1);
        ``hits``/``command_text`` are best-effort on error.
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
        return [], command_text, "'grep' not found on this system"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [], command_text, f"grep sweep failed: {exc}"
    if result.returncode not in (0, 1):
        detail = result.stderr.strip() or f"exit {result.returncode}"
        return [], command_text, f"grep sweep failed: {detail}"
    hits: list[GrepHit] = []
    for raw in result.stdout.splitlines()[:_MAX_GREP_LINES]:
        path, _, rest = raw.partition(":")
        line_str, _, snippet = rest.partition(":")
        if not line_str.isdigit():
            continue
        if path.startswith("./"):
            path = path[2:]
        hits.append(GrepHit(path=path, line=int(line_str), snippet=snippet))
    return hits, command_text, None


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


def _grep_row(hit: GrepHit, cause: str | None = None) -> dict:
    row = {"file": hit.path, "line": hit.line, "snippet": hit.snippet.strip()}
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
) -> None:
    print(f"dekko sanity: '{target}' ({action}) vs. grep '{bare_name}'")
    print(f"  grep: {grep_command}")
    _print_bucket_text("matches", *matches)
    _print_bucket_text("dekko-only", *dekko_only)
    _print_bucket_text("grep-only", *grep_only)
    if module_level:
        print(
            f"  dekko also reports {len(module_level)} module-level call "
            f"site(s) (no line info): {', '.join(sorted(module_level))}"
        )
    if grep_only[1] == 0:
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

    grep_hits, grep_command, grep_error = _run_grep(root, bare_name)
    if grep_error is not None:
        print(f"dekko: {grep_error}", file=sys.stderr)
        return EXIT_GREP_FAILED
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
            ),
        )
        for h in grep_only_hits
    ]
    match_rows = [_grep_row(grep_by_loc[loc]) for loc in matched_locs]
    dekko_only_rows = [_hit_row(*loc) for loc in dekko_only_locs]

    matches = _fit_rows(match_rows, budget, limit)
    dekko_only = _fit_rows(dekko_only_rows, budget, limit)
    grep_only = _fit_rows(grep_only_rows, budget, limit)

    if as_json:
        doc = {
            "action": "sanity",
            "query_action": query_action,
            "target": label,
            "bare_name": bare_name,
            "include_tests": include_tests,
            "grep_command": grep_command,
            "matches": matches[0],
            "dekko_only": dekko_only[0],
            "grep_only": grep_only[0],
            "counts": {
                "matches": matches[1],
                "dekko_only": dekko_only[1],
                "grep_only": grep_only[1],
            },
        }
        if module_level:
            doc["dekko_module_level"] = sorted(module_level)
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
    )
    return EXIT_OK
