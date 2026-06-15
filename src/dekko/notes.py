"""Symbol-anchored notes: durable, committable annotations on code.

Notes live in ``.dekko/notes.json`` keyed by symbol id
(``path::Qualified.name``). They are meant to be committed — the
``.dekko/.gitignore`` keeps this one file tracked while ignoring the
generated map and cache. Read commands surface a symbol's notes inline,
so an agent sees them before editing.

Because ids embed a file path and qualname, renaming or moving a symbol
orphans its notes; ``list --orphaned`` finds notes whose id is no longer
in the map so they can be re-anchored or removed.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from . import cache as cache_mod

NOTES_VERSION = 1
_NOTES_FILE = "notes.json"

EXIT_OK = 0
EXIT_NOT_FOUND = 3


def _notes_path(root: Path) -> Path:
    """Location of the notes file under a repo root."""
    return root / cache_mod.CACHE_DIR / _NOTES_FILE


def load(root: Path) -> dict[str, list[dict]]:
    """Load the symbol-id → note-records map (empty when absent)."""
    try:
        doc = json.loads(_notes_path(root).read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    notes = doc.get("notes")
    return notes if isinstance(notes, dict) else {}


def texts_by_id(root: Path) -> dict[str, list[str]]:
    """Symbol id → note texts, for inline rendering."""
    return {
        sym_id: [r.get("text", "") for r in records]
        for sym_id, records in load(root).items()
    }


def save(root: Path, notes: dict[str, list[dict]]) -> None:
    """Write the notes file, ensuring it stays git-tracked.

    Empty symbol-id entries are dropped so a removed note leaves no
    residue.
    """
    cache_mod.ensure_notes_tracked(root)
    pruned = {sid: recs for sid, recs in notes.items() if recs}
    doc = {"version": NOTES_VERSION, "notes": pruned}
    _notes_path(root).write_text(json.dumps(doc, indent=2) + "\n")


def add(root: Path, sym_id: str, text: str) -> dict:
    """Append a note to a symbol and persist it.

    Args:
        root: Repository root.
        sym_id: Resolved symbol id to anchor the note to.
        text: Note body.

    Returns:
        The stored note record.
    """
    notes = load(root)
    record = {
        "text": text,
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    notes.setdefault(sym_id, []).append(record)
    save(root, notes)
    return record


def remove(root: Path, sym_id: str, index: int | None) -> int:
    """Remove one note (by index) or all notes for a symbol.

    Args:
        root: Repository root.
        sym_id: Symbol id whose notes to drop.
        index: 1-based note index to remove, or ``None`` for all.

    Returns:
        The number of notes removed.
    """
    notes = load(root)
    records = notes.get(sym_id)
    if not records:
        return 0
    if index is None:
        removed = len(records)
        notes.pop(sym_id, None)
    elif 1 <= index <= len(records):
        records.pop(index - 1)
        removed = 1
    else:
        return 0
    save(root, notes)
    return removed


def orphaned(root: Path, known_ids: set[str]) -> dict[str, list[dict]]:
    """Notes whose symbol id is not among ``known_ids``."""
    return {
        sym_id: records
        for sym_id, records in load(root).items()
        if sym_id not in known_ids
    }
