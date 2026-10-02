"""Calls, references and the module graph through JS/TS barrel files.

A barrel (``export { X } from "./x"``, ``export * from "./x"``) names
another file. These tests build real layouts on disk and run the real
extract + resolve pipeline.
"""

import json
from pathlib import Path

from dekko.core.model import CallGraph
from dekko.core.resolver import resolve
from dekko.repo_ops import map_repository

BASE = (
    "export class Base {\n"
    "  constructor(a: number, b?: string) {}\n"
    "}\n"
    "export function helper(): number {\n  return 1;\n}\n"
)
THEMED = "export default function ThemedText(): number {\n  return 1;\n}\n"
OTHER = "export function helper(): number {\n  return 2;\n}\n"
INDEX = (
    'export * from "./base";\n'
    'export { Base as Renamed } from "./base";\n'
    'export { default as Text } from "./themed";\n'
    'export * as shapes from "./base";\n'
)
TEXT = "export function Text(): number {\n  return 3;\n}\n"


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _resolve(root: Path) -> CallGraph:
    files, _ = map_repository(
        root, subpath=None, excludes=(), max_file_size=1_000_000
    )
    return resolve(files, root=root)


def _fixture(root: Path, app: str) -> CallGraph:
    _write(root, "lib/base.ts", BASE)
    _write(root, "lib/themed.ts", THEMED)
    _write(root, "other/base.ts", OTHER)
    _write(root, "lib/index.ts", INDEX)
    _write(root, "text.ts", TEXT)
    _write(root, "app.ts", app)
    return _resolve(root)


def _externals(graph: CallGraph, caller: str) -> set[str]:
    return {e.callee for e in graph.external if e.caller == caller}


def test_star_reexport_settles_a_name_tie(tmp_path: Path) -> None:
    graph = _fixture(
        tmp_path,
        'import { helper } from "./lib/index";\n'
        "export function run(): number {\n  return helper();\n}\n",
    )
    assert graph.calls_out["app.ts::run"] == ["lib/base.ts::helper"]
    assert graph.ambiguous == []


def test_renamed_reexport_reaches_the_class_and_its_constructor(
    tmp_path: Path,
) -> None:
    graph = _fixture(
        tmp_path,
        'import { Renamed } from "./lib/index";\n'
        "export function run(): object {\n  return new Renamed(1);\n}\n",
    )
    assert graph.calls_out["app.ts::run"] == [
        "lib/base.ts::Base",
        "lib/base.ts::Base.constructor",
    ]


def test_import_through_a_barrel_proves_the_class_like_a_direct_one(
    tmp_path: Path,
) -> None:
    graph = _fixture(
        tmp_path,
        'import { Base } from "./lib/index";\n'
        "export function run(): object {\n"
        '  return new Base(1, "x", 3);\n}\n',
    )
    assert "lib/base.ts::Base" in graph.calls_out["app.ts::run"]


def test_default_reexport_beats_a_file_named_like_the_import(
    tmp_path: Path,
) -> None:
    graph = _fixture(
        tmp_path,
        'import { Text } from "./lib/index";\n'
        "export function run(): number {\n  return Text();\n}\n",
    )
    assert graph.calls_out["app.ts::run"] == ["lib/themed.ts::ThemedText"]


def test_default_reexport_settles_a_reference(tmp_path: Path) -> None:
    graph = _fixture(
        tmp_path,
        'import { Text } from "./lib/index";\n'
        "export const table = { render: Text };\n",
    )
    assert [e.callee for e in graph.referenced] == [
        "lib/themed.ts::ThemedText"
    ]


def test_namespace_import_of_a_barrel_follows_the_member(
    tmp_path: Path,
) -> None:
    graph = _fixture(
        tmp_path,
        'import * as lib from "./lib/index";\n'
        "export function run(): number {\n  return lib.helper();\n}\n",
    )
    assert graph.calls_out["app.ts::run"] == ["lib/base.ts::helper"]


def test_namespace_reexport_follows_the_member(tmp_path: Path) -> None:
    graph = _fixture(
        tmp_path,
        'import { shapes } from "./lib/index";\n'
        "export function run(): number {\n  return shapes.helper();\n}\n",
    )
    assert graph.calls_out["app.ts::run"] == ["lib/base.ts::helper"]


def test_two_hop_barrel(tmp_path: Path) -> None:
    _write(tmp_path, "top.ts", 'export { helper } from "./lib/index";\n')
    graph = _fixture(
        tmp_path,
        'import { helper } from "./top";\n'
        "export function run(): number {\n  return helper();\n}\n",
    )
    assert graph.calls_out["app.ts::run"] == ["lib/base.ts::helper"]


