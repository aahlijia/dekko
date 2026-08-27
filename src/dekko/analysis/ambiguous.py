"""Repo-wide resolver-trust report: where call resolution was ambiguous.

An ambiguous call is a bare name at a call site that matched 2+
repo-wide candidates and could not be disambiguated (same-file,
receiver-typed, or import-hinted resolution all failed). Such a call
never becomes a resolved edge — it never shows up in
``calls_in``/``calls_out`` — so it is invisible to ``query``,
``trace``, ``workset``, and friends unless a caller specifically knows
to ask. This module aggregates every ambiguous call site repo-wide so
an agent can ask "how much of this call graph is uncertain" before
trusting a caller/callee/workset answer for an impact-analysis
decision, instead of discovering the gap one wrong answer at a time
(the failure mode ``test-repos/reports/`` rounds 07-16 kept
re-hitting).

No extraction or resolver changes are needed: ``MapIndex.ambiguous_in``
/``ambiguous_out`` (``render/mapfile.py``) are already built by every
load path directly off ``CallGraph.ambiguous``. This module only reads
them, reconstructing the original ``(caller, name, candidates)``
triples at query time via ``_raw_triples`` since ``ambiguous_in``
(keyed by candidate) and ``ambiguous_out`` (keyed by caller) are each a
lossy, partial view of those triples on their own.

Granularity limit (see ``docs/cli.md``'s "Interpreting `dekko
ambiguous`" section): ``resolver.py``'s ``_record_ambiguous`` keys its
accumulator on ``(caller_id, name)``, not ``(caller_id, name, line)``
— multiple physical call sites of the same ambiguous name within the
same caller collapse into one triple here. Counts in this report are
"distinct (caller, name) collisions," not "physical ambiguous
call-site count."

Methodology limit -- this report is structurally blind to
single-candidate false confidence (round 23
``test-repos/reports/23-tokentest-7repo-fable5eval/cline.md`` §2.1,
``spring-boot.md`` §2.1/§2.2): ``CallGraph.ambiguous`` is only ever
populated when a bare call name matches 2+ repo-defined candidates
with no disambiguating signal. When exactly *one* repo-defined
candidate shares a call's bare name, ``resolver.py``'s
``_pick_candidate`` accepts it via its ``len(candidates) == 1`` fast
path — even when many call sites sharing that name are really calls to
a same-named builtin/stdlib/third-party method (JS's
``Date.now()``/``Map.has()``, Java's AssertJ ``.isTrue()``, ...), not
the repo symbol. Such calls resolve "successfully" and never appear
here at all, however inflated the resulting fan-in — this report's
"0 ambiguous sites" cannot be read as "resolution is trustworthy" on
its own; cross-check a suspiciously high fan-in with ``dekko sanity``.
``_is_noise_call``'s denylists (``_BUILTIN_METHOD_NAMES``,
``_CHAIN_BUILDER_METHOD_NAMES``, ``_RUST_STD_METHOD_NAMES``,
``_JAVA_ASSERTION_METHOD_NAMES``, ``_BUILDER_METHOD_NAMES``) catch
known instances of this shape by routing them to ``external`` instead,
but the denylist approach is reactive by construction — see
``.features/plans/round23/01-resolver-single-candidate-false-confidence.md``
for the full analysis and the deferred structural (arity-aware)
follow-up.
"""

import json
import sys
from collections import Counter

from dekko.analysis import query
from dekko.classify import is_test_path
from dekko.core.resolver import MODULE_CALLER_SUFFIX
from dekko.render.mapfile import MapIndex
from dekko.textutil import fit_to_budget, signature, token_footer

EXIT_OK = 0
EXIT_ERROR = 2
EXIT_NOT_FOUND = 3

_Triple = tuple[str, str, list[str]]


