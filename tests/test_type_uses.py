"""``type_uses`` through the map: render, load, in-memory index, cache.

Cross-cutting on purpose: one record type crosses ``render_json``,
``mapfile.load_map``/``index_from_maps``/``without_tests`` and the
per-file cache, and the property that matters is that every path
hands the read side the same list.
"""

import json
from pathlib import Path

from dekko import repo_ops
from dekko.core import resolver
from dekko.core.model import CallGraph, FileMap, TypeUse
from dekko.integrations import cli
from dekko.render import mapfile
from dekko.render.render_json import render_json

from conftest import RepoFactory

TS = {
    "app.ts": (
        "export interface Ctx { id: string }\n"
        "export function make() {\n"
        "  return (ctx: Ctx) => ctx.id;\n"
        "}\n"
    ),
    "app.test.ts": "describe('x', () => { items.map((c: Ctx) => c) });\n",
    "other.ts": "export const n = 1;\n",
}


def _use(path: str, owner: str | None) -> TypeUse:
    return TypeUse(
        owner_id=owner,
        path=path,
        line=3,
        site="arrow_function",
        usage="param",
        param_name="ctx",
        type="Ctx",
    )


def test_render_then_load_round_trips_every_field(tmp_path: Path) -> None:
    files = [
        FileMap(
            path="a.ts",
            language="typescript",
            type_uses=[_use("a.ts", "a.ts::make"), _use("a.ts", None)],
        )
    ]
    (tmp_path / ".dekko").mkdir()
    (tmp_path / ".dekko" / "map.json").write_bytes(
        render_json(files, CallGraph(), "t")
    )
    doc = json.loads((tmp_path / ".dekko" / "map.json").read_text())
    assert doc["type_uses"][0] == {
        "owner_id": "a.ts::make",
        "path": "a.ts",
        "line": 3,
        "site": "arrow_function",
        "usage": "param",
        "param_name": "ctx",
        "type": "Ctx",
    }
    index = mapfile.load_map(tmp_path)
    assert index is not None
    assert index.type_uses == files[0].type_uses


def test_index_from_maps_matches_load_map(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(TS)
    loaded = mapfile.load_map(root)
    assert loaded is not None
    files, _ = repo_ops.map_repository(
        root, subpath=None, excludes=(), max_file_size=1_000_000
    )
    in_memory = mapfile.index_from_maps(files, resolver.resolve(files), "t")
    assert sorted(map(str, in_memory.type_uses)) == sorted(
        map(str, loaded.type_uses)
    )
    assert len(loaded.type_uses) == 2


def test_without_tests_filters_sites_by_path(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(TS)
    index = mapfile.load_map(root)
    assert index is not None
    assert {u.path for u in index.type_uses} == {"app.ts", "app.test.ts"}
    assert {u.path for u in index.without_tests().type_uses} == {"app.ts"}


def test_document_without_the_section_loads_empty(tmp_path: Path) -> None:
    (tmp_path / ".dekko").mkdir()
    doc = json.loads(render_json([], CallGraph(), "t"))
    del doc["type_uses"]
    (tmp_path / ".dekko" / "map.json").write_text(json.dumps(doc))
    index = mapfile.load_map(tmp_path)
    assert index is not None
    assert index.type_uses == []


def test_cache_hit_keeps_sites_on_incremental_remap(
    make_mapped_repo: RepoFactory,
) -> None:
    # Touch an unrelated file so ``app.ts`` is served from the
    # per-file cache on the second map; its sites must survive.
    root = make_mapped_repo(TS)
    before = mapfile.load_map(root)
    assert before is not None
    (root / "other.ts").write_text("export const n = 2;\n")
    assert cli.main(["map", str(root)]) == 0
    after = mapfile.load_map(root)
    assert after is not None
    assert [str(u) for u in after.type_uses] == [
        str(u) for u in before.type_uses
    ]
