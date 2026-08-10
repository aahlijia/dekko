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

# Kept as its own line so ``_preamble()`` can drop it when ``search``
# isn't actually available (see ``_search_available`` below) —
# steering an agent toward a command that then fails is worse than
# staying silent about it. See IMPLEMENTATION-PLAN.md's 3.1 note:
# cline's report caught this banner recommending ``search``
# unconditionally, even when the invoked ``dekko`` binary predated the
# subcommand entirely.
_SEARCH_LINE = (
    "  • search <text>    — find a symbol by what it does, not its "
    "name (use before grepping blind)\n"
)

_PREAMBLE = (
    "dekko orientation — this repo has a .dekko/ map. Prefer dekko's\n"
    "structural tools over grep/reading whole files:\n"
    f"{_SEARCH_LINE}"
    "  • outline <file>  — a file's shape (signatures, no bodies), "
    "~1/10 cost\n"
    "  • workset [REV]    — all you need to work a change, one budget\n"
    "  • query/context <sym> — locate & understand a symbol + callers\n"
    "  • affected [REV]   — which tests a change impacts\n"
    "Notes show inline in query/context output; keep them current."
)


def _search_available() -> bool:
    """Whether this process's own ``search`` subcommand is usable.

    Guards ``_preamble()`` against steering an agent toward ``search``
    when it isn't actually there to use — the real-world case that
    prompted this (a global ``uv tool install`` binary predating this
    branch's ``search`` subcommand entirely) can't be detected from
    inside a *different*, up-to-date process, but a broken/partial
    install of *this* process's own package is a real failure mode a
    plain ``import`` guard does catch, so this stays defensive rather
    than a no-op ``True``.
    """
    try:
        from . import search as _search
    except ImportError:  # pragma: no cover - defensive, see docstring
        return False
    return hasattr(_search, "run")


def _preamble() -> str:
    """The steering preamble, minus the ``search`` line when unusable."""
    if _search_available():
        return _PREAMBLE
    return _PREAMBLE.replace(_SEARCH_LINE, "")


def _session(index: MapIndex, budget: int | None, as_json: bool) -> int:
    """Render the orientation digest: preamble + budgeted summary."""
    preamble = _preamble()
    body = summary.render_text(index).splitlines()
    kept, meter = fit_to_budget(body, budget, None, prefix=preamble)
    if as_json:
        doc = {
            "preamble": preamble,
            "summary": "\n".join(kept),
            "meta": meter.as_dict(),
        }
        print(json.dumps(doc, indent=2))
        return EXIT_OK
    print(preamble)
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