def _raw_triples(index: MapIndex) -> list[_Triple]:
    """Reconstruct ``(caller, name, candidates)`` triples from the index.

    ``ambiguous_in``/``ambiguous_out`` are each a derived, lossy view
    of the same underlying triples ``resolver.py`` recorded (candidate
    → callers, and caller → names, respectively) — neither alone
    reconstructs "for this (caller, name) pair, what was the full
    candidate list" without a join. Inverting ``ambiguous_in`` once
    (O(total ambiguous_in entries), already loaded either way) and
    joining it against ``ambiguous_out`` does that join without any
    ``MapIndex``/core-model changes.

    A ``without_tests()`` view can leave a ``(caller, name)`` pair in
    ``ambiguous_out`` with every one of its candidates filtered out of
    ``ambiguous_in`` (all candidates were themselves test symbols,
    even though the caller is production code) — ``ambiguous_out``
    only filters by the *caller's* test status, not by whether any
    candidate survived. Such a pair carries no usable candidate data,
    so it is dropped here rather than surfaced as a 0-candidate
    "ambiguous" row.

    Args:
        index: Loaded map index.

    Returns:
        Every ``(caller_id, name, candidate_ids)`` triple with at
        least one surviving candidate.
    """
    pair_candidates: dict[tuple[str, str], list[str]] = {}
    for cand, pairs in index.ambiguous_in.items():
        for caller, name in pairs:
            pair_candidates.setdefault((caller, name), []).append(cand)
    triples: list[_Triple] = []
    for caller, names in index.ambiguous_out.items():
        for name in names:
            candidates = pair_candidates.get((caller, name), [])
            if candidates:
                triples.append((caller, name, candidates))
    return triples


def _caller_path(index: MapIndex, caller_id: str) -> str:
    """The file path a caller id belongs to, module-caller-aware."""
    if caller_id.endswith(MODULE_CALLER_SUFFIX):
        return caller_id[: -len(MODULE_CALLER_SUFFIX)]
    sym = index.symbols_by_id.get(caller_id)
    return sym.path if sym else caller_id


def _caller_header(index: MapIndex, caller_id: str) -> str:
    """One-line text rendering of a caller: its site, module-aware."""
    if caller_id.endswith(MODULE_CALLER_SUFFIX):
        path = caller_id[: -len(MODULE_CALLER_SUFFIX)]
        return f"{path}  (module level)"
    sym = index.symbols_by_id.get(caller_id)
    if sym is None:
        return caller_id
    return f"{sym.path}:{sym.start_line}  {signature(sym)}"


def _caller_json(index: MapIndex, caller_id: str) -> dict:
    """Structured rendering of a caller, module-caller-aware."""
    if caller_id.endswith(MODULE_CALLER_SUFFIX):
        path = caller_id[: -len(MODULE_CALLER_SUFFIX)]
        return {
            "id": caller_id,
            "path": path,
            "line": None,
            "signature": None,
            "module_level": True,
        }
    sym = index.symbols_by_id.get(caller_id)
    if sym is None:
        return {
            "id": caller_id,
            "path": None,
            "line": None,
            "signature": None,
            "module_level": False,
        }
    return {
        "id": sym.id,
        "path": sym.path,
        "line": sym.start_line,
        "signature": signature(sym),
        "module_level": False,
    }


def by_name(
    index: MapIndex, triples: list[_Triple]
) -> list[tuple[str, int, float, int, int]]:
    """Distinct colliding names ranked by occurrence.

    Args:
        index: Loaded map index (for caller → file resolution).
        triples: ``(caller, name, candidates)`` triples.

    Returns:
        ``(name, count, avg_candidates, max_candidates, file_count)``
        tuples, sorted by count descending then name ascending
        (``file_count`` is the number of distinct caller files that
        collided on that name).
    """
    counts: Counter[str] = Counter()
    totals: dict[str, int] = {}
    maxes: dict[str, int] = {}
    files: dict[str, set[str]] = {}
    for caller, name, candidates in triples:
        counts[name] += 1
        totals[name] = totals.get(name, 0) + len(candidates)
        maxes[name] = max(maxes.get(name, 0), len(candidates))
        files.setdefault(name, set()).add(_caller_path(index, caller))
    ranked = [
        (name, count, totals[name] / count, maxes[name], len(files[name]))
        for name, count in counts.items()
    ]
    ranked.sort(key=lambda row: (-row[1], row[0]))
    return ranked


def by_file(index: MapIndex, triples: list[_Triple]) -> list[tuple[str, int]]:
    """Caller files ranked by ambiguous-site count.

    Args:
        index: Loaded map index (for caller → file resolution).
        triples: ``(caller, name, candidates)`` triples.

    Returns:
        ``(path, count)`` tuples, sorted by count descending then path
        ascending.
    """
    counts = Counter(_caller_path(index, caller) for caller, _, _ in triples)
    return sorted(counts.items(), key=lambda row: (-row[1], row[0]))


def ambiguous_rate(index: MapIndex, total_ambiguous: int) -> float:
    """Fraction of call attempts that resolved ambiguously.

    Args:
        index: Loaded map index.
        total_ambiguous: Count of ambiguous ``(caller, name)`` sites.

    Returns:
        ``total_ambiguous / (resolved + total_ambiguous)``, or ``0.0``
        when there were no call attempts at all.
    """
    resolved = sum(len(v) for v in index.calls_out.values())
    denom = resolved + total_ambiguous
    return total_ambiguous / denom if denom else 0.0


