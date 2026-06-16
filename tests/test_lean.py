"""FR1 file backbone: floor guarantee, dense encoding, determinism.

Plus the FR2/FR4 atom layer: name + signature atoms with Q1 centrality.
"""

import json
from collections import Counter
from pathlib import Path

import pytest

from dekko import cli, render_lean, server, summary
from dekko.mapfile import load_map

from conftest import RepoFactory

# A repo with production files in nested dirs plus a tests/ tree, so the
# floor-vs-demotable partition and directory grouping both have teeth.
FILES = {
    "src/pkg/core.py": (
        '"""Core engine: orchestrates the pipeline end to end."""\n'
        "def run() -> None:\n"
        "    pass\n"
    ),
    "src/pkg/util.py": (
        '"""Small helpers shared across the package."""\n'
        "def helper() -> int:\n"
        "    return 1\n"
    ),
    "src/pkg/nodoc.py": "def bare() -> None:\n    pass\n",
    "tests/test_core.py": "def test_run() -> None:\n    pass\n",
    "tests/test_util.py": "def test_helper() -> None:\n    pass\n",
}


def _backbone(make_mapped_repo: RepoFactory) -> tuple[Path, list]:
    root = make_mapped_repo(FILES)
    index = load_map(root)
    assert index is not None
    return root, render_lean.compute_backbone(index)


def test_every_production_file_present_with_purpose(
    make_mapped_repo: RepoFactory,
) -> None:
    _, groups = _backbone(make_mapped_repo)
    by_path = {
        row.path: row for g in groups for row in g.rows
    }
    assert "src/pkg/core.py" in by_path
    assert "src/pkg/util.py" in by_path
    assert by_path["src/pkg/core.py"].purpose.startswith("Core engine")
    # Production files are the floor: never demotable.
    assert by_path["src/pkg/core.py"].demotable is False
    assert by_path["src/pkg/util.py"].demotable is False


def test_test_files_are_demotable(make_mapped_repo: RepoFactory) -> None:
    _, groups = _backbone(make_mapped_repo)
    tests = next(g for g in groups if g.directory == "tests")
    assert tests.demotable is True
    assert all(r.demotable for r in tests.rows)


def test_docless_file_has_empty_purpose(
    make_mapped_repo: RepoFactory,
) -> None:
    _, groups = _backbone(make_mapped_repo)
    by_path = {row.path: row for g in groups for row in g.rows}
    assert by_path["src/pkg/nodoc.py"].purpose == ""


def test_groups_and_rows_sorted(make_mapped_repo: RepoFactory) -> None:
    _, groups = _backbone(make_mapped_repo)
    dirs = [g.directory for g in groups]
    assert dirs == sorted(dirs)
    for g in groups:
        paths = [r.path for r in g.rows]
        assert paths == sorted(paths)


def test_determinism_byte_identical(
    make_mapped_repo: RepoFactory,
) -> None:
    _, groups = _backbone(make_mapped_repo)
    first = render_lean.render_backbone(groups)
    second = render_lean.render_backbone(groups)
    assert first == second


def test_dense_encoding_amortizes_dir_prefix(
    make_mapped_repo: RepoFactory,
) -> None:
    _, groups = _backbone(make_mapped_repo)
    lines = render_lean.render_backbone(groups)
    # Directory header carries the path once...
    assert "src/pkg/" in lines
    # ...and file rows are basename-only, indented, no repeated prefix.
    core = next(ln for ln in lines if "core.py" in ln)
    assert core.startswith("  core.py")
    assert "src/pkg/core.py" not in core


def test_docless_row_has_no_separator(
    make_mapped_repo: RepoFactory,
) -> None:
    _, groups = _backbone(make_mapped_repo)
    lines = render_lean.render_backbone(groups)
    row = next(ln for ln in lines if "nodoc.py" in ln)
    assert row == "  nodoc.py"


def test_width_zero_drops_purpose_keeps_path(
    make_mapped_repo: RepoFactory,
) -> None:
    _, groups = _backbone(make_mapped_repo)
    lines = render_lean.render_backbone(groups, width=0)
    row = next(ln for ln in lines if "core.py" in ln)
    # Floor's narrowest rung: path survives, purpose is gone.
    assert row == "  core.py"
    assert "Core engine" not in "\n".join(lines)