def test_default_through_two_barrels(tmp_path: Path) -> None:
    _write(
        tmp_path, "top.ts", 'export { Text as Label } from "./lib/index";\n'
    )
    graph = _fixture(
        tmp_path,
        'import { Label } from "./top";\n'
        "export function run(): number {\n  return Label();\n}\n",
    )
    assert graph.calls_out["app.ts::run"] == ["lib/themed.ts::ThemedText"]


def test_star_and_named_for_one_name_reach_one_symbol(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "top.ts",
        'export * from "./lib/base";\nexport { helper } from "./lib/base";\n',
    )
    graph = _fixture(
        tmp_path,
        'import { helper } from "./top";\n'
        "export function run(): number {\n  return helper();\n}\n",
    )
    assert graph.calls_out["app.ts::run"] == ["lib/base.ts::helper"]


def test_import_then_export_is_a_hop(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "top.ts",
        'import { helper } from "./lib/base";\nexport { helper };\n',
    )
    graph = _fixture(
        tmp_path,
        'import { helper } from "./top";\n'
        "export function run(): number {\n  return helper();\n}\n",
    )
    assert graph.calls_out["app.ts::run"] == ["lib/base.ts::helper"]


def test_source_less_rename_is_followed(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "top.ts",
        'import { helper } from "./lib/base";\nexport { helper as assist };\n',
    )
    graph = _fixture(
        tmp_path,
        'import { assist } from "./top";\n'
        "export function run(): number {\n  return assist();\n}\n",
    )
    assert graph.calls_out["app.ts::run"] == ["lib/base.ts::helper"]


def test_barrel_cycle_terminates_and_stays_external(tmp_path: Path) -> None:
    _write(tmp_path, "a.ts", 'export * from "./b";\n')
    _write(tmp_path, "b.ts", 'export * from "./a";\n')
    _write(
        tmp_path,
        "app.ts",
        'import { ghost } from "./a";\n'
        "export function run(): number {\n  return ghost();\n}\n",
    )
    graph = _resolve(tmp_path)
    assert "app.ts::run" not in graph.calls_out
    assert _externals(graph, "app.ts::run") == {"ghost"}


def test_a_type_and_a_value_behind_a_barrel_give_no_verdict(
    tmp_path: Path,
) -> None:
    both = (
        "export interface Shape {\n  area(): number;\n}\n"
        "export function Shape(): number {\n  return 1;\n}\n"
    )
    _write(tmp_path, "lib/shape.ts", both)
    _write(tmp_path, "old/shape.ts", both)
    _write(tmp_path, "lib/index.ts", 'export * from "./shape";\n')
    _write(
        tmp_path,
        "app.ts",
        'import { Shape } from "./lib/index";\n'
        "export function run(): number {\n  return Shape();\n}\n",
    )
    graph = _resolve(tmp_path)
    assert "app.ts::run" not in graph.calls_out


def test_barrel_through_a_tsconfig_alias_uses_the_barrels_own_scope(
    tmp_path: Path,
) -> None:
    config = {
        "compilerOptions": {"baseUrl": ".", "paths": {"@impl/*": ["impl/*"]}}
    }
    _write(tmp_path, "pkg/tsconfig.json", json.dumps(config))
    _write(tmp_path, "pkg/impl/base.ts", OTHER)
    _write(tmp_path, "elsewhere/base.ts", OTHER)
    _write(tmp_path, "pkg/index.ts", 'export { helper } from "@impl/base";\n')
    _write(
        tmp_path,
        "app.ts",
        'import { helper } from "./pkg/index";\n'
        "export function run(): number {\n  return helper();\n}\n",
    )
    graph = _resolve(tmp_path)
    assert graph.calls_out["app.ts::run"] == ["pkg/impl/base.ts::helper"]


def test_reexports_are_module_graph_edges(tmp_path: Path) -> None:
    graph = _fixture(tmp_path, "export const x = 1;\n")
    edges = {(e.importer, e.imported): e.names for e in graph.modules.edges}
    assert edges[("lib/index.ts", "lib/base.ts")] == ["*", "Renamed", "shapes"]
    assert edges[("lib/index.ts", "lib/themed.ts")] == ["Text"]


def test_an_external_reexport_is_an_external_module(tmp_path: Path) -> None:
    _write(tmp_path, "index.ts", 'export * from "execa";\n')
    graph = _resolve(tmp_path)
    assert graph.modules.external == {"index.ts": ["execa"]}
