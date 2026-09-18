"""JS/TS workspace-package import resolution (round 31 cline.md §4.1 A).

``import { X } from "@scope/pkg"`` names a package, not a file, so the
stem-based "is this import in-repo" test filed every such call and
heritage clause under ``external``. These tests build real monorepo
layouts on disk and run the real extract + resolve pipeline.
"""

import json
from pathlib import Path

from dekko.core.model import CallGraph, FileMap, Import
from dekko.core.resolver import (
    _imports_by_file,
    _WorkspaceImport,
    load_workspace_packages,
    resolve,
    workspace_fingerprint,
)
from dekko.repo_ops import map_repository

HANDLER = "export interface ApiHandler {\n  send(): void;\n}\n"
IMPL = (
    'import type { ApiHandler } from "@acme/llms";\n'
    "export class VsHandler implements ApiHandler {\n"
    "  send(): void {}\n"
    "}\n"
)


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _pkg(root: Path, rel_dir: str, **fields: object) -> None:
    _write(root, f"{rel_dir}/package.json".lstrip("/"), json.dumps(fields))


def _resolve(root: Path, with_root: bool = True) -> CallGraph:
    files, _ = map_repository(
        root, subpath=None, excludes=(), max_file_size=1_000_000
    )
    return resolve(files, root=root if with_root else None)


def _monorepo(root: Path) -> None:
    _pkg(root, "", name="acme", workspaces=["packages/*", "apps/*"])
    _pkg(root, "packages/llms", name="@acme/llms")
    _pkg(root, "apps/vscode", name="acme-vscode")
    _write(root, "packages/llms/src/providers/handler.ts", HANDLER)
    _write(root, "apps/vscode/src/vs-handler.ts", IMPL)


def test_workspace_alias_heritage_edge_resolves(tmp_path: Path) -> None:
    _monorepo(tmp_path)
    graph = _resolve(tmp_path)
    assert graph.heritage_in[
        "packages/llms/src/providers/handler.ts::ApiHandler"
    ] == ["apps/vscode/src/vs-handler.ts::VsHandler"]
    assert graph.heritage_external == []


def test_without_root_behavior_is_unchanged(tmp_path: Path) -> None:
    # No filesystem root -> no workspace table -> the old (external)
    # classification, so every root-less caller is byte-identical.
    _monorepo(tmp_path)
    graph = _resolve(tmp_path, with_root=False)
    assert graph.heritage == []
    assert [e.callee for e in graph.heritage_external] == ["ApiHandler"]


def test_workspace_call_narrows_collision_to_imported_package(
    tmp_path: Path,
) -> None:
    _pkg(tmp_path, "", name="acme", workspaces=["packages/*"])
    _pkg(tmp_path, "packages/llms", name="@acme/llms")
    _pkg(tmp_path, "packages/core", name="@acme/core")
    _write(
        tmp_path,
        "packages/llms/src/catalog/access.ts",
        "export function getModels(id: string): string[] {\n"
        "  return [id];\n}\n",
    )
    _write(
        tmp_path,
        "packages/core/src/registry.ts",
        "export function getModels(id: string): string[] {\n  return [];\n}\n",
    )
    _write(
        tmp_path,
        "packages/core/src/use.ts",
        'import { getModels } from "@acme/llms";\n'
        "export function run(): string[] {\n"
        '  return getModels("x");\n}\n',
    )
    graph = _resolve(tmp_path)
    assert graph.calls_out["packages/core/src/use.ts::run"] == [
        "packages/llms/src/catalog/access.ts::getModels"
    ]


def test_workspace_import_beats_coincidental_stem_match(
    tmp_path: Path,
) -> None:
    # The pre-fix ladder "resolved" this to apps/vscode's AccountService
    # purely because that file's *stem* equals the imported name --
    # cline had 10 such wrong call edges (ClineAccountService).
    _pkg(tmp_path, "", name="acme", workspaces=["packages/*", "apps/*"])
    _pkg(tmp_path, "packages/core", name="@acme/core")
    _pkg(tmp_path, "apps/vscode", name="acme-vscode")
    _pkg(tmp_path, "apps/cli", name="acme-cli")
    body = "export class AccountService {\n  ping(): void {}\n}\n"
    _write(tmp_path, "packages/core/src/account/account-service.ts", body)
    _write(tmp_path, "apps/vscode/src/AccountService.ts", body)
    _write(
        tmp_path,
        "apps/cli/src/main.ts",
        'import { AccountService } from "@acme/core";\n'
        "export function make(): AccountService {\n"
        "  return new AccountService();\n}\n",
    )
    graph = _resolve(tmp_path)
    callees = graph.calls_out["apps/cli/src/main.ts::make"]
    assert callees
    assert all(c.startswith("packages/core/") for c in callees)