def test_narrowing_width_truncates_purpose(
    make_mapped_repo: RepoFactory,
) -> None:
    _, groups = _backbone(make_mapped_repo)
    wide = render_lean.render_backbone(groups, width=72)
    narrow = render_lean.render_backbone(groups, width=12)
    wide_core = next(ln for ln in wide if "core.py" in ln)
    narrow_core = next(ln for ln in narrow if "core.py" in ln)
    assert len(narrow_core) < len(wide_core)
    assert narrow_core.startswith("  core.py")


def test_collapse_demotable_folds_test_dir(
    make_mapped_repo: RepoFactory,
) -> None:
    _, groups = _backbone(make_mapped_repo)
    lines = render_lean.render_backbone(groups, collapse_demotable=True)
    assert "tests/  (2 files)" in lines
    # Production dir stays expanded even when collapsing is on.
    assert "src/pkg/" in lines
    assert any("core.py" in ln for ln in lines)
    # No individual test file leaked through the collapse.
    assert not any("test_core.py" in ln for ln in lines)


def test_purpose_truncated_to_width_cap(
    make_mapped_repo: RepoFactory,
) -> None:
    long_doc = "x" * 200
    root = make_mapped_repo(
        {"src/long.py": f'"""{long_doc}"""\ndef f() -> None:\n    pass\n'}
    )
    index = load_map(root)
    assert index is not None
    groups = render_lean.compute_backbone(index)
    row = next(r for g in groups for r in g.rows if r.path == "src/long.py")
    assert len(row.purpose) <= render_lean.LEAN_PURPOSE_WIDTH


# --- FR2/FR4 atom layer ---------------------------------------------

# hub() is called by caller(); test_hub is test code. Gives a fan-in
# gradient (hub=1, caller=0) and a demotable file.
ATOM_FILES = {
    "src/pkg/core.py": (
        '"""Core."""\n'
        "def hub() -> int:\n"
        "    return 1\n"
        "\n"
        "\n"
        "def caller() -> int:\n"
        "    return hub()\n"
    ),
    "tests/test_core.py": "def test_hub() -> None:\n    pass\n",
}


def _atoms(
    make_mapped_repo: RepoFactory, churn: Counter | None = None
) -> dict:
    root = make_mapped_repo(ATOM_FILES)
    index = load_map(root)
    assert index is not None
    return render_lean.build_atoms(index, churn or Counter())


def _find(atoms: dict, path: str, name: str) -> render_lean.SymbolAtom:
    return next(a for a in atoms[path] if a.name == name)


def test_atom_carries_name_signature_path_demotable(
    make_mapped_repo: RepoFactory,
) -> None:
    atoms = _atoms(make_mapped_repo)
    hub = _find(atoms, "src/pkg/core.py", "hub")
    assert hub.name == "hub"
    assert hub.signature == "hub() -> int"
    assert hub.path == "src/pkg/core.py"
    assert hub.demotable is False
    test_atom = _find(atoms, "tests/test_core.py", "test_hub")
    assert test_atom.demotable is True


def test_atoms_in_definition_order(
    make_mapped_repo: RepoFactory,
) -> None:
    atoms = _atoms(make_mapped_repo)
    names = [a.name for a in atoms["src/pkg/core.py"]]
    assert names == ["hub", "caller"]


def test_centrality_is_fan_in_without_churn(
    make_mapped_repo: RepoFactory,
) -> None:
    atoms = _atoms(make_mapped_repo)
    hub = _find(atoms, "src/pkg/core.py", "hub")
    caller = _find(atoms, "src/pkg/core.py", "caller")
    # hub is called once, caller by no one.
    assert hub.centrality == 1.0
    assert caller.centrality == 0.0


def test_churn_boosts_centrality(
    make_mapped_repo: RepoFactory,
) -> None:
    churn = Counter({"src/pkg/core.py": 4})
    atoms = _atoms(make_mapped_repo, churn)
    hub = _find(atoms, "src/pkg/core.py", "hub")
    # max_churn == 4, weight = 1 + 4/4 = 2.0, so fan-in 1 -> 2.0.
    assert hub.centrality == 2.0


