"""ES private methods (``#name``) in the JS/TS extractor.

tree-sitter names a ``#x`` class member with a
``private_property_identifier`` node, not the ``property_identifier``
a public member gets. A ``#x`` method is a symbol named ``#x`` (the
``#`` kept: ``#x`` and ``x`` are two different members, and calls
already say ``#x``), it is never part of an exported class's public
surface, and a ``this.#x`` member access is a property read like any
other.
"""

from pathlib import Path

import pytest

from dekko.core.extractor import extract_file
from dekko.core.languages import spec_for_path
from dekko.core.model import FileMap

SOURCE = """\
export class Logger {
  static #output(level: string, msg: string): void { console.log(level, msg) }
  static output(msg: string): void { Logger.#output("info", msg) }
  get #label(): string { return "x" }
  async #flush(): Promise<void> {}
  #priv(): number { return 1 }
  callPriv(): number { return this.#priv() }
  handler(): unknown { return this.#flush.bind(this) }
}
"""


def _extract(tmp_path: Path, name: str, source: str) -> FileMap:
    (tmp_path / name).write_text(source)
    return extract_file(tmp_path, name, spec_for_path(name))


@pytest.mark.parametrize("name", ["log.ts", "log.js"])
def test_private_methods_are_symbols_named_with_the_hash(
    tmp_path: Path, name: str
) -> None:
    source = SOURCE
    if name.endswith(".js"):
        source = (
            source.replace(": string", "")
            .replace(": void", "")
            .replace(": number", "")
            .replace(": Promise<void>", "")
            .replace(": unknown", "")
        )
    fm = _extract(tmp_path, name, source)
    by_qualname = {s.qualname: s for s in fm.symbols}
    for qualname in (
        "Logger.#output",
        "Logger.#label",
        "Logger.#flush",
        "Logger.#priv",
    ):
        sym = by_qualname[qualname]
        assert sym.name == qualname.split(".", 1)[1]
        assert sym.kind == "method"
    # The public ``output`` is a separate member.
    assert "Logger.output" in by_qualname


def test_private_method_is_never_exported(tmp_path: Path) -> None:
    fm = _extract(tmp_path, "log.ts", SOURCE)
    by_qualname = {s.qualname: s for s in fm.symbols}
    assert by_qualname["Logger.callPriv"].exported
    assert not by_qualname["Logger.#priv"].exported
    assert not by_qualname["Logger.#output"].exported


def test_private_member_access_is_a_read(tmp_path: Path) -> None:
    fm = _extract(tmp_path, "log.ts", SOURCE)
    names = [r.name for r in fm.reads]
    # ``this.#flush.bind(this)`` reads ``#flush``; the calls
    # ``this.#priv()`` and ``Logger.#output(..)`` are calls, not reads.
    assert "#flush" in names
    assert "#priv" not in names
    assert "#output" not in names
