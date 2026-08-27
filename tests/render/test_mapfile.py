"""map.json round-trip, provenance, and freshness checks."""

import json
from pathlib import Path

import pytest

from dekko.render import mapfile
from dekko.integrations import cli
from dekko.core.model import (
    CallGraph,
    Edge,
    ExternalCall,
    FileMap,
    HeritageEdge,
    ModuleEdge,
    ModuleGraph,
    Symbol,
)

from conftest import RepoFactory

CHAIN = {
    "a.py": (
        "def helper(x: int) -> int:\n"
        "    return x + 1\n"
        "\n"
        "\n"
        "def main() -> None:\n"
        "    helper(1)\n"
    )
}


def test_load_round_trip(make_mapped_repo: RepoFactory) -> None:
    root = make_mapped_repo(CHAIN)
    index = mapfile.load_map(root)
    assert index is not None
    helper = index.symbols_by_qualname["helper"][0]
    assert helper.path == "a.py"
    assert helper.params[0].type == "int"
    main_id = index.symbols_by_qualname["main"][0].id
    assert helper.id in index.calls_out[main_id]
    assert main_id in index.calls_in[helper.id]


def test_provenance_written(make_mapped_repo: RepoFactory) -> None:
    root = make_mapped_repo(CHAIN)
    doc = json.loads((root / ".dekko" / "map.json").read_text())
    assert doc["version"] == mapfile.MAP_DOC_VERSION
    prov = doc["provenance"]
    assert prov["tool_version"]
    assert prov["spec_hash"]
    assert set(prov["files"]) == {"a.py"}


def test_freshness_transitions(make_mapped_repo: RepoFactory) -> None:
    root = make_mapped_repo(CHAIN)
    index = mapfile.load_map(root)
    assert mapfile.check_freshness(root, index).fresh

    (root / "a.py").write_text(CHAIN["a.py"] + "\n\nX = 1\n")
    fresh = mapfile.check_freshness(root, index)
    assert not fresh.fresh
    assert fresh.changed == ["a.py"]

    (root / "b.py").write_text("def extra() -> None:\n    pass\n")
    fresh = mapfile.check_freshness(root, index)
    assert fresh.added == ["b.py"]


def test_dekkoignore_hand_edit_marks_map_stale(
    make_mapped_repo: RepoFactory,
) -> None:
    # Proves the "falls out for free" staleness claim: hand-editing
    # .dekko/.dekkoignore with no CLI flags involved is enough to make
    # check_freshness() see the newly-ignored file as removed, because
    # discover() reads the ignore file live on every call.
    root = make_mapped_repo(dict(CHAIN, **{"b.py": "X = 1\n"}))
    index = mapfile.load_map(root)
    assert mapfile.check_freshness(root, index).fresh

    (root / ".dekko" / ".dekkoignore").write_text("b.py\n")

    fresh = mapfile.check_freshness(root, index)
    assert not fresh.fresh
    assert fresh.removed == ["b.py"]


def test_removed_file_detected(make_mapped_repo: RepoFactory) -> None:
    root = make_mapped_repo(
        dict(CHAIN, **{"b.py": "def extra() -> None:\n    pass\n"})
    )
    index = mapfile.load_map(root)
    (root / "b.py").unlink()
    fresh = mapfile.check_freshness(root, index)
    assert not fresh.fresh
    assert fresh.removed == ["b.py"]