def test_centrality_key_orders_lowest_first(
    make_mapped_repo: RepoFactory,
) -> None:
    atoms = _atoms(make_mapped_repo)
    flat = [a for rows in atoms.values() for a in rows]
    flat.sort(key=render_lean.centrality_key)
    # Uncalled symbols (centrality 0) shed before the called hub.
    assert flat[0].centrality <= flat[-1].centrality
    assert flat[-1].name == "hub"


def test_centrality_key_deterministic_ties(
    make_mapped_repo: RepoFactory,
) -> None:
    atoms = _atoms(make_mapped_repo)
    flat = [a for rows in atoms.values() for a in rows]
    once = sorted(flat, key=render_lean.centrality_key)
    twice = sorted(flat, key=render_lean.centrality_key)
    assert [a.sym_id for a in once] == [a.sym_id for a in twice]


def test_class_atom_signature(make_mapped_repo: RepoFactory) -> None:
    root = make_mapped_repo(
        {"src/c.py": '"""C."""\nclass Thing:\n    pass\n'}
    )
    index = load_map(root)
    assert index is not None
    atoms = render_lean.build_atoms(index, Counter())
    thing = _find(atoms, "src/c.py", "Thing")
    assert thing.signature == "class Thing"


def test_file_churn_empty_on_non_git_root(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(ATOM_FILES)
    # tmp_path is not a git repo; best-effort churn is empty.
    assert summary.file_churn(root) == Counter()


# --- FR3 module-edge text -------------------------------------------

# main.py (src/app) calls into src/pkg and src/util -> two cross-dir
# edges from one source directory, exercising grouping.
EDGE_FILES = {
    "src/pkg/core.py": '"""Core."""\ndef base() -> int:\n    return 1\n',
    "src/util/helper.py": '"""Util."""\ndef aid() -> int:\n    return 2\n',
    "src/app/main.py": (
        '"""App."""\n'
        "from pkg.core import base\n"
        "from util.helper import aid\n"
        "def go() -> int:\n"
        "    return base() + aid()\n"
    ),
}


def _edges(make_mapped_repo: RepoFactory) -> list[tuple[str, str]]:
    root = make_mapped_repo(EDGE_FILES)
    index = load_map(root)
    assert index is not None
    return render_lean.module_edges(index)


def test_module_edges_are_cross_directory(
    make_mapped_repo: RepoFactory,
) -> None:
    edges = _edges(make_mapped_repo)
    assert ("src/app", "src/pkg") in edges
    assert ("src/app", "src/util") in edges
    # No same-directory self-loops.
    assert all(src != dst for src, dst in edges)


def test_module_edges_empty_without_cross_dir_calls(
    make_mapped_repo: RepoFactory,
) -> None:
    # ATOM_FILES' only edge (caller -> hub) is within src/pkg.
    root = make_mapped_repo(ATOM_FILES)
    index = load_map(root)
    assert index is not None
    assert render_lean.module_edges(index) == []


def test_render_module_edges_groups_targets(
    make_mapped_repo: RepoFactory,
) -> None:
    edges = _edges(make_mapped_repo)
    lines = render_lean.render_module_edges(edges)
    app = next(ln for ln in lines if ln.startswith("src/app/"))
    # Targets joined on one line, sorted, trailing slash on each dir.
    assert app == "src/app/ → src/pkg/, src/util/"


def test_render_module_edges_empty(
    make_mapped_repo: RepoFactory,
) -> None:
    assert render_lean.render_module_edges([]) == []


def test_render_module_edges_deterministic(
    make_mapped_repo: RepoFactory,
) -> None:
    edges = _edges(make_mapped_repo)
    assert render_lean.render_module_edges(edges) == (
        render_lean.render_module_edges(edges)
    )


# --- NFR2 degradation ladder ----------------------------------------

# hub() is called by go() and test_hub(); leaf() and go() are uncalled.
# Cross-dir calls give module edges; tests/ is a demotable group.
LADDER_FILES = {
    "src/pkg/core.py": (
        '"""Core engine for the package."""\n'
        "def hub() -> int:\n"
        "    return 1\n"
        "\n"
        "\n"
        "def leaf() -> int:\n"
        "    return 0\n"
    ),
    "src/app/main.py": (
        '"""Application entry point."""\n'
        "from pkg.core import hub\n"
        "def go() -> int:\n"
        "    return hub()\n"
    ),
    "tests/test_core.py": (
        "from pkg.core import hub\n"
        "def test_hub() -> None:\n"
        "    assert hub() == 1\n"
    ),
}


def _gen(
    make_mapped_repo: RepoFactory, override: int | None = None
) -> tuple[list[str], render_lean.LeanReport]:
    root = make_mapped_repo(LADDER_FILES)
    index = load_map(root)
    assert index is not None
    cfg = render_lean.CapConfig(override=override)
    return render_lean.generate(index, root, cfg)


def test_full_fidelity_fits_small_repo(
    make_mapped_repo: RepoFactory,
) -> None:
    lines, report = _gen(make_mapped_repo)
    out = "\n".join(lines)
    assert report.dropped_any is False
    assert report.tokens <= report.cap
    # Every layer present: purposes, signatures, module edges.
    assert "Core engine for the package" in out
    assert "hub() -> int" in out
    assert "leaf() -> int" in out
    assert "module edges:" in out


def test_header_reports_budget(make_mapped_repo: RepoFactory) -> None:
    lines, report = _gen(make_mapped_repo)
    assert lines[0].startswith("lean map · ~")
    assert f"/{report.cap} tok" in lines[0]


def test_cap_scales_with_file_count(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(LADDER_FILES)
    index = load_map(root)
    assert index is not None
    model = render_lean.build_model(index, root)
    n = sum(len(g.rows) for g in model.groups)
    cap = render_lean.effective_cap(model, render_lean.CapConfig())
    assert cap == render_lean.LEAN_CAP_BASE + render_lean.LEAN_CAP_PER_FILE * n


def test_cap_is_floor_aware_under_tiny_override(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(LADDER_FILES)
    index = load_map(root)
    assert index is not None
    model = render_lean.build_model(index, root)
    cap = render_lean.effective_cap(
        model, render_lean.CapConfig(override=1)
    )
    # The cap bends up to the path-only floor; never down to 1.
    assert cap > 1
    assert cap == render_lean._floor_cost(model)


def test_tiny_budget_falls_to_floor_but_keeps_paths(
    make_mapped_repo: RepoFactory,
) -> None:
    lines, report = _gen(make_mapped_repo, override=1)
    out = "\n".join(lines)
    assert report.floored is True
    assert report.tokens <= report.cap
    # Production file paths survive (the never-elided floor)...
    assert "core.py" in out
    assert "main.py" in out
    # ...but all depth is gone.
    assert "hub() -> int" not in out
    assert "module edges:" not in out
    assert report.mermaid_dropped and report.module_edges_dropped


def test_tiny_budget_header_lists_drops_and_recovery(
    make_mapped_repo: RepoFactory,
) -> None:
    lines, _ = _gen(make_mapped_repo, override=1)
    header = "\n".join(lines[:3])
    assert "dropped:" in header
    assert "recover:" in header
    assert "dekko outline" in header


def test_ladder_deterministic(make_mapped_repo: RepoFactory) -> None:
    root = make_mapped_repo(LADDER_FILES)
    index = load_map(root)
    assert index is not None
    model = render_lean.build_model(index, root)
    cap = render_lean.effective_cap(model, render_lean.CapConfig())
    first, _ = render_lean.render(model, cap)
    second, _ = render_lean.render(model, cap)
    assert first == second


def test_low_centrality_signatures_shed_first(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(LADDER_FILES)
    index = load_map(root)
    assert index is not None
    model = render_lean.build_model(index, root)
    full = render_lean.effective_cap(model, render_lean.CapConfig())
    floor = render_lean._floor_cost(model)
    # Sweep caps between floor and full; the hub (high centrality) must
    # never lose its signature while the leaf (centrality 0) keeps one.
    for cap in range(floor, full + 1, 5):
        out = "\n".join(render_lean.render(model, cap)[0])
        leaf_kept = "leaf() -> int" in out
        hub_kept = "hub() -> int" in out
        assert not (leaf_kept and not hub_kept)


def test_module_edges_outlive_symbols(
    make_mapped_repo: RepoFactory,
) -> None:
    # Edges shed at rung 5, after all names (rung 4): if edges were
    # dropped, every name must already be gone.
    _, report = _gen(make_mapped_repo, override=1)
    if report.module_edges_dropped:
        assert report.names_dropped == report.total_symbols


def test_report_as_dict_shape(make_mapped_repo: RepoFactory) -> None:
    _, report = _gen(make_mapped_repo, override=1)
    d = report.as_dict()
    assert set(d) == {
        "tokens",
        "cap",
        "mermaid_dropped",
        "demotable_collapsed",
        "signatures_dropped",
        "names_dropped",
        "module_edges_dropped",
        "purpose_width",
        "floored",
        "signals",
        "tokens_per_signal",
        "already_seen",
    }
    assert d["floored"] is True


# --- FR6 mermaid block ----------------------------------------------


def test_build_mermaid_renders_dir_graph(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(EDGE_FILES)
    index = load_map(root)
    assert index is not None
    block = render_lean.build_mermaid(index)
    assert block[0] == "```mermaid"
    assert block[-1] == "```"
    assert any("flowchart" in ln for ln in block)


def test_build_mermaid_empty_without_cross_dir_edges(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(ATOM_FILES)  # only same-dir edges
    index = load_map(root)
    assert index is not None
    assert render_lean.build_mermaid(index) == []


def test_build_mermaid_capped_by_node_count(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(EDGE_FILES)
    index = load_map(root)
    assert index is not None
    # Three dir nodes exceed a cap of 1 -> omitted.
    assert render_lean.build_mermaid(index, max_nodes=1) == []


def test_build_model_populates_mermaid(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(EDGE_FILES)
    index = load_map(root)
    assert index is not None
    model = render_lean.build_model(index, root)
    assert model.mermaid and model.mermaid[0] == "```mermaid"


def test_mermaid_present_at_full_fidelity_dropped_when_tight(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(EDGE_FILES)
    index = load_map(root)
    assert index is not None
    model = render_lean.build_model(index, root)
    full_cap = render_lean.effective_cap(model, render_lean.CapConfig())
    full_out = "\n".join(render_lean.render(model, full_cap)[0])
    assert "```mermaid" in full_out
    # Tiny budget: the mermaid is the first rung dropped.
    floor = render_lean._floor_cost(model)
    tight_out, report = render_lean.render(model, floor)
    assert report.mermaid_dropped is True
    assert "```mermaid" not in tight_out


# --- `dekko lean` command (CLI + MCP) -------------------------------


def test_cli_lean_prints_to_stdout(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(LADDER_FILES)
    code = cli.main(["lean", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert out.startswith("lean map · ~")
    assert "core.py" in out


def test_cli_lean_output_writes_file(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(LADDER_FILES)
    dest = root / ".dekko" / "LEAN.md"
    code = cli.main(
        ["lean", "--root", str(root), "--output", str(dest)]
    )
    assert code == 0
    assert dest.exists()
    assert dest.read_text().startswith("lean map · ~")
    # Stdout carries a confirmation, not the map itself.
    assert "wrote" in capsys.readouterr().out


def test_cli_lean_json(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(LADDER_FILES)
    code = cli.main(["lean", "--root", str(root), "--json"])
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["map"].startswith("lean map · ~")
    assert {"tokens", "cap", "floored"} <= doc["meta"].keys()


def test_cli_lean_budget_floors(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(LADDER_FILES)
    code = cli.main(["lean", "--root", str(root), "--budget", "1"])
    assert code == 0
    out = capsys.readouterr().out
    assert "core.py" in out                 # floor paths survive
    assert "hub() -> int" not in out         # all depth shed


def test_lean_registered_and_tool_count() -> None:
    assert "lean" in cli.SUBCOMMANDS
    names = {t["name"] for t in server.TOOLS}
    assert "lean" in names
    assert "ledger" in names
    # Canonical MCP tool-count assertion now lives here.
    assert len(server.TOOLS) == 18


def test_mcp_lean_tool(make_mapped_repo: RepoFactory) -> None:
    root = make_mapped_repo(LADDER_FILES)
    ctx = server.Context(default_root=root, no_regen=False)
    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "lean", "arguments": {}},
    }
    result = server.handle(ctx, msg)["result"]
    assert not result["isError"]
    assert result["content"][0]["text"].startswith("lean map · ~")
