"""The receiver walk reads TS type operators and unions.

``Partial<Svc>``, ``Readonly<Svc>`` and ``Pick<Svc, "run">`` are still
``Svc`` to a caller, so ``this.deps.run()`` on a field declared that way
reaches ``Svc.run``. A type the walk can't compute (``typeof svc``,
``ReturnType<..>``, an indexed access) says nothing about where the
value comes from, so it is unknown, not outside the repo. A union is
outside the repo only when every member is, once ``null``/``undefined``
members are dropped.
"""

from pathlib import Path

import pytest

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


_SVC = (
    "export class Svc {\n"
    "  run(): number { return 1 }\n"
    "  zzOnly(): number { return 1 }\n"
    "}\n"
    "export const svc = new Svc()\n"
    "export function make(): Svc { return new Svc() }\n"
)
_OTHER = "export class Other {\n  run(): number { return 2 }\n}\n"


def _holder(declared: str, call: str) -> str:
    return (
        "import { Svc, svc, make } from './svc'\n"
        "export class Holder {\n"
        f"  private deps: {declared}\n"
        f"  constructor(d: {declared}) {{ this.deps = d }}\n"
        f"  go(): void {{ this.deps.{call}() }}\n"
        "}\n"
    )


@pytest.mark.parametrize(
    "declared",
    [
        "Partial<Svc>",
        "Readonly<Svc>",
        "Required<Svc>",
        "NonNullable<Svc>",
        'Pick<Svc, "run">',
        'Omit<Svc, "zzOnly">',
        "Readonly<Partial<Svc>>",
    ],
)
def test_a_pass_through_utility_type_reads_its_argument(
    tmp_path: Path, declared: str
) -> None:
    g = _graph(
        tmp_path,
        {
            "src/svc.ts": _SVC,
            "src/other.ts": _OTHER,
            "src/holder.ts": _holder(declared, "run"),
        },
    )
    assert _callees(g, "src/holder.ts::Holder.go") == {"src/svc.ts::Svc.run"}


@pytest.mark.parametrize(
    "declared", ["Svc | undefined", "null | Svc", "Svc | null | undefined"]
)
def test_a_union_reads_its_one_non_nullish_member(
    tmp_path: Path, declared: str
) -> None:
    g = _graph(
        tmp_path,
        {
            "src/svc.ts": _SVC,
            "src/other.ts": _OTHER,
            "src/holder.ts": _holder(declared, "run"),
        },
    )
    assert _callees(g, "src/holder.ts::Holder.go") == {"src/svc.ts::Svc.run"}


@pytest.mark.parametrize(
    "declared",
    [
        "typeof svc",
        "ReturnType<typeof make>",
        "InstanceType<typeof Svc>",
        'Svc["zzOnly"]',
        "Error | Svc",
    ],
)
def test_a_type_the_walk_cant_compute_is_unknown(
    tmp_path: Path, declared: str
) -> None:
    # Unknown hands the call to the name ladder, which finds the repo's
    # one ``zzOnly``; an outside verdict would have dropped it.
    g = _graph(
        tmp_path,
        {"src/svc.ts": _SVC, "src/holder.ts": _holder(declared, "zzOnly")},
    )
    assert _callees(g, "src/holder.ts::Holder.go") == {
        "src/svc.ts::Svc.zzOnly"
    }


@pytest.mark.parametrize(
    "declared",
    ["string | Buffer", "AbortSignal | undefined", "Partial<Response>"],
)
def test_a_union_of_outside_types_stays_outside(
    tmp_path: Path, declared: str
) -> None:
    g = _graph(
        tmp_path,
        {"src/svc.ts": _SVC, "src/holder.ts": _holder(declared, "zzOnly")},
    )
    assert _callees(g, "src/holder.ts::Holder.go") == set()
