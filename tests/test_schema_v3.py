"""map.json doc version 3: edge lines, externals, test flags, compat."""

import json
from pathlib import Path

from dekko import cli
from dekko.mapfile import load_map

from conftest import RepoFactory

SRC = {
    "src/app.py": (
        '"""App module."""\n'
        "\n"
        "\n"
        "def helper():\n"
        '    """Add one."""\n'
        "    return 1\n"
        "\n"
        "\n"
        "def main():\n"
        "    helper()\n"
        "    helper()\n"
        "    external_thing()\n"
        "\n"
        "\n"
        'print("x")\n'
    ),
    "tests/test_app.py": ("def test_main():\n    pass\n"),
}


def _map_doc(root: Path) -> dict:
    return json.loads((root / ".dekko" / "map.json").read_text())


def test_doc_version_is_3(make_mapped_repo: RepoFactory) -> None:
    doc = _map_doc(make_mapped_repo(SRC))
    assert doc["version"] == 3


def test_edges_carry_call_site_lines(make_mapped_repo: RepoFactory) -> None:
    root = make_mapped_repo(SRC)
    doc = _map_doc(root)
    edges = {(e["caller"], e["callee"]): e["lines"] for e in doc["edges"]}
    key = ("src/app.py::main", "src/app.py::helper")
    assert edges[key] == [10, 11]

    index = load_map(root)
    assert index is not None
    assert index.edge_lines[key] == [10, 11]


def test_externals_normalized_with_lines(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(SRC)
    doc = _map_doc(root)
    by_callee = {e["callee"]: e for e in doc["external"]}
    ext = by_callee["external_thing"]
    assert ext["caller"] == "src/app.py::main"
    assert ext["lines"] == [12]
    assert by_callee["print"]["caller"] == "src/app.py::<module>"

    index = load_map(root)
    assert index is not None
    callers = {e.caller for e in index.externals_by_name["external_thing"]}
    assert callers == {"src/app.py::main"}


def test_symbols_tagged_with_test_flag(make_mapped_repo: RepoFactory) -> None:
    index = load_map(make_mapped_repo(SRC))
    assert index is not None
    assert index.symbols_by_id["tests/test_app.py::test_main"].test
    assert not index.symbols_by_id["src/app.py::main"].test


def test_docs_round_trip_through_map(make_mapped_repo: RepoFactory) -> None:
    index = load_map(make_mapped_repo(SRC))
    assert index is not None
    assert index.docs_by_path["src/app.py"] == "App module."
    assert index.symbols_by_id["src/app.py::helper"].doc == "Add one."


def test_warm_run_output_matches_cold(make_mapped_repo: RepoFactory) -> None:
    root = make_mapped_repo(SRC)
    cold = _map_doc(root)
    assert cli.main(["map", str(root), "--quiet"]) == 0
    warm = _map_doc(root)
    for key in ("files", "symbols", "edges", "ambiguous", "external"):
        assert warm[key] == cold[key]


def test_v2_document_loads_with_defaults(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(SRC)
    path = root / ".dekko" / "map.json"
    doc = json.loads(path.read_text())

    doc["version"] = 2
    for entry in doc["files"]:
        entry.pop("doc", None)
    for sym in doc["symbols"]:
        sym.pop("doc", None)
        sym.pop("test", None)
    for edge in doc["edges"]:
        edge.pop("lines", None)
    doc["external"] = [
        {"caller": e["caller"] or None, "callee": e["callee"]}
        for e in doc["external"]
    ]
    path.write_text(json.dumps(doc))

    index = load_map(root)
    assert index is not None
    key = ("src/app.py::main", "src/app.py::helper")
    assert index.edge_lines[key] == []
    helper = index.symbols_by_id["src/app.py::helper"]
    assert helper.doc is None
    assert helper.test is False
    assert index.docs_by_path["src/app.py"] is None
    assert "external_thing" in index.externals_by_name
