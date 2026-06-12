"""Report call graph statistics and hotspots."""

import json
from collections import Counter

from .mapfile import MapIndex


def run(index: MapIndex, as_json: bool = False) -> int:
    """Execute the stats command.

    Args:
        index: The loaded map index.
        as_json: Emit structured JSON instead of text.
    """
    fan_in = []
    fan_out = []

    for sym_id in index.symbols_by_id:
        fan_in.append((sym_id, len(index.calls_in.get(sym_id, []))))
        fan_out.append((sym_id, len(index.calls_out.get(sym_id, []))))

    fan_in.sort(key=lambda x: (-x[1], x[0]))
    fan_out.sort(key=lambda x: (-x[1], x[0]))

    # Largest files by symbol count
    file_sizes = []
    for path, syms in index.symbols_by_path.items():
        file_sizes.append((path, len(syms)))
    file_sizes.sort(key=lambda x: (-x[1], x[0]))

    # Language breakdown by file count
    lang_counts = Counter(index.languages_by_path.values())
    langs = sorted(lang_counts.items(), key=lambda x: (-x[1], x[0]))

    if as_json:
        out = {
            "top_fan_in": [{"id": sym_id, "count": c} for sym_id, c in fan_in[:10] if c > 0],
            "top_fan_out": [{"id": sym_id, "count": c} for sym_id, c in fan_out[:10] if c > 0],
            "largest_files": [{"path": p, "symbols": c} for p, c in file_sizes[:10] if c > 0],
            "languages": [{"language": lang, "files": c} for lang, c in langs if lang],
        }
        print(json.dumps(out, indent=2))
        return 0

    print("Call Graph Statistics")
    print("=====================")
    print("\nTop 10 Hotspots (Fan-in):")
    for sym_id, c in fan_in[:10]:
        if c == 0:
            break
        print(f"  {c:4d}  {sym_id}")

    print("\nTop 10 Hotspots (Fan-out):")
    for sym_id, c in fan_out[:10]:
        if c == 0:
            break
        print(f"  {c:4d}  {sym_id}")

    print("\nLargest Files (by symbol count):")
    for path, c in file_sizes[:10]:
        if c == 0:
            break
        print(f"  {c:4d}  {path}")

    print("\nLanguage Breakdown (by file count):")
    for lang, c in langs:
        if lang:
            print(f"  {c:4d}  {lang}")

    return 0
