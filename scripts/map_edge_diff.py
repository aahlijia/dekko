#!/usr/bin/env python3
"""Diff two ``map.json`` files by edge *sets*, for resolver changes.

Raw equality between two maps' sections is useless: ids are interned
ints that shift whenever the symbol table does. This compares
``(caller, callee)`` string pairs instead, so a resolver change can be
judged by what it actually added and removed.

The workflow that caught every resolver regression in round 31::

    cp repo/.dekko/map.json /tmp/before.json
    # ...change the resolver, then re-map with the dev build...
    python scripts/map_edge_diff.py /tmp/before.json repo/.dekko/map.json

Then read the LOST sample against the source. A lost edge that was
correct is a regression; a lost edge that was wrong is the fix working.
Exit status is always 0: this reports, it does not judge.
"""

import argparse
import json
import random
from pathlib import Path

PAIR_SECTIONS = ("edges", "external", "referenced")


def _pairs(doc: dict, section: str) -> set[tuple[str, str]]:
    ids = doc["ids"]
    return {(ids[e["caller"]], ids[e["callee"]]) for e in doc.get(section, [])}


def _heritage(doc: dict) -> set[tuple[str, str]]:
    ids = doc["ids"]
    return {
        (ids[e["subtype"]], ids[e["supertype"]])
        for e in doc.get("heritage", [])
    }


def _modules(doc: dict) -> set[tuple[str, str]]:
    ids = doc["ids"]
    edges = doc.get("module_graph", {}).get("edges", [])
    return {(ids[e["importer"]], ids[e["imported"]]) for e in edges}


def _report(
    label: str,
    before: set[tuple[str, str]],
    after: set[tuple[str, str]],
    sample: int,
    rng: random.Random,
) -> None:
    new, lost = after - before, before - after
    print(
        f"{label:12s} {len(before):7d} -> {len(after):7d}  "
        f"+{len(new)} -{len(lost)}"
    )
    for tag, pairs in (("LOST", lost), ("NEW", new)):
        chosen = rng.sample(sorted(pairs), min(sample, len(pairs)))
        for caller, callee in chosen:
            print(f"    {tag:4s} {caller}  ->  {callee}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    parser.add_argument(
        "--sample",
        type=int,
        default=8,
        help="lost/new pairs to print per section (default: 8)",
    )
    parser.add_argument("--seed", type=int, default=31)
    args = parser.parse_args()

    before = json.loads(args.before.read_text())
    after = json.loads(args.after.read_text())
    rng = random.Random(args.seed)

    for section in PAIR_SECTIONS:
        _report(
            section,
            _pairs(before, section),
            _pairs(after, section),
            args.sample,
            rng,
        )
    _report("heritage", _heritage(before), _heritage(after), args.sample, rng)
    _report("modules", _modules(before), _modules(after), args.sample, rng)
    for section in ("ambiguous", "heritage_ambiguous", "heritage_external"):
        print(
            f"{section:18s} {len(before.get(section, [])):7d} -> "
            f"{len(after.get(section, [])):7d}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
