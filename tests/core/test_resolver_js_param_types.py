"""A JS/TS parameter typed outside the repo, and a bare call's import.

``signal.throwIfAborted()`` on ``signal: AbortSignal`` can only call
the DOM's method, yet nothing read a plain parameter's type, so the
ladder handed the call to the repo's one ``throwIfAborted``. A
parameter whose declared type is outside the repo now sends its calls
external; one typed in the repo (or untyped, or generic) is left to
the rungs that already read it.

``getSeg().segment(x)`` where an imported ``getSeg()`` returns
``Intl.Segmenter`` is read the same way: the bare call at the head of
the chain follows its import to the function and reads that function's
declared return type in its own file.
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


_MGR = (
    "export class Mgr {\n"
    "  zzAbort(): void {}\n"
    "  zzDebug(s: string): void {}\n"
    "  zzText(): string { return '' }\n"
    "  zzEntries(): number { return 1 }\n"
    "  zzSegment(s: string): number { return 1 }\n"
    "}\n"
)


def test_an_outside_typed_parameter_sends_its_calls_external(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            "src/mgr.ts": _MGR,
            "src/use.ts": (
                "import type { ExtLogger } from 'some-external-pkg'\n"
                "export function run(\n"
                "  signal: AbortSignal,\n"
                "  logger: ExtLogger,\n"
                "  r: Response,\n"
                "  m: Map<string, number>,\n"
                "): void {\n"
                "  signal.zzAbort()\n"
                "  logger.zzDebug('a')\n"
                "  r.zzText()\n"
                "  m.zzEntries()\n"
                "}\n"
            ),
        },
    )
    assert _callees(g, "src/use.ts::run") == set()


def test_an_in_repo_or_unread_parameter_type_keeps_its_edge(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            "src/mgr.ts": _MGR,
            "src/use.ts": (
                "import { Mgr } from './mgr'\n"
                "export function a(p: Mgr): void { p.zzAbort() }\n"
                "export function b(p: Partial<Mgr>): void { p.zzAbort() }\n"
                "export function c(p: Mgr | undefined): void { p.zzAbort() }\n"
                "export function d(p: any): void { p.zzAbort() }\n"
                "export function e<T>(p: T): void { p.zzAbort() }\n"
                "export function f(p): void { p.zzAbort() }\n"
            ),
        },
    )
    for fn in ("a", "b", "c", "d", "e", "f"):
        assert _callees(g, f"src/use.ts::{fn}") == {"src/mgr.ts::Mgr.zzAbort"}


def test_an_imported_call_is_read_by_its_return_type(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "src/mgr.ts": _MGR,
            "src/seg.ts": (
                "import { Mgr } from './mgr'\n"
                "export function getSeg(): Intl.Segmenter {\n"
                "  return new Intl.Segmenter()\n"
                "}\n"
                "export function getMgr(): Mgr { return new Mgr() }\n"
            ),
            "src/use.ts": (
                "import { getSeg, getMgr } from './seg'\n"
                "export function run(x: string): void {\n"
                "  getSeg().zzSegment(x)\n"
                "  getMgr().zzText()\n"
                "}\n"
            ),
        },
    )
    assert _callees(g, "src/use.ts::run") == {
        "src/seg.ts::getSeg",
        "src/seg.ts::getMgr",
        "src/mgr.ts::Mgr.zzText",
    }


def test_the_return_type_is_read_in_the_callee_file(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "src/mgr.ts": _MGR,
            "src/seg.ts": (
                "import { Mgr } from './mgr'\n"
                "export function getMgr(): Mgr { return new Mgr() }\n"
            ),
            "src/use.ts": (
                "import { getMgr } from './seg'\n"
                "class Mgr { zzText(): string { return '' } }\n"
                "export function run(): void {\n"
                "  getMgr().zzText()\n"
                "}\n"
            ),
        },
    )
    callees = _callees(g, "src/use.ts::run")
    assert "src/mgr.ts::Mgr.zzText" in callees
    assert "src/use.ts::Mgr.zzText" not in callees


def test_a_script_file_type_named_without_import_keeps_its_edge(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            "src/mgr.ts": _MGR,
            "src/ctl.ts": "class Ctl { zzAbort(): void {} }\n",
            "src/use.ts": "function run(c: Ctl): void { c.zzAbort() }\n",
        },
    )
    assert _callees(g, "src/use.ts::run") == {"src/ctl.ts::Ctl.zzAbort"}
