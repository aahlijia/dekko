"""A compact repo digest: the middle ground between MAP.md and a query.

``dekko summary`` renders ~40 lines an agent or human can read whole:
header counts, a per-directory rollup (with coupling and a purpose
line), the load-bearing and orchestrating symbols, entry points, and
any parse errors. It reuses ``stats`` for the global rankings and adds
the directory view on top.
"""

import json
import subprocess
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from . import stats
from .mapfile import MapIndex, format_unsupported
from .model import TYPE_KINDS
from .resolver import MODULE_CALLER_SUFFIX
from .textutil import dir_of, fit_to_budget, oneline, signature

# Index-file stems whose doc best describes their directory.
_INDEX_STEMS = ("__init__", "mod", "lib", "index")
_TOP = 5
_MAX_DIRS = 12
_MAX_ENTRYPOINTS = 8
_MAX_HOTSPOTS = 10
# 2.3: cap on the parse-errors list so an uncapped grammar-gap doesn't
# dominate the digest (spring-boot/tensorflow/zed showed 97%+ of
# `summary`'s output being one repeated "no parser" message).
_MAX_PARSE_ERRORS = 15
# Kinds that are never plausible entry points: type definitions and
# module-scope data bindings, not callables (3.5 entrypoints noise).
_NON_ENTRYPOINT_KINDS = TYPE_KINDS | {"variable"}
# How far back churn is measured for the risk view.
_CHURN_WINDOW_DAYS = 90
# 2.3 item 2: bare-CLI default token cap. `summary` is meant to be a
# fuller digest than `orient`'s 1500-token preamble+digest, so this is
# deliberately larger — big enough to hold every section on a typical
# repo, small enough that an uncapped-by-accident future addition can't
# silently blow up an agent's context the way the pre-fix parse-errors
# list did (see `_MAX_PARSE_ERRORS` above). An explicit `--budget N`
# (any size) always overrides this default, same escape hatch `orient`
# already offers for its own default.
DEFAULT_BUDGET = 5000


def _id_dir(sym_id: str) -> str:
    """Directory of the file a symbol or module id belongs to."""
    return dir_of(sym_id.split("::", 1)[0])


def _dir_purpose(index: MapIndex, directory: str, files: list[str]) -> str:
    """Best purpose line for a directory: its index file's doc, else any."""
    for path in files:
        stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        if stem in _INDEX_STEMS and index.docs_by_path.get(path):
            return index.docs_by_path[path]
    for path in sorted(files):
        doc = index.docs_by_path.get(path)
        if doc:
            return doc
    return ""


def _edge_coupling(index: MapIndex) -> dict[str, tuple[int, int]]:
    """Per-directory ``(internal_edges, cross_dir_edges)`` counts."""
    coupling: dict[str, list[int]] = {}
    for caller, callees in index.calls_out.items():
        caller_dir = _id_dir(caller)
        for callee in callees:
            callee_dir = _id_dir(callee)
            if caller_dir == callee_dir:
                coupling.setdefault(caller_dir, [0, 0])[0] += 1
            else:
                coupling.setdefault(caller_dir, [0, 0])[1] += 1
                coupling.setdefault(callee_dir, [0, 0])[1] += 1
    return {d: (n[0], n[1]) for d, n in coupling.items()}


def _directories(index: MapIndex) -> list[dict]:
    """Per-directory rollup rows, most symbols first."""
    files_by_dir: dict[str, list[str]] = {}
    for path in index.languages_by_path:
        files_by_dir.setdefault(dir_of(path), []).append(path)
    coupling = _edge_coupling(index)
    rows = []
    for directory, files in files_by_dir.items():
        symbols = sum(len(index.symbols_by_path.get(p, [])) for p in files)
        internal, cross = coupling.get(directory, (0, 0))
        rows.append(
            {
                "path": directory,
                "files": len(files),
                "symbols": symbols,
                "internal_edges": internal,
                "cross_edges": cross,
                "purpose": _dir_purpose(index, directory, files),
            }
        )
    rows.sort(key=lambda r: (-r["symbols"], r["path"]))
    return rows


def _entrypoints(index: MapIndex) -> list:
    """Likely entry points: ``main`` plus uncalled exported/decorated.

    Excludes test-path symbols (a decorated pytest method is not an
    entry point) and non-callable kinds (module-scope data bindings,
    type definitions) — those are meaningful for ``unused``'s
    deliberately-permissive conservative-roots logic, but not for
    "here's where execution begins" (this is a narrower,
    presentation-only filter; ``unused._is_root`` is left untouched).
    """
    found = []
    for sym_id, sym in index.symbols_by_id.items():
        if sym.test or sym.kind in _NON_ENTRYPOINT_KINDS:
            continue
        uncalled = not index.calls_in.get(sym_id)
        if sym.name == "main" or (
            uncalled and (sym.decorated or sym.exported)
        ):
            found.append(sym)
    found.sort(key=lambda s: (s.path, s.start_line))
    return found


