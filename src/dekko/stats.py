"""Aggregate metrics over the map: hotspots, sizes, language mix."""

import json
from collections import Counter

from .mapfile import MapIndex
from .model import Symbol
from .render_md import signature


def _edge_count(index: MapIndex) -> int:
    """Total resolved call edges, including module-level origins."""
    return sum(len(callees) for callees in index.calls_out.values())


def _hotspots(
    index: MapIndex, adjacency: dict[str, list[str]], top: int
) -> list[tuple[Symbol, int]]:
    """Top symbols by adjacency size (fan-in or fan-out)."""
    ranked: list[tuple[Symbol, int]] = []
    for sym_id, sym in index.symbols_by_id.items():
        count = len(adjacency.get(sym_id, []))
        if count:
            ranked.append((sym, count))
    ranked.sort(key=lambda pair: (-pair[1], pair[0].path, pair[0].start_line))
    return ranked[:top]


def _largest_files(index: MapIndex, top: int) -> list[tuple[str, int]]:
    """Files with the most symbols."""
    sizes = [(path, len(syms)) for path, syms in index.symbols_by_path.items()]
    sizes.sort(key=lambda pair: (-pair[1], pair[0]))
    return sizes[:top]


def _language_mix(index: MapIndex) -> list[tuple[str, int, int]]:
    """Per-language ``(language, file_count, symbol_count)``."""
    files = Counter(index.languages_by_path.values())
    syms: Counter[str] = Counter()
    for sym in index.symbols_by_id.values():
        syms[sym.language] += 1
    langs = sorted(files, key=lambda lang: (-files[lang], lang))
    return [(lang, files[lang], syms.get(lang, 0)) for lang in langs]


def compute(index: MapIndex, top: int) -> dict:
    """Build the full stats document.

    Args:
        index: Loaded map index.
        top: How many entries to keep in each ranked list.

    Returns:
        A JSON-serializable stats dict.
    """
    return {
        "files": len(index.languages_by_path),
        "symbols": len(index.symbols_by_id),
        "edges": _edge_count(index),
        "languages": [
            {"language": lang, "files": nf, "symbols": ns}
            for lang, nf, ns in _language_mix(index)
        ],
        "top_fan_in": [
            {"id": s.id, "count": n, "signature": signature(s)}
            for s, n in _hotspots(index, index.calls_in, top)
        ],
        "top_fan_out": [
            {"id": s.id, "count": n, "signature": signature(s)}
            for s, n in _hotspots(index, index.calls_out, top)
        ],
        "largest_files": [
            {"path": p, "symbols": n} for p, n in _largest_files(index, top)
        ],
    }


def _print_hotspots(title: str, hotspots: list[tuple[Symbol, int]]) -> None:
    """Print a labeled fan-in/fan-out ranking."""
    if not hotspots:
        return
    print(title)
    for sym, count in hotspots:
        print(f"  {count:>4}  {sym.path}:{sym.start_line}  {signature(sym)}")


def run(index: MapIndex, top: int, as_json: bool) -> int:
    """Print map statistics as text or JSON.

    Args:
        index: Loaded map index.
        top: How many entries to keep in each ranked list.
        as_json: Emit structured JSON instead of text.

    Returns:
        Always ``0``.
    """
    if as_json:
        print(json.dumps(compute(index, top), indent=2))
        return 0

    print(
        f"dekko: {len(index.languages_by_path)} files, "
        f"{len(index.symbols_by_id)} symbols, {_edge_count(index)} edges"
    )
    mix = ", ".join(
        f"{lang} {nf}f/{ns}s" for lang, nf, ns in _language_mix(index)
    )
    print(f"languages: {mix}")
    _print_hotspots("top fan-in:", _hotspots(index, index.calls_in, top))
    _print_hotspots("top fan-out:", _hotspots(index, index.calls_out, top))
    largest = _largest_files(index, top)
    if largest:
        print("largest files:")
        for path, count in largest:
            print(f"  {count:>4}  {path}")
    return 0
