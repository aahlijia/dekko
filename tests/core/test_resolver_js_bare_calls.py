"""A bare JS/TS call never names a method.

In JavaScript and TypeScript a class member, an object-literal method
and an interface member are only reachable through a receiver. A bare
``f()`` names a binding in lexical scope (a local, a parameter, an
import, a module-level function), so a method that shares its name is
never the target: a ``useState`` setter called ``setError`` is not the
repo's one ``Mgr.setError``, and ``getSessionId()`` inside a class with
its own ``getSessionId`` method is the imported function.
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


def _external(graph: CallGraph, caller: str) -> set[str]:
    return {e.callee for e in graph.external if e.caller == caller}


def _ambiguous(graph: CallGraph, caller: str) -> set[str]:
    return {name for who, name, _ in graph.ambiguous if who == caller}


_MGR = (
    "export class Mgr {\n"
    "  zzSetError(e: string): void {}\n"
    "  zzWrite(s: string): void {}\n"
    "  zzGetStats(): number { return 1 }\n"
    "}\n"
)


def test_a_destructured_local_is_not_a_lone_method(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "src/mgr.ts": _MGR,
            "src/use.ts": (
                "declare function useState(s: string):"
                " [string, (e: string) => void]\n"
                "export function Comp(): void {\n"
                "  const [, zzSetError] = useState('')\n"
                "  zzSetError('x')\n"
                "}\n"
            ),
        },
    )
    assert _callees(g, "src/use.ts::Comp") == set()
    assert "zzSetError" in _external(g, "src/use.ts::Comp")


def test_a_call_result_or_require_binding_is_not_a_method(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            "src/mgr.ts": _MGR,
            "src/use.ts": (
                "declare function useThing(): (s: string) => void\n"
                "export function Comp2(): void {\n"
                "  const zzWrite = useThing()\n"
                "  zzWrite('hello')\n"
                "}\n"
                "export function Comp3(): number {\n"
                "  const { zzGetStats } = require('./stats')\n"
                "  return zzGetStats()\n"
                "}\n"
            ),
        },
    )
    assert _callees(g, "src/use.ts::Comp2") == set()
    assert _callees(g, "src/use.ts::Comp3") == set()


def test_an_import_beats_the_enclosing_class_method(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "src/state.ts": "export function zzGet(): string { return '' }\n",
            "src/eng.ts": (
                "import { zzGet } from './state'\n"
                "export class Eng {\n"
                "  zzGet(): string { return zzGet() }\n"
                "  run(): string { return zzGet() }\n"
                "}\n"
            ),
        },
    )
    assert _callees(g, "src/eng.ts::Eng.run") == {"src/state.ts::zzGet"}
    assert _callees(g, "src/eng.ts::Eng.zzGet") == {"src/state.ts::zzGet"}


def test_a_renamed_import_still_resolves_through_its_alias(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            "src/m.ts": "export function zzRestore(): void {}\n",
            "src/eng.ts": (
                "import { zzRestore as zzAgain } from './m'\n"
                "export class Eng {\n"
                "  zzAgain(): void {}\n"
                "  run(): void { zzAgain() }\n"
                "}\n"
            ),
        },
    )
    assert _callees(g, "src/eng.ts::Eng.run") == {"src/m.ts::zzRestore"}


def test_two_methods_only_make_a_bare_call_external(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "src/a.ts": "export class A { zzUnsub(): void {} }\n",
            "src/b.ts": "export class B { zzUnsub(): void {} }\n",
            "src/use.ts": (
                "declare function sub(): () => void\n"
                "export function f(): void {\n"
                "  const zzUnsub = sub()\n"
                "  zzUnsub()\n"
                "}\n"
            ),
        },
    )
    assert _callees(g, "src/use.ts::f") == set()
    assert _ambiguous(g, "src/use.ts::f") == set()
    assert "zzUnsub" in _external(g, "src/use.ts::f")


def test_js_and_tsx_files_follow_the_same_rule(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "src/mgr.ts": _MGR,
            "src/use.js": (
                "export function comp() {\n"
                "  const zzWrite = make()\n"
                "  zzWrite('x')\n"
                "}\n"
            ),
            "src/view.tsx": (
                "export function View(): null {\n"
                "  const zzWrite = make()\n"
                "  zzWrite('x')\n"
                "  return null\n"
                "}\n"
            ),
        },
    )
    assert _callees(g, "src/use.js::comp") == set()
    assert _callees(g, "src/view.tsx::View") == set()


def test_bare_function_and_receiver_calls_are_unchanged(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            "src/mgr.ts": (
                "export function zzHelp(): void {}\n"
                "export class Mgr {\n"
                "  zzSetError(e: string): void {}\n"
                "  run(): void {\n"
                "    zzHelp()\n"
                "    this.zzSetError('x')\n"
                "  }\n"
                "}\n"
            ),
        },
    )
    assert _callees(g, "src/mgr.ts::Mgr.run") == {
        "src/mgr.ts::zzHelp",
        "src/mgr.ts::Mgr.zzSetError",
    }


def test_a_java_bare_call_still_reaches_its_class_method(
    tmp_path: Path,
) -> None:
    # Java, unlike JS/TS, names the enclosing class's members bare.
    g = _graph(
        tmp_path,
        {
            "src/main/java/p/Mgr.java": (
                "package p;\n"
                "class Mgr {\n"
                "  void zzHelp() {}\n"
                "  void run() { zzHelp(); }\n"
                "}\n"
            ),
        },
    )
    assert _callees(g, "src/main/java/p/Mgr.java::Mgr.run") == {
        "src/main/java/p/Mgr.java::Mgr.zzHelp"
    }
