"""The unused command: root rules, used-via-container, exit codes."""

import json
import time

import pytest

from dekko.integrations import cli
from dekko.analysis import unused
from dekko.render.mapfile import MapIndex
from dekko.core.model import ExternalCall, Import, Param, Symbol

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
        params=list(kw.get("params", [])),  # type: ignore[arg-type]
        returns=kw.get("returns"),  # type: ignore[arg-type]
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
    idx.heritage_in = dict(  # type: ignore[arg-type]
        kw.get("heritage_in", {})
    )
    idx.heritage_external_out = dict(  # type: ignore[arg-type]
        kw.get("heritage_external_out", {})
    )
    idx.ambiguous_in = dict(  # type: ignore[arg-type]
        kw.get("ambiguous_in", {})
    )
    idx.ambiguous_out = dict(  # type: ignore[arg-type]
        kw.get("ambiguous_out", {})
    )
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


# --- find_suspects: unit-level tests over hand-built MapIndex fixtures ---


def test_find_suspects_flags_colliding_name_with_direct_fan_in() -> None:
    # `has` is excluded from find_unused via one resolved call -- but
    # `has` is also a proven collider (2 candidates) at an unrelated
    # call site elsewhere in the repo, so its exclusion is a suspect.
    has_target = _sym("has", "bar.py", qualname="Bar.has", kind="method")
    idx = _index(
        [has_target],
        calls_in={has_target.id: ["caller.py::caller"]},
        ambiguous_in={
            "cand1": [("elsewhere.py::c", "has")],
            "cand2": [("elsewhere.py::c", "has")],
        },
        ambiguous_out={"elsewhere.py::c": ["has"]},
    )
    suspects = unused.find_suspects(idx, ())
    assert [s.name for s in suspects] == ["has"]


def test_find_suspects_excludes_name_never_seen_by_ambiguous() -> None:
    # Genuine, unambiguous fan-in whose name never collides anywhere
    # -- not a suspect.
    clean = _sym("clean_helper", "bar.py")
    idx = _index([clean], calls_in={clean.id: ["caller.py::caller"]})
    assert unused.find_suspects(idx, ()) == []


def test_find_suspects_excludes_root_even_if_name_collides() -> None:
    # A symbol that would otherwise qualify (name collides, has direct
    # fan-in) but is excluded from `unused` purely via `_is_root`
    # (here: exported) is not a call-graph-trust suspect at all.
    exported = _sym(
        "has", "bar.py", qualname="Bar.has", kind="method", exported=True
    )
    idx = _index(
        [exported],
        calls_in={exported.id: ["caller.py::caller"]},
        ambiguous_in={"cand1": [("elsewhere.py::c", "has")]},
        ambiguous_out={"elsewhere.py::c": ["has"]},
    )
    assert unused.find_suspects(idx, ()) == []


def test_find_suspects_excludes_container_marked_only() -> None:
    # Worker is kept alive only because its method `run` was called
    # (container-marking) -- Worker's own id has no direct calls_in/
    # referenced_in entry, so even though "Worker" happens to also be
    # a proven collider, it must not be flagged: this is a different,
    # unrelated exclusion mechanism from "this exact symbol's own name
    # is a proven collider."
    method = _sym("run", "a.py", qualname="Worker.run", kind="method")
    klass = _sym("Worker", "a.py", qualname="Worker", kind="class")
    idx = _index(
        [method, klass],
        calls_in={method.id: ["b.py::caller"]},
        ambiguous_in={"cand1": [("elsewhere.py::c", "Worker")]},
        ambiguous_out={"elsewhere.py::c": ["Worker"]},
    )
    assert unused.find_suspects(idx, ()) == []


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


TS_GUARD_AND_CONCAT_ONLY = {
    "consts.ts": (
        "export const biomeArgIdx = 3;\n"
        "export const clearLine = 'clear';\n"
        "export const CYAN = '\\x1b[36m';\n"
    ),
    "use.ts": (
        "import { biomeArgIdx, clearLine, CYAN } from './consts';\n"
        "\n"
        "export function run(panelFocus: boolean): string {\n"
        "  const override = biomeArgIdx >= 0 ? '1' : '0';\n"
        "  const line = 'x' + clearLine;\n"
        "  const color = panelFocus ? '' : CYAN;\n"
        "  return override + line + color;\n"
        "}\n"
    ),
}


