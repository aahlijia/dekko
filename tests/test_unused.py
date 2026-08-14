"""The unused command: root rules, used-via-container, exit codes."""

import json

import pytest

from dekko import cli
from dekko import unused
from dekko.mapfile import MapIndex
from dekko.model import Import, Symbol

from conftest import RepoFactory


def _sym(name: str, path: str, **kw: object) -> Symbol:
    return Symbol(
        id=f"{path}::{kw.get('qualname', name)}",
        name=name,
        qualname=str(kw.get("qualname", name)),
        kind=str(kw.get("kind", "function")),
        path=path,
        language=str(kw.get("language", "python")),
        decorated=bool(kw.get("decorated", False)),
        exported=bool(kw.get("exported", False)),
    )


def _index(symbols: list[Symbol], **kw: object) -> MapIndex:
    idx = MapIndex(root_label="t")
    for sym in symbols:
        idx.symbols_by_id[sym.id] = sym
        idx.symbols_by_path.setdefault(sym.path, []).append(sym)
        idx.languages_by_path[sym.path] = sym.language
    idx.calls_in = dict(kw.get("calls_in", {}))  # type: ignore[arg-type]
    idx.referenced_in = dict(  # type: ignore[arg-type]
        kw.get("referenced_in", {})
    )
    idx.imports_by_path = dict(kw.get("imports", {}))  # type: ignore
    return idx


def test_go_capitalized_is_a_root() -> None:
    idx = _index(
        [
            _sym("Exported", "m.go", language="go"),
            _sym("hidden", "m.go", language="go"),
        ]
    )
    names = {s.name for s in unused.find_unused(idx, ())}
    assert names == {"hidden"}


def test_rust_pub_and_decorated_are_roots() -> None:
    idx = _index(
        [
            _sym("pub_fn", "m.rs", language="rust", exported=True),
            _sym("attr_fn", "m.rs", language="rust", decorated=True),
            _sym("plain", "m.rs", language="rust"),
        ]
    )
    assert [s.name for s in unused.find_unused(idx, ())] == ["plain"]


def test_main_dunder_and_test_paths_are_roots() -> None:
    idx = _index(
        [
            _sym("main", "app.py"),
            _sym("__init__", "app.py", qualname="C.__init__", kind="method"),
            _sym("helper", "tests/test_app.py"),
            _sym("dead", "app.py"),
        ]
    )
    assert [s.name for s in unused.find_unused(idx, ())] == ["dead"]


def test_class_used_via_method_is_kept() -> None:
    method = _sym("run", "a.py", qualname="Worker.run", kind="method")
    klass = _sym("Worker", "a.py", qualname="Worker", kind="class")
    idx = _index([method, klass], calls_in={method.id: ["b.py::caller"]})
    assert unused.find_unused(idx, ()) == []


def test_init_reexport_is_a_root() -> None:
    idx = _index(
        [_sym("thing", "pkg/mod.py"), _sym("hidden", "pkg/mod.py")],
        imports={
            "pkg/__init__.py": [
                Import(path="pkg/__init__.py", name="thing", source="pkg.mod")
            ]
        },
    )
    assert [s.name for s in unused.find_unused(idx, ())] == ["hidden"]


def test_referenced_but_never_called_is_kept() -> None:
    # Bug #2(b)/Performance #3: a callback wired up by reference
    # (object-literal value, array element, bare call argument, ...)
    # and never itself called must not read as dead code just because
    # calls_in is empty.
    handler = _sym("handleClick", "a.ts", language="typescript")
    idx = _index([handler], referenced_in={handler.id: ["b.ts::wireUp"]})
    assert unused.find_unused(idx, ()) == []


def test_roots_glob() -> None:
    idx = _index([_sym("keep", "gen/x.py"), _sym("drop", "src/y.py")])
    names = {s.name for s in unused.find_unused(idx, ("gen/*",))}
    assert names == {"drop"}


PY = {
    "a.py": "def used() -> int:\n    return 1\n\n\ndef dead() -> int:\n"
    "    return 2\n",
    "b.py": "from a import used\n\n\ndef main() -> int:\n    return used()\n",
}


def test_unused_integration_and_exit_codes(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY)
    code = cli.main(["unused", "--root", str(root)])
    out = capsys.readouterr().out
    assert code == 1
    assert "dead() -> int" in out
    assert "used() -> int" not in out  # called by main
    assert "def main" not in out  # name 'main' is a root


