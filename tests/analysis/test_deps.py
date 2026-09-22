"""The deps command: summary/--file/--cycles/--export, budget-capping,
CLI wiring, and its deliberate CLI-only (no MCP tool) surface."""

import json
from itertools import pairwise
from pathlib import Path

import pytest

from dekko.analysis import deps
from dekko.integrations import cli
from dekko.integrations import server
from dekko.render import mapfile

from conftest import RepoFactory

# A 3-file cycle (a -> b -> c -> a) plus a standalone external-only
# file and one file with no imports/importers at all.
CYCLE_REPO = {
    "a.py": "from .b import bfunc\ndef afunc():\n    return bfunc()\n",
    "b.py": "from .c import cfunc\ndef bfunc():\n    return cfunc()\n",
    "c.py": "from .a import afunc\ndef cfunc():\n    return afunc()\n",
    "standalone.py": "import os\ndef main():\n    return os.getcwd()\n",
    "quiet.py": "x = 1\n",
}

# Round 33 Track 2, claude-buddy's exact shape: two loops sharing one
# file (state). art -> theme -> state -> art and state -> xp -> state.
# One strongly-connected cluster, four files, five edges, and NOT the
# ring the sorted member list (art, state, theme, xp) would suggest.
SHARED_NODE_REPO = {
    "art.py": "from .theme import t\ndef a():\n    return t()\n",
    "theme.py": "from .state import s\ndef t():\n    return s()\n",
    "state.py": (
        "from .art import a\nfrom .xp import x\n"
        "def s():\n    return a() + x()\n"
    ),
    "xp.py": "from .state import s\ndef x():\n    return s()\n",
}

TWO_CYCLE_REPO = {
    "x.py": "from .y import yfunc\ndef xfunc():\n    return yfunc()\n",
    "y.py": "from .x import xfunc\ndef yfunc():\n    return xfunc()\n",
}

ACYCLIC_REPO = {
    "a.py": "from .b import helper\ndef top():\n    return helper()\n",
    "b.py": "def helper():\n    return 1\n",
}

TEST_FILE_IMPORT_REPO = {
    "a.py": "def helper():\n    return 1\n",
    "tests/test_a.py": (
        "from ..a import helper\ndef test_helper():\n    return helper()\n"
    ),
}


def test_deps_is_cli_only_no_mcp_tool() -> None:
    assert not any("dep" in t["name"].lower() for t in server.TOOLS)