def test_unused_does_not_flag_const_read_as_binary_or_ternary_operand(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # round-18 claude-buddy finding: module-level `const`s read only
    # as a guard-condition/ternary operand or a string-concatenation
    # operand (never called, never a value in one of the previously
    # covered reference shapes) were false-flagged as unused.
    root = make_mapped_repo(TS_GUARD_AND_CONCAT_ONLY)
    assert cli.main(["unused", "--root", str(root)]) == 0
    assert "no unused symbols" in capsys.readouterr().out


PY_KEYWORD_ARGUMENT_CALLBACK = {
    "handlers.py": ("def valid_ndk_path(path):\n    return path\n"),
    "configure.py": (
        "from handlers import valid_ndk_path\n\n\n"
        "def get_var(name, check_success=None):\n"
        "    return name\n\n\n"
        "def main():\n"
        "    get_var('ANDROID_NDK_HOME',"
        " check_success=valid_ndk_path)\n"
    ),
}


def test_unused_does_not_flag_python_keyword_argument_callback(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # Round-22 (tensorflow) finding: valid_ndk_path is never *called*
    # anywhere, only wired up as a call's keyword-argument value in
    # configure.py — before Python's reference_query, this was
    # indistinguishable from genuinely dead code.
    root = make_mapped_repo(PY_KEYWORD_ARGUMENT_CALLBACK)
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


JAVA_METHOD_REFERENCE_ONLY = {
    "Foo.java": (
        "public class Foo {\n"
        "    void configureBuildInfoTask(BuildInfo info) {}\n"
        "    public void wire() {\n"
        "        java.util.function.Consumer<BuildInfo> c = "
        "this::configureBuildInfoTask;\n"
        "        c.accept(new BuildInfo());\n"
        "    }\n"
        "}\n\n"
        "class BuildInfo {}\n"
    ),
}


def test_unused_does_not_flag_java_method_only_used_via_method_reference(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # round-19 spring-boot finding: this::configureBuildInfoTask (a
    # Java 8 method reference passed as a callback) was invisible to
    # both call_query (no argument list) and Java's reference_query
    # (didn't exist) -- ~15% of the repo's .java files use `::`.
    root = make_mapped_repo(JAVA_METHOD_REFERENCE_ONLY)
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
    assert doc["kind_totals"] == {"callables": 1, "types": 0}


# --- --kinds: unit-level tests over hand-built MapIndex fixtures -----


def test_kinds_default_ignores_heritage_and_type_usage_evidence() -> None:
    # Backward-compat crux: a class with heritage_in (implemented) and
    # a type-usage match, but zero calls_in/referenced_in, must still
    # be flagged under the default ("callables") kind — heritage/
    # type-usage evidence must never leak into the unchanged default.
    base = _sym("Base", "a.py", kind="class")
    sub = _sym("Sub", "a.py", kind="class")
    user = _sym(
        "use_base",
        "a.py",
        params=[Param(name="b", type="Base")],
    )
    idx = _index([base, sub, user], heritage_in={base.id: [sub.id]})
    names = {s.name for s in unused.find_unused(idx, ())}
    assert "Base" in names


def test_kinds_types_kept_alive_by_heritage() -> None:
    base = _sym("Base", "a.py", kind="class")
    sub = _sym("Sub", "a.py", kind="class")
    idx = _index([base, sub], heritage_in={base.id: [sub.id]})
    names = {s.name for s in unused.find_unused(idx, (), kinds="types")}
    # Base is kept alive by heritage; Sub itself has neither
    # subtypes nor type-usage evidence, so it's flagged.
    assert names == {"Sub"}


def test_kinds_types_kept_alive_by_type_usage() -> None:
    config = _sym("Config", "a.py", kind="class")
    user = _sym(
        "start",
        "a.py",
        params=[Param(name="cfg", type="Config")],
    )
    idx = _index([config, user])
    names = {s.name for s in unused.find_unused(idx, (), kinds="types")}
    assert names == set()


def test_kinds_types_flags_genuinely_dead_type() -> None:
    dead = _sym("NeverUsed", "a.py", kind="class")
    idx = _index([dead])
    names = {s.name for s in unused.find_unused(idx, (), kinds="types")}
    assert names == {"NeverUsed"}


def test_kinds_types_restricts_scan_to_type_symbols() -> None:
    # A dead function must not surface under --kinds types even
    # though it has no calls_in either — the scan itself is
    # restricted to TYPE_KINDS symbols in this mode.
    dead_fn = _sym("dead_fn", "a.py", kind="function")
    idx = _index([dead_fn])
    assert unused.find_unused(idx, (), kinds="types") == []


def test_kinds_types_still_honors_construction_call_evidence() -> None:
    # Deviation from the design doc's literal pseudocode (see
    # unused.py's _used_keys docstring): callables evidence
    # (calls_in/referenced_in) is always consulted, even for
    # --kinds types, so a class that's only ever *constructed* isn't
    # wrongly flagged just because it has no heritage/type-usage
    # evidence of its own.
    ctor = _sym("__init__", "a.py", qualname="Widget.__init__", kind="method")
    widget = _sym("Widget", "a.py", kind="class")
    idx = _index([ctor, widget], calls_in={ctor.id: ["b.py::caller"]})
    assert unused.find_unused(idx, (), kinds="types") == []


def test_kinds_all_unions_callables_and_types_without_duplication() -> None:
    dead_fn = _sym("dead_fn", "a.py", kind="function")
    dead_type = _sym("DeadType", "a.py", kind="class")
    config = _sym("Config", "a.py", kind="class")
    user = _sym(
        "start",
        "a.py",
        params=[Param(name="cfg", type="Config")],
    )
    idx = _index(
        [dead_fn, dead_type, config, user],
        calls_in={user.id: ["b.py::caller"]},
    )
    found = unused.find_unused(idx, (), kinds="all")
    names = [s.name for s in found]
    # Config is kept alive by type-usage, start by a direct call;
    # dead_fn and DeadType are both flagged, each exactly once (no
    # symbol is both a callable and a TYPE_KINDS symbol, so no dedup
    # logic is needed).
    assert sorted(names) == ["DeadType", "dead_fn"]
    assert len(names) == len(set(names))


# --- --kinds: end-to-end fixtures through the real parse pipeline ----


HERITAGE_FIXTURE = {
    "a.py": (
        "class Base:\n    pass\n\n\n"
        "class Sub(Base):\n    pass\n\n\n"
        "class NeverUsed:\n    pass\n\n\n"
        "def main() -> int:\n    return 1\n"
    )
}


def test_unused_kinds_types_keeps_heritage_implemented_class(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(HERITAGE_FIXTURE)
    code = cli.main(["unused", "--root", str(root), "--kinds", "types"])
    out = capsys.readouterr().out
    assert code == 1
    assert "class Base" not in out
    assert "class NeverUsed" in out
    # Sub itself has no subtypes/usage/calls of its own — genuinely
    # unused despite extending something else.
    assert "class Sub" in out


TYPE_USAGE_FIXTURE = {
    "config.py": "class Config:\n    pass\n",
    "app.py": (
        "from config import Config\n"
        "\n"
        "\n"
        "def start(cfg: Config) -> None:\n"
        "    pass\n"
        "\n"
        "\n"
        "def main() -> int:\n"
        "    return 1\n"
    ),
}


def test_unused_kinds_types_keeps_type_usage_matched_class(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(TYPE_USAGE_FIXTURE)
    code = cli.main(["unused", "--root", str(root), "--kinds", "types"])
    # Config is kept alive by type-usage; start/main aren't TYPE_KINDS
    # symbols, so they're outside the --kinds types scan entirely —
    # nothing left to flag.
    assert code == 0
    assert "no unused symbols" in capsys.readouterr().out


def test_unused_kinds_default_unchanged_by_flag(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # Regression guard: `--kinds` omitted must be byte-identical to
    # `--kinds callables` explicitly given, on the existing fixture.
    root = make_mapped_repo(PY)
    code_implicit = cli.main(["unused", "--root", str(root)])
    out_implicit = capsys.readouterr().out
    code_explicit = cli.main(
        ["unused", "--root", str(root), "--kinds", "callables"]
    )
    out_explicit = capsys.readouterr().out
    assert code_implicit == code_explicit
    assert out_implicit == out_explicit


def test_unused_kinds_all_header_shows_subtotal(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    files = dict(TYPE_USAGE_FIXTURE)
    files["dead.py"] = "def dead_fn() -> int:\n    return 1\n"
    root = make_mapped_repo(files)
    code = cli.main(["unused", "--root", str(root), "--kinds", "all"])
    out = capsys.readouterr().out
    assert code == 1
    assert "callables" in out and "types" in out


EXPORTED_TYPE_FIXTURE = {
    "widget.ts": "export class Widget {\n  x: number = 1;\n}\n",
    "main.ts": "export function main(): void {}\n",
}


def test_unused_kinds_types_exported_class_is_root(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # _is_root's existing sym.exported check is kind-agnostic and
    # needs no new logic to cover types too.
    root = make_mapped_repo(EXPORTED_TYPE_FIXTURE)
    code = cli.main(["unused", "--root", str(root), "--kinds", "types"])
    assert code == 0
    assert "no unused symbols" in capsys.readouterr().out


TEST_ONLY_IMPLEMENTOR_FIXTURE = {
    "base.py": (
        "class Base:\n    pass\n\n\ndef main() -> int:\n    return 1\n"
    ),
    "tests/test_base.py": (
        "from base import Base\n\n\nclass FakeBase(Base):\n    pass\n"
    ),
}


def test_unused_kinds_types_no_tests_excludes_test_only_implementor(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(TEST_ONLY_IMPLEMENTOR_FIXTURE)
    # Without --no-tests, the test-file subclass counts as heritage
    # evidence and keeps Base alive.
    code = cli.main(["unused", "--root", str(root), "--kinds", "types"])
    assert code == 0
    assert "no unused symbols" in capsys.readouterr().out
    # With --no-tests, the test-only implementor is filtered out of
    # heritage_in before scoring, so Base is flagged again.
    code = cli.main(
        ["unused", "--root", str(root), "--kinds", "types", "--no-tests"]
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "class Base" in out


GENERIC_FIXTURE = {
    "a.py": (
        "from typing import Generic, TypeVar\n"
        "\n"
        "T = TypeVar('T')\n"
        "\n"
        "\n"
        "class Foo(Generic[T]):\n"
        "    pass\n"
        "\n"
        "\n"
        "def main() -> int:\n"
        "    return 1\n"
    )
}


def test_unused_kinds_types_generic_no_false_negative(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # Foo extending Generic[T] resolves as Foo's own heritage_out, not
    # evidence that anything else uses Foo — must not spuriously keep
    # Foo alive (per type-heritage-graph-design.md's own edge case).
    root = make_mapped_repo(GENERIC_FIXTURE)
    code = cli.main(["unused", "--root", str(root), "--kinds", "types"])
    out = capsys.readouterr().out
    assert code == 1
    assert "class Foo" in out


def test_unused_invalid_kinds_rejected() -> None:
    with pytest.raises(SystemExit):
        cli.main(["unused", "--kinds", "bogus"])


def test_kinds_types_at_scale_stays_fast() -> None:
    # Regression guard for the O(types * symbols) naive approach
    # (measured ~370s on spring-boot's ~14k types / ~69k symbols) —
    # query.type_usage_name_index inverts the loop to O(symbols).
    # 3,000 functions each referencing a distinct type name would take
    # minutes under the naive per-type type_usage_rows call; the
    # inverted index finishes in well under a second either way.
    n = 3000
    types = [_sym(f"Type{i}", "a.py", kind="class") for i in range(n)]
    funcs = [
        _sym(
            f"use_{i}",
            "a.py",
            params=[Param(name="x", type=f"Type{i}")],
        )
        for i in range(n)
    ]
    idx = _index(types + funcs)
    start = time.monotonic()
    found = unused.find_unused(idx, (), kinds="types")
    elapsed = time.monotonic() - start
    assert found == []  # every type is used by exactly one function
    assert elapsed < 5.0


DEAD_FUNCS = {
    "a.py": (
        "def main() -> int:\n"
        "    return 0\n\n\n"
        "def dead_one() -> int:\n"
        "    return 1\n\n\n"
        "def dead_two() -> int:\n"
        "    return 2\n\n\n"
        "def dead_three() -> int:\n"
        "    return 3\n\n\n"
        "def dead_four() -> int:\n"
        "    return 4\n"
    ),
}


# --- Rust trait-dispatched methods: unit-level fixtures -------------


def _rust_method(container: str, method: str) -> Symbol:
    return _sym(
        method,
        "m.rs",
        qualname=f"{container}.{method}",
        kind="method",
        language="rust",
    )


def test_rust_trait_dispatch_std_trait_keeps_method_alive() -> None:
    # Round-23 (zed) finding: impl Display for MyError { fn fmt ... }
    # has no explicit callers -- Display::fmt is invoked implicitly
    # via `{}`/`.to_string()`, invisible to a call-expression walk.
    struct = _sym("MyError", "m.rs", kind="struct", language="rust")
    method = _rust_method("MyError", "fmt")
    idx = _index(
        [struct, method],
        heritage_external_out={
            struct.id: [
                ExternalCall(caller=struct.id, callee="Display", lines=[5])
            ]
        },
    )
    # Only the method's root-ness is under test here; the struct
    # itself has no usage evidence in this minimal fixture and is
    # flagged independently -- irrelevant to this check.
    assert "fmt" not in {s.name for s in unused.find_unused(idx, ())}


def test_rust_trait_dispatch_strips_module_prefix_and_generics() -> None:
    # `use std::fmt; impl fmt::Display for X` and `impl From<String>
    # for X` both carry the clause exactly as written in
    # heritage_external_out -- module-qualified and/or generic.
    struct = _sym("X", "m.rs", kind="struct", language="rust")
    fmt_method = _rust_method("X", "fmt")
    from_method = _rust_method("X", "from")
    idx = _index(
        [struct, fmt_method, from_method],
        heritage_external_out={
            struct.id: [
                ExternalCall(
                    caller=struct.id, callee="fmt::Display", lines=[5]
                ),
                ExternalCall(
                    caller=struct.id, callee="From<String>", lines=[9]
                ),
            ]
        },
    )
    found_names = {s.name for s in unused.find_unused(idx, ())}
    assert "fmt" not in found_names
    assert "from" not in found_names


def test_rust_trait_dispatch_inherent_method_still_flagged() -> None:
    # Negative control: an inherent method (no trait impl at all) with
    # zero callers is still genuinely dead code.
    struct = _sym("Inherent", "m.rs", kind="struct", language="rust")
    method = _rust_method("Inherent", "fmt")
    idx = _index([struct, method])
    assert "fmt" in {s.name for s in unused.find_unused(idx, ())}


def test_rust_trait_dispatch_custom_trait_still_flagged() -> None:
    # A custom, non-std trait must not be exempted -- the fix is
    # scoped to _RUST_STD_TRAIT_NAMES, not "any trait impl."
    struct = _sym("Custom", "m.rs", kind="struct", language="rust")
    method = _rust_method("Custom", "do_thing")
    idx = _index(
        [struct, method],
        heritage_external_out={
            struct.id: [
                ExternalCall(caller=struct.id, callee="MyTrait", lines=[3])
            ]
        },
    )
    assert "do_thing" in {s.name for s in unused.find_unused(idx, ())}


# --- Rust trait-dispatched methods: end-to-end fixtures --------------

RUST_TRAIT_DISPATCH_FIXTURE = {
    "lib.rs": (
        "use std::fmt;\n\n"
        "pub struct MyError;\n\n"
        "impl fmt::Display for MyError {\n"
        "    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {\n"
        '        write!(f, "error")\n'
        "    }\n"
        "}\n\n"
        "struct Inherent;\n\n"
        "impl Inherent {\n"
        "    fn fmt(&self) -> String {\n"
        '        String::from("x")\n'
        "    }\n"
        "}\n\n"
        "trait MyTrait {\n"
        "    fn do_thing(&self);\n"
        "}\n\n"
        "struct Custom;\n\n"
        "impl MyTrait for Custom {\n"
        "    fn do_thing(&self) {}\n"
        "}\n\n"
        "fn main() {\n"
        "    let e = MyError;\n"
        '    println!("{}", e);\n'
        "}\n"
    ),
}


def test_unused_does_not_flag_rust_std_trait_dispatched_method(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(RUST_TRAIT_DISPATCH_FIXTURE)
    code = cli.main(["unused", "--root", str(root)])
    out = capsys.readouterr().out
    # MyError.fmt is implicitly dispatched via Display -- not flagged.
    assert "MyError.fmt" not in out
    # Negative controls: still flagged.
    assert code == 1
    assert "Inherent.fmt" in out  # inherent method, no trait impl
    assert "Custom.do_thing" in out  # custom, non-std trait


RUST_TRAIT_DISPATCH_KNOWN_LIMITATION_FIXTURE = {
    "lib.rs": (
        "use std::fmt;\n\n"
        "pub struct Widget;\n\n"
        "impl fmt::Display for Widget {\n"
        "    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {\n"
        '        write!(f, "widget")\n'
        "    }\n"
        "}\n\n"
        "impl Widget {\n"
        "    fn dead_helper(&self) -> i32 {\n"
        "        1\n"
        "    }\n"
        "}\n\n"
        "fn main() {\n"
        "    let w = Widget;\n"
        '    println!("{}", w);\n'
        "}\n"
    ),
}


def test_unused_rust_trait_dispatch_known_limitation(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # Documented, accepted false negative (see round-23 design doc
    # 03-rust-trait-dispatch-unused-false-positive.md): the fix is
    # type-level, not per-impl-block. Widget::dead_helper is a
    # genuinely dead inherent method unrelated to Display, but because
    # Widget implements a std trait somewhere, every method on Widget
    # -- not just fmt -- reads as a plausible root. Broader than the
    # design doc's own "same-name collision" framing of this risk;
    # noted explicitly here so it's visible, not silently uncovered.
    root = make_mapped_repo(RUST_TRAIT_DISPATCH_KNOWN_LIMITATION_FIXTURE)
    code = cli.main(["unused", "--root", str(root)])
    out = capsys.readouterr().out
    assert code == 0
    assert "dead_helper" not in out


# --- TS reference shapes: spread/typeof/subscript --------------------

TS_SPREAD_ONLY = {
    "consts.ts": "export const TOOL_DEFAULTS = { foo: 1 };\n",
    "use.ts": (
        "import { TOOL_DEFAULTS } from './consts';\n\n"
        "export function useIt(def: Record<string, unknown>) {\n"
        "  return { ...TOOL_DEFAULTS, ...def };\n"
        "}\n"
    ),
}


def test_unused_does_not_flag_ts_const_referenced_via_spread(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(TS_SPREAD_ONLY)
    assert cli.main(["unused", "--root", str(root)]) == 0
    assert "no unused symbols" in capsys.readouterr().out


TS_TYPEOF_ONLY = {
    "consts.ts": "export const TOOL_DEFAULTS = { foo: 1 };\n",
    "use.ts": (
        "import { TOOL_DEFAULTS } from './consts';\n\n"
        "export type ToolDefaultsType = typeof TOOL_DEFAULTS;\n"
    ),
}


def test_unused_does_not_flag_ts_const_referenced_via_typeof(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(TS_TYPEOF_ONLY)
    assert cli.main(["unused", "--root", str(root)]) == 0
    assert "no unused symbols" in capsys.readouterr().out


TS_SUBSCRIPT_ONLY = {
    "consts.ts": (
        "export const TASK_ID_PREFIXES: Record<string, string> = {\n"
        "  x: 'y',\n"
        "};\n"
    ),
    "use.ts": (
        "import { TASK_ID_PREFIXES } from './consts';\n\n"
        "export function useIt(type: string) {\n"
        "  return TASK_ID_PREFIXES[type];\n"
        "}\n"
    ),
}


def test_unused_does_not_flag_ts_const_referenced_via_subscript(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(TS_SUBSCRIPT_ONLY)
    assert cli.main(["unused", "--root", str(root)]) == 0
    assert "no unused symbols" in capsys.readouterr().out


TS_ROUND23_REGRESSION_FIXTURE = {
    "Tool.ts": (
        "export const TOOL_DEFAULTS = { foo: 1 };\n\n"
        "type ToolDefaultsType = typeof TOOL_DEFAULTS;\n\n"
        "export function useDefaults(def: Record<string, unknown>) {\n"
        "  return { ...TOOL_DEFAULTS, ...def };\n"
        "}\n"
    ),
    "Task.ts": (
        "export const TASK_ID_PREFIXES: Record<string, string> = {\n"
        "  x: 'y',\n"
        "};\n\n"
        "export function idFor(type: string) {\n"
        "  return TASK_ID_PREFIXES[type];\n"
        "}\n"
    ),
}


def test_unused_ts_round23_two_symbol_regression(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # Literal repro of round-23 claude-code.md §2.3: TOOL_DEFAULTS
    # (Tool.ts:757, referenced via typeof + spread) and
    # TASK_ID_PREFIXES (Task.ts:79, referenced via subscript) both
    # previously read `fan-in: 0` and surfaced as unused.
    root = make_mapped_repo(TS_ROUND23_REGRESSION_FIXTURE)
    assert cli.main(["unused", "--root", str(root)]) == 0
    assert "no unused symbols" in capsys.readouterr().out


def test_unused_top_flag_aliases_limit(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # --top is a same-dest alias for --limit (round 22 item B): unused
    # has no separate ranked-summary view, so the two flags are
    # interchangeable here, unlike stats/ambiguous where they differ.
    root = make_mapped_repo(DEAD_FUNCS)
    assert cli.main(["unused", "--root", str(root), "--top", "3"]) == 1
    top_out = capsys.readouterr().out
    assert cli.main(["unused", "--root", str(root), "--limit", "3"]) == 1
    limit_out = capsys.readouterr().out
    assert top_out == limit_out
    assert "dead_one" in top_out
    assert "dead_four" not in top_out  # beyond the cap of 3


# --- --suspect: end-to-end fixtures through the real parse pipeline --

# Foo.has is excluded from `unused` by a genuine, resolved call
# (self.has() inside Foo.check, resolved via same-class dispatch) --
# but the bare name "has" also collides across 3 repo-wide candidates
# (a.has, b.has, Foo.has) at the unrelated bare call in c.py, so
# `ambiguous` independently proves "has" collision-prone. Foo.has's
# exclusion is exactly the shape `--suspect` exists to flag.
SUSPECT_FIXTURE = {
    "foo.py": (
        "class Foo:\n"
        "    def has(self) -> bool:\n"
        "        return True\n\n"
        "    def check(self) -> bool:\n"
        "        return self.has()\n"
    ),
    "a.py": "def has() -> bool:\n    return True\n",
    "b.py": "def has() -> bool:\n    return False\n",
    "c.py": "def caller() -> bool:\n    return has()\n",
}


def test_unused_suspect_off_by_default_no_section(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SUSPECT_FIXTURE)
    code = cli.main(["unused", "--root", str(root)])
    out = capsys.readouterr().out
    assert code == 1
    assert "suspects:" not in out
    assert "Foo.has" not in out  # excluded by genuine fan-in, as today


def test_unused_suspect_flag_adds_section(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SUSPECT_FIXTURE)
    code = cli.main(["unused", "--root", str(root), "--suspect"])
    out = capsys.readouterr().out
    assert code == 1
    assert "Foo.has" not in out.split("suspects:")[0]  # not in main list
    assert "suspects:" in out
    assert "Foo.has" in out.split("suspects:")[1]
    assert "dekko ambiguous --name has" in out


def test_unused_suspect_flag_no_change_to_main_list(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # --suspect must not change the primary unused list's own output.
    root = make_mapped_repo(SUSPECT_FIXTURE)
    cli.main(["unused", "--root", str(root)])
    without = capsys.readouterr().out
    cli.main(["unused", "--root", str(root), "--suspect"])
    with_suspect = capsys.readouterr().out
    assert with_suspect[: len(without)] == without


def test_unused_suspect_json_round_trip(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SUSPECT_FIXTURE)
    code = cli.main(["unused", "--root", str(root), "--suspect", "--json"])
    assert code == 1
    doc = json.loads(capsys.readouterr().out)
    assert "suspects" in doc
    entry = next(s for s in doc["suspects"] if s["collides_with"] == "has")
    assert entry["id"] == "foo.py::Foo.has"
    assert entry["check_command"] == "dekko ambiguous --name has"


def test_unused_no_suspect_json_omits_key(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SUSPECT_FIXTURE)
    code = cli.main(["unused", "--root", str(root), "--json"])
    assert code == 1
    doc = json.loads(capsys.readouterr().out)
    assert "suspects" not in doc


# --- C/C++ ABI caveat (round-23 design doc 22, layer 1) --------------


def test_c_abi_caveat_none_when_no_c_cpp_symbols() -> None:
    found = [_sym("dead", "a.py", language="python")]
    assert unused._c_abi_caveat(found) is None


def test_c_abi_caveat_none_for_empty_results() -> None:
    assert unused._c_abi_caveat([]) is None


def test_c_abi_caveat_present_for_c_symbol() -> None:
    found = [_sym("TF_GraphVersions", "c_api.c", language="c")]
    caveat = unused._c_abi_caveat(found)
    assert caveat is not None
    assert 'extern "C"' in caveat
    assert "skeptically" in caveat


def test_c_abi_caveat_present_for_cpp_symbol() -> None:
    found = [_sym("TF_GraphVersions", "c_api.cc", language="cpp")]
    assert unused._c_abi_caveat(found) is not None


def test_c_abi_caveat_present_when_mixed_with_other_languages() -> None:
    found = [
        _sym("dead_py", "a.py", language="python"),
        _sym("TF_GraphVersions", "c_api.cc", language="cpp"),
    ]
    assert unused._c_abi_caveat(found) is not None


def test_unused_text_mode_caveat_absent_for_python_only(
    capsys: pytest.CaptureFixture,
) -> None:
    dead = _sym("dead", "a.py", language="python")
    idx = _index([dead])
    unused.run(idx, (), as_json=False, limit=50)
    out = capsys.readouterr().out
    assert "dead()" in out
    assert "note:" not in out


def test_unused_text_mode_caveat_present_for_c_symbol(
    capsys: pytest.CaptureFixture,
) -> None:
    dead = _sym("TF_GraphVersions", "c_api.c", language="c")
    idx = _index([dead])
    unused.run(idx, (), as_json=False, limit=50)
    out = capsys.readouterr().out
    assert out.rstrip("\n").endswith(unused._C_ABI_CAVEAT)


def test_unused_json_mode_caveats_empty_for_python_only(
    capsys: pytest.CaptureFixture,
) -> None:
    dead = _sym("dead", "a.py", language="python")
    idx = _index([dead])
    unused.run(idx, (), as_json=True, limit=50)
    doc = json.loads(capsys.readouterr().out)
    assert doc["caveats"] == []


def test_unused_json_mode_caveats_present_for_c_symbol(
    capsys: pytest.CaptureFixture,
) -> None:
    dead = _sym("TF_GraphVersions", "c_api.c", language="c")
    idx = _index([dead])
    unused.run(idx, (), as_json=True, limit=50)
    doc = json.loads(capsys.readouterr().out)
    assert doc["caveats"] == [unused._C_ABI_CAVEAT]


def test_unused_caveat_absent_when_no_unused_symbols_found(
    capsys: pytest.CaptureFixture,
) -> None:
    # A repo whose C/C++ files happen to produce zero unused hits stays
    # silent -- the gate is on `found`, not "this repo has a .c file".
    # 'main' is always a root, so this C symbol never lands in `found`.
    main_fn = _sym("main", "a.c", language="c")
    idx = _index([main_fn])
    unused.run(idx, (), as_json=False, limit=50)
    out = capsys.readouterr().out
    assert "no unused symbols" in out
    assert "note:" not in out
