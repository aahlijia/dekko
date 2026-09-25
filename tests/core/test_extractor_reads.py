"""Property reads and object-literal membership in the JS/TS extractor.

A getter or an object-literal handler is used by being *read*
(``cmd.isHidden``), which no call query sees. The extractor records
those reads per file and marks which definitions are literal members,
and of what call the literal is an argument.
"""

from pathlib import Path

from dekko.core.extractor import extract_file
from dekko.core.languages import spec_for_path
from dekko.core.model import FileMap

DEFINITIONS = """\
import type { Command } from './types.js'
import createReconciler from 'react-reconciler'
export function helper(): number { return 1 }
const fast = {
  name: 'fast',
  get isHidden() { return helper() > 0 },
  run() { return 2 },
} satisfies Command
const reconciler = createReconciler<number>({
  hideInstance(node) { node.hidden = true },
})
Bun.listen({ socket: { data(sock, buf) { return buf } } })
export function makeHandle() {
  return { get bridgeSessionId() { return 'x' } }
}
class Widget { get label() { return 'w' } }
function lonely() {}
"""

READS = """\
export function pick(cmd: any) {
  const { immediate } = cmd
  if (cmd?.isHidden || !cmd.isHidden) return immediate
  cmd.run()
  cmd.label = 3
  cmd.count += 1
  new cmd.Factory()
  return foo(cmd.bridgeSessionId, cmd.x.y)
}
const top = globalThis.settings
"""


def _extract(tmp_path: Path, name: str, source: str) -> FileMap:
    (tmp_path / name).write_text(source)
    return extract_file(tmp_path, name, spec_for_path(name))


def test_ts_object_literal_members_carry_their_consumer(
    tmp_path: Path,
) -> None:
    fm = _extract(tmp_path, "defs.ts", DEFINITIONS)
    by_name = {s.qualname: s for s in fm.symbols}
    expected = {
        "helper": (False, None),
        "isHidden": (True, None),
        "run": (True, None),
        "hideInstance": (True, "createReconciler"),
        "data": (True, "Bun.listen"),
        "bridgeSessionId": (True, None),
        "Widget.label": (False, None),
        "lonely": (False, None),
    }
    for qualname, (in_literal, consumer) in expected.items():
        sym = by_name[qualname]
        assert (sym.in_literal, sym.literal_consumer) == (in_literal, consumer)


def test_ts_property_reads_skip_calls_writes_and_constructors(
    tmp_path: Path,
) -> None:
    fm = _extract(tmp_path, "reads.ts", READS)
    names = [r.name for r in fm.reads]
    # ``cmd?.isHidden`` and ``!cmd.isHidden`` are both reads; the
    # destructured ``immediate`` is one; ``cmd.x.y`` reads both hops.
    assert names.count("isHidden") == 2
    assert {"immediate", "bridgeSessionId", "x", "y", "settings"} <= set(names)
    # A call, an assignment target, a compound assignment target and
    # a constructor are not reads.
    assert not {"run", "label", "count", "Factory"} & set(names)


def test_ts_reads_are_attributed_to_the_enclosing_definition(
    tmp_path: Path,
) -> None:
    fm = _extract(tmp_path, "reads.ts", READS)
    by_name = {r.name: r for r in fm.reads}
    assert by_name["immediate"].caller_id == "reads.ts::pick"
    assert by_name["immediate"].line == 2
    # A module-level ``const`` is a variable symbol of its own, so a
    # read in its initializer belongs to it, like a module-level call.
    assert by_name["settings"].caller_id == "reads.ts::top"
    assert all(r.path == "reads.ts" for r in fm.reads)


def test_ts_read_outside_any_definition_has_no_caller(
    tmp_path: Path,
) -> None:
    fm = _extract(tmp_path, "top.ts", "if (globalThis.flag) {}\n")
    assert [(r.name, r.caller_id) for r in fm.reads] == [("flag", None)]


def test_js_has_reads_and_python_has_none(tmp_path: Path) -> None:
    js = _extract(tmp_path, "r.js", "function f(c) { return c.flag }\n")
    assert [r.name for r in js.reads] == ["flag"]
    py = _extract(tmp_path, "r.py", "def f(c):\n    return c.flag\n")
    assert py.reads == []
    assert spec_for_path("r.py").read_query is None