def compute(index: MapIndex, top: int) -> dict:
    """Build the full ambiguous-edge report document.

    Args:
        index: Loaded map index.
        top: How many entries to keep in each ranked list.

    Returns:
        A JSON-serializable report dict.
    """
    triples = _raw_triples(index)
    total = len(triples)
    names = by_name(index, triples)
    files = by_file(index, triples)
    resolved = sum(len(v) for v in index.calls_out.values())
    return {
        "total_ambiguous_sites": total,
        "distinct_names": len(names),
        "distinct_files": len(files),
        "resolved_edges": resolved,
        "ambiguous_rate": round(ambiguous_rate(index, total), 4),
        "top_by_name": [
            {
                "name": name,
                "count": count,
                "avg_candidates": round(avg, 2),
                "max_candidates": mx,
                "files": nf,
            }
            for name, count, avg, mx, nf in names[:top]
        ],
        "top_by_file": [
            {"path": path, "count": count} for path, count in files[:top]
        ],
    }


def _print_summary_text(doc: dict) -> None:
    """Print the default (no ``--by``/``--name``) text summary."""
    denom = doc["resolved_edges"] + doc["total_ambiguous_sites"]
    lines = [
        f"dekko: {doc['total_ambiguous_sites']} ambiguous call sites, "
        f"{doc['distinct_names']} distinct colliding names, across "
        f"{doc['distinct_files']} files",
        f"  resolved edges: {doc['resolved_edges']:,} · ambiguous "
        f"rate: {doc['ambiguous_rate']:.1%} "
        f"({doc['total_ambiguous_sites']} of {denom:,} call attempts)",
        "",
        "top colliding names:",
    ]
    lines.extend(
        f"    {row['count']:>4}  {row['name']}  (avg "
        f"{row['avg_candidates']:.1f} candidates, {row['files']} files)"
        for row in doc["top_by_name"]
    )
    lines.append("")
    lines.append("top concentrated files:")
    lines.extend(
        f"    {row['count']:>4}  {row['path']}" for row in doc["top_by_file"]
    )
    lines.append("")
    example = doc["top_by_name"][0]["name"] if doc["top_by_name"] else "NAME"
    lines.append(
        "note: use `--by name` / `--by file` for the full list, or "
        f"`--name {example}` to see every caller + candidate set for "
        "one name."
    )
    text = "\n".join(lines)
    print(text)
    print(token_footer(text))


def _run_summary(index: MapIndex, top: int, as_json: bool) -> int:
    """Handle the default (no ``--by``/``--name``) summary view."""
    doc = compute(index, top)
    if as_json:
        print(json.dumps(doc, indent=2))
        return EXIT_OK
    if doc["total_ambiguous_sites"] == 0:
        print("dekko: no ambiguous call sites")
        return EXIT_OK
    _print_summary_text(doc)
    return EXIT_OK


def _run_by_name(
    index: MapIndex, limit: int, budget: int | None, as_json: bool
) -> int:
    """Handle ``--by name``: every colliding name, ranked."""
    ranked = by_name(index, _raw_triples(index))
    if as_json:
        entries = [
            {
                "name": name,
                "count": count,
                "avg_candidates": round(avg, 2),
                "max_candidates": mx,
                "files": nf,
            }
            for name, count, avg, mx, nf in ranked
        ]
        serialized = [json.dumps(e) for e in entries]
        kept_ser, meter = fit_to_budget(serialized, budget, limit)
        doc = {"results": entries[: len(kept_ser)], "meta": meter.as_dict()}
        print(json.dumps(doc, indent=2))
        return EXIT_OK

    if not ranked:
        print("dekko: no ambiguous call sites")
        return EXIT_OK
    rows = [
        f"  {count:>4}  {name}  {nf} files, avg {avg:.1f} candidates  "
        f"(dekko ambiguous --name {name} for detail)"
        for name, count, avg, _mx, nf in ranked
    ]
    kept, meter = fit_to_budget(rows, budget, limit)
    for row in kept:
        print(row)
    print(meter.footer())
    return EXIT_OK


