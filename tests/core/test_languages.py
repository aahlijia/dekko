"""Per-language extraction and resolution tests for Tier-1 specs."""

from pathlib import Path

from dekko.repo_ops import map_repository
from dekko.core.languages import is_supported, known_unsupported_language
from dekko.core.model import FileMap, Symbol
from dekko.core.resolver import resolve

FIXTURES = Path(__file__).parent.parent / "fixtures"


def test_known_unsupported_language_flags_astro() -> None:
    assert known_unsupported_language("Card.astro") == "astro"
    assert not is_supported("Card.astro")


def test_known_unsupported_language_ignores_ordinary_non_code() -> None:
    # Files dekko simply doesn't recognize (docs, config, data, no
    # extension at all) must not be flagged — only confirmed language
    # gaps belong in the "no parser" bucket.
    for name in ("README.md", "package.json", "image.png", "Makefile"):
        assert known_unsupported_language(name) is None


def _map(lang_dir: str) -> tuple[list[FileMap], set[tuple[str, str]]]:
    files, _ = map_repository(
        FIXTURES / lang_dir,
        subpath=None,
        excludes=(),
        max_file_size=1_000_000,
    )
    graph = resolve(files)
    return files, {(e.caller, e.callee) for e in graph.edges}


def _symbols(files: list[FileMap]) -> dict[str, Symbol]:
    return {
        f"{fm.path}::{sym.qualname}": sym for fm in files for sym in fm.symbols
    }


def test_c() -> None:
    files, edges = _map("c")
    syms = _symbols(files)
    hyp = syms["math.c::hyp"]
    assert [(p.name, p.type) for p in hyp.params] == [
        ("a", "double"),
        ("b", "double"),
    ]
    assert hyp.returns == "double"
    main = syms["main.c::main"]
    assert ("argv", "char **") in [(p.name, p.type) for p in main.params]
    assert ("math.c::hyp", "math.c::square") in edges
    assert ("main.c::main", "math.c::hyp") in edges


def test_cpp() -> None:
    files, edges = _map("cpp")
    syms = _symbols(files)
    assert "shapes.cpp::geo.Circle" in syms
    area = syms["shapes.cpp::geo.Circle.area"]
    assert area.kind == "method"
    assert area.returns == "double"
    ctor = syms["shapes.cpp::geo.Circle.Circle"]
    assert [(p.name, p.type) for p in ctor.params] == [("r", "double")]
    assert ("shapes.cpp::geo.Circle.area", "shapes.cpp::geo.pi") in edges


def test_javascript() -> None:
    files, edges = _map("js")
    syms = _symbols(files)
    assert syms["lib.js::greet"].kind == "function"
    greet_all = syms["lib.js::Greeter.greetAll"]
    assert greet_all.kind == "method"
    assert [p.name for p in greet_all.params] == ["...names"]
    assert syms["app.js::main"].kind == "function"
    assert ("app.js::main", "lib.js::Greeter") in edges
    assert ("app.js::main", "lib.js::greet") in edges
    assert ("app.js::main", "lib.js::Greeter.greetAll") in edges
    assert ("lib.js::Greeter.greetAll", "lib.js::greet") in edges
    assert ("app.js::<module>", "app.js::main") in edges


def test_typescript() -> None:
    files, edges = _map("ts")
    syms = _symbols(files)
    fetch = syms["svc.ts::fetchItem"]
    assert [(p.name, p.type) for p in fetch.params] == [
        ("id", "number"),
        ("eager?", "boolean"),
    ]
    assert fetch.returns == "Item"
    assert syms["svc.ts::Item"].kind == "interface"
    load = syms["svc.ts::Service.load"]
    assert load.returns == "Item"
    assert ("svc.ts::Service.load", "svc.ts::fetchItem") in edges
    assert ("svc.ts::Service.load", "svc.ts::Service.add") in edges

    # Bug #2(a): a const-arrow closure declared inside a method body is
    # a closure-local helper, not a member of the enclosing class — it
    # must not climb past QueryEngine.wrap to inherit QueryEngine's
    # qualname/kind.
    assert "svc.ts::QueryEngine.wrappedHelper" not in syms
    wrapped = syms["svc.ts::wrappedHelper"]
    assert wrapped.kind == "function"
    assert ("svc.ts::wrappedHelper", "svc.ts::fetchItem") in edges
    assert ("svc.ts::QueryEngine.wrap", "svc.ts::wrappedHelper") in edges


