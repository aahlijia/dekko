"""Select the tests impacted by a change.

``dekko affected [REV]`` diffs the working tree against a git rev, then
reports which test files a runner should exercise. Two independent
kinds of evidence are combined:

1. **Call edges** — reverse-BFS the call graph from every added/changed
   symbol; any test symbol reached is impacted, labelled ``direct``
   (reached in one hop) or ``transitive`` (further away).
2. **Imports** (always on) — any test file whose imports resolve to a
   changed *file* is impacted, labelled ``import``. This catches tests
   that touch changed code through fixtures, references, or deleted
   symbols, where no static call edge survives.

Static analysis cannot see fixture injection, parametrization, or
dynamic dispatch, so the report is a set of strong leads — run them,
don't treat the absence of a test as proof it is unaffected.
"""

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from dekko import repo_ops
from dekko.storage import cache as cache_mod
from dekko.analysis import diff
from dekko.render import mapfile
from dekko.core import walker
from dekko.classify import is_test_path
from dekko.core.model import Import, Symbol
from dekko.textutil import fit_to_budget, signature
from dekko.core.resolver import _module_matches

EXIT_NONE = 0
EXIT_IMPACTED = 1
EXIT_ERROR = 2

# Evidence tiers, strongest first.
_TIERS = ("direct", "transitive", "import")

# Mirrors workset.DEFAULT_BUDGET: without a cap, a single large-repo
# commit can render an unbounded report (round-08 eval: ~124K tokens
# for one tensorflow commit) — a sane default keeps `affected` cheap
# by default like every other read command, while `--budget 0`/a large
# explicit value still opts back out.
DEFAULT_BUDGET = 6000


@dataclass
class TestImpact:
    """One impacted test file and why it is impacted.

    Attributes:
        path: Repo-relative path of the test file.
        tier: Strongest evidence — ``direct``, ``transitive``, or
            ``import``.
        symbols: Impacted test symbols reached through call edges
            (empty when the only evidence is an import).
    """

    path: str
    tier: str
    symbols: list[Symbol] = field(default_factory=list)


def _changed_for_calls(result: diff.DiffResult) -> set[str]:
    """Symbol ids present in the new tree (added + changed)."""
    return {d.symbol.id for d in result.added + result.changed}


def _changed_files(result: diff.DiffResult) -> set[str]:
    """Every file touched by the diff (added, changed, or removed)."""
    deltas = result.added + result.changed + result.removed
    return {d.symbol.path for d in deltas}


def _reverse_hops(
    seed_ids: set[str], callers: dict[str, list[str]]
) -> dict[str, int]:
    """Minimum reverse-call distance from any seed to each reachable id.

    Seeds are distance 0; their direct callers 1, and so on. Module-level
    caller ids (``path::<module>``) are included so a test module's
    top-level call still registers.
    """
    dist = dict.fromkeys(seed_ids, 0)
    frontier = list(seed_ids)
    hop = 0
    while frontier:
        hop += 1
        nxt: list[str] = []
        for sid in frontier:
            for caller in callers.get(sid, []):
                if caller in dist:
                    continue
                dist[caller] = hop
                nxt.append(caller)
        frontier = nxt
    return dist


def _id_path(sym_id: str) -> str:
    """Repo-relative file path embedded in a symbol or module id."""
    return sym_id.split("::", 1)[0]


def _call_impacts(
    seed_ids: set[str],
    callers: dict[str, list[str]],
    symbols: dict[str, Symbol],
) -> dict[str, TestImpact]:
    """Test files reached from seed symbols through call edges.

    Args:
        seed_ids: Symbol ids to walk back from (added/changed, or a
            single symbol for ``workset``'s symbol seed).
        callers: Symbol id → caller ids (a snapshot's or the index's).
        symbols: Symbol id → symbol, for tagging impacted test symbols.
    """
    dist = _reverse_hops(seed_ids, callers)
    impacts: dict[str, TestImpact] = {}
    for sym_id, hop in dist.items():
        path = _id_path(sym_id)
        if not is_test_path(path):
            continue
        tier = "direct" if hop <= 1 else "transitive"
        impact = impacts.get(path)
        if impact is None:
            impact = TestImpact(path=path, tier=tier)
            impacts[path] = impact
        elif _TIERS.index(tier) < _TIERS.index(impact.tier):
            impact.tier = tier
        sym = symbols.get(sym_id)
        if sym is not None and sym.test:
            impact.symbols.append(sym)
    return impacts


def _finalize(impacts: dict[str, TestImpact]) -> list[TestImpact]:
    """Order each file's symbols, then files strongest-evidence first."""
    for impact in impacts.values():
        impact.symbols.sort(key=lambda s: s.start_line)
    return sorted(
        impacts.values(), key=lambda i: (_TIERS.index(i.tier), i.path)
    )


