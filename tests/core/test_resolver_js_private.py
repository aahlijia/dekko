"""Calls to ES private methods (``#name``) resolve to them.

``this.#m()`` and ``C.#m()`` name the class's own private member, never
a public ``m`` of the same class or of another one, and a call made
inside ``#m`` belongs to ``#m``, not to the class around it.
"""

from pathlib import Path

from dekko.core.model import CallGraph
from dekko.core.resolver import resolved_id_name, resolve
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


LOGGER = """\
export function sink(level: string, msg: string): void {}
export class Logger {
  static #output(level: string, msg: string): void { sink(level, msg) }
  static output(msg: string): void {}
  static error(msg: string): void { Logger.#output("error", msg) }
  #priv(): number { return 1 }
  priv(): number { return 2 }
  callPriv(): number { return this.#priv() }
}
"""


def test_private_calls_reach_the_private_member(tmp_path: Path) -> None:
    g = _graph(tmp_path, {"src/log.ts": LOGGER})
    assert _callees(g, "src/log.ts::Logger.error") == {
        "src/log.ts::Logger.#output"
    }
    assert _callees(g, "src/log.ts::Logger.callPriv") == {
        "src/log.ts::Logger.#priv"
    }


def test_a_call_inside_a_private_method_belongs_to_it(tmp_path: Path) -> None:
    g = _graph(tmp_path, {"src/log.ts": LOGGER})
    assert _callees(g, "src/log.ts::Logger.#output") == {"src/log.ts::sink"}
    assert "src/log.ts::sink" not in _callees(g, "src/log.ts::Logger")


def test_an_id_name_keeps_the_hash_and_drops_only_an_overload() -> None:
    assert resolved_id_name("a.ts::C.#m") == "#m"
    assert resolved_id_name("a.ts::C.#m#2") == "#m"
    assert resolved_id_name("A.java::C.of#2") == "of"
    assert resolved_id_name("a.py::helper") == "helper"