def test_cross_package_reexport_is_not_narrowed_away(tmp_path: Path) -> None:
    # @acme/core re-exports ApiHandler from @acme/llms: zero candidates
    # live under core, which is no evidence -- fall through to the
    # rest of the ladder instead of dropping the edge.
    _monorepo(tmp_path)
    _pkg(tmp_path, "packages/core", name="@acme/core")
    _write(
        tmp_path,
        "packages/core/src/index.ts",
        'export type { ApiHandler } from "@acme/llms";\n',
    )
    _write(
        tmp_path,
        "apps/vscode/src/other.ts",
        'import type { ApiHandler } from "@acme/core";\n'
        "export class Other implements ApiHandler {\n  send(): void {}\n}\n",
    )
    graph = _resolve(tmp_path)
    assert (
        "apps/vscode/src/other.ts::Other"
        in graph.heritage_in[
            "packages/llms/src/providers/handler.ts::ApiHandler"
        ]
    )


def test_named_package_outside_workspace_globs_is_ignored(
    tmp_path: Path,
) -> None:
    # cline ships a stub package literally named "vscode" that is no
    # workspace member; it must not capture real `from "vscode"` imports.
    _pkg(tmp_path, "", name="acme", workspaces=["apps/*"])
    _pkg(tmp_path, "apps/ext", name="acme-ext")
    _pkg(tmp_path, "apps/ext/standalone/runtime-files/vscode", name="vscode")
    assert load_workspace_packages(tmp_path) == {"acme-ext": "apps/ext"}


def test_no_workspaces_declared_yields_empty_table(tmp_path: Path) -> None:
    _pkg(tmp_path, "", name="solo")
    _pkg(tmp_path, "packages/a", name="@solo/a")
    assert load_workspace_packages(tmp_path) == {}
    assert workspace_fingerprint(tmp_path) == ""


def test_workspace_glob_forms(tmp_path: Path) -> None:
    _pkg(
        tmp_path,
        "",
        name="acme",
        workspaces={"packages": ["packages/**", "!packages/legacy/*"]},
    )
    _pkg(tmp_path, "packages/a", name="a")
    _pkg(tmp_path, "packages/group/b", name="b")
    _pkg(tmp_path, "packages/legacy/c", name="c")
    assert load_workspace_packages(tmp_path) == {
        "a": "packages/a",
        "b": "packages/group/b",
    }


def test_pnpm_workspace_yaml(tmp_path: Path) -> None:
    _pkg(tmp_path, "", name="acme")
    _write(
        tmp_path,
        "pnpm-workspace.yaml",
        "# monorepo\npackages:\n  - 'packages/*'  # libs\n"
        '  - "!packages/skip"\ncatalog:\n  - not-a-glob\n',
    )
    _pkg(tmp_path, "packages/a", name="@acme/a")
    _pkg(tmp_path, "packages/skip", name="@acme/skip")
    _pkg(tmp_path, "not-a-glob", name="decoy")
    assert load_workspace_packages(tmp_path) == {"@acme/a": "packages/a"}


def test_duplicate_member_name_is_dropped(tmp_path: Path) -> None:
    _pkg(tmp_path, "", name="acme", workspaces=["packages/*"])
    _pkg(tmp_path, "packages/a", name="@acme/dup")
    _pkg(tmp_path, "packages/b", name="@acme/dup")
    assert load_workspace_packages(tmp_path) == {}


def test_node_modules_package_json_is_excluded(tmp_path: Path) -> None:
    _pkg(tmp_path, "", name="acme", workspaces=["**"])
    _pkg(tmp_path, "packages/a", name="@acme/a")
    _pkg(tmp_path, "node_modules/left-pad", name="left-pad")
    assert load_workspace_packages(tmp_path) == {"@acme/a": "packages/a"}


def test_workspace_fingerprint_tracks_the_table(tmp_path: Path) -> None:
    _monorepo(tmp_path)
    before = workspace_fingerprint(tmp_path)
    assert before
    assert workspace_fingerprint(tmp_path) == before
    _pkg(tmp_path, "packages/llms", name="@acme/renamed")
    assert workspace_fingerprint(tmp_path) != before


def test_only_js_ts_bindings_are_tagged() -> None:
    # A Python `from shared import helper` must never be read as the
    # JS workspace package that happens to be named "shared".
    pkgs = {"shared": "packages/shared"}
    files = [
        FileMap(
            path="tools/run.py",
            language="python",
            imports=[Import("tools/run.py", "helper", "shared.helper")],
        ),
        FileMap(
            path="apps/web/main.ts",
            language="typescript",
            imports=[
                Import("apps/web/main.ts", "helper", "shared/helper"),
                Import("apps/web/main.ts", "local", "./shared/local"),
            ],
        ),
    ]
    table = _imports_by_file(files, pkgs)
    assert not isinstance(table["tools/run.py"]["helper"], _WorkspaceImport)
    assert not isinstance(table["apps/web/main.ts"]["local"], _WorkspaceImport)
    tagged = table["apps/web/main.ts"]["helper"]
    assert isinstance(tagged, _WorkspaceImport)
    assert tagged.package_dir == "packages/shared"
