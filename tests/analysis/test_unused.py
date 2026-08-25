"""The unused command: root rules, used-via-container, exit codes."""

import json
import time

import pytest

from dekko.integrations import cli
from dekko.analysis import unused
from dekko.render.mapfile import MapIndex
from dekko.core.model import Import, Param, Symbol

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
