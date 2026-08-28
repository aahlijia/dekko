"""The ambiguous command: aggregation, drill-down, budget-capping."""

import json

import pytest

from dekko.analysis import ambiguous
from dekko.core.model import Symbol
from dekko.core.resolver import MODULE_CALLER_SUFFIX
from dekko.integrations import cli
from dekko.render import mapfile
from dekko.render.mapfile import MapIndex

from conftest import RepoFactory

AMBIGUOUS_CALL = {
    "a.py": "def target() -> int:\n    return 1\n",
    "b.py": "def target() -> int:\n    return 2\n",
    "c.py": "def caller() -> int:\n    return target()\n",
}

MODULE_LEVEL_AMBIGUOUS = {
    "a.py": "def target() -> int:\n    return 1\n",
    "b.py": "def target() -> int:\n    return 2\n",
    "c.py": "target()\n",
}

TEST_CALLER_AMBIGUOUS = {
    "a.py": "def target() -> int:\n    return 1\n",
    "b.py": "def target() -> int:\n    return 2\n",
    "tests/test_c.py": "def test_caller() -> int:\n    return target()\n",
}

# Both candidates share (path, qualname) -- a repeated top-level
# definition in the same file -- so the "path+qualname alone can't
# disambiguate" hint must fire on the --name drill-down.
SAME_QUALNAME_COLLISION = {
    "a.py": (
        "def dup() -> int:\n"
        "    return 1\n"
        "\n"
        "\n"
        "def dup() -> int:\n"
        "    return 2\n"
    ),
    "c.py": "def entry() -> int:\n    return dup()\n",
}

CANDIDATE_CAP_FILES = {
    f"mod_{i}.py": "def dup() -> int:\n    return 1\n" for i in range(25)
}
CANDIDATE_CAP_FILES["caller.py"] = "def entry() -> int:\n    return dup()\n"

# Round 22 cline.md §3.1: exactly one real repo-wide ``trim`` symbol; a
# call through an untyped local variable (``opts.config.trim()``) is
# really JS's built-in ``String.prototype.trim()``, not the repo
# symbol -- must never surface in ``dekko ambiguous`` at all (not even
# with 1 candidate), since the noise guard should route it to
# ``external`` before it ever reaches the ambiguous bucket.
BUILTIN_NOISE_CALL = {
    "util.ts": "export function trim(): number {\n    return 1;\n}\n",
    "caller.ts": (
        "export function run(opts: any): void {\n    opts.config.trim();\n}\n"
    ),
}


def _many_ambiguous_names(n: int) -> dict[str, str]:
    """A fixture with ``n`` distinct bare-name collisions, one caller."""
    files: dict[str, str] = {}
    caller_lines = []
    for i in range(n):
        name = f"dup{i}"
        files[f"a{i}.py"] = f"def {name}() -> int:\n    return 1\n"
        files[f"b{i}.py"] = f"def {name}() -> int:\n    return 2\n"
        caller_lines.append(
            f"def call_{name}() -> int:\n    return {name}()\n"
        )
    files["caller.py"] = "\n\n".join(caller_lines) + "\n"
    return files


def _sym(sym_id: str, path: str) -> Symbol:
    return Symbol(
        id=sym_id,
        name=sym_id,
        qualname=sym_id,
        kind="function",
        path=path,
        language="python",
    )


# --- unit tests: aggregation logic against a hand-built MapIndex -----


def test_raw_triples_reconstructs_pairs() -> None:
    idx = MapIndex(root_label="t")
    idx.ambiguous_in = {
        "cand1": [("caller1", "target")],
        "cand2": [("caller1", "target")],
    }
    idx.ambiguous_out = {"caller1": ["target"]}
    triples = ambiguous._raw_triples(idx)
    assert triples == [("caller1", "target", ["cand1", "cand2"])]


def test_raw_triples_drops_zero_candidate_pair() -> None:
    # A ``without_tests()`` view can leave a name in ambiguous_out for
    # a caller whose every candidate got filtered out of ambiguous_in
    # (all candidates were test symbols, even though the caller isn't)
    # -- such a pair carries no usable candidate data and must not
    # surface as a phantom 0-candidate row.
    idx = MapIndex(root_label="t")
    idx.ambiguous_in = {}
    idx.ambiguous_out = {"caller1": ["target"]}
    assert ambiguous._raw_triples(idx) == []


