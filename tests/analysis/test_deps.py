"""The deps command: summary/--file/--cycles/--export, budget-capping,
CLI wiring, and its deliberate CLI-only (no MCP tool) surface."""

import json

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
    assert "1 cycles (3 files)" in out
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


def test_deps_cycles_text(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(CYCLE_REPO)
    code = cli.main(["deps", "--root", str(root), "--cycles"])
    assert code == 0
    out = capsys.readouterr().out
    assert "cycle 1 (3 files):" in out
    assert "a.py -> b.py -> c.py -> a.py" in out


def test_deps_cycles_json(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(CYCLE_REPO)
    code = cli.main(["deps", "--root", str(root), "--cycles", "--json"])
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert len(doc["results"]) == 1
    assert set(doc["results"][0]["files"]) == {"a.py", "b.py", "c.py"}
    assert doc["results"][0]["self_import"] is False


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
    assert "cycle 1 (2 files):" in out
    assert "x.py -> y.py -> x.py" in out


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
