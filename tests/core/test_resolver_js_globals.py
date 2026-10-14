"""A JS/TS call on a literal or a standard global is external.

``Math.max(..)``, ``JSON.parse(..)``, ``Promise.all(..)``,
``'a'.split('b')`` and ``/re/.exec(s)`` call the language's own
objects, so the repo's one method or function of that name is never the
target, not even one defined in the calling file. A file that binds the
name itself (an import, a parameter, a local) or a repo that declares a
type of that name (a ``declare global`` augmentation) keeps today's
resolution.
"""

from pathlib import Path

from dekko.core.model import CallGraph
from dekko.core.resolver import resolve
from dekko.repo_ops import map_repository


def _graph(root: Path, sources: dict[str, str]) -> CallGraph:
    for rel, text in sources.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    files, _ = map_repository(
        root,
        subpath=None,
        excludes=(),
        max_file_size=1_000_000,
    )
    return resolve(files, root=root)


def _callees(graph: CallGraph, caller: str) -> set[str]:
    return {e.callee for e in graph.edges if e.caller == caller}


_CACHE = (
    "export class Cache {\n"
    "  max(): number { return 1 }\n"
    "  parse(s: string): number { return 1 }\n"
    "  all(): number { return 1 }\n"
    "  exec(s: string): number { return 1 }\n"
    "  split(s: string): number { return 1 }\n"
    "  keys(): number { return 1 }\n"
    "  mark(s: string): number { return 1 }\n"
    "  reload(): number { return 1 }\n"
    "}\n"
)


def test_a_standard_global_receiver_is_external(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "src/cache.ts": _CACHE,
            "src/use.ts": (
                "export async function run(): Promise<void> {\n"
                "  Math.max(1, 2)\n"
                "  JSON.parse('1')\n"
                "  await Promise.all([])\n"
                "  Object.keys({}).forEach((k) => k)\n"
                "  performance.mark('x')\n"
                "  location.reload()\n"
                "}\n"
            ),
        },
    )
    assert _callees(g, "src/use.ts::run") == set()


def test_a_literal_receiver_is_external(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "src/cache.ts": _CACHE,
            "src/use.ts": (
                "export function run(s: string): void {\n"
                "  'a'.split('b');\n"
                "  `x${s}`.split('');\n"
                "  /a/.exec(s);\n"
                "}\n"
            ),
        },
    )
    assert _callees(g, "src/use.ts::run") == set()


def test_a_global_beats_a_same_file_function(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "src/clock.ts": (
                "export function now(): number { return 1 }\n"
                "export function warn(m: string): void {}\n"
                "export function tick(): number {\n"
                "  console.warn('x')\n"
                "  return Date.now()\n"
                "}\n"
            ),
        },
    )
    assert _callees(g, "src/clock.ts::tick") == set()


def test_an_import_named_like_a_global_keeps_its_edge(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            "src/mathx.ts": "export function max(): number { return 1 }\n",
            "src/use.ts": (
                "import * as Math from './mathx'\n"
                "export function run(): number { return Math.max() }\n"
            ),
        },
    )
    assert _callees(g, "src/use.ts::run") == {"src/mathx.ts::max"}


def test_a_parameter_or_local_named_like_a_global_keeps_its_edge(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            "src/cache.ts": _CACHE,
            "src/use.ts": (
                "import { Cache } from './cache'\n"
                "export function viaParam(JSON: Cache): number {\n"
                "  return JSON.parse('1')\n"
                "}\n"
                "export function viaLocal(): number {\n"
                "  const Intl = new Cache()\n"
                "  return Intl.max()\n"
                "}\n"
            ),
        },
    )
    assert _callees(g, "src/use.ts::viaParam") == {"src/cache.ts::Cache.parse"}
    assert _callees(g, "src/use.ts::viaLocal") == {
        "src/cache.ts::Cache",
        "src/cache.ts::Cache.max",
    }


def test_a_repo_augmented_global_type_stays_reachable(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            "src/ext.ts": (
                "export class String {\n  zzShout(): string { return '' }\n}\n"
            ),
            "src/use.ts": (
                "export function run(): string { return 'a'.zzShout() }\n"
            ),
        },
    )
    assert _callees(g, "src/use.ts::run") == {"src/ext.ts::String.zzShout"}


def test_other_languages_keep_their_globals(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "src/main/java/p/Math.java": (
                "package p;\nclass Math { static int zzMax() { return 1; } }\n"
            ),
            "src/main/java/p/Use.java": (
                "package p;\n"
                "class Use { int run() { return Math.zzMax(); } }\n"
            ),
        },
    )
    assert _callees(g, "src/main/java/p/Use.java::Use.run") == {
        "src/main/java/p/Math.java::Math.zzMax"
    }
