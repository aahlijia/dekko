"""Per-language extraction and resolution tests for Tier-1 specs."""

from pathlib import Path

from dekko.cli import map_repository
from dekko.model import FileMap, Symbol
from dekko.resolver import resolve

FIXTURES = Path(__file__).parent / "fixtures"


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
    assert syms["svc.ts::Item"].kind == "class"
    load = syms["svc.ts::Service.load"]
    assert load.returns == "Item"
    assert ("svc.ts::Service.load", "svc.ts::fetchItem") in edges
    assert ("svc.ts::Service.load", "svc.ts::Service.add") in edges


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
    assert syms["srv.go::Server"].kind == "class"
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