def compute(index: MapIndex) -> dict:
    """Build the summary document."""
    base = stats.compute(index, _TOP)
    return {
        "root": index.root_label,
        "files": base["files"],
        "symbols": base["symbols"],
        "edges": base["edges"],
        "languages": base["languages"],
        "directories": _directories(index)[:_MAX_DIRS],
        "top_fan_in": base["top_fan_in"],
        "top_fan_out": base["top_fan_out"],
        "largest_files": base["largest_files"],
        "entrypoints": [
            {"id": s.id, "signature": signature(s)}
            for s in _entrypoints(index)[:_MAX_ENTRYPOINTS]
        ],
        "parse_errors": [
            {"path": p, "error": e}
            for p, e in sorted(index.errors_by_path.items())
        ][:_MAX_PARSE_ERRORS],
        "parse_errors_total": len(index.errors_by_path),
        "parse_errors_breakdown": _parse_error_breakdown(index),
        "unsupported": (index.provenance or {}).get("unsupported"),
    }


def _git_churn(root: Path, window_days: int) -> Counter[str]:
    """Per-file commit-touch counts over the recent window.

    Best-effort: any git failure (no repo, no history, git missing)
    yields an empty counter so callers can omit the section silently.

    Args:
        root: Repository root.
        window_days: How many days back to count file changes.

    Returns:
        Repo-relative path → number of commits that touched it.
    """
    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "log",
                f"--since={window_days} days ago",
                "--pretty=format:",
                "--name-only",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return Counter()
    if proc.returncode != 0:
        return Counter()
    return Counter(
        line.strip() for line in proc.stdout.splitlines() if line.strip()
    )


def file_churn(
    root: Path, window_days: int = _CHURN_WINDOW_DAYS
) -> Counter[str]:
    """Per-file commit-touch counts over the recent window.

    Public, best-effort wrapper over the git-churn reader so other
    renderers (the lean map's symbol centrality) can reuse the same
    signal. Returns an empty counter on any git failure, so callers
    degrade cleanly to a churn-free ranking.

    Args:
        root: Repository root.
        window_days: How many days back to count file changes.

    Returns:
        Repo-relative path → number of commits that touched it.
    """
    return _git_churn(root, window_days)


def _file_fan_in(index: MapIndex, path: str) -> int:
    """Total incoming call edges to every symbol defined in a file."""
    return sum(
        len(index.calls_in.get(sym.id, []))
        for sym in index.symbols_by_path.get(path, [])
    )


def churn_hotspots(
    index: MapIndex,
    root: Path,
    window_days: int = _CHURN_WINDOW_DAYS,
    top: int = _MAX_HOTSPOTS,
) -> list[dict]:
    """Files that change often *and* are widely depended on.

    Risk view: recent churn (commits touching a file) weighted by the
    file's normalized fan-in. A frequently edited, load-bearing file is
    where a regression spreads furthest. Strictly best-effort — returns
    an empty list whenever git is unavailable or the tree has no
    history, so the caller can omit the section cleanly.

    Args:
        index: Loaded map index.
        root: Repository root (for ``git log``).
        window_days: Churn window in days.
        top: Maximum number of rows to return.

    Returns:
        Rows ``{"path", "churn", "fan_in", "score"}``, highest risk
        first; empty when there is no usable churn data.
    """
    churn = _git_churn(root, window_days)
    if not churn:
        return []
    fan_in = {
        path: _file_fan_in(index, path) for path in index.languages_by_path
    }
    max_fan_in = max(fan_in.values(), default=0)
    if not max_fan_in:
        return []
    rows = []
    for path, n_fan_in in fan_in.items():
        commits = churn.get(path, 0)
        if not commits or not n_fan_in:
            continue
        score = commits * (n_fan_in / max_fan_in)
        rows.append(
            {
                "path": path,
                "churn": commits,
                "fan_in": n_fan_in,
                "score": round(score, 1),
            }
        )
    rows.sort(key=lambda r: (-r["score"], r["path"]))
    return rows[:top]


def _fmt_module(sym_id: str) -> str:
    """Human label for a hotspot id (module-level origins included)."""
    if sym_id.endswith(MODULE_CALLER_SUFFIX):
        return f"{sym_id[: -len(MODULE_CALLER_SUFFIX)]} (module level)"
    return sym_id


