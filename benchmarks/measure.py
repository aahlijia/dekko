"""Measurement harness for the Active Context Layer (design §7, step 3).

This establishes the **falsifiable baseline** for the overarching goal
G★: dekko's context layer must reduce the tokens an agent spends to work
a task, at equal task success. It is deliberately *not* part of the wheel
(it lives outside ``src/``) — it is a benchmark, run by hand or in CI.

It measures the value proposition that exists **today**, before the hooks
land: for a fixed set of representative tasks against a repo, the token
cost of the naive **whole-file-read baseline** versus the **dekko tool**
that delivers equivalent navigational/editing context. Both costs use the
same :func:`dekko.textutil.estimate_tokens` so the comparison is apples to
apples under the ``chars4`` pin.

Two task families:

* **comparative** (``outline``/``context``/``workset``) — a baseline and a
  dekko cost, so the reduction is a real ratio.
* **coverage** (``lean``) — no naive baseline maps cleanly to a
  whole-repo map, so we report absolute cost against what it covers
  (files + symbols), the FR-D3 density view.

When the hooks layer (step 4) lands, the same harness gains the *live*
half of G★ via :func:`session_cost`, which reads the real per-session
token tally straight from the transcript ledger — letting an operator
diff "hooks off" against "hooks on" on identical work.
"""

import argparse
import io
import json
import sys
from collections.abc import Callable
from contextlib import redirect_stdout
from dataclasses import dataclass
from pathlib import Path

from dekko import contextpack, ledger, mapfile, outline as outline_mod, query
from dekko import workset as workset_mod
from dekko.mapfile import MapIndex
from dekko.resolver import MODULE_CALLER_SUFFIX
from dekko.textutil import estimate_tokens


@dataclass(frozen=True)
class Task:
    """One measured scenario.

    Attributes:
        kind: ``"outline"``, ``"context"``, ``"workset"``, or ``"lean"``.
        target: File path or symbol name the task operates on (``""`` for
            ``lean``, which is whole-repo).
        label: Human label for the report row.
    """

    kind: str
    target: str
    label: str


@dataclass
class Result:
    """The measured cost of one task under both strategies.

    Attributes:
        task: The task measured.
        baseline: Tokens an agent would spend the naive way (``0`` for
            coverage tasks with no baseline).
        dekko: Tokens the dekko tool's output costs.
        covers: Free-form coverage note (files/symbols), for ``lean``.
    """

    task: Task
    baseline: int
    dekko: int
    covers: str = ""

    @property
    def saved(self) -> int:
        """Tokens not spent by using dekko (can be negative)."""
        return self.baseline - self.dekko

    @property
    def reduction(self) -> float:
        """Fractional reduction vs baseline, or ``0.0`` when no baseline."""
        return self.saved / self.baseline if self.baseline else 0.0

    def as_dict(self) -> dict:
        """Structured row for JSON output."""
        return {
            "label": self.task.label,
            "kind": self.task.kind,
            "baseline": self.baseline,
            "dekko": self.dekko,
            "saved": self.saved,
            "reduction": round(self.reduction, 3),
            "covers": self.covers,
        }


# A fixed task set against this repo (dekko itself). Targets are chosen to
# exercise large files and well-connected symbols where the savings — or
# their absence — show clearly. Override with a custom list in tests.
TASKS: tuple[Task, ...] = (
    Task("outline", "src/dekko/cli.py", "outline cli.py (large file)"),
    Task("outline", "src/dekko/render_lean.py", "outline render_lean.py"),
    Task("context", "fit_to_budget", "context fit_to_budget (hot symbol)"),
    Task("context", "build_pack", "context build_pack"),
    Task("workset", "blended_scores", "workset --symbol blended_scores"),
    Task("lean", "", "lean (whole-repo map)"),
)