def _import_hits(
    imports_by_path: dict[str, list[Import]], changed_files: set[str]
) -> set[str]:
    """Test files whose imports resolve to any changed file."""
    hits: set[str] = set()
    for path, imports in imports_by_path.items():
        if not is_test_path(path):
            continue
        for imp in imports:
            if any(_module_matches(imp.source, cf) for cf in changed_files):
                hits.add(path)
                break
    return hits


def analyze(result: diff.DiffResult, new: diff.Snapshot) -> list[TestImpact]:
    """Combine call-edge and import evidence into impacted test files.

    Args:
        result: The diff between the rev and the working tree.
        new: Snapshot of the working tree (symbols, callers, imports).

    Returns:
        Impacted test files, strongest evidence first then by path.
    """
    impacts = _call_impacts(
        _changed_for_calls(result), new.callers, new.symbols
    )
    for path in _import_hits(new.imports, _changed_files(result)):
        if path not in impacts:
            impacts[path] = TestImpact(path=path, tier="import")
    return _finalize(impacts)


def impacts_from_symbol(
    index: mapfile.MapIndex, seed_ids: set[str]
) -> list[TestImpact]:
    """Call-edge and same-file-import impacts for a static symbol seed.

    Used by ``workset``'s symbol seed: walks the index's call graph back
    from ``seed_ids`` and reports the test files reached, same as
    ``analyze()``'s call-edge tier. Also includes an import-tier
    fallback for test files that import/``#include`` a seed symbol's own
    file — the same evidence ``analyze()``'s ``_import_hits`` uses for a
    diff's changed-file set, narrowed here to the seed symbols' files.
    This closes a real false-negative for languages (C++ in particular)
    whose whole-file-include model leaves same-named cross-file calls
    unresolved as ``ambiguous`` in the resolver, never reaching
    ``calls_in`` at all — see investigation-1.5-cpp-gtest-affected.md.
    It is narrower than ``analyze()``'s tier (a whole diff's changed
    files vs. one seed's own file), since a bare symbol seed has no
    diff to draw a broader changed-file set from.
    """
    impacts = _call_impacts(seed_ids, index.calls_in, index.symbols_by_id)
    seed_files = {
        sym.path
        for sid in seed_ids
        if (sym := index.symbols_by_id.get(sid)) is not None
    }
    for path in _import_hits(index.imports_by_path, seed_files):
        if path not in impacts:
            impacts[path] = TestImpact(path=path, tier="import")
    return _finalize(impacts)


def _impact_json(impact: TestImpact) -> dict:
    """Structured rendering of one impacted test file."""
    return {
        "path": impact.path,
        "tier": impact.tier,
        "symbols": [
            {"id": s.id, "line": s.start_line, "signature": signature(s)}
            for s in impact.symbols
        ],
    }


def _impact_rows(impacts: list[TestImpact], limit: int) -> list[str]:
    """Flatten impacted files and their symbols into display rows.

    File-header rows and symbol rows share one list so a token budget
    can trim from the weakest-tier end (impacts are strongest first).
    """
    rows: list[str] = []
    for impact in impacts:
        rows.append(f"  [{impact.tier}] {impact.path}")
        rows.extend(
            f"      {sym.start_line}  {signature(sym)}"
            for sym in impact.symbols[:limit]
        )
        extra = len(impact.symbols) - limit
        if extra > 0:
            rows.append(f"      ... and {extra} more")
    return rows


def render(
    impacts: list[TestImpact],
    rev: str,
    as_json: bool,
    limit: int,
    root: Path,
    budget: int | None = None,
    provenance: dict | None = None,
) -> None:
    """Emit the impacted-test report as text or JSON.

    ``provenance`` (a map's provenance dict, see ``mapfile.load_map``)
    qualifies the report with the same "some files weren't mapped"
    caveat ``query``'s not-found replies already carry — a "no
    impacted tests" result on a diff that touched only
    vendored-excluded or unparseable files (e.g. tensorflow's
    ``third_party/xla``) would otherwise read identically to a
    genuinely safe change. Omitted (``None``) by default so existing
    callers that don't have a provenance dict handy are unaffected.
    """
    coverage = mapfile.format_unsupported(provenance)
    if as_json:
        entries = [_impact_json(i) for i in impacts]
        serialized = [json.dumps(e) for e in entries]
        kept_ser, meter = fit_to_budget(serialized, budget, None)
        doc = {
            "rev": rev,
            "impacted": entries[: len(kept_ser)],
            "command": _test_hint(impacts, root),
            "meta": meter.as_dict(),
        }
        if coverage:
            doc["coverage_warning"] = coverage
        print(json.dumps(doc, indent=2))
        return
    if not impacts:
        print(f"dekko: no impacted tests vs {rev[:12]}")
        if coverage:
            print(
                f"  note: {coverage} — this answer may be incomplete",
                file=sys.stderr,
            )
        return
    header = f"dekko: {len(impacts)} impacted test files vs {rev[:12]}"
    rows = _impact_rows(impacts, limit)
    kept, meter = fit_to_budget(rows, budget, None, prefix=header)
    print(header)
    for row in kept:
        print(row)
    hint = _test_hint(impacts, root)
    if hint:
        print(f"\n{hint}")
    print(meter.footer())