def render_text(index: MapIndex) -> str:
    """Render the digest as compact text."""
    doc = compute(index)
    lines = [
        f"dekko: {doc['root']} — {doc['files']} files, "
        f"{doc['symbols']} symbols, {doc['edges']} edges"
    ]
    mix = ", ".join(
        f"{lng['language']} {lng['files']}f/{lng['symbols']}s"
        for lng in doc["languages"]
    )
    lines.append(f"languages: {mix}")
    note = format_unsupported(index.provenance)
    if note:
        lines.append(f"coverage: {note} — results below may be incomplete")
    lines.append("directories (files/symbols, int+cross edges):")
    for d in doc["directories"]:
        suffix = f"  — {d['purpose']}" if d["purpose"] else ""
        lines.append(
            f"  {d['files']:>3}f {d['symbols']:>4}s  "
            f"{d['internal_edges']}+{d['cross_edges']}  "
            f"{d['path']}/{suffix}"
        )
    _append_ranked(lines, "load-bearing (fan-in):", doc["top_fan_in"])
    _append_ranked(lines, "orchestrators (fan-out):", doc["top_fan_out"])
    if doc["largest_files"]:
        lines.append("largest files (symbols):")
        lines += [
            f"  {f['symbols']:>4}  {f['path']}" for f in doc["largest_files"]
        ]
    if doc["entrypoints"]:
        lines.append("entrypoints:")
        lines += [f"  {e['signature']}" for e in doc["entrypoints"]]
    if doc["parse_errors"]:
        lines.append("parse errors:")
        lines += [f"  {e['path']}: {e['error']}" for e in doc["parse_errors"]]
        hidden = doc["parse_errors_total"] - len(doc["parse_errors"])
        if hidden > 0:
            lines.append(
                f"  ... and {hidden} more ({doc['parse_errors_breakdown']})"
            )
    return "\n".join(lines)


def _parse_error_breakdown(index: MapIndex) -> str:
    """Per-language counts across *all* parse errors, for the capped footer.

    Collapsing to per-language counts is more informative than a raw
    truncation when the noise is overwhelmingly one repeated message
    (e.g. a grammar not present in the offline Tier-1 set) — mirrors
    ``mapfile.format_unsupported``'s "N files unparsed — no parser
    for: lang (N), ..." shape, but for parse failures rather than
    unsupported-grammar skips.
    """
    by_lang: Counter[str] = Counter()
    for path in index.errors_by_path:
        by_lang[index.languages_by_path.get(path) or "unknown"] += 1
    return ", ".join(f"{lang} ({n})" for lang, n in sorted(by_lang.items()))


def _append_ranked(lines: list[str], title: str, ranked: list[dict]) -> None:
    """Append a labelled fan-in/out ranking, if non-empty."""
    if not ranked:
        return
    lines.append(title)
    lines += [f"  {r['count']:>4}  {_fmt_module(r['id'])}" for r in ranked]


def render_overview(
    doc: dict,
    href: Callable[[str], str],
    diagram: list[str] | None = None,
    hotspots: list[dict] | None = None,
) -> list[str]:
    """Render a ``compute`` document as a MAP.md ``## Overview`` section.

    The markdown skin of :func:`compute`: the same numbers as
    ``dekko summary``, linked to the file and symbol anchors MAP.md
    uses so the digest and the document agree.

    Args:
        doc: A document produced by :func:`compute`.
        href: Maps a file path or symbol id to a markdown link target
            (e.g. a shared ``_Links.href``); it handles both
            single-file (``#anchor``) and sharded (``page.md#anchor``)
            shapes.
        diagram: Optional pre-rendered markdown lines (e.g. a mermaid
            block) inserted after the directory table.
        hotspots: Optional churn x fan-in rows from
            :func:`churn_hotspots`; the risk table is omitted when
            empty or ``None``.

    Returns:
        Markdown lines for the overview section.
    """
    lines = ["## Overview", ""]
    lines += _overview_unsupported(doc.get("unsupported"))
    lines += _overview_dirs(doc["directories"])
    if diagram:
        lines += diagram
    lines += _overview_ranked(
        "Load-bearing", "most called", doc["top_fan_in"], href
    )
    lines += _overview_ranked(
        "Orchestrators", "most calls out", doc["top_fan_out"], href
    )
    lines += _overview_largest(doc.get("largest_files", []), href)
    lines += _overview_hotspots(hotspots or [], href)
    lines += _overview_entrypoints(doc["entrypoints"], href)
    lines += _overview_errors(doc, href)
    return lines


