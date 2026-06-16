"""Proactive orientation: the opt-in push layer (F4).

Every other dekko surface is *pull* — it helps only when the agent
knows to ask. This module is the thin **push** layer that orients a
fresh agent and nudges it toward dekko's structural tools before it
over-reads. It is pure orchestration of the existing pull tools
(``summary``, ``outline``): it adds no extraction, no schema, and no
state, and it is inert until a user wires it into a hook (see the README
"Proactive orientation" section) or the bundled ``dekko-orient`` skill.

Two modes behind one command:

* **session** (default) — a fixed steering preamble plus the budgeted
  ``summary`` digest, for a SessionStart hook. Uses the auto-regenerating
  load (correctness over speed; it fires once per session).
* **``--read PATH``** — a one-line advisory to ``outline`` a file before
  reading it whole, emitted only when the file is large. Fires on the
  hot path (every read), so it never regenerates, never blocks, and
  degrades to silence on any miss.
"""

import json
from pathlib import Path

from . import mapfile, outline, summary
from .mapfile import MapIndex
from .textutil import fit_to_budget

EXIT_OK = 0

DEFAULT_BUDGET = 1500
DEFAULT_THRESHOLD = 1000

_PREAMBLE = (
    "dekko orientation — this repo has a .dekko/ map. Prefer dekko's\n"
    "structural tools over reading whole files:\n"
    "  • outline <file>  — a file's shape (signatures, no bodies), "
    "~1/10 cost\n"
    "  • workset [REV]    — all you need to work a change, one budget\n"
    "  • query/context <sym> — locate & understand a symbol + callers\n"
    "  • affected [REV]   — which tests a change impacts\n"
    "Notes show inline in query/context output; keep them current."
)


def _session(index: MapIndex, budget: int | None, as_json: bool) -> int:
    """Render the orientation digest: preamble + budgeted summary."""
    body = summary.render_text(index).splitlines()
    kept, meter = fit_to_budget(body, budget, None, prefix=_PREAMBLE)
    if as_json:
        doc = {
            "preamble": _PREAMBLE,
            "summary": "\n".join(kept),
            "meta": meter.as_dict(),
        }
        print(json.dumps(doc, indent=2))
        return EXIT_OK
    print(_PREAMBLE)
    for line in kept:
        print(line)
    print(meter.footer())
    return EXIT_OK


def _rel_path(root: Path, read_path: str) -> str | None:
    """Normalize a read target to a repo-relative POSIX path, or None."""
    p = Path(read_path)
    if not p.is_absolute():
        p = root / p
    try:
        rel = p.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    return rel.as_posix()


def _advise(
    index: MapIndex, root: Path, read_path: str, threshold: int
) -> int:
    """Nudge to outline a large file; stay silent otherwise. Never blocks."""
    rel = _rel_path(root, read_path)
    if rel is None:
        return EXIT_OK
    est = outline.size_estimate(index, root, rel)
    if est is None:
        return EXIT_OK
    full, outline_tokens = est
    if full < threshold:
        return EXIT_OK
    pct = round(100 * outline_tokens / full)
    print(
        f"dekko: {rel} ≈ {full} tok — outline ≈ {outline_tokens} tok "
        f"({pct}%); run `dekko outline {rel}` before reading it whole."
    )
    return EXIT_OK


def run(
    root: Path,
    read_path: str | None,
    budget: int | None,
    threshold: int,
    as_json: bool,
    no_regen: bool,
) -> int:
    """Orient an agent (session) or nudge before a large read (--read).

    Args:
        root: Repository root containing the map.
        read_path: If given, advisory mode for this file; else session.
        budget: Session-digest token budget, or ``None``.
        threshold: ``--read`` advises only when the file reaches this
            many tokens.
        as_json: Emit structured JSON (session mode only).
        no_regen: Fail instead of regenerating a stale map (session only).

    Returns:
        Process exit code. Advisory mode is always ``0``; session mode
        mirrors the read-command codes (``5`` for a stale map under
        ``--no-regen``).
    """
    if read_path is not None:
        index = mapfile.load_map(root)
        if index is None:
            return EXIT_OK
        return _advise(index, root, read_path, threshold)
    from . import cli

    index, code = cli._load_or_regen(root, no_regen)
    if index is None:
        return code
    return _session(index, budget, as_json)