def test_typescript_top_level_variable_exports_indexed() -> None:
    files, _ = _map("ts")
    syms = _symbols(files)
    jobs = syms["exports.ts::jobs"]
    assert jobs.kind == "variable"
    assert jobs.exported is True
    config = syms["exports.ts::CONFIG"]
    assert config.kind == "variable"
    assert config.exported is False
    # An arrow-function value must still produce exactly one
    # "function" symbol, never a duplicate "variable" symbol for the
    # same declarator node.
    build = syms["exports.ts::build"]
    assert build.kind == "function"
    fm = next(f for f in files if f.path == "exports.ts")
    assert sum(1 for s in fm.symbols if s.name == "build") == 1


def test_javascript_top_level_variable_exports_indexed() -> None:
    files, _ = _map("js")
    syms = _symbols(files)
    jobs = syms["exports.js::jobs"]
    assert jobs.kind == "variable"
    assert jobs.exported is True
    config = syms["exports.js::CONFIG"]
    assert config.kind == "variable"
    assert config.exported is False
    build = syms["exports.js::build"]
    assert build.kind == "function"
    fm = next(f for f in files if f.path == "exports.js")
    assert sum(1 for s in fm.symbols if s.name == "build") == 1


def test_go() -> None:
    files, edges = _map("go")
    syms = _symbols(files)
    new_server = syms["srv.go::NewServer"]
    assert [(p.name, p.type) for p in new_server.params] == [
        ("name", "string")
    ]
    assert new_server.returns == "*Server"
    greet = syms["srv.go::Server.Greet"]
    assert greet.kind == "method"
    assert syms["srv.go::Server"].kind == "struct"
    assert ("srv.go::main", "srv.go::NewServer") in edges
    assert ("srv.go::main", "srv.go::Server.Greet") in edges
    assert ("srv.go::Server.Greet", "srv.go::label") in edges


def test_java() -> None:
    files, edges = _map("java")
    syms = _symbols(files)
    main = syms["App.java::App.main"]
    assert [(p.name, p.type) for p in main.params] == [("args", "String[]")]
    assert main.returns == "void"
    assert syms["App.java::Helper.twice"].kind == "method"
    assert ("App.java::App.main", "App.java::App") in edges
    assert ("App.java::App.main", "App.java::App.run") in edges
    assert ("App.java::App.run", "App.java::Helper.twice") in edges

    # F1: bug #3's kind-mapping fix (RawRef -> _CLASSDEF_KIND) is
    # already fully generic across kinds; these close the previously
    # unasserted Java enum/record cases (interface was already covered
    # by the TS test above; class is covered by App itself).
    assert syms["App.java::Status"].kind == "enum"
    assert syms["App.java::Point"].kind == "record"


def test_rust_kind_mapping() -> None:
    # F1: closes the Rust-side gap left by the bug #3 kind-mapping
    # verification — struct was already extracted correctly but
    # unasserted for `.kind`, and enum/trait had no fixture coverage
    # at all. `kinds.rs` is a separate fixture file (not lib.rs/
    # main.rs) so this addition can't perturb those files' own
    # exact-set symbol assertions in test_extractor.py.
    files, _ = _map("rust")
    syms = _symbols(files)
    assert syms["lib.rs::Point"].kind == "struct"
    assert syms["kinds.rs::Shape"].kind == "enum"
    assert syms["kinds.rs::Named"].kind == "trait"