def test_deps_summary_text(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(CYCLE_REPO)
    code = cli.main(["deps", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "5 files" in out
    assert "resolved import edges" in out
    assert "1 circular-import cluster (3 files)" in out
    assert "most-depended-on files:" in out


def test_deps_summary_json(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(CYCLE_REPO)
    code = cli.main(["deps", "--root", str(root), "--json"])
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["files"] == 5
    assert doc["cycles"] == 1
    assert doc["cycle_files"] == 3
    assert doc["self_cycles"] == 0


def test_deps_summary_acyclic_repo_no_cycle_line(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(ACYCLIC_REPO)
    code = cli.main(["deps", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "detected" not in out


# Round-29 Track 4a (flagged rounds 27/28/29 on awesome-go): a repo
# with zero resolved import edges purely because its only language
# (Go) has no per-language import resolver at all -- every Go import
# reports external unconditionally, not a mapping failure -- must say
# so up front rather than read as "did something break."
GO_ONLY_REPO = {
    "main.go": (
        'package main\n\nimport "fmt"\n\nfunc main() {\n'
        '\tfmt.Println("hi")\n}\n'
    ),
    "helper.go": "package main\n\nfunc helper() int {\n\treturn 1\n}\n",
}


def test_deps_go_only_repo_discloses_import_resolution_gap(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(GO_ONLY_REPO)
    code = cli.main(["deps", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "0 resolved import edges" in out
    assert "go" in out
    assert "imports dekko does not resolve" in out


def test_deps_go_only_repo_json_discloses_import_resolution_gap(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(GO_ONLY_REPO)
    code = cli.main(["deps", "--root", str(root), "--json"])
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["edges"] == 0
    assert (
        "imports dekko does not resolve" in doc["import_resolution_coverage"]
    )


def test_deps_real_resolved_edges_no_import_resolution_gap_note(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # A repo with genuine resolved edges (Python, fully covered) must
    # never show the Go-scope-gap note -- it's gated on edges == 0.
    root = make_mapped_repo(ACYCLIC_REPO)
    code = cli.main(["deps", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "imports dekko does not resolve" not in out


def test_deps_discloses_unsupported_file_coverage_gap(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # Round-29 Track 4b: `deps` never disclosed skipped-file coverage
    # (unsupported languages, vendored/too-large/symlinked exclusions)
    # at all, distinct from the Go import-resolution-scope note above.
    root = make_mapped_repo(
        dict(ACYCLIC_REPO, **{"Card.astro": "---\nconst x = 1;\n---\n"})
    )
    code = cli.main(["deps", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "no parser for: astro" in out


def test_deps_file_view_text(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(CYCLE_REPO)
    code = cli.main(["deps", "--root", str(root), "--file", "a.py"])
    assert code == 0
    out = capsys.readouterr().out
    assert "imports (1):" in out
    assert "b.py" in out
    assert "imported by (1):" in out
    assert "c.py" in out
    assert "external (0):" in out


def test_deps_file_view_json(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(CYCLE_REPO)
    code = cli.main(["deps", "--root", str(root), "--file", "a.py", "--json"])
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["path"] == "a.py"
    assert doc["imports"] == ["b.py"]
    assert doc["imported_by"] == ["c.py"]
    assert doc["external"] == []


def test_deps_file_view_external_disclosure(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(CYCLE_REPO)
    code = cli.main(["deps", "--root", str(root), "--file", "standalone.py"])
    assert code == 0
    out = capsys.readouterr().out
    assert "external (1): os" in out


def test_deps_file_view_standalone_file_empty_lists(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(CYCLE_REPO)
    code = cli.main(["deps", "--root", str(root), "--file", "quiet.py"])
    assert code == 0
    out = capsys.readouterr().out
    assert "imports (0):" in out
    assert "imported by (0):" in out
    assert "external (0):" in out


def test_deps_file_not_found(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(CYCLE_REPO)
    code = cli.main(["deps", "--root", str(root), "--file", "nope.py"])
    assert code == deps.EXIT_NOT_FOUND
    assert "no mapped file 'nope.py'" in capsys.readouterr().err


def test_deps_file_ambiguous_suffix(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(
        {
            "pkg_a/index.ts": "export function a() { return 1; }\n",
            "pkg_b/index.ts": "export function b() { return 2; }\n",
        }
    )
    code = cli.main(["deps", "--root", str(root), "--file", "index.ts"])
    assert code == deps.EXIT_AMBIGUOUS
    err = capsys.readouterr().err
    assert "'index.ts' is ambiguous; candidates:" in err
    assert "pkg_a/index.ts" in err
    assert "pkg_b/index.ts" in err
    assert capsys.readouterr().out == ""


def test_deps_file_bare_suffix_resolves_unambiguous_match(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(CYCLE_REPO)
    code = cli.main(["deps", "--root", str(root), "--file", "a.py"])
    assert code == 0
    out = capsys.readouterr().out
    assert "imports (1):" in out
    assert "b.py" in out


def test_deps_file_zero_symbol_barrel_file_still_resolves(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # A pure re-export file extracts zero symbols, so it never gets a
    # key in index.symbols_by_path -- deps --file must still resolve
    # it by exact full path via the wider languages_by_path pool (D2's
    # "why not just reuse paths_matching as-is" regression guard).
    root = make_mapped_repo(
        {
            "sdk/SdkController.js": (
                "export function Controller() { return 1; }\n"
            ),
            "index.js": 'export { Controller } from "./sdk/SdkController";\n',
        }
    )
    index = mapfile.load_map(root)
    assert index is not None
    assert "index.js" not in index.symbols_by_path
    assert "index.js" in index.languages_by_path
    code = cli.main(["deps", "--root", str(root), "--file", "index.js"])
    assert code == 0
    out = capsys.readouterr().out
    assert "imports (0):" in out
    assert "imported by (0):" in out


def _assert_every_arrow_is_a_real_edge(out: str, root: Path) -> int:
    """The invariant round 33 Track 2 exists to establish: every ``->``
    on the page is a direct import in ``module_deps_out``. Returns the
    number of arrows checked so a test can assert it saw some."""
    index = mapfile.load_map(root)
    assert index is not None
    deps_out = index.module_deps_out
    checked = 0
    for line in out.splitlines():
        if " -> " not in line:
            continue
        body = line.split("shortest loop:", 1)[-1]
        chain = [p.strip() for p in body.split(" -> ")]
        for a, b in pairwise(chain):
            assert b in deps_out.get(a, ()), f"fabricated edge {a} -> {b}"
            checked += 1
    return checked


def test_deps_cycles_text(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(CYCLE_REPO)
    code = cli.main(["deps", "--root", str(root), "--cycles"])
    assert code == 0
    out = capsys.readouterr().out
    assert "cluster 1 (3 files, 3 import edges among them):" in out
    assert "  a.py, b.py, c.py" in out
    assert "shortest loop: a.py -> b.py -> c.py -> a.py" in out
    assert (
        "  edges:\n    a.py -> b.py\n    b.py -> c.py\n    c.py -> a.py" in out
    )
    assert _assert_every_arrow_is_a_real_edge(out, root) == 6


def test_deps_cycles_shared_node_prints_a_real_loop_not_the_sorted_list(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # Before: "art.py -> state.py -> theme.py -> xp.py -> art.py", four
    # arrows, zero of them real edges (claude-buddy.md §1).
    root = make_mapped_repo(SHARED_NODE_REPO)
    assert cli.main(["deps", "--root", str(root), "--cycles"]) == 0
    out = capsys.readouterr().out
    assert "cluster 1 (4 files, 5 import edges among them):" in out
    assert "art.py, state.py, theme.py, xp.py" in out
    assert "art.py -> state.py" not in out
    assert "shortest loop: state.py -> xp.py -> state.py" in out
    assert _assert_every_arrow_is_a_real_edge(out, root) == 7


def test_deps_cycles_large_cluster_clips_members_and_counts_edges(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # 20 files in one ring plus every pair mutually importing: 20 + 380
    # edges, well over the list cap. Members clip, edges become a
    # count, and no row is allowed to grow with the cluster.
    n = 20
    files = {}
    for i in range(n):
        imports = "".join(
            f"from .m{j} import f{j}\n" for j in range(n) if j != i
        )
        files[f"m{i}.py"] = f"{imports}def f{i}():\n    return 1\n"
    root = make_mapped_repo(files)
    assert cli.main(["deps", "--root", str(root), "--cycles"]) == 0
    out = capsys.readouterr().out
    assert "cluster 1 (20 files, 380 import edges among them):" in out
    assert ", +8 more" in out
    assert "  edges: 380 (see `dekko deps --file <member>`" in out
    assert "two-file loops inside: 190" in out
    assert "    m" not in out  # no per-edge rows
    assert _assert_every_arrow_is_a_real_edge(out, root) == 2
    assert max(len(line) for line in out.splitlines()) < 400

    assert cli.main(["deps", "--root", str(root), "--cycles", "--json"]) == 0
    entry = json.loads(capsys.readouterr().out)["results"][0]
    assert entry["internal_edges"] == 380
    assert entry["two_file_loops"] == 190
    assert len(entry["shortest_loop"]) == 2
    assert "edges" not in entry


def test_deps_cycles_self_import_block_unchanged(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(
        {"loop.py": "from .loop import f\ndef f():\n    return 1\n"}
    )
    assert cli.main(["deps", "--root", str(root), "--cycles"]) == 0
    out = capsys.readouterr().out
    assert "cycle 1 (1 file):\n  loop.py  (self-import)" in out
    assert cli.main(["deps", "--root", str(root), "--cycles", "--json"]) == 0
    entry = json.loads(capsys.readouterr().out)["results"][0]
    assert entry == {"files": ["loop.py"], "self_import": True}


def test_deps_cycles_output_is_deterministic(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SHARED_NODE_REPO)
    runs = []
    for _ in range(2):
        assert cli.main(["deps", "--root", str(root), "--cycles"]) == 0
        runs.append(capsys.readouterr().out)
    assert runs[0] == runs[1]


def test_deps_cycles_json(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(CYCLE_REPO)
    code = cli.main(["deps", "--root", str(root), "--cycles", "--json"])
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert len(doc["results"]) == 1
    entry = doc["results"][0]
    # pre-round-33 keys, unchanged
    assert set(entry["files"]) == {"a.py", "b.py", "c.py"}
    assert entry["self_import"] is False
    # round-33 keys
    assert entry["internal_edges"] == 3
    assert entry["shortest_loop"] == ["a.py", "b.py", "c.py"]
    assert entry["two_file_loops"] == 0
    assert entry["edges"] == [
        ["a.py", "b.py"],
        ["b.py", "c.py"],
        ["c.py", "a.py"],
    ]


def test_deps_cycles_none_detected(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(ACYCLIC_REPO)
    code = cli.main(["deps", "--root", str(root), "--cycles"])
    assert code == 0
    assert "no circular imports detected" in capsys.readouterr().out


def test_deps_two_file_cycle(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(TWO_CYCLE_REPO)
    code = cli.main(["deps", "--root", str(root), "--cycles"])
    assert code == 0
    out = capsys.readouterr().out
    # A two-file cluster prints only the loop line: members and edges
    # would say the same thing three times.
    assert "cluster 1 (2 files, 2 import edges among them):" in out
    assert "shortest loop: x.py -> y.py -> x.py" in out
    assert "  x.py, y.py" not in out
    assert "edges:" not in out


def test_deps_export_mermaid(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(ACYCLIC_REPO)
    code = cli.main(["deps", "--root", str(root), "--export", "mermaid"])
    assert code == 0
    out = capsys.readouterr().out
    assert "flowchart LR" in out
    assert "a.py" in out
    assert "b.py" in out


def test_deps_export_dot(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(ACYCLIC_REPO)
    code = cli.main(["deps", "--root", str(root), "--export", "dot"])
    assert code == 0
    out = capsys.readouterr().out
    assert "digraph dekko" in out


def test_deps_export_to_file(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(ACYCLIC_REPO)
    out_file = root / "deps.dot"
    code = cli.main(
        [
            "deps",
            "--root",
            str(root),
            "--export",
            "dot",
            "--output",
            str(out_file),
        ]
    )
    assert code == 0
    assert out_file.exists()
    assert "digraph dekko" in out_file.read_text()


def test_deps_export_too_big_guard(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(ACYCLIC_REPO)
    code = cli.main(
        [
            "deps",
            "--root",
            str(root),
            "--export",
            "mermaid",
            "--max-nodes",
            "0",
        ]
    )
    assert code == deps.EXIT_TOO_BIG
    assert "raise --max-nodes" in capsys.readouterr().err


def test_deps_mutually_exclusive_flags(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(CYCLE_REPO)
    code = cli.main(
        ["deps", "--root", str(root), "--file", "a.py", "--cycles"]
    )
    assert code == deps.EXIT_ERROR
    assert "give one of FILE, --cycles, --export" in capsys.readouterr().err


def test_deps_positional_file_matches_flag_output(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(CYCLE_REPO)
    code = cli.main(["deps", "--root", str(root), "a.py"])
    assert code == 0
    positional_out = capsys.readouterr().out

    code = cli.main(["deps", "--root", str(root), "--file", "a.py"])
    assert code == 0
    flag_out = capsys.readouterr().out

    assert positional_out == flag_out


def test_deps_positional_and_flag_both_given_is_an_error(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(CYCLE_REPO)
    code = cli.main(["deps", "--root", str(root), "a.py", "--file", "b.py"])
    assert code == deps.EXIT_ERROR
    assert "give FILE or --file, not both" in capsys.readouterr().err


def test_deps_positional_file_with_cycles_is_mutually_exclusive(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(CYCLE_REPO)
    code = cli.main(["deps", "--root", str(root), "a.py", "--cycles"])
    assert code == deps.EXIT_ERROR
    assert "give one of FILE, --cycles, --export" in capsys.readouterr().err


def test_deps_no_target_still_defaults_to_summary(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(CYCLE_REPO)
    code = cli.main(["deps", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "most-depended-on files:" in out


def test_deps_no_tests_excludes_test_file_edges(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(TEST_FILE_IMPORT_REPO)
    code = cli.main(
        ["deps", "--root", str(root), "--file", "a.py", "--no-tests"]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "imported by (0):" in out


def test_deps_includes_test_file_edges_by_default(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(TEST_FILE_IMPORT_REPO)
    code = cli.main(["deps", "--root", str(root), "--file", "a.py"])
    assert code == 0
    out = capsys.readouterr().out
    assert "imported by (1):" in out
    assert "tests/test_a.py" in out


def test_deps_budget_caps_file_view_rows(
    make_mapped_repo: RepoFactory,
) -> None:
    files = {"a.py": "\n".join(f"import mod{i}\n" for i in range(30))}
    for i in range(30):
        files[f"mod{i}.py"] = "x = 1\n"
    root = make_mapped_repo(files)
    index = mapfile.load_map(root)
    assert index is not None
    code = deps.run(
        index,
        file="a.py",
        cycles=False,
        top=10,
        limit=5,
        budget=None,
        as_json=False,
    )
    assert code == 0


def test_deps_rust_crate_import_resolves_against_named_lib_root(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # round-19 zed finding: a crate whose Cargo.toml overrides
    # `[lib] path = "src/<name>.rs"` (216/222 of zed's own crates with
    # a [lib] path override use this shape) has no literal lib.rs/
    # main.rs anywhere -- before the _rust_crate_root fallback, every
    # crate::-prefixed import in such a crate resolved as external
    # rather than to the real in-repo file.
    root = make_mapped_repo(
        {
            "crates/editor/src/editor.rs": "pub struct Editor;\n",
            "crates/editor/src/code_context_menus.rs": (
                "use crate::editor::Editor;\n"
                "pub fn make() -> Editor { Editor }\n"
            ),
        }
    )
    code = cli.main(
        [
            "deps",
            "--root",
            str(root),
            "--file",
            "crates/editor/src/code_context_menus.rs",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "imports (1):" in out
    assert "crates/editor/src/editor.rs" in out
    assert "external (0):" in out


def test_deps_rust_item_resolves_at_named_crate_root_top_level(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # round-22 zed finding (5a): `crate::App`, re-exported at the top
    # level of a custom-named crate root (`[lib] path =
    # "src/gpui.rs"`), previously resolved as external -- the
    # "item defined at crate-root scope" fallback only ever tried
    # mod.rs/lib.rs/main.rs, with no way to know this crate's own
    # root file is actually named gpui.rs.
    root = make_mapped_repo(
        {
            "crates/gpui/src/gpui.rs": "pub struct App;\n",
            "crates/gpui/src/geometry.rs": (
                "use crate::App;\npub fn make() -> App { App }\n"
            ),
        }
    )
    code = cli.main(
        [
            "deps",
            "--root",
            str(root),
            "--file",
            "crates/gpui/src/geometry.rs",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "imports (1):" in out
    assert "crates/gpui/src/gpui.rs" in out
    assert "external (0):" in out


def test_deps_rust_cross_crate_import_resolves_to_sibling_crate(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # round-22 zed finding (5b): a Cargo-workspace sibling crate
    # referenced by its bare crate name (`use editor::Editor;` from a
    # different crate) previously always resolved as external,
    # unconditionally -- the dominant cross-crate shape in a large
    # multi-crate workspace.
    root = make_mapped_repo(
        {
            "crates/editor/src/lib.rs": "pub struct Editor;\n",
            "crates/workspace/src/pane.rs": (
                "use editor::Editor;\npub fn make() -> Editor { Editor }\n"
            ),
        }
    )
    code = cli.main(
        [
            "deps",
            "--root",
            str(root),
            "--file",
            "crates/workspace/src/pane.rs",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "imports (1):" in out
    assert "crates/editor/src/lib.rs" in out
    assert "external (0):" in out


def test_deps_compute_top_by_deps_in_ranking(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(
        {
            "hot.py": "x = 1\n",
            "a.py": "from .hot import x\n",
            "b.py": "from .hot import x\n",
            "c.py": "from .hot import x\n",
        }
    )
    index = mapfile.load_map(root)
    assert index is not None
    doc = deps.compute(index, top=5)
    assert doc["top_by_deps_in"][0] == {"path": "hot.py", "count": 3}


# --- round 31: runtime-import disclosure ----------------------------

# A file whose only real dependency is wired at runtime, so static
# import extraction correctly resolves zero edges for it. Mirrors
# tensorflow's LazyLoader-wired keras modules, where round 31 measured
# 6 of 9 real edges invisible behind a bare "imports (0)".
DYNAMIC_IMPORT_REPO = {
    "lazy.py": (
        "import importlib\n"
        "def load():\n"
        "    return importlib.import_module('.target', __package__)\n"
    ),
    "target.py": "def thing():\n    return 1\n",
    "plain.py": "def helper():\n    return 2\n",
}


def test_deps_file_view_discloses_dynamic_imports(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(DYNAMIC_IMPORT_REPO)
    code = cli.main(["deps", "--root", str(root), "--file", "lazy.py"])
    assert code == 0
    out = capsys.readouterr().out
    # The zero is still reported -- it is accurate for static imports.
    assert "imports (0):" in out
    # ...but no longer bare.
    assert "importlib.import_module()" in out
    assert "not proof of no dependencies" in out


def test_deps_file_view_dynamic_imports_json(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(DYNAMIC_IMPORT_REPO)
    code = cli.main(
        ["deps", "--root", str(root), "--file", "lazy.py", "--json"]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["imports"] == []
    assert doc["dynamic_imports"] == [
        {"construct": "importlib.import_module()", "occurrences": 1}
    ]
    assert "dynamic_import_note" in doc


def test_deps_file_view_no_dynamic_imports_stays_silent(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    """The disclosure is evidence-gated, not printed on every zero."""
    root = make_mapped_repo(DYNAMIC_IMPORT_REPO)
    code = cli.main(
        ["deps", "--root", str(root), "--file", "plain.py", "--json"]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["imports"] == []
    assert "dynamic_imports" not in doc
    assert "dynamic_import_note" not in doc


def test_deps_dynamic_note_wording_when_static_edges_exist(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    """A nonzero static count is framed as incomplete, not as a zero."""
    root = make_mapped_repo(
        {
            "mixed.py": (
                "import importlib\n"
                "from .target import thing\n"
                "def load():\n"
                "    thing()\n"
                "    return importlib.import_module('.other', __package__)\n"
            ),
            "target.py": "def thing():\n    return 1\n",
            "other.py": "def other():\n    return 3\n",
        }
    )
    code = cli.main(["deps", "--root", str(root), "--file", "mixed.py"])
    assert code == 0
    out = capsys.readouterr().out
    assert "imports (1):" in out
    assert "covers static imports only" in out
    assert "not proof of no dependencies" not in out


def test_deps_dynamic_scan_handles_unreadable_and_unknown_language(
    tmp_path: pytest.TempPathFactory,
) -> None:
    """The scan never raises -- it degrades to 'no disclosure'."""
    from pathlib import Path

    root = Path(str(tmp_path))
    assert deps._dynamic_import_constructs(root, "missing.py", "python") == []
    assert deps._dynamic_import_constructs(None, "x.py", "python") == []
    assert deps._dynamic_import_constructs(root, "x.cob", "cobol") == []
    assert deps._dynamic_import_constructs(root, "x.py", None) == []
