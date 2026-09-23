"""Known-answer ranking check for ``dekko search``.

Runs every query in ``search_known_answers.json`` against a local
checkout of its repo and reports where the known answer ranked, plus
the set's top-1 hit rate and mean reciprocal rank. Hub queries (the
answer is heavily called) are flagged, so a ranking change can't pass
by simply ignoring connectivity.

Each repo must be checked out at the fixture's commit under
``--repos-dir`` and already mapped (``dekko map <repo>``). Queries
run the way ``dekko search`` does by default: test-path symbols are
excluded before ranking.

Usage::

    python benchmarks/search_known_answers.py --repos-dir ../repos
    python benchmarks/search_known_answers.py --repos-dir ../repos --json
"""

import argparse
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from dekko.analysis import search
from dekko.render.mapfile import MapIndex, load_map

FIXTURE = Path(__file__).with_name("search_known_answers.json")

# Beyond this rank a known answer counts as missed (reciprocal rank 0).
MAX_RANK = 50

RankFn = Callable[[MapIndex, str], list[search.SearchHit]]


@dataclass
class Result:
    """Where one query's known answer landed."""

    repo: str
    query: str
    hub: bool
    rank: int | None
    top: str


def _hit_key(hit: search.SearchHit) -> str:
    return f"{hit.symbol.path}:{hit.symbol.qualname}"


def _default_rank(index: MapIndex, query: str) -> list[search.SearchHit]:
    return search.rank(index, query)


def _load(repos_dir: Path, repo: str) -> MapIndex:
    index = load_map(repos_dir / repo)
    if index is None:
        sys.exit(f"no map for {repos_dir / repo}; run `dekko map` there")
    return index.without_tests()


def run_queries(
    repos_dir: Path,
    rank_fn: RankFn = _default_rank,
    fixture_path: Path = FIXTURE,
) -> list[Result]:
    """Rank every fixture query and record its best-placed answer.

    Args:
        repos_dir: Directory holding one mapped checkout per repo.
        rank_fn: Ranking to measure; defaults to ``search.rank``.
        fixture_path: The query set; defaults to the committed one.

    Returns:
        One :class:`Result` per query, in fixture order.
    """
    fixture = json.loads(fixture_path.read_text())
    indexes: dict[str, MapIndex] = {}
    results = []
    for q in fixture["queries"]:
        repo = q["repo"]
        if repo not in indexes:
            indexes[repo] = _load(repos_dir, repo)
        hits = rank_fn(indexes[repo], q["query"])[:MAX_RANK]
        answers = set(q["answers"])
        rank = next(
            (i for i, h in enumerate(hits, 1) if _hit_key(h) in answers),
            None,
        )
        top = _hit_key(hits[0]) if hits else ""
        results.append(Result(repo, q["query"], q["hub"], rank, top))
    return results


def summarize(results: list[Result]) -> dict:
    """Top-1 hit rate and mean reciprocal rank over ``results``."""
    top1 = sum(1 for r in results if r.rank == 1)
    mrr = sum(1 / r.rank for r in results if r.rank) / len(results)
    return {"queries": len(results), "top1": top1, "mrr": round(mrr, 3)}


def _print_text(results: list[Result]) -> None:
    for r in results:
        rank = str(r.rank) if r.rank else f">{MAX_RANK}"
        hub = "hub" if r.hub else "   "
        print(f"{rank:>4}  {hub}  {r.repo:13} {r.query}")
        if r.rank != 1:
            print(f"{'':20}top: {r.top}")
    s = summarize(results)
    print(f"\ntop-1 {s['top1']}/{s['queries']} · MRR {s['mrr']}")


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--repos-dir",
        type=Path,
        required=True,
        help="directory holding the mapped checkouts, one per repo",
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    results = run_queries(args.repos_dir)
    if args.as_json:
        doc = {
            "summary": summarize(results),
            "results": [vars(r) for r in results],
        }
        print(json.dumps(doc, indent=2))
    else:
        _print_text(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