def _capture_tokens(fn: Callable[[], int]) -> int:
    """Run a stdout-printing command and return its output's token cost."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn()
    return estimate_tokens(buf.getvalue())


def _file_tokens(root: Path, path: str) -> int:
    """Token cost of reading a whole file, or ``0`` if unreadable."""
    try:
        return estimate_tokens((root / path).read_text())
    except OSError:
        return 0


def _caller_paths(index: MapIndex, sym_id: str) -> set[str]:
    """Distinct files that call into a symbol (the def file included)."""
    paths: set[str] = set()
    for caller_id in index.calls_in.get(sym_id, []):
        if caller_id.endswith(MODULE_CALLER_SUFFIX):
            base = caller_id[: -len(MODULE_CALLER_SUFFIX)]
            caller = index.symbols_by_id.get(base)
        else:
            caller = index.symbols_by_id.get(caller_id)
        if caller is not None:
            paths.add(caller.path)
    return paths


def _measure_outline(index: MapIndex, root: Path, task: Task) -> Result:
    """Whole-file read vs ``dekko outline`` for one file."""
    baseline = _file_tokens(root, task.target)
    dekko = _capture_tokens(
        lambda: outline_mod.run(
            index, task.target, root=root, budget=None,
            limit=200, as_json=False,
        )
    )
    return Result(task, baseline, dekko)


def _measure_context(index: MapIndex, root: Path, task: Task) -> Result:
    """Read the symbol's file + caller files vs ``dekko context``.

    The baseline is what an agent reads to understand a symbol and who
    calls it; the pack delivers the same neighborhood as signatures.
    """
    sym, _ = query.resolve_target(index, task.target)
    if sym is None:
        return Result(task, 0, 0, covers="unresolved")
    paths = _caller_paths(index, sym.id) | {sym.path}
    baseline = sum(_file_tokens(root, p) for p in paths)
    dekko = _capture_tokens(
        lambda: contextpack.run(
            index, task.target, hops=1, budget=None,
            as_json=False, root=root,
        )
    )
    return Result(task, baseline, dekko, covers=f"{len(paths)} files")


def _measure_workset(index: MapIndex, root: Path, task: Task) -> Result:
    """Read the symbol's file + impacted tests vs ``dekko workset``.

    Builds the bundle from the already-loaded ``index`` (rather than
    ``workset.run``, which re-loads and would stumble on a stale map),
    keeping the measurement consistent with the other comparative tasks.
    """
    seed, _ = workset_mod.seed_from_symbol(index, task.target)
    if seed is None:
        return Result(task, 0, 0, covers="unresolved")
    paths = {p for s in seed.touched for p in (s.path,)} | set(seed.files)
    paths |= {imp.path for imp in seed.impacts}
    baseline = sum(_file_tokens(root, p) for p in paths)
    bundle = workset_mod.build(index, seed, workset_mod.DEFAULT_PACKS)
    dekko = _capture_tokens(
        lambda: workset_mod._render_text(bundle, None)
    )
    return Result(task, baseline, dekko, covers=f"{len(paths)} files")


def _measure_lean(index: MapIndex, root: Path, task: Task) -> Result:
    """Absolute cost of the whole-repo lean map and what it covers."""
    from dekko import render_lean

    _lines, report = render_lean.generate(index, root)
    n_files = len(index.languages_by_path)
    n_syms = len(index.symbols_by_id)
    return Result(
        task, 0, report.tokens,
        covers=f"{n_files} files, {n_syms} symbols",
    )


_MEASURERS = {
    "outline": _measure_outline,
    "context": _measure_context,
    "workset": _measure_workset,
    "lean": _measure_lean,
}


def run_task(index: MapIndex, root: Path, task: Task) -> Result:
    """Measure a single task, dispatching on its kind."""
    return _MEASURERS[task.kind](index, root, task)


def run_all(
    root: Path, tasks: tuple[Task, ...] = TASKS
) -> list[Result]:
    """Measure every task against the map at ``root``.

    Args:
        root: Repository root containing a dekko map.
        tasks: Tasks to measure (defaults to the built-in set).

    Returns:
        One :class:`Result` per task.

    Raises:
        RuntimeError: If ``root`` has no loadable map.
    """
    index = mapfile.load_map(root)
    if index is None:
        raise RuntimeError(f"no dekko map under {root}; run `dekko map` first")
    return [run_task(index, root, task) for task in tasks]


def session_cost(transcript: Path, root: Path) -> dict:
    """Live per-session context cost from the ledger (the future on/off).

    The step-4 hooks change what ends up in a session's transcript; this
    reads the real token tally back out so an operator can diff hooks-off
    against hooks-on on identical work. Usable today against any recorded
    session.

    Args:
        transcript: Path to a session JSONL.
        root: Repository root the session ran in.

    Returns:
        The ledger view's summary dict.
    """
    index = mapfile.load_map(root) or MapIndex(root_label=root.name)
    return ledger.build_view(transcript, index, root).as_dict()


def render_report(results: list[Result]) -> list[str]:
    """Render results as a dense text report with an aggregate line."""
    lines = ["dekko context-layer benchmark (tokens: baseline → dekko)"]
    comparative = [r for r in results if r.baseline]
    for r in results:
        if r.baseline:
            lines.append(
                f"  {r.task.label}: {r.baseline} → {r.dekko}  "
                f"(-{round(100 * r.reduction)}%)"
            )
        else:
            cov = f" — {r.covers}" if r.covers else ""
            lines.append(f"  {r.task.label}: {r.dekko} tok{cov}")
    if comparative:
        tot_base = sum(r.baseline for r in comparative)
        tot_dekko = sum(r.dekko for r in comparative)
        pct = round(100 * (tot_base - tot_dekko) / tot_base)
        lines.append(
            f"overall: {tot_base} → {tot_dekko} tokens across "
            f"{len(comparative)} tasks (-{pct}%)"
        )
    return lines


def main(argv: list[str] | None = None) -> int:
    """CLI: ``python benchmarks/measure.py [--root DIR] [--json]``."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=".", metavar="DIR")
    parser.add_argument("--json", dest="as_json", action="store_true")
    parser.add_argument(
        "--session", default=None, metavar="PATH",
        help="instead of tasks, report a session transcript's context cost",
    )
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    if args.session:
        doc = session_cost(Path(args.session).resolve(), root)
        print(json.dumps(doc, indent=2))
        return 0
    results = run_all(root)
    if args.as_json:
        print(json.dumps([r.as_dict() for r in results], indent=2))
        return 0
    for line in render_report(results):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