def test_by_name_ranking_and_stats() -> None:
    idx = MapIndex(root_label="t")
    idx.symbols_by_id = {
        "c1": _sym("c1", "caller1.py"),
        "c2": _sym("c2", "caller2.py"),
    }
    triples = [
        ("c1", "Generate", ["x1", "x2", "x3"]),
        ("c2", "Generate", ["x1", "x2"]),
        ("c1", "New", ["y1", "y2"]),
    ]
    ranked = ambiguous.by_name(idx, triples)
    # (name, count, avg_candidates, max_candidates, file_count)
    assert ranked[0] == ("Generate", 2, 2.5, 3, 2)
    assert ranked[1] == ("New", 1, 2.0, 2, 1)


def test_by_file_ranking() -> None:
    idx = MapIndex(root_label="t")
    idx.symbols_by_id = {
        "c1": _sym("c1", "hot.py"),
        "c2": _sym("c2", "cold.py"),
    }
    triples = [
        ("c1", "A", ["x"]),
        ("c1", "B", ["y"]),
        ("c2", "C", ["z"]),
    ]
    ranked = ambiguous.by_file(idx, triples)
    assert ranked == [("hot.py", 2), ("cold.py", 1)]


def test_by_file_module_level_caller_no_crash() -> None:
    idx = MapIndex(root_label="t")
    caller_id = f"mod.py{MODULE_CALLER_SUFFIX}"
    triples = [(caller_id, "target", ["x"])]
    assert ambiguous.by_file(idx, triples) == [("mod.py", 1)]


def test_ambiguous_rate() -> None:
    idx = MapIndex(root_label="t")
    idx.calls_out = {"a": ["b", "c"], "d": ["e"]}
    assert ambiguous.ambiguous_rate(idx, 1) == 0.25


def test_ambiguous_rate_zero_denominator() -> None:
    idx = MapIndex(root_label="t")
    assert ambiguous.ambiguous_rate(idx, 0) == 0.0


def test_compute_shape() -> None:
    idx = MapIndex(root_label="t")
    idx.symbols_by_id = {"c1": _sym("c1", "a.py")}
    idx.ambiguous_in = {"x1": [("c1", "target")], "x2": [("c1", "target")]}
    idx.ambiguous_out = {"c1": ["target"]}
    idx.calls_out = {}
    doc = ambiguous.compute(idx, top=10)
    assert doc["total_ambiguous_sites"] == 1
    assert doc["distinct_names"] == 1
    assert doc["distinct_files"] == 1
    assert doc["ambiguous_rate"] == 1.0
    assert doc["top_by_name"][0]["name"] == "target"
    assert doc["top_by_name"][0]["count"] == 1
    assert doc["top_by_file"][0] == {"path": "a.py", "count": 1}


# --- standing high-ambiguous-rate flag ----------------------------------


