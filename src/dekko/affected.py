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
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import diff
from . import mapfile
from . import walker
from .classify import is_test_path
from .model import Symbol
from .textutil import signature
from .resolver import _module_matches

EXIT_NONE = 0
EXIT_IMPACTED = 1
EXIT_ERROR = 2

# Evidence tiers, strongest first.
_TIERS = ("direct", "transitive", "import")


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
    result: diff.DiffResult, new: diff.Snapshot
) -> dict[str, TestImpact]:
    """Test files reached from changed symbols through call edges."""
    dist = _reverse_hops(_changed_for_calls(result), new.callers)
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
        sym = new.symbols.get(sym_id)
        if sym is not None and sym.test:
            impact.symbols.append(sym)
    return impacts


def _import_hits(new: diff.Snapshot, changed_files: set[str]) -> set[str]:
    """Test files whose imports resolve to any changed file."""
    hits: set[str] = set()
    for path, imports in new.imports.items():
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
    impacts = _call_impacts(result, new)
    for path in _import_hits(new, _changed_files(result)):
        if path not in impacts:
            impacts[path] = TestImpact(path=path, tier="import")
    for impact in impacts.values():
        impact.symbols.sort(key=lambda s: s.start_line)
    return sorted(
        impacts.values(), key=lambda i: (_TIERS.index(i.tier), i.path)
    )


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


def render(
    impacts: list[TestImpact], rev: str, as_json: bool, limit: int
) -> None:
    """Emit the impacted-test report as text or JSON."""
    if as_json:
        doc = {
            "rev": rev,
            "impacted": [_impact_json(i) for i in impacts],
            "command": _pytest_hint(impacts),
        }
        print(json.dumps(doc, indent=2))
        return
    if not impacts:
        print(f"dekko: no impacted tests vs {rev[:12]}")
        return
    print(f"dekko: {len(impacts)} impacted test files vs {rev[:12]}")
    for impact in impacts:
        print(f"  [{impact.tier}] {impact.path}")
        for sym in impact.symbols[:limit]:
            print(f"      {sym.start_line}  {signature(sym)}")
        extra = len(impact.symbols) - limit
        if extra > 0:
            print(f"      ... and {extra} more")
    hint = _pytest_hint(impacts)
    if hint:
        print(f"\n{hint}")


def _pytest_hint(impacts: list[TestImpact]) -> str:
    """A ready-to-paste pytest invocation, or empty when none apply."""
    if not impacts:
        return ""
    return "pytest " + " ".join(i.path for i in impacts)


def run(root: Path, rev: str | None, as_json: bool, limit: int) -> int:
    """Execute ``dekko affected`` against a repository.

    Args:
        root: Repository root (its working tree is the new side).
        rev: Git rev for the old side, or ``None`` to derive a default.
        as_json: Emit structured JSON instead of text.
        limit: Max impacted symbols shown per test file.

    Returns:
        ``0`` no impact, ``1`` impacted tests found, ``2`` bad rev.
    """
    index = mapfile.load_map(root)
    prov = (index.provenance if index else None) or {}
    subpath = prov.get("subpath")
    excludes = tuple(prov.get("excludes", []))
    max_file_size = prov.get("max_file_size", walker.DEFAULT_MAX_FILE_SIZE)
    target_rev = rev or prov.get("git_commit") or "HEAD"

    with tempfile.TemporaryDirectory(prefix="dekko-affected-") as tmp:
        old_root = Path(tmp)
        if not diff.export_rev(root, target_rev, old_root):
            print(
                f"dekko: cannot export git rev '{target_rev}' "
                f"(unknown rev or not a git repo)",
                file=sys.stderr,
            )
            return EXIT_ERROR
        old = diff.snapshot(old_root, subpath, excludes, max_file_size)

    new = diff.snapshot(root, subpath, excludes, max_file_size)
    result = diff.compare(target_rev, old, new)
    impacts = analyze(result, new)
    render(impacts, target_rev, as_json, limit)
    return EXIT_IMPACTED if impacts else EXIT_NONE
