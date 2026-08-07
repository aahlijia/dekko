"""The stats command: counts, hotspots, language mix."""

import json

import pytest

from dekko import cli
from dekko import stats

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


def test_hotspots_exclude_noise_names() -> None:
    # A symbol named ``String``/``expect``/etc. must never surface in
    # a fan-in/fan-out ranking, even with a very high adjacency count
    # — see investigation-1.2-resolver-fanin.md: these names collide
    # with JS/TS built-ins/globals often enough that even after the
    # resolver-level fix, a residual high count here is still a red
    # flag, not a real hotspot worth surfacing.
    from dekko.mapfile import MapIndex
    from dekko.model import Symbol

    idx = MapIndex(root_label="t")

    def _add(sym_id: str, name: str) -> Symbol:
        sym = Symbol(
            id=sym_id,
            name=name,
            qualname=name,
            kind="function",
            path="a.ts",
            language="typescript",
        )
        idx.symbols_by_id[sym.id] = sym
        return sym

    noisy = _add("a.ts::String", "String")
    real = _add("a.ts::realHelper", "realHelper")
    idx.calls_in[noisy.id] = [f"caller{i}" for i in range(500)]
    idx.calls_in[real.id] = ["caller0", "caller1"]

    doc = stats.compute(idx, top=10)
    ids = [row["id"] for row in doc["top_fan_in"]]
    assert noisy.id not in ids
    assert real.id in ids


def test_largest_files_ranking() -> None:
    from dekko.mapfile import MapIndex
    from dekko.model import Symbol

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
