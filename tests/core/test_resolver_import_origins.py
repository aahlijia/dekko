"""A JS/TS import names a file, and that file answers first.

The import hint used to be a stem test: is some segment of the
specifier a candidate's file stem. Behind a tsconfig alias that matches
every same-named file in the repo, and behind an import alias it tests
the wrong name. These tests build real layouts on disk and run the
real extract + resolve pipeline.
"""

import json
from pathlib import Path

from dekko.core.model import CallGraph
from dekko.core.resolver import (
    _OriginImport,
    _imports_by_file,
    resolve,
    tsconfig_fingerprint,
)
from dekko.repo_ops import map_repository

HELPER = "export function helper(): number {\n  return 1;\n}\n"
TSCONFIG = {
    "compilerOptions": {"baseUrl": ".", "paths": {"@lib/*": ["lib/*"]}}
}


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _resolve(root: Path, with_root: bool = True) -> CallGraph:
    files, _ = map_repository(
        root, subpath=None, excludes=(), max_file_size=1_000_000
    )
    return resolve(files, root=root if with_root else None)


def _two_helpers(root: Path) -> None:
    _write(root, "tsconfig.json", json.dumps(TSCONFIG))
    _write(root, "lib/base.ts", HELPER)
    _write(root, "other/base.ts", HELPER)


def test_tsconfig_alias_import_picks_the_aliased_file(tmp_path: Path) -> None:
    _two_helpers(tmp_path)
    _write(
        tmp_path,
        "app.ts",
        'import { helper } from "@lib/base";\n'
        "export function run(): number {\n  return helper();\n}\n",
    )
    graph = _resolve(tmp_path)
    assert graph.calls_out["app.ts::run"] == ["lib/base.ts::helper"]
    assert graph.ambiguous == []


def test_without_a_root_an_alias_import_keeps_the_old_hints(
    tmp_path: Path,
) -> None:
    # No root, no alias table: the specifier resolves to no file, so
    # the stem test is all there is and two ``base.ts`` tie.
    _two_helpers(tmp_path)
    _write(
        tmp_path,
        "app.ts",
        'import { helper } from "@lib/base";\n'
        "export function run(): number {\n  return helper();\n}\n",
    )
    graph = _resolve(tmp_path, with_root=False)
    assert "app.ts::run" not in graph.calls_out
    assert [name for _, name, _ in graph.ambiguous] == ["helper"]


def test_import_alias_is_undone_before_a_namesake_wins(tmp_path: Path) -> None:
    # ``h`` is a real symbol elsewhere; the call means ``helper``.
    _write(tmp_path, "lib/base.ts", HELPER)
    _write(
        tmp_path, "misc.ts", "export function h(): number {\n  return 2;\n}\n"
    )
    _write(
        tmp_path,
        "app.ts",
        'import { helper as h } from "./lib/base";\n'
        "export function run(): number {\n  return h();\n}\n",
    )
    graph = _resolve(tmp_path)
    assert graph.calls_out["app.ts::run"] == ["lib/base.ts::helper"]


def test_origin_beats_a_wrong_stem_match(tmp_path: Path) -> None:
    # ``widgets/Panel.ts`` is the only stem match (on the appended
    # name), and it is not the file the alias names.
    config = {
        "compilerOptions": {
            "baseUrl": ".",
            "paths": {"@views": ["lib/screens/index.ts"]},
        }
    }
    _write(tmp_path, "tsconfig.json", json.dumps(config))
    panel = "export function Panel(): number {\n  return 1;\n}\n"
    _write(tmp_path, "lib/screens/index.ts", panel)
    _write(tmp_path, "widgets/Panel.ts", panel)
    _write(
        tmp_path,
        "app.ts",
        'import { Panel } from "@views";\n'
        "export function run(): number {\n  return Panel();\n}\n",
    )
    graph = _resolve(tmp_path)
    assert graph.calls_out["app.ts::run"] == ["lib/screens/index.ts::Panel"]