def test_v1_map_is_always_stale(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(CHAIN)
    doc = json.loads((root / ".dekko" / "map.json").read_text())
    doc["version"] = 1
    del doc["provenance"]
    (root / ".dekko" / "map.json").write_text(json.dumps(doc))

    index = mapfile.load_map(root)
    assert index is not None
    assert not mapfile.check_freshness(root, index).fresh


def test_missing_map_loads_none(tmp_path: Path) -> None:
    assert mapfile.load_map(tmp_path) is None


def test_load_map_raises_on_too_new_doc_version(
    make_mapped_repo: RepoFactory,
) -> None:
    # A stale in-memory process (long-lived MCP server/daemon) reading
    # a map.json written by a newer dekko build, whose doc version it
    # has no parsing logic for, must fail loudly and clearly instead
    # of returning None (which callers like repo_ops.load_or_regen
    # would read as "missing" and regenerate over, silently
    # downgrading a perfectly good, newer-format map.json) or letting
    # some unrelated downstream TypeError/KeyError surface first.
    root = make_mapped_repo(CHAIN)
    doc_path = root / ".dekko" / "map.json"
    doc = json.loads(doc_path.read_text())
    doc["version"] = mapfile.MAP_DOC_VERSION + 1
    doc_path.write_text(json.dumps(doc))

    with pytest.raises(mapfile.MapFormatTooNewError) as exc_info:
        mapfile.load_map(root)

    message = str(exc_info.value)
    assert str(mapfile.MAP_DOC_VERSION + 1) in message
    assert str(mapfile.MAP_DOC_VERSION) in message
    assert "restart" in message.lower()


@pytest.mark.parametrize(
    "bad_version",
    [None, "not-a-number", 5.7, True],
    ids=["null", "string", "float", "bool"],
)
def test_load_map_raises_on_malformed_doc_version(
    make_mapped_repo: RepoFactory,
    bad_version: object,
) -> None:
    # A malformed/corrupted "version" field (null, non-numeric, or a
    # float rather than an int) must not fall through the
    # MapFormatTooNewError guard's `isinstance(doc_version, int)`
    # check and hit the old opaque TypeError from
    # `doc_version > MAP_DOC_VERSION` comparing incompatible types —
    # it needs its own clear, distinct error instead (the document is
    # broken, not merely "too new").
    root = make_mapped_repo(CHAIN)
    doc_path = root / ".dekko" / "map.json"
    doc = json.loads(doc_path.read_text())
    doc["version"] = bad_version
    doc_path.write_text(json.dumps(doc))

    with pytest.raises(mapfile.MapFormatInvalidError) as exc_info:
        mapfile.load_map(root)

    message = str(exc_info.value)
    assert "version" in message.lower()
    assert "dekko map" in message


def test_provenance_records_unsupported_files(
    make_mapped_repo: RepoFactory,
) -> None:
    # A repo with an unparseable file type (Astro, the confirmed
    # 2026-07-31 eval gap) must not map "clean" — the skip has to
    # survive into map.json so read commands can warn about it.
    root = make_mapped_repo(
        dict(CHAIN, **{"Card.astro": "---\nconst x = 1;\n---\n<div/>\n"})
    )
    index = mapfile.load_map(root)
    assert index is not None
    assert index.provenance is not None
    unsupported = index.provenance["unsupported"]
    assert unsupported == {"count": 1, "languages": {"astro": 1}}
    assert mapfile.format_unsupported(index.provenance) == (
        "1 files unparsed — no parser for: astro (1)"
    )


def test_format_unsupported_none_when_fully_covered(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(CHAIN)
    index = mapfile.load_map(root)
    assert index is not None
    assert index.provenance["unsupported"] is None
    assert mapfile.format_unsupported(index.provenance) is None


def test_provenance_records_vendored_excluded_files(
    make_mapped_repo: RepoFactory,
) -> None:
    # Track E / 1.5: files under a default-excluded dir that sometimes
    # holds first-party code (tensorflow's third_party/xla is the
    # motivating case) must be aggregated into the map's provenance so
    # `status`/`summary` can surface a coverage note, distinct from
    # the "no parser for" note.
    root = make_mapped_repo(
        dict(
            CHAIN,
            **{
                "third_party/xla/lib.py": (
                    "def xla_helper() -> None:\n    pass\n"
                ),
                "vendor/pkg/mod.py": "def vendored() -> None:\n    pass\n",
            },
        )
    )
    index = mapfile.load_map(root)
    assert index is not None
    assert index.provenance is not None
    vendored = index.provenance["vendored_excluded"]
    assert vendored == {
        "count": 2,
        "dirs": {"third_party": 1, "vendor": 1},
    }
    assert mapfile.format_unsupported(index.provenance) == (
        "2 files under default-excluded directories "
        "(third_party (1), vendor (1)) were not mapped — pass "
        "--exclude '' or a narrower default-dir allowlist to include "
        "them if they hold first-party code"
    )


def test_format_unsupported_combines_unsupported_and_vendored_notes(
    make_mapped_repo: RepoFactory,
) -> None:
    # Both coverage gaps can apply to the same map at once; the
    # combined note must include both, not just whichever was checked
    # first.
    root = make_mapped_repo(
        dict(
            CHAIN,
            **{
                "Card.astro": "---\nconst x = 1;\n---\n<div/>\n",
                "third_party/xla/lib.py": (
                    "def xla_helper() -> None:\n    pass\n"
                ),
            },
        )
    )
    index = mapfile.load_map(root)
    assert index is not None
    note = mapfile.format_unsupported(index.provenance)
    assert note is not None
    assert "1 files unparsed — no parser for: astro (1)" in note
    assert "1 files under default-excluded directories" in note
    assert "third_party (1)" in note


def test_vendored_excluded_none_when_no_vendored_dirs_present(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(CHAIN)
    index = mapfile.load_map(root)
    assert index is not None
    assert index.provenance["vendored_excluded"] is None


def test_provenance_records_too_large_files_with_paths(
    tmp_path: Path,
) -> None:
    # round-18 zed finding: a real, first-party file skipped only for
    # exceeding --max-file-size vanished with zero disclosure ("no
    # mapped file or directory", no hint a size cap was the reason).
    # The path itself (not just a count) must survive into provenance
    # so `status`/`summary`/`map_status` can name it.
    big_file = tmp_path / "big_module.py"
    big_file.write_text("def f() -> None:\n    pass\n" + "# pad\n" * 100)
    small_file = tmp_path / "small_module.py"
    small_file.write_text("def g() -> None:\n    pass\n")
    assert (
        cli.main(
            [
                "map",
                str(tmp_path),
                "--quiet",
                "--max-file-size",
                "50",
            ]
        )
        == 0
    )
    index = mapfile.load_map(tmp_path)
    assert index is not None
    assert index.provenance is not None
    too_large = index.provenance["too_large"]
    assert too_large == {"count": 1, "paths": ["big_module.py"]}
    note = mapfile.format_unsupported(index.provenance)
    assert note is not None
    assert "1 file(s) exceeded the size cap" in note
    assert "big_module.py" in note
    assert "--max-file-size" in note


def test_too_large_none_when_no_files_exceed_cap(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(CHAIN)
    index = mapfile.load_map(root)
    assert index is not None
    assert index.provenance["too_large"] is None


def test_version_stamp_stale_even_with_unchanged_source(
    make_mapped_repo: RepoFactory,
) -> None:
    # Bug #1: a map whose provenance predates the running dekko build
    # must read as stale even when every file's content hash still
    # matches — content-only diffing can never catch an extractor
    # change (or a real version bump) on an untouched source tree.
    root = make_mapped_repo(CHAIN)
    map_path = root / ".dekko" / "map.json"
    doc = json.loads(map_path.read_text())
    doc["provenance"]["tool_version"] = "0.0.0-stale"
    map_path.write_text(json.dumps(doc))

    index = mapfile.load_map(root)
    assert index is not None
    fresh = mapfile.check_freshness(root, index)
    assert not fresh.fresh
    assert fresh.reason == "version"
    # No file-hash diff was attempted for a version mismatch.
    assert fresh.added == fresh.removed == fresh.changed == []
    # round-09 §2.3: the raw signal that fired must be readable off
    # the verdict itself, not just re-derivable by the caller.
    assert fresh.version_stale is True
    assert fresh.built_version == "0.0.0-stale"


def test_spec_hash_stale_even_with_matching_tool_version(
    make_mapped_repo: RepoFactory,
) -> None:
    # The finer-grained half of bug #1: an extraction-logic change
    # under an unchanged (or unreleased) version string must still
    # invalidate, not just a released version bump.
    root = make_mapped_repo(CHAIN)
    map_path = root / ".dekko" / "map.json"
    doc = json.loads(map_path.read_text())
    doc["provenance"]["spec_hash"] = "deadbeef"
    map_path.write_text(json.dumps(doc))

    index = mapfile.load_map(root)
    assert index is not None
    fresh = mapfile.check_freshness(root, index)
    assert not fresh.fresh
    assert fresh.reason == "version"
    # round-09 §2.3: this is exactly the "same tool_version, different
    # spec_hash" shape a long-lived ``dekko serve`` process can hit
    # silently — ``version_stale`` alone must not claim this fired,
    # and the raw hash values must be available to build a message
    # that names the real differentiator.
    assert fresh.version_stale is False
    assert fresh.spec_stale is True
    assert fresh.built_spec_hash == "deadbeef"
    assert fresh.running_spec_hash != "deadbeef"


def test_describe_version_stale_version_only() -> None:
    fresh = mapfile.Freshness(
        fresh=False,
        reason="version",
        version_stale=True,
        spec_stale=False,
        built_version="0.40.0",
        running_version="0.43.20",
        built_spec_hash="abc123",
        running_spec_hash="abc123",
    )
    text = mapfile.describe_version_stale(fresh)
    assert text.startswith("stale (version):")
    assert "built by dekko 0.40.0, running 0.43.20" in text
    assert "spec_hash" not in text


def test_describe_version_stale_spec_only() -> None:
    # round-09 §2.3: identical tool_version on both sides, only
    # spec_hash drifted — the message must name spec_hash and carry
    # the long-lived-process caveat, not repeat the (identical, thus
    # self-contradictory-looking) version string as the differentiator.
    fresh = mapfile.Freshness(
        fresh=False,
        reason="version",
        version_stale=False,
        spec_stale=True,
        built_version="0.43.20",
        running_version="0.43.20",
        built_spec_hash="deadbeef0000",
        running_spec_hash="cafef00dbaad",
    )
    text = mapfile.describe_version_stale(fresh)
    assert text.startswith("stale (spec_hash):")
    assert "tool_version:" not in text
    assert "deadbeef0000" in text
    assert "cafef00dbaad" in text
    assert "long-lived process" in text
    assert "restart it" in text


def test_describe_version_stale_both() -> None:
    fresh = mapfile.Freshness(
        fresh=False,
        reason="version",
        version_stale=True,
        spec_stale=True,
        built_version="0.40.0",
        running_version="0.43.20",
        built_spec_hash="deadbeef0000",
        running_spec_hash="cafef00dbaad",
    )
    text = mapfile.describe_version_stale(fresh)
    assert text.startswith("stale (version+spec_hash):")
    assert "built by dekko 0.40.0, running 0.43.20" in text
    assert "deadbeef0000" in text
    # The long-lived-process caveat only applies to the spec-only case.
    assert "long-lived process" not in text


def test_freshness_reason_missing_for_v1_map(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(CHAIN)
    doc = json.loads((root / ".dekko" / "map.json").read_text())
    doc["version"] = 1
    del doc["provenance"]
    (root / ".dekko" / "map.json").write_text(json.dumps(doc))

    index = mapfile.load_map(root)
    assert index is not None
    fresh = mapfile.check_freshness(root, index)
    assert not fresh.fresh
    assert fresh.reason == "missing"


def test_freshness_reason_content_on_change(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(CHAIN)
    index = mapfile.load_map(root)
    (root / "a.py").write_text(CHAIN["a.py"] + "\n\nX = 1\n")
    fresh = mapfile.check_freshness(root, index)
    assert not fresh.fresh
    assert fresh.reason == "content"


def test_freshness_reason_none_when_fresh(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(CHAIN)
    index = mapfile.load_map(root)
    fresh = mapfile.check_freshness(root, index)
    assert fresh.fresh
    assert fresh.reason is None


def test_provenance_sidecar_written_and_matches_embedded(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(CHAIN)
    sidecar = root / ".dekko" / "provenance.json"
    assert sidecar.exists()
    embedded = mapfile.load_map(root).provenance
    from_sidecar = mapfile.load_provenance(root)
    assert from_sidecar == embedded


def test_load_provenance_falls_back_without_sidecar(
    make_mapped_repo: RepoFactory,
) -> None:
    # Maps written before this sidecar existed (or one a user deleted)
    # still work — load_provenance re-parses map.json directly and
    # returns the same dict load_map would expose.
    root = make_mapped_repo(CHAIN)
    embedded = mapfile.load_map(root).provenance
    (root / ".dekko" / "provenance.json").unlink()
    assert mapfile.load_provenance(root) == embedded


def test_load_provenance_falls_back_on_desynced_sidecar(
    make_mapped_repo: RepoFactory,
) -> None:
    # A hand-edited (or otherwise externally regenerated) map.json
    # must not be shadowed by a now-stale sidecar still claiming the
    # old provenance — load_provenance detects the mismatched
    # map.json stat signature and re-parses instead of trusting it.
    root = make_mapped_repo(CHAIN)
    map_path = root / ".dekko" / "map.json"
    doc = json.loads(map_path.read_text())
    doc["provenance"]["tool_version"] = "0.0.0-hand-edited"
    map_path.write_text(json.dumps(doc))

    prov = mapfile.load_provenance(root)
    assert prov is not None
    assert prov["tool_version"] == "0.0.0-hand-edited"


def test_load_provenance_none_when_map_missing(tmp_path: Path) -> None:
    assert mapfile.load_provenance(tmp_path) is None


def test_load_provenance_recovers_from_corrupt_sidecar(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(CHAIN)
    embedded = mapfile.load_map(root).provenance
    (root / ".dekko" / "provenance.json").write_text("not json {")
    assert mapfile.load_provenance(root) == embedded


def test_check_freshness_provenance_matches_check_freshness(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(CHAIN)
    index = mapfile.load_map(root)
    prov = mapfile.load_provenance(root)

    assert (
        mapfile.check_freshness_provenance(root, prov).fresh
        == mapfile.check_freshness(root, index).fresh
    )

    (root / "a.py").write_text(CHAIN["a.py"] + "\n\nX = 1\n")
    via_index = mapfile.check_freshness(root, index)
    via_prov = mapfile.check_freshness_provenance(root, prov)
    assert via_index.fresh == via_prov.fresh is False
    assert via_index.changed == via_prov.changed == ["a.py"]


def test_check_freshness_provenance_version_stale(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(CHAIN)
    prov = dict(mapfile.load_provenance(root))
    prov["tool_version"] = "0.0.0-stale"
    fresh = mapfile.check_freshness_provenance(root, prov)
    assert not fresh.fresh
    assert fresh.reason == "version"


def test_check_freshness_provenance_none_is_missing() -> None:
    fresh = mapfile.check_freshness_provenance(Path("/nonexistent"), None)
    assert not fresh.fresh
    assert fresh.reason == "missing"


def test_load_map_works_without_orjson(
    make_mapped_repo: RepoFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Confirms the stdlib json fallback (no orjson installed) still
    # round-trips a map correctly, not just whichever backend happens
    # to be active in the test environment.
    root = make_mapped_repo(CHAIN)
    monkeypatch.setattr(mapfile, "orjson", None)
    index = mapfile.load_map(root)
    assert index is not None
    assert "helper" in index.symbols_by_qualname
    assert mapfile.load_provenance(root) == index.provenance


def _sym(path: str, name: str, kind: str = "function") -> Symbol:
    return Symbol(
        id=f"{path}::{name}",
        name=name,
        qualname=name,
        kind=kind,
        path=path,
        language="python",
        start_line=1,
        end_line=2,
    )


def _intern(doc: dict, value: str) -> int:
    """Test-side mirror of ``mapfile.build_id_table``'s intern step.

    Hand-edited map.json fixtures below inject new caller/callee/
    candidate entries; since v5+ documents store those as integer
    indices into the top-level ``"ids"`` table (round-15 plan) rather
    than raw strings, a fixture that wants to add
    ``{"caller": "a.py::main", ...}`` must add ``"a.py::main"`` to
    ``doc["ids"]`` (or reuse its existing index) and reference the
    index instead.
    """
    ids = doc.setdefault("ids", [])
    if value not in ids:
        ids.append(value)
    return ids.index(value)


def test_load_map_reads_referenced_edge_lines(
    make_mapped_repo: RepoFactory,
) -> None:
    # Package B: the reference-site lines already round-trip through
    # map.json's "referenced" edges (render_json.py already writes
    # them); load_map() must not silently drop them on the read side.
    root = make_mapped_repo(CHAIN)
    map_path = root / ".dekko" / "map.json"
    doc = json.loads(map_path.read_text())
    doc["referenced"] = [
        {
            "caller": _intern(doc, "a.py::main"),
            "callee": _intern(doc, "a.py::helper"),
            "lines": [42],
        }
    ]
    map_path.write_text(json.dumps(doc))

    index = mapfile.load_map(root)
    assert index is not None
    key = ("a.py::main", "a.py::helper")
    assert index.ref_lines[key] == [42]
    assert index.referenced_out["a.py::main"] == ["a.py::helper"]
    assert index.referenced_in["a.py::helper"] == ["a.py::main"]


def test_index_from_maps_reads_referenced_edge_lines() -> None:
    files = [
        FileMap(path="a.py", language="python", symbols=[_sym("a.py", "f")]),
        FileMap(path="b.py", language="python", symbols=[_sym("b.py", "g")]),
    ]
    graph = CallGraph(
        referenced=[Edge(caller="a.py::f", callee="b.py::g", lines=[42])]
    )
    index = mapfile.index_from_maps(files, graph, "demo")
    assert index.ref_lines[("a.py::f", "b.py::g")] == [42]
    assert index.referenced_out["a.py::f"] == ["b.py::g"]
    assert index.referenced_in["b.py::g"] == ["a.py::f"]


def test_without_tests_drops_ref_lines_touching_test_paths() -> None:
    files = [
        FileMap(path="a.py", language="python", symbols=[_sym("a.py", "f")]),
        FileMap(path="b.py", language="python", symbols=[_sym("b.py", "g")]),
        FileMap(
            path="tests/test_a.py",
            language="python",
            symbols=[_sym("tests/test_a.py", "h")],
        ),
    ]
    graph = CallGraph(
        referenced=[
            Edge(caller="a.py::f", callee="b.py::g", lines=[10]),
            Edge(caller="tests/test_a.py::h", callee="b.py::g", lines=[20]),
        ]
    )
    index = mapfile.index_from_maps(files, graph, "demo")
    filtered = index.without_tests()
    assert filtered.ref_lines == {("a.py::f", "b.py::g"): [10]}


def test_index_from_maps_builds_ambiguous_out() -> None:
    # round-09 §2.1 part A's disclosure fix: ``ambiguous_out`` is the
    # outgoing-side counterpart of ``ambiguous_in`` — for a given
    # caller, which names it called ambiguously — so ``query callees``
    # can disclose the same kind of gap ``query callers`` already
    # discloses.
    files = [
        FileMap(path="a.py", language="python", symbols=[_sym("a.py", "f")]),
        FileMap(path="b.py", language="python", symbols=[_sym("b.py", "g")]),
        FileMap(path="c.py", language="python", symbols=[_sym("c.py", "g")]),
    ]
    graph = CallGraph(ambiguous=[("a.py::f", "g", ["b.py::g", "c.py::g"])])
    index = mapfile.index_from_maps(files, graph, "demo")
    assert index.ambiguous_out["a.py::f"] == ["g"]
    assert index.ambiguous_in["b.py::g"] == [("a.py::f", "g")]
    assert index.ambiguous_in["c.py::g"] == [("a.py::f", "g")]


def test_load_map_reads_ambiguous_out(make_mapped_repo: RepoFactory) -> None:
    root = make_mapped_repo(CHAIN)
    map_path = root / ".dekko" / "map.json"
    doc = json.loads(map_path.read_text())
    doc["ambiguous"] = [
        {
            "caller": _intern(doc, "a.py::main"),
            "name": "g",
            "candidates": [
                _intern(doc, "b.py::g"),
                _intern(doc, "c.py::g"),
            ],
        }
    ]
    map_path.write_text(json.dumps(doc))

    index = mapfile.load_map(root)
    assert index is not None
    assert index.ambiguous_out["a.py::main"] == ["g"]


def test_without_tests_drops_ambiguous_out_from_test_callers() -> None:
    files = [
        FileMap(path="a.py", language="python", symbols=[_sym("a.py", "f")]),
        FileMap(path="b.py", language="python", symbols=[_sym("b.py", "g")]),
        FileMap(path="c.py", language="python", symbols=[_sym("c.py", "g")]),
        FileMap(
            path="tests/test_a.py",
            language="python",
            symbols=[_sym("tests/test_a.py", "h")],
        ),
    ]
    graph = CallGraph(
        ambiguous=[
            ("a.py::f", "g", ["b.py::g", "c.py::g"]),
            ("tests/test_a.py::h", "g", ["b.py::g", "c.py::g"]),
        ]
    )
    index = mapfile.index_from_maps(files, graph, "demo")
    filtered = index.without_tests()
    assert filtered.ambiguous_out == {"a.py::f": ["g"]}


def test_without_tests_drops_symbol_test_flag_not_just_path() -> None:
    # round-12 master report §3.4 / §2: the round-11 fix taught the
    # Rust extractor to set ``Symbol.test = True`` for definitions
    # nested in an inline ``#[cfg(test)] mod tests { ... }`` block
    # (a file path that is *not* itself a test path, e.g. plain
    # ``src/lib.rs``), but ``without_tests()`` only ever consulted
    # ``classify.is_test_path`` and never read the flag — so nothing
    # was actually excluded. This reproduces that shape without a
    # real Rust parse: a symbol living at a non-test path with
    # ``test=True`` set directly, alongside an ordinary production
    # symbol at the same path.
    prod = _sym("src/lib.rs", "real_fn")
    inline_test = Symbol(
        id="src/lib.rs::tests::test_bounds_intersects",
        name="test_bounds_intersects",
        qualname="tests::test_bounds_intersects",
        kind="function",
        path="src/lib.rs",
        language="rust",
        start_line=10,
        end_line=12,
        test=True,
    )
    files = [
        FileMap(
            path="src/lib.rs", language="rust", symbols=[prod, inline_test]
        ),
    ]
    graph = CallGraph(
        referenced=[
            Edge(
                caller=inline_test.id,
                callee=prod.id,
                lines=[11],
            )
        ]
    )
    index = mapfile.index_from_maps(files, graph, "demo")
    filtered = index.without_tests()
    assert inline_test.id not in filtered.symbols_by_id
    assert prod.id in filtered.symbols_by_id
    assert filtered.referenced_in.get(prod.id, []) == []
    assert filtered.ref_lines == {}


def test_atomic_write_bytes_writes_full_content(tmp_path: Path) -> None:
    target = tmp_path / "map.json"
    mapfile.atomic_write_bytes(target, b'{"hello": "world"}')
    assert target.read_bytes() == b'{"hello": "world"}'


def test_atomic_write_bytes_overwrites_existing_file(
    tmp_path: Path,
) -> None:
    target = tmp_path / "map.json"
    target.write_bytes(b"stale content")
    mapfile.atomic_write_bytes(target, b"fresh content")
    assert target.read_bytes() == b"fresh content"


def test_atomic_write_bytes_leaves_no_temp_file_behind(
    tmp_path: Path,
) -> None:
    target = tmp_path / "map.json"
    mapfile.atomic_write_bytes(target, b"data")
    remaining = list(tmp_path.iterdir())
    assert remaining == [target]


def test_atomic_write_bytes_never_exposes_partial_content(
    tmp_path: Path,
) -> None:
    """A reader never sees a half-written file, only old-or-new.

    Round-12 master report §4.1b: a concurrent reader that opened
    ``map.json``/``cache.json`` mid-``write_text`` could observe
    however many bytes had been flushed so far. Since ``os.replace``
    is atomic on the same filesystem, a reader opening ``target`` at
    any point either sees the complete old content or the complete
    new content -- this simulates the "before the replace" and
    "after the replace" observation points directly.
    """
    target = tmp_path / "map.json"
    target.write_bytes(b"old-complete-content")
    before = target.read_bytes()
    assert before == b"old-complete-content"

    mapfile.atomic_write_bytes(target, b"new-complete-content")
    after = target.read_bytes()
    assert after == b"new-complete-content"


def test_index_from_maps_builds_heritage_adjacency() -> None:
    files = [
        FileMap(
            path="a.py",
            language="python",
            symbols=[_sym("a.py", "Dog", "class")],
        ),
        FileMap(
            path="b.py",
            language="python",
            symbols=[_sym("b.py", "Animal", "class")],
        ),
    ]
    graph = CallGraph(
        heritage=[
            HeritageEdge(
                subtype="a.py::Dog",
                supertype="b.py::Animal",
                relation="extends",
                lines=[3],
            )
        ]
    )
    index = mapfile.index_from_maps(files, graph, "demo")
    assert index.heritage_out["a.py::Dog"] == ["b.py::Animal"]
    assert index.heritage_in["b.py::Animal"] == ["a.py::Dog"]
    assert index.heritage_lines[("a.py::Dog", "b.py::Animal")] == [3]
    assert index.heritage_relation[("a.py::Dog", "b.py::Animal")] == "extends"


def test_index_from_maps_builds_heritage_ambiguous() -> None:
    files = [
        FileMap(
            path="a.py",
            language="python",
            symbols=[_sym("a.py", "Dog", "class")],
        ),
        FileMap(
            path="b.py",
            language="python",
            symbols=[_sym("b.py", "Base", "class")],
        ),
        FileMap(
            path="c.py",
            language="python",
            symbols=[_sym("c.py", "Base", "class")],
        ),
    ]
    graph = CallGraph(
        heritage_ambiguous=[
            ("a.py::Dog", "Base", ["b.py::Base", "c.py::Base"])
        ]
    )
    index = mapfile.index_from_maps(files, graph, "demo")
    assert index.heritage_ambiguous_out["a.py::Dog"] == ["Base"]
    assert index.heritage_ambiguous_in["b.py::Base"] == [("a.py::Dog", "Base")]
    assert index.heritage_ambiguous_in["c.py::Base"] == [("a.py::Dog", "Base")]


def test_index_from_maps_builds_heritage_external_out() -> None:
    files = [
        FileMap(
            path="a.py",
            language="python",
            symbols=[_sym("a.py", "MyModel", "class")],
        )
    ]
    graph = CallGraph(
        heritage_external=[
            ExternalCall(
                caller="a.py::MyModel",
                callee="pydantic.BaseModel",
                lines=[1],
            )
        ]
    )
    index = mapfile.index_from_maps(files, graph, "demo")
    exts = index.heritage_external_out["a.py::MyModel"]
    assert len(exts) == 1
    assert exts[0].callee == "pydantic.BaseModel"


def test_load_map_reads_heritage(make_mapped_repo: RepoFactory) -> None:
    root = make_mapped_repo(
        {
            "base.py": "class Animal:\n    pass\n",
            "dog.py": (
                "from base import Animal\n\n\nclass Dog(Animal):\n    pass\n"
            ),
        }
    )
    index = mapfile.load_map(root)
    assert index is not None
    assert index.heritage_out["dog.py::Dog"] == ["base.py::Animal"]
    assert index.heritage_in["base.py::Animal"] == ["dog.py::Dog"]
    assert index.heritage_relation[("dog.py::Dog", "base.py::Animal")] == (
        "extends"
    )


def test_load_map_reads_heritage_rust_and_cpp(
    make_mapped_repo: RepoFactory,
) -> None:
    # Phase 2 round-trip: write via render_json, read back via
    # load_map, on a repo mixing Rust's `impl` relation and C++'s
    # multi-base `extends` relation in the same map.json — confirms
    # MAP_DOC_VERSION 6 and id-interning (round 15's own concern)
    # extend to the new languages without any render_json.py/
    # mapfile.py changes, exactly as the design predicted.
    root = make_mapped_repo(
        {
            "shapes.rs": (
                "pub trait Shape {}\n"
                "\n"
                "pub struct Circle;\n"
                "\n"
                "impl Shape for Circle {}\n"
            ),
            "shapes.cpp": (
                "class Base1 {};\n"
                "class Base2 {};\n"
                "class Derived : public Base1, private Base2 {\n"
                "public:\n"
                "};\n"
            ),
        }
    )
    index = mapfile.load_map(root)
    assert index is not None
    assert index.heritage_out["shapes.rs::Circle"] == ["shapes.rs::Shape"]
    assert (
        index.heritage_relation[("shapes.rs::Circle", "shapes.rs::Shape")]
        == "impl"
    )
    assert index.heritage_out["shapes.cpp::Derived"] == [
        "shapes.cpp::Base1",
        "shapes.cpp::Base2",
    ]
    assert (
        index.heritage_relation[("shapes.cpp::Derived", "shapes.cpp::Base1")]
        == "extends"
    )

    doc = json.loads((root / ".dekko" / "map.json").read_text())
    assert doc["version"] == mapfile.MAP_DOC_VERSION


def test_without_tests_drops_heritage_touching_test_paths() -> None:
    files = [
        FileMap(
            path="a.py",
            language="python",
            symbols=[_sym("a.py", "Dog", "class")],
        ),
        FileMap(
            path="b.py",
            language="python",
            symbols=[_sym("b.py", "Animal", "class")],
        ),
        FileMap(
            path="tests/test_a.py",
            language="python",
            symbols=[_sym("tests/test_a.py", "TestDog", "class")],
        ),
    ]
    graph = CallGraph(
        heritage=[
            HeritageEdge(
                subtype="a.py::Dog",
                supertype="b.py::Animal",
                relation="extends",
                lines=[1],
            ),
            HeritageEdge(
                subtype="tests/test_a.py::TestDog",
                supertype="b.py::Animal",
                relation="extends",
                lines=[1],
            ),
        ],
        heritage_ambiguous=[
            ("tests/test_a.py::TestDog", "Animal", ["b.py::Animal"])
        ],
        heritage_external=[
            ExternalCall(
                caller="tests/test_a.py::TestDog",
                callee="unittest.TestCase",
                lines=[1],
            )
        ],
    )
    index = mapfile.index_from_maps(files, graph, "demo")
    filtered = index.without_tests()
    assert filtered.heritage_out == {"a.py::Dog": ["b.py::Animal"]}
    assert filtered.heritage_in == {"b.py::Animal": ["a.py::Dog"]}
    assert "tests/test_a.py::TestDog" not in filtered.heritage_ambiguous_out
    assert filtered.heritage_external_out == {}


def test_index_from_maps_builds_module_graph_adjacency() -> None:
    files = [
        FileMap(path="a.py", language="python"),
        FileMap(path="b.py", language="python"),
    ]
    graph = CallGraph(
        modules=ModuleGraph(
            edges=[ModuleEdge(importer="a.py", imported="b.py", names=["x"])],
            deps_out={"a.py": ["b.py"]},
            deps_in={"b.py": ["a.py"]},
            external={"a.py": ["os"]},
        )
    )
    index = mapfile.index_from_maps(files, graph, "demo")
    assert index.module_deps_out["a.py"] == ["b.py"]
    assert index.module_deps_in["b.py"] == ["a.py"]
    assert index.module_edge_names[("a.py", "b.py")] == ["x"]
    assert index.module_external["a.py"] == ["os"]


def test_load_map_reads_module_graph(make_mapped_repo: RepoFactory) -> None:
    root = make_mapped_repo(
        {
            "a.py": (
                "from .b import helper\ndef main():\n    return helper()\n"
            ),
            "b.py": "def helper():\n    return 1\n",
        }
    )
    index = mapfile.load_map(root)
    assert index is not None
    assert index.module_deps_out["a.py"] == ["b.py"]
    assert index.module_deps_in["b.py"] == ["a.py"]
    assert index.module_edge_names[("a.py", "b.py")] == ["helper"]


def test_without_tests_drops_module_edges_touching_test_paths() -> None:
    files = [
        FileMap(path="a.py", language="python"),
        FileMap(path="tests/test_a.py", language="python"),
    ]
    graph = CallGraph(
        modules=ModuleGraph(
            edges=[
                ModuleEdge(importer="tests/test_a.py", imported="a.py"),
            ],
            deps_out={"tests/test_a.py": ["a.py"]},
            deps_in={"a.py": ["tests/test_a.py"]},
            external={"tests/test_a.py": ["pytest"]},
        )
    )
    index = mapfile.index_from_maps(files, graph, "demo")
    filtered = index.without_tests()
    assert filtered.module_deps_out == {}
    assert filtered.module_deps_in == {}
    assert filtered.module_external == {}