def _overview_unsupported(unsupported: dict | None) -> list[str]:
    """A coverage warning when confirmed-unsupported files were skipped."""
    if not unsupported:
        return []
    by_lang = unsupported.get("languages", {})
    detail = ", ".join(f"{lang} ({n})" for lang, n in by_lang.items())
    count = unsupported.get("count", 0)
    return [
        f"> **Coverage:** {count} file(s) skipped — no parser for: "
        f"{detail}. Results throughout this map may be incomplete "
        "for symbols only used in these files.",
        "",
    ]


def _overview_largest(
    largest: list[dict], href: Callable[[str], str]
) -> list[str]:
    """A linked list of the files with the most symbols."""
    if not largest:
        return []
    lines = ["**Largest files** (symbols):", ""]
    for f in largest:
        link = f"[`{f['path']}`]({href(f['path'])})"
        lines.append(f"- {link} — {f['symbols']}")
    lines.append("")
    return lines


def _overview_hotspots(
    hotspots: list[dict], href: Callable[[str], str]
) -> list[str]:
    """Churn x fan-in risk table, linked to each file's section."""
    if not hotspots:
        return []
    lines = [
        "**Hotspots** (recent churn x fan-in — change carefully):",
        "",
        "| File | Commits | Fan-in | Risk |",
        "|---|--:|--:|--:|",
    ]
    for h in hotspots:
        link = f"[`{h['path']}`]({href(h['path'])})"
        lines.append(
            f"| {link} | {h['churn']} | {h['fan_in']} | {h['score']} |"
        )
    lines.append("")
    return lines


def _overview_dirs(dirs: list[dict]) -> list[str]:
    """Per-directory rollup table."""
    if not dirs:
        return []
    lines = [
        "| Directory | Files | Symbols | Internal | Cross-dir | Purpose |",
        "|---|--:|--:|--:|--:|---|",
    ]
    for d in dirs:
        purpose = oneline(d["purpose"], 60) if d["purpose"] else ""
        lines.append(
            f"| `{d['path']}/` | {d['files']} | {d['symbols']} | "
            f"{d['internal_edges']} | {d['cross_edges']} | {purpose} |"
        )
    lines.append("")
    return lines


def _overview_ranked(
    title: str,
    gloss: str,
    ranked: list[dict],
    href: Callable[[str], str],
) -> list[str]:
    """A linked fan-in/fan-out ranking with its degree count."""
    if not ranked:
        return []
    lines = [f"**{title}** ({gloss}):", ""]
    for r in ranked:
        link = f"[`{r['signature']}`]({href(r['id'])})"
        lines.append(f"- {link} — {r['count']}")
    lines.append("")
    return lines


def _overview_entrypoints(
    entrypoints: list[dict], href: Callable[[str], str]
) -> list[str]:
    """Linked entry-point list."""
    if not entrypoints:
        return []
    lines = ["**Entry points:**", ""]
    lines += [f"- [`{e['signature']}`]({href(e['id'])})" for e in entrypoints]
    lines.append("")
    return lines


def _overview_errors(doc: dict, href: Callable[[str], str]) -> list[str]:
    """Linked parse-error list, capped (2.3) with a per-language footer."""
    errors = doc["parse_errors"]
    if not errors:
        return []
    lines = ["**Parse errors:**", ""]
    lines += [
        f"- [`{e['path']}`]({href(e['path'])}): {e['error']}" for e in errors
    ]
    hidden = doc["parse_errors_total"] - len(errors)
    if hidden > 0:
        lines.append(
            f"- ... and {hidden} more ({doc['parse_errors_breakdown']})"
        )
    lines.append("")
    return lines


def run(
    index: MapIndex,
    as_json: bool,
    budget: int | None = None,
) -> int:
    """Print the summary as text or JSON.

    Args:
        index: Loaded map index.
        as_json: Emit structured JSON instead of text (never budgeted).
        budget: Approximate token cap for the text rendering; trailing
            sections (entry points, parse errors) are shed first and a
            cost footer reports the omission. ``None`` disables the cap.
            The digest scales with repo size (a large monorepo renders
            ~30k chars), so budget-less output can dominate an agent's
            context.

    Returns:
        Always ``0``.
    """
    if as_json:
        print(json.dumps(compute(index), indent=2))
        return 0
    text = render_text(index)
    if budget is None:
        print(text)
        return 0
    kept, meter = fit_to_budget(text.splitlines(), budget, None)
    print("\n".join(kept))
    print(meter.footer())
    return 0