# Cap on how many paths a "ready to paste" test-runner invocation
# embeds per language group. Without this, the hint is unbounded
# regardless of budget — a real ~1,500-impact repo embedded every path
# in this one line, blowing a workset budget 3.6x over its stated cap
# (bug #6/B6). A command holding hundreds/thousands of paths also
# stops being "ready to paste" long before it stops being technically
# valid.
_MAX_HINT_PATHS = 20

# Extensions grouped by their (confidently known, static) test runner.
# Extensions not covered here get no hint line at all — silence beats
# a wrong guess, matching the existing "no impacts -> empty string"
# contract.
_PY_EXTS = frozenset({".py"})
_RUST_EXTS = frozenset({".rs"})
_GO_EXTS = frozenset({".go"})
_JS_EXTS = frozenset({".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"})
_JVM_EXTS = frozenset({".java", ".kt", ".kts", ".groovy"})

# Lockfile -> package-manager test invocation, strongest signal first.
_JS_LOCKFILE_RUNNERS = (
    ("bun.lock", "bun run test"),
    ("bun.lockb", "bun run test"),
    ("pnpm-lock.yaml", "pnpm test"),
    ("yarn.lock", "yarn test"),
)


def _cap_paths(paths: list[str]) -> tuple[list[str], int]:
    """Cap a path list at ``_MAX_HINT_PATHS``; return (shown, extra)."""
    if len(paths) <= _MAX_HINT_PATHS:
        return paths, 0
    return paths[:_MAX_HINT_PATHS], len(paths) - _MAX_HINT_PATHS


def _py_hint(paths: list[str]) -> str:
    """A ready-to-paste ``pytest`` invocation for impacted ``.py`` files."""
    shown, extra = _cap_paths(paths)
    hint = "pytest " + " ".join(shown)
    if extra:
        hint += f"  # +{extra} more impacted test files not shown"
    return hint


def _named_hint(command: str, paths: list[str]) -> str:
    """A whole-suite runner invocation, with impacted paths as a comment.

    Used for runners (``cargo test``, ``go test ./...``) that don't
    accept arbitrary source paths the way ``pytest`` does.
    """
    shown, extra = _cap_paths(paths)
    names = ", ".join(shown)
    if extra:
        names += f", +{extra} more"
    return f"{command}  # impacted: {names}"


