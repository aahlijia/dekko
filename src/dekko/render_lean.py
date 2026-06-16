"""The lean map's file backbone (FR1): the never-elided file index.

Every in-scope file gets one line — its basename and one-line purpose —
grouped by directory so the path prefix is paid once (FR6 dense
encoding). Production files are the *floor*: their paths are never
dropped. Test, fixture, and vendored files are marked ``demotable`` so
the degradation ladder (NFR2) can fold a whole directory of them to a
single line.

This module renders the floor only. The budget ladder and the wider
``lean`` command (header, Meter, symbol/signature layers) compose on top
of the structured groups produced here; ``compute_backbone`` is a pure,
deterministic function of the map so the same input yields byte-stable
output (NFR3).
"""

from dataclasses import dataclass

from .classify import is_test_path
from .mapfile import MapIndex
from .textutil import dir_of, oneline

# Default (and maximum) purpose width. The render layer may narrow this
# toward 0 as the budget tightens (the FR1 floor sub-ladder); it never
# widens past what was captured at compute time.
LEAN_PURPOSE_WIDTH = 72
# Separator between a file's basename and its purpose. Two spaces, no
# column alignment: padding would spend tokens on whitespace for a
# reader (Claude) that does not need it.
SEP = "  "


@dataclass(frozen=True)
class BackboneRow:
    """One file's line in the navigation index.

    Attributes:
        path: Repo-relative POSIX path (the stable id).
        purpose: One-line purpose, already collapsed and truncated to
            ``LEAN_PURPOSE_WIDTH``, or ``""`` when the file has no
            module doc.
        demotable: True for test/fixture/vendored files. The ladder may
            collapse these to a per-directory line; production rows
            never collapse (the FR1 floor guarantee).
    """

    path: str
    purpose: str
    demotable: bool


@dataclass(frozen=True)
class BackboneGroup:
    """A directory and its rows, for path-amortized rendering.

    Attributes:
        directory: Repo-relative directory, or ``.`` for the root.
        rows: The directory's files, in ascending path order.
        demotable: True when every row is demotable, so the whole
            directory may be collapsed to one line.
    """

    directory: str
    rows: tuple[BackboneRow, ...]
    demotable: bool


def compute_backbone(index: MapIndex) -> list[BackboneGroup]:
    """Build the deterministic file backbone (the FR1 floor).

    Reads only the in-scope file set (``languages_by_path``), each
    file's purpose (``docs_by_path``), and its test classification. No
    call-graph data is consulted: the backbone is the navigation index,
    not the dependency view.

    Args:
        index: Loaded map index.

    Returns:
        Directory groups in ascending directory order, each with its
        rows in ascending path order.
    """
    rows_by_dir: dict[str, list[BackboneRow]] = {}
    for path in index.languages_by_path:
        doc = index.docs_by_path.get(path) or ""
        purpose = oneline(doc, LEAN_PURPOSE_WIDTH) if doc else ""
        row = BackboneRow(
            path=path, purpose=purpose, demotable=is_test_path(path)
        )
        rows_by_dir.setdefault(dir_of(path), []).append(row)
    groups: list[BackboneGroup] = []
    for directory in sorted(rows_by_dir):
        rows = tuple(sorted(rows_by_dir[directory], key=lambda r: r.path))
        groups.append(
            BackboneGroup(
                directory=directory,
                rows=rows,
                demotable=all(r.demotable for r in rows)
            )
        )
    return groups


def render_backbone(
    groups: list[BackboneGroup],
    width: int = LEAN_PURPOSE_WIDTH,
    collapse_demotable: bool = False
) -> list[str]:
    """Render backbone groups to dense lean lines.

    Production groups are always expanded — their file paths are the
    floor. ``collapse_demotable`` folds each all-demotable directory to
    a single ``dir/  (N files)`` line; the *decision* to collapse
    belongs to the budget ladder, but the rendering of either shape
    lives here.

    Args:
        groups: Groups from :func:`compute_backbone`.
        width: Purpose-text width. ``0`` drops purposes, leaving
            basenames only (the floor's narrowest rung).
        collapse_demotable: Collapse all-demotable directories to one
            line each.

    Returns:
        Output lines, ready to join with newlines.
    """
    lines: list[str] = []
    for group in groups:
        if collapse_demotable and group.demotable:
            lines.append(_collapsed_line(group))
            continue
        lines.append(f"{group.directory}/")
        lines += [_row_line(row, width) for row in group.rows]
    return lines


def _row_line(row: BackboneRow, width: int) -> str:
    """A single indented file row, with purpose when width allows."""
    base = row.path.rsplit("/", 1)[-1]
    if width and row.purpose:
        return f"  {base}{SEP}{oneline(row.purpose, width)}"
    return f"  {base}"


def _collapsed_line(group: BackboneGroup) -> str:
    """One-line summary of an all-demotable directory."""
    n = len(group.rows)
    noun = "file" if n == 1 else "files"
    return f"{group.directory}/  ({n} {noun})"
