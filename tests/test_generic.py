"""Tier-2 generic fallback tests (Ruby fixture).

Tier-2 grammars resolve through the optional ``dekko[all]`` pack, so
these tests skip when it is not installed.
"""

import importlib.util
from pathlib import Path

import pytest

from dekko.cli import map_repository
from dekko.resolver import resolve

FIXTURES = Path(__file__).parent / "fixtures"

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("tree_sitter_language_pack") is None,
    reason="Tier-2 grammar pack not installed (pip install dekko[all])",
)


def test_ruby_generic_extraction() -> None:
    files, _ = map_repository(
        FIXTURES / "ruby",
        subpath=None,
        excludes=(),
        max_file_size=1_000_000,
    )
    assert len(files) == 1
    fm = files[0]
    assert fm.error is None
    assert fm.language == "ruby"
    qualnames = {sym.qualname for sym in fm.symbols}
    assert {
        "normalize",
        "Store",
        "Store.initialize",
        "Store.put",
        "Store.get",
    } <= qualnames
    put = next(sym for sym in fm.symbols if sym.qualname == "Store.put")
    assert put.kind == "method"
    assert [p.name for p in put.params] == ["key", "value"]

    graph = resolve(files)
    edges = {(e.caller, e.callee) for e in graph.edges}
    assert ("store.rb::Store.put", "store.rb::normalize") in edges
    assert ("store.rb::<module>", "store.rb::Store.put") in edges