def _js_hint(root: Path) -> str:
    """The repo's own declared JS/TS test command, or empty if unknown.

    Reads ``package.json``'s ``scripts.test`` as a presence check (a
    declared test script at all) rather than embedding its text, then
    picks the matching package-manager invocation from the strongest
    lockfile signal at ``root`` — this is what actually runs whatever
    ``scripts.test`` says, correctly, for the package manager the repo
    uses (``bun test``, `vitest`, etc. are invoked *through* it).
    """
    try:
        pkg = json.loads((root / "package.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return ""
    if not isinstance(pkg, dict) or not pkg.get("scripts", {}).get("test"):
        return ""
    for lockfile, command in _JS_LOCKFILE_RUNNERS:
        if (root / lockfile).is_file():
            return command
    return "npm test"


def _jvm_hint(root: Path) -> str:
    """A Gradle/Maven test invocation, or empty when neither is present."""
    if (root / "gradlew").is_file():
        return "./gradlew test"
    if (root / "pom.xml").is_file():
        return "mvn test"
    return ""


def _group_hint(ext: str, paths: list[str], root: Path) -> str:
    """One language group's runner hint, or empty when none applies."""
    if ext in _PY_EXTS:
        return _py_hint(paths)
    if ext in _RUST_EXTS:
        return _named_hint("cargo test", paths)
    if ext in _GO_EXTS:
        return _named_hint("go test ./...", paths)
    if ext in _JS_EXTS:
        return _js_hint(root)
    if ext in _JVM_EXTS:
        return _jvm_hint(root)
    return ""


def _test_hint(impacts: list[TestImpact], root: Path) -> str:
    """Ready-to-paste test-runner invocation(s), one per language group.

    Impacted files are grouped by extension; each group with a
    confidently known, static runner gets its own hint line — ``pytest``
    for Python (byte-identical to the historical Python-only behavior),
    ``cargo test``/``go test ./...`` for Rust/Go, the repo's own
    declared ``package.json`` test script (run through the strongest
    lockfile-inferred package manager) for JS/TS, and a Gradle/Maven
    invocation for JVM languages. A group with no confident mapping is
    silently omitted, same as today's "no impacts -> empty string"
    contract. Each group is capped at ``_MAX_HINT_PATHS`` paths
    independently.
    """
    if not impacts:
        return ""
    groups: dict[str, list[str]] = {}
    for impact in impacts:
        groups.setdefault(Path(impact.path).suffix, []).append(impact.path)
    hints = [
        hint
        for ext, paths in groups.items()
        if (hint := _group_hint(ext, paths, root))
    ]
    return "\n".join(hints)


def changes(
    root: Path,
    rev: str | None,
    index: mapfile.MapIndex | None = None,
    jobs: int = 1,
) -> tuple[list[TestImpact], diff.DiffResult, diff.Snapshot, str, dict] | None:
    """Impacted tests plus the underlying diff for worktree-vs-rev.

    Maps the working tree and the sources at ``rev``, diffs them, and
    runs :func:`analyze`. Shared by ``affected`` (which keeps only the
    impacts) and ``workset`` (which also needs the diff for its touched
    symbols).

    Args:
        root: Repository root (its working tree is the new side).
        rev: Git rev for the old side, or ``None`` to derive a default.
        index: An already-loaded current-tree index, if the caller has
            one (e.g. ``workset.seed_from_rev``, which loads its own
            index before calling here). Avoids a second, redundant
            ``mapfile.load_map`` for the same map.json. Falls back to
            loading it here when ``None``, matching prior behavior for
            callers with no index to hand in (``affected.run``).
        jobs: Resolved worker count passed through to
            ``diff.old_snapshot``/``diff.snapshot_new_side`` — see
            ``diff.snapshot``. Round-12 master report §3.3: this is
            the dominant cost on a first-touch/cold-rev-cache call, a
            separate code path ``dekko map --full``'s own ``--jobs``
            fix never reached.

    Returns:
        ``(impacts, result, new, target_rev, provenance)``, or ``None``
        when the rev cannot be exported (the explanatory message is
        printed to stderr before returning). ``provenance`` is the
        current map's provenance dict (possibly empty), so callers can
        qualify a "no impacted tests" result the way ``affected.run``
        does — see ``render``.
    """
    if index is None:
        index = repo_ops.load_current_index_no_regen(root)
    prov = (index.provenance if index else None) or {}
    subpath = prov.get("subpath")
    excludes = tuple(prov.get("excludes", []))
    max_file_size = prov.get("max_file_size", walker.DEFAULT_MAX_FILE_SIZE)
    target_rev = rev or prov.get("git_commit") or "HEAD"

    old_cache = cache_mod.IncrementalCache(cache_mod.load(root))
    old = diff.old_snapshot(
        root,
        target_rev,
        subpath,
        excludes,
        max_file_size,
        old_cache,
        jobs=jobs,
    )
    if old is None:
        print(
            f"dekko: cannot export git rev '{target_rev}' "
            f"(unknown rev or not a git repo)",
            file=sys.stderr,
        )
        return None

    new = diff.snapshot_new_side(
        root, subpath, excludes, max_file_size, index, jobs=jobs
    )
    result = diff.compare(target_rev, old, new)
    impacts = analyze(result, new)
    return impacts, result, new, target_rev, prov


def run(
    root: Path,
    rev: str | None,
    as_json: bool,
    limit: int,
    budget: int | None = None,
    jobs: int = 1,
) -> int:
    """Execute ``dekko affected`` against a repository.

    Args:
        root: Repository root (its working tree is the new side).
        rev: Git rev for the old side, or ``None`` to derive a default.
        as_json: Emit structured JSON instead of text.
        limit: Max impacted symbols shown per test file.
        budget: Approximate token budget for the report, or ``None``.
        jobs: Resolved worker count — see ``changes``.

    Returns:
        ``0`` no impact, ``1`` impacted tests found, ``2`` bad rev.
    """
    outcome = changes(root, rev, jobs=jobs)
    if outcome is None:
        return EXIT_ERROR
    impacts, _result, _new, target_rev, prov = outcome
    render(impacts, target_rev, as_json, limit, root, budget, prov)
    return EXIT_IMPACTED if impacts else EXIT_NONE