def test_unused_clean_exit_zero(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo({"a.py": "def main() -> int:\n    return 1\n"})
    assert cli.main(["unused", "--root", str(root)]) == 0
    assert "no unused symbols" in capsys.readouterr().out


def test_unused_decorated_is_root(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    src = {
        "a.py": "import click\n\n\n@click.command()\n"
        "def cmd() -> None:\n    pass\n"
    }
    root = make_mapped_repo(src)
    cli.main(["unused", "--root", str(root)])
    assert "no unused symbols" in capsys.readouterr().out


TS_CALLBACK = {
    "handlers.ts": (
        "export function handleClick(): void {\n  console.log('clicked');\n}\n"
    ),
    "wire.ts": (
        "import { handleClick } from './handlers';\n"
        "\n"
        "export const config = {\n"
        "  onClick: handleClick,\n"
        "};\n"
    ),
}


def test_unused_does_not_flag_pass_by_reference_callback(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # End-to-end repro of bug #2(b): handleClick is never *called*
    # anywhere, only wired up as an object-literal property value in
    # wire.ts — before the referenced_in plumbing, this was
    # indistinguishable from genuinely dead code.
    root = make_mapped_repo(TS_CALLBACK)
    assert cli.main(["unused", "--root", str(root)]) == 0
    assert "no unused symbols" in capsys.readouterr().out


GO_TYPE_ONLY = {
    "types.go": ("package main\n\ntype prEvent struct {\n\tName string\n}\n"),
    "main.go": (
        "package main\n\n"
        "func process(e prEvent) {\n"
        "\t_ = e.Name\n"
        "}\n\n"
        "func main() {\n"
        '\tprocess(prEvent{Name: "a"})\n'
        "}\n"
    ),
}


def test_unused_does_not_flag_go_struct_used_only_as_param_type(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # End-to-end repro of Track G / bug #1.1a: prEvent is never
    # *called* (structs aren't invoked), only used as a parameter
    # type and constructed via a composite literal — before the Go
    # reference_query, this was indistinguishable from dead code.
    root = make_mapped_repo(GO_TYPE_ONLY)
    assert cli.main(["unused", "--root", str(root)]) == 0
    assert "no unused symbols" in capsys.readouterr().out


GO_FIELD_TYPE_ONLY = {
    "types.go": ("package main\n\ntype RepoMeta struct {\n\tName string\n}\n"),
    "main.go": (
        "package main\n\n"
        "type Wrapper struct {\n"
        "\tMeta RepoMeta\n"
        "}\n\n"
        "func main() {\n"
        '\tw := Wrapper{Meta: RepoMeta{Name: "a"}}\n'
        "\t_ = w\n"
        "}\n"
    ),
}


def test_unused_does_not_flag_go_struct_used_only_as_field_type(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # End-to-end repro of the deliberately-uncovered case documented in
    # Track G's STATUS block: RepoMeta is used only as another
    # struct's field type, never as a parameter/return/var type —
    # before ``field_declaration type:`` was added to
    # ``_GO_REFERENCE_QUERY``, this was indistinguishable from dead
    # code.
    root = make_mapped_repo(GO_FIELD_TYPE_ONLY)
    assert cli.main(["unused", "--root", str(root)]) == 0
    assert "no unused symbols" in capsys.readouterr().out


TSX_COMPONENT_ONLY = {
    "sidebar.tsx": (
        "export function Sidebar(): JSX.Element {\n"
        "  return <div>menu</div>;\n"
        "}\n"
    ),
    "app.tsx": (
        "import { Sidebar } from './sidebar';\n\n"
        "export function App(): JSX.Element {\n"
        "  return <div><Sidebar /></div>;\n"
        "}\n"
    ),
}


def test_unused_does_not_flag_tsx_component_used_only_as_jsx_tag(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # End-to-end repro of Track G / bug #1.1b: Sidebar is never called
    # as a plain function, only rendered as <Sidebar /> — before the
    # jsx_opening_element/jsx_self_closing_element ref capture, this
    # read as dead code.
    root = make_mapped_repo(TSX_COMPONENT_ONLY)
    assert cli.main(["unused", "--root", str(root)]) == 0
    assert "no unused symbols" in capsys.readouterr().out


def test_unused_json(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY)
    assert cli.main(["unused", "--root", str(root), "--json"]) == 1
    doc = json.loads(capsys.readouterr().out)
    assert [d["id"] for d in doc["results"]] == ["a.py::dead"]
    assert doc["meta"]["total"] == 1