def test_cheap_rate_matches_compute_on_ambiguous_call(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(AMBIGUOUS_CALL)
    index = mapfile.load_map(root)
    total, rate = ambiguous.cheap_rate(index)
    doc = ambiguous.compute(index, top=10)
    assert total == doc["total_ambiguous_sites"]
    assert rate == doc["ambiguous_rate"]


def test_cheap_rate_matches_compute_module_level(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(MODULE_LEVEL_AMBIGUOUS)
    index = mapfile.load_map(root)
    total, rate = ambiguous.cheap_rate(index)
    doc = ambiguous.compute(index, top=10)
    assert total == doc["total_ambiguous_sites"]
    assert rate == doc["ambiguous_rate"]


def test_cheap_rate_matches_compute_test_caller(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(TEST_CALLER_AMBIGUOUS)
    index = mapfile.load_map(root)
    total, rate = ambiguous.cheap_rate(index)
    doc = ambiguous.compute(index, top=10)
    assert total == doc["total_ambiguous_sites"]
    assert rate == doc["ambiguous_rate"]


_LOW_RATE_FILES = {
    "a.py": "def target() -> int:\n    return 1\n",
    "b.py": "def target() -> int:\n    return 2\n",
    "helper.py": (
        "def r0() -> int:\n    return 1\n\n\ndef r1() -> int:\n"
        "    return 1\n\n\ndef r2() -> int:\n    return 1\n\n\n"
        "def r3() -> int:\n    return 1\n\n\ndef r4() -> int:\n"
        "    return 1\n\n\ndef r5() -> int:\n    return 1\n\n\n"
        "def r6() -> int:\n    return 1\n\n\ndef r7() -> int:\n"
        "    return 1\n\n\ndef r8() -> int:\n    return 1\n\n\n"
        "def r9() -> int:\n    return 1\n"
    ),
    "c.py": (
        "from helper import (\n"
        "    r0, r1, r2, r3, r4, r5, r6, r7, r8, r9,\n"
        ")\n\n\n"
        "def caller() -> int:\n"
        "    total = target()\n"
        "    total += r0() + r1() + r2() + r3() + r4()\n"
        "    total += r5() + r6() + r7() + r8() + r9()\n"
        "    return total\n"
    ),
}


def test_high_rate_note_none_below_threshold(
    make_mapped_repo: RepoFactory,
) -> None:
    # 1 ambiguous collision diluted by 10 unambiguous resolved calls
    # (1 / 11 ≈ 9%) is nowhere near the 30% threshold.
    root = make_mapped_repo(_LOW_RATE_FILES)
    index = mapfile.load_map(root)
    _total, rate = ambiguous.cheap_rate(index)
    assert rate < ambiguous.HIGH_AMBIGUOUS_RATE
    assert ambiguous.high_rate_note(index) is None


def test_high_rate_note_none_just_below_threshold() -> None:
    idx = MapIndex(root_label="t")
    idx.ambiguous_out = {"c1": [f"n{i}" for i in range(29)]}
    idx.calls_out = {"c1": [f"r{i}" for i in range(71)]}
    assert ambiguous.high_rate_note(idx) is None


def test_high_rate_note_present_at_threshold_boundary() -> None:
    idx = MapIndex(root_label="t")
    idx.ambiguous_out = {"c1": ["a", "b", "c"]}
    idx.calls_out = {"c1": [f"r{i}" for i in range(7)]}
    _total, rate = ambiguous.cheap_rate(idx)
    assert rate == ambiguous.HIGH_AMBIGUOUS_RATE
    assert ambiguous.high_rate_note(idx) is not None


def test_high_rate_note_present_above_threshold() -> None:
    idx = MapIndex(root_label="t")
    idx.ambiguous_out = {"c1": ["a", "b", "c"]}
    idx.calls_out = {"c1": ["x"]}
    note = ambiguous.high_rate_note(idx)
    assert note is not None
    assert "75%" in note
    assert "3 sites" in note
    assert "query callers/workset fan-in" in note
    assert "dekko ambiguous --by name" in note


# --- integration tests: CLI end-to-end ---------------------------------


def test_ambiguous_summary_text(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(AMBIGUOUS_CALL)
    code = cli.main(["ambiguous", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "1 ambiguous call sites" in out
    assert "1 distinct colliding names" in out
    assert "target" in out
    assert "top concentrated files:" in out
    assert "c.py" in out


def test_ambiguous_summary_json(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(AMBIGUOUS_CALL)
    code = cli.main(["ambiguous", "--root", str(root), "--json"])
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["total_ambiguous_sites"] == 1
    assert doc["distinct_names"] == 1
    assert doc["distinct_files"] == 1
    assert doc["top_by_name"][0]["name"] == "target"
    assert doc["top_by_name"][0]["max_candidates"] == 2


def test_ambiguous_no_ambiguity_is_clean_exit_zero(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo({"a.py": "def main() -> int:\n    return 1\n"})
    code = cli.main(["ambiguous", "--root", str(root)])
    assert code == 0
    assert "no ambiguous call sites" in capsys.readouterr().out


def test_ambiguous_no_ambiguity_json(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo({"a.py": "def main() -> int:\n    return 1\n"})
    code = cli.main(["ambiguous", "--root", str(root), "--json"])
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["total_ambiguous_sites"] == 0
    assert doc["ambiguous_rate"] == 0.0
    assert doc["top_by_name"] == []


def test_ambiguous_by_name(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(AMBIGUOUS_CALL)
    code = cli.main(["ambiguous", "--root", str(root), "--by", "name"])
    assert code == 0
    out = capsys.readouterr().out
    assert "target" in out
    assert "dekko ambiguous --name target for detail" in out


def test_ambiguous_by_name_json(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(AMBIGUOUS_CALL)
    code = cli.main(
        ["ambiguous", "--root", str(root), "--by", "name", "--json"]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["results"] == [
        {
            "name": "target",
            "count": 1,
            "avg_candidates": 2.0,
            "max_candidates": 2,
            "files": 1,
        }
    ]
    assert doc["meta"]["total"] == 1


def test_ambiguous_by_file(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(AMBIGUOUS_CALL)
    code = cli.main(["ambiguous", "--root", str(root), "--by", "file"])
    assert code == 0
    out = capsys.readouterr().out
    assert "c.py" in out


def test_ambiguous_by_file_json(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(AMBIGUOUS_CALL)
    code = cli.main(
        ["ambiguous", "--root", str(root), "--by", "file", "--json"]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["results"] == [{"path": "c.py", "count": 1}]


def test_ambiguous_name_drilldown(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(AMBIGUOUS_CALL)
    code = cli.main(["ambiguous", "--root", str(root), "--name", "target"])
    assert code == 0
    out = capsys.readouterr().out
    assert "called ambiguously from 1 site(s)" in out
    assert "c.py:1  caller() -> int" in out
    assert "a.py:1  target() -> int" in out
    assert "b.py:1  target() -> int" in out


def test_ambiguous_name_drilldown_json(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(AMBIGUOUS_CALL)
    code = cli.main(
        ["ambiguous", "--root", str(root), "--name", "target", "--json"]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["name"] == "target"
    assert len(doc["results"]) == 1
    entry = doc["results"][0]
    assert entry["caller"]["path"] == "c.py"
    assert {c["path"] for c in entry["candidates"]} == {"a.py", "b.py"}


def test_ambiguous_name_drilldown_module_level_caller(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(MODULE_LEVEL_AMBIGUOUS)
    code = cli.main(["ambiguous", "--root", str(root), "--name", "target"])
    assert code == 0
    out = capsys.readouterr().out
    assert "c.py  (module level)" in out


def test_ambiguous_by_file_module_level_caller(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(MODULE_LEVEL_AMBIGUOUS)
    code = cli.main(["ambiguous", "--root", str(root), "--by", "file"])
    assert code == 0
    out = capsys.readouterr().out
    assert "c.py" in out
    assert "<module>" not in out


def test_ambiguous_name_not_found(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(AMBIGUOUS_CALL)
    code = cli.main(["ambiguous", "--root", str(root), "--name", "targett"])
    assert code == ambiguous.EXIT_NOT_FOUND
    err = capsys.readouterr().err
    assert "no ambiguous calls to 'targett'" in err
    assert "closest colliding names:" in err
    assert "target" in err


def test_ambiguous_mutually_exclusive_by_and_name(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(AMBIGUOUS_CALL)
    code = cli.main(
        ["ambiguous", "--root", str(root), "--by", "name", "--name", "target"]
    )
    assert code == ambiguous.EXIT_ERROR
    assert "give --by or --name, not both" in capsys.readouterr().err


def test_ambiguous_candidate_cap_and_qualify_hint(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # zed's bug (B10): a very-high-cardinality bare-name collision
    # must truncate at _MAX_AMBIGUOUS_CANDIDATES (20), not dump every
    # candidate unconditionally.
    root = make_mapped_repo(CANDIDATE_CAP_FILES)
    code = cli.main(["ambiguous", "--root", str(root), "--name", "dup"])
    assert code == 0
    out = capsys.readouterr().out
    candidate_rows = [
        ln for ln in out.splitlines() if ln.strip().startswith("mod_")
    ]
    assert len(candidate_rows) == 20
    assert "+5 more (qualify with" in out


def test_ambiguous_name_same_qualname_hint(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SAME_QUALNAME_COLLISION)
    code = cli.main(["ambiguous", "--root", str(root), "--name", "dup"])
    assert code == 0
    out = capsys.readouterr().out
    assert "path+qualname alone can't disambiguate" in out


def test_ambiguous_no_tests_excludes_test_caller(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(TEST_CALLER_AMBIGUOUS)
    with_tests = cli.main(["ambiguous", "--root", str(root)])
    assert with_tests == 0
    assert "1 ambiguous call sites" in capsys.readouterr().out

    no_tests = cli.main(["ambiguous", "--root", str(root), "--no-tests"])
    assert no_tests == 0
    assert "no ambiguous call sites" in capsys.readouterr().out


def test_ambiguous_builtin_method_noise_not_listed(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(BUILTIN_NOISE_CALL)
    code = cli.main(["ambiguous", "--root", str(root)])
    assert code == 0
    assert "no ambiguous call sites" in capsys.readouterr().out


def test_ambiguous_by_name_budget_capping(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(_many_ambiguous_names(20))
    uncapped = cli.main(["ambiguous", "--root", str(root), "--by", "name"])
    assert uncapped == 0
    full_out = capsys.readouterr().out
    assert full_out.count("avg 2.0 candidates") == 20

    capped = cli.main(
        ["ambiguous", "--root", str(root), "--by", "name", "--budget", "60"]
    )
    assert capped == 0
    capped_out = capsys.readouterr().out
    assert capped_out.count("avg 2.0 candidates") < 20
    assert "omitted" in capped_out
    assert "raise --budget" in capped_out
