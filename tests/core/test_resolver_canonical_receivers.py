"""Resolution end to end over calls whose receivers are canonicalized.

The extractor now stores ``chalk.red().bold`` for ``chalk.red("x")
.bold()``. These check the resolver's decisions are what they were
on the verbatim text for the shapes it keys on (an external import
head, a ``this`` chain, a parameter-rooted chain, a Rust std path),
and that the one intended change happened: a multi-line chain whose
head used to carry a trailing space is now seen as the import it is.
"""

from pathlib import Path

from dekko.core import languages
from dekko.core.extractor import extract_file
from dekko.core.model import CallGraph, FileMap
from dekko.core.resolver import resolve


def _graph(tmp_path: Path, sources: dict[str, str]) -> CallGraph:
    files: list[FileMap] = []
    for filename, source in sources.items():
        spec = languages.spec_for_path(filename)
        assert spec is not None
        (tmp_path / filename).write_text(source)
        fm = extract_file(tmp_path, filename, spec)
        assert fm.error is None
        files.append(fm)
    return resolve(files, root=tmp_path)


def _edge_pairs(graph: CallGraph) -> set[tuple[str, str]]:
    return {(e.caller, e.callee) for e in graph.edges}


def _external_callees(graph: CallGraph) -> set[str]:
    return {e.callee for e in graph.external}


def test_external_import_head_through_a_chain_stays_external(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            "ui.ts": (
                "import chalk from 'chalk';\n"
                "export function paint() { return chalk.red('x').bold(); }\n"
            ),
            "lib.ts": "export function bold(s: string) { return s; }\n",
        },
    )
    assert ("ui.ts::paint", "lib.ts::bold") not in _edge_pairs(graph)
    assert "chalk.red().bold" in _external_callees(graph)


def test_this_chain_still_resolves_to_the_container(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        {
            "a.ts": (
                "export class Runner {\n"
                "  helper() { return 1; }\n"
                "  run() { return this.helper(); }\n"
                "}\n"
            ),
        },
    )
    assert ("a.ts::Runner.run", "a.ts::Runner.helper") in _edge_pairs(graph)


def test_param_rooted_inline_object_type_still_matches(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            "client.ts": (
                "export class Client { getSchedule() { return 1; } }\n"
            ),
            "use.ts": (
                "import { Client } from './client';\n"
                "export function go(input: { client: Client }) {\n"
                "  return input.client.getSchedule();\n"
                "}\n"
            ),
        },
    )
    assert ("use.ts::go", "client.ts::Client.getSchedule") in _edge_pairs(
        graph
    )


def test_rust_std_path_through_a_chain_stays_external(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        {
            "main.rs": (
                "fn load(p: &str) -> String {\n"
                "    std::fs::read_to_string(p).unwrap()\n"
                "}\n"
                "fn unwrap() {}\n"
            ),
        },
    )
    assert ("main.rs::load", "main.rs::unwrap") not in _edge_pairs(graph)
    assert "std::fs::read_to_string().unwrap" in _external_callees(graph)


def test_multiline_chain_head_now_sees_its_import(tmp_path: Path) -> None:
    # Before canonical text the stored receiver was ``z `` (trailing
    # space), which matched no import, so ``.string()`` fell to the
    # bare-name ladder and could resolve to a repo function called
    # ``string``. That was a false edge; it is gone.
    graph = _graph(
        tmp_path,
        {
            "schema.ts": (
                "import { z } from 'zod';\n"
                "export const S = z\n"
                "  .string();\n"
                "export function string() { return 's'; }\n"
            ),
        },
    )
    assert not any(
        callee.endswith("::string") for _, callee in _edge_pairs(graph)
    )
    assert "z.string" in _external_callees(graph)
