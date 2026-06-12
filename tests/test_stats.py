"""The stats command: counts, hotspots, language mix."""

import json

import pytest

from lidar_map import cli
from lidar_map import stats

from conftest import RepoFactory

SRC = {
    "a.py": "def f() -> int:\n    return 1\n",
    "b.py": "from a import f\n\n\ndef g() -> int:\n    return f()\n\n\n"
    "def h() -> int:\n    return f()\n",
}


def test_stats_text(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SRC)
    assert cli.main(["stats", "--root", str(root)]) == 0
    out = capsys.readouterr().out
    assert "2 files" in out
    assert "languages: python" in out
    assert "top fan-in:" in out
    assert "f() -> int" in out  # f is the fan-in hotspot


def test_stats_json_shape_and_hotspot(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SRC)
    args = ["stats", "--root", str(root), "--json", "--top", "3"]
    assert cli.main(args) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["files"] == 2
    assert doc["symbols"] == 3
    # f is called by both g and h → fan-in 2, the top hotspot
    assert doc["top_fan_in"][0]["id"] == "a.py::f"
    assert doc["top_fan_in"][0]["count"] == 2
    langs = {lang["language"]: lang for lang in doc["languages"]}
    assert langs["python"]["files"] == 2


def test_largest_files_ranking() -> None:
    from lidar_map.mapfile import MapIndex
    from lidar_map.model import Symbol

    idx = MapIndex(root_label="t")
    for i in range(3):
        sym = Symbol(
            id=f"big.py::s{i}",
            name=f"s{i}",
            qualname=f"s{i}",
            kind="function",
            path="big.py",
            language="python",
        )
        idx.symbols_by_id[sym.id] = sym
        idx.symbols_by_path.setdefault("big.py", []).append(sym)
    small = Symbol(
        id="small.py::s",
        name="s",
        qualname="s",
        kind="function",
        path="small.py",
        language="python",
    )
    idx.symbols_by_id[small.id] = small
    idx.symbols_by_path["small.py"] = [small]
    idx.languages_by_path = {"big.py": "python", "small.py": "python"}

    doc = stats.compute(idx, top=10)
    assert doc["largest_files"][0] == {"path": "big.py", "symbols": 3}