def _run_by_file(
    index: MapIndex, limit: int, budget: int | None, as_json: bool
) -> int:
    """Handle ``--by file``: every caller file, ranked."""
    ranked = by_file(index, _raw_triples(index))
    if as_json:
        entries = [{"path": path, "count": count} for path, count in ranked]
        serialized = [json.dumps(e) for e in entries]
        kept_ser, meter = fit_to_budget(serialized, budget, limit)
        doc = {"results": entries[: len(kept_ser)], "meta": meter.as_dict()}
        print(json.dumps(doc, indent=2))
        return EXIT_OK

    if not ranked:
        print("dekko: no ambiguous call sites")
        return EXIT_OK
    rows = [f"  {count:>4}  {path}" for path, count in ranked]
    kept, meter = fit_to_budget(rows, budget, limit)
    for row in kept:
        print(row)
    print(meter.footer())
    return EXIT_OK


def _caller_sort_key(index: MapIndex, caller_id: str) -> tuple:
    """Sort key for ``--name`` drill-down rows: prod before test."""
    path = _caller_path(index, caller_id)
    return (is_test_path(path), path, caller_id)


def _name_row_text(
    index: MapIndex, caller: str, candidate_ids: list[str]
) -> str:
    """One caller's site + its full candidate list, text form."""
    candidates = [
        index.symbols_by_id[cid]
        for cid in candidate_ids
        if cid in index.symbols_by_id
    ]
    lines = [f"  {_caller_header(index, caller)}"]
    lines.extend(f"  {row}" for row in query.render_candidates(candidates))
    return "\n".join(lines)


def _name_entry_json(
    index: MapIndex, caller: str, candidate_ids: list[str]
) -> dict:
    """One caller's site + its full candidate list, structured."""
    candidates = [
        index.symbols_by_id[cid]
        for cid in candidate_ids
        if cid in index.symbols_by_id
    ]
    return {
        "caller": _caller_json(index, caller),
        "candidates": [query._sym_json(index, c) for c in candidates],
    }


def _run_name(
    index: MapIndex, name: str, limit: int, budget: int | None, as_json: bool
) -> int:
    """Handle ``--name NAME``: every caller site + candidate set."""
    triples = _raw_triples(index)
    matches = [(caller, cands) for caller, nm, cands in triples if nm == name]
    if not matches:
        distinct_names = sorted({nm for _, nm, _ in triples})
        print(f"dekko: no ambiguous calls to '{name}'", file=sys.stderr)
        suggestions = query._close_names(name, distinct_names)
        if suggestions:
            print("closest colliding names:", file=sys.stderr)
            for suggestion in suggestions:
                print(f"  {suggestion}", file=sys.stderr)
        return EXIT_NOT_FOUND

    matches.sort(key=lambda pair: _caller_sort_key(index, pair[0]))
    if as_json:
        entries = [_name_entry_json(index, c, cands) for c, cands in matches]
        serialized = [json.dumps(e) for e in entries]
        kept_ser, meter = fit_to_budget(serialized, budget, limit)
        doc = {
            "name": name,
            "results": entries[: len(kept_ser)],
            "meta": meter.as_dict(),
        }
        print(json.dumps(doc, indent=2))
        return EXIT_OK

    rows = [_name_row_text(index, c, cands) for c, cands in matches]
    header = f"dekko: '{name}' called ambiguously from {len(matches)} site(s)"
    kept, meter = fit_to_budget(rows, budget, limit, prefix=header)
    print(header)
    for row in kept:
        print(row)
    print(meter.footer())
    return EXIT_OK


def run(
    index: MapIndex,
    by: str | None,
    name: str | None,
    top: int,
    limit: int,
    budget: int | None,
    as_json: bool,
) -> int:
    """Execute ``dekko ambiguous`` against a loaded index.

    Callers must enforce ``by``/``name`` mutual exclusivity themselves
    (the CLI does this in ``cli.py``'s ``run_ambiguous``, matching the
    project's existing "give one, not both" precedent for
    ``workset --rev``/``--symbol``) — this function assumes at most
    one of the two is set.

    Args:
        index: Loaded map index.
        by: ``"name"``/``"file"`` to list the full ranked group, or
            ``None`` for the default top-N summary.
        name: Drill down to every caller + candidate set for one
            colliding name, or ``None``.
        top: Entries per ranked list in the default summary.
        limit: Max text/JSON result rows for ``--by``/``--name`` views.
        budget: Approximate token budget for those rows, or ``None``.
        as_json: Emit structured JSON instead of text.

    Returns:
        ``0`` on success (always, for the summary/``--by`` views —
        "no ambiguity" is a legitimate answer, not a failure), ``3``
        when ``--name`` names something with no ambiguous occurrences.
    """
    if name is not None:
        return _run_name(index, name, limit, budget, as_json)
    if by == "name":
        return _run_by_name(index, limit, budget, as_json)
    if by == "file":
        return _run_by_file(index, limit, budget, as_json)
    return _run_summary(index, top, as_json)
