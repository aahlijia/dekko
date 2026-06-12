"""Extraction tests for the Tier-1 Python and Rust queries."""

from pathlib import Path

from lidar_map import languages
from lidar_map.extractor import _parse_rust_use, extract_file
from lidar_map.model import Symbol

FIXTURES = Path(__file__).parent / "fixtures"


def _by_qualname(symbols: list[Symbol]) -> dict[str, Symbol]:
    return {sym.qualname: sym for sym in symbols}


def test_python_symbols() -> None:
    spec = languages.spec_for_path("util.py")
    assert spec is not None
    fm = extract_file(FIXTURES / "python", "util.py", spec)
    assert fm.error is None
    syms = _by_qualname(fm.symbols)
    assert set(syms) == {"helper", "Config", "Config.load", "Config.validate"}

    helper = syms["helper"]
    assert helper.kind == "function"
    assert [(p.name, p.type) for p in helper.params] == [
        ("x", "int"),
        ("y", "int"),
    ]
    assert helper.returns == "int"

    load = syms["Config.load"]
    assert load.kind == "method"
    assert [(p.name, p.type) for p in load.params] == [
        ("self", None),
        ("path", "str"),
    ]
    assert load.returns == '"Config"'
    assert syms["Config"].kind == "class"


def test_python_splat_params_and_imports() -> None:
    spec = languages.spec_for_path("main.py")
    assert spec is not None
    fm = extract_file(FIXTURES / "python", "main.py", spec)
    run = _by_qualname(fm.symbols)["run"]
    assert [p.name for p in run.params] == ["args", "*extra", "**kw"]
    assert run.params[0].type == "list[str]"

    imports = {(i.name, i.source) for i in fm.imports}
    assert ("util", "util") in imports
    assert ("helper", "util.helper") in imports


def test_python_relative_import_sources(tmp_path: Path) -> None:
    spec = languages.spec_for_path("rel.py")
    assert spec is not None
    (tmp_path / "rel.py").write_text(
        "from . import sibling\n"
        "from .. import parent\n"
        "from .pkg import thing\n"
        "from ..pkg import other\n"
    )
    fm = extract_file(tmp_path, "rel.py", spec)
    imports = {(i.name, i.source) for i in fm.imports}
    # Relative dots must not be doubled.
    assert ("sibling", ".sibling") in imports
    assert ("parent", "..parent") in imports
    assert ("thing", ".pkg.thing") in imports
    assert ("other", "..pkg.other") in imports


def test_python_calls_attributed_to_enclosing_function() -> None:
    spec = languages.spec_for_path("main.py")
    assert spec is not None
    fm = extract_file(FIXTURES / "python", "main.py", spec)
    in_run = {c.name for c in fm.calls if c.caller_id}
    assert {"Config", "validate", "helper"} <= in_run
    top_level = {c.name for c in fm.calls if c.caller_id is None}
    assert "run" in top_level


def test_rust_symbols() -> None:
    spec = languages.spec_for_path("lib.rs")
    assert spec is not None
    fm = extract_file(FIXTURES / "rust", "lib.rs", spec)
    assert fm.error is None
    syms = _by_qualname(fm.symbols)
    assert set(syms) == {"Point", "Point.new", "Point.dist", "norm"}

    new = syms["Point.new"]
    assert new.kind == "method"
    assert [(p.name, p.type) for p in new.params] == [
        ("x", "f64"),
        ("y", "f64"),
    ]
    assert new.returns == "Self"

    dist = syms["Point.dist"]
    assert dist.params[0].name == "&self"
    assert dist.params[1].type == "&Point"
    assert syms["norm"].kind == "function"


def test_rust_calls_and_receivers() -> None:
    spec = languages.spec_for_path("main.rs")
    assert spec is not None
    fm = extract_file(FIXTURES / "rust", "main.rs", spec)
    calls = {(c.name, c.receiver) for c in fm.calls if c.caller_id}
    assert ("new", "Point") in calls
    assert ("norm", None) in calls
    assert any(name == "dist" for name, _ in calls)


def test_parse_rust_use() -> None:
    assert _parse_rust_use("a::b::c") == [("c", "a::b::c")]
    assert _parse_rust_use("a::b as d") == [("d", "a::b")]
    assert sorted(_parse_rust_use("a::{b, c as d}")) == [
        ("b", "a::b"),
        ("d", "a::c"),
    ]
    assert _parse_rust_use("a::*") == []
    assert ("e", "x::e") in _parse_rust_use("x::{y::{z}, e}")