def test_workspace_entry_import_picks_the_entry_files_symbol(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "package.json",
        json.dumps({"name": "acme", "workspaces": ["packages/*"]}),
    )
    _write(
        tmp_path,
        "packages/core/package.json",
        json.dumps({"name": "@acme/core", "main": "src/index.ts"}),
    )
    _write(tmp_path, "packages/core/src/index.ts", HELPER)
    _write(tmp_path, "packages/core/src/internal/helper.ts", HELPER)
    _write(
        tmp_path,
        "packages/app/package.json",
        json.dumps({"name": "@acme/app"}),
    )
    _write(
        tmp_path,
        "packages/app/src/main.ts",
        'import { helper } from "@acme/core";\n'
        "export function run(): number {\n  return helper();\n}\n",
    )
    graph = _resolve(tmp_path)
    assert graph.calls_out["packages/app/src/main.ts::run"] == [
        "packages/core/src/index.ts::helper"
    ]


def test_alias_import_settles_a_reference(tmp_path: Path) -> None:
    _two_helpers(tmp_path)
    _write(
        tmp_path,
        "app.ts",
        'import { helper } from "@lib/base";\n'
        "export const table = { run: helper };\n",
    )
    graph = _resolve(tmp_path)
    assert [e.callee for e in graph.referenced] == ["lib/base.ts::helper"]


def test_alias_import_settles_a_heritage_clause(tmp_path: Path) -> None:
    _write(tmp_path, "tsconfig.json", json.dumps(TSCONFIG))
    body = "export class Base {\n  ping(): void {}\n}\n"
    _write(tmp_path, "lib/base.ts", body)
    _write(tmp_path, "other/base.ts", body)
    _write(
        tmp_path,
        "app.ts",
        'import { Base } from "@lib/base";\n'
        "export class App extends Base {}\n",
    )
    graph = _resolve(tmp_path)
    assert graph.heritage_out["app.ts::App"] == ["lib/base.ts::Base"]


def test_a_type_and_a_value_under_one_name_give_no_verdict(
    tmp_path: Path,
) -> None:
    both = (
        "export interface Shape {\n  area(): number;\n}\n"
        "export function Shape(): number {\n  return 1;\n}\n"
    )
    _write(tmp_path, "tsconfig.json", json.dumps(TSCONFIG))
    _write(tmp_path, "lib/base.ts", both)
    _write(tmp_path, "other/base.ts", both)
    _write(
        tmp_path,
        "app.ts",
        'import { Shape } from "@lib/base";\n'
        "export function run(): number {\n  return Shape();\n}\n",
    )
    graph = _resolve(tmp_path)
    assert "app.ts::run" not in graph.calls_out


def test_a_python_import_never_gets_origins(tmp_path: Path) -> None:
    _write(tmp_path, "lib/base.py", "def helper():\n    return 1\n")
    _write(
        tmp_path,
        "app.py",
        "from lib.base import helper\n\n\ndef run():\n    return helper()\n",
    )
    files, _ = map_repository(
        tmp_path, subpath=None, excludes=(), max_file_size=1_000_000
    )
    graph = resolve(files, root=tmp_path)
    assert graph.calls_out["app.py::run"] == ["lib/base.py::helper"]
    table = _imports_by_file(files)
    assert not any(
        isinstance(imp, _OriginImport)
        for bindings in table.values()
        for imp in bindings.values()
    )


def test_tsconfig_fingerprint_tracks_the_alias_table(tmp_path: Path) -> None:
    assert tsconfig_fingerprint(tmp_path) == ""
    _write(tmp_path, "tsconfig.json", json.dumps(TSCONFIG))
    before = tsconfig_fingerprint(tmp_path)
    assert before
    moved = {"compilerOptions": {"baseUrl": ".", "paths": {"@lib/*": ["o/*"]}}}
    _write(tmp_path, "tsconfig.json", json.dumps(moved))
    assert tsconfig_fingerprint(tmp_path) != before
