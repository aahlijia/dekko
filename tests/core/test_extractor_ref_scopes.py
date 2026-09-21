"""Round 32 Track 5b: ``RawRef.bound``, what a bare identifier is
lexically bound to.

The reference query captures every bare identifier in value position.
Before 5b nothing told ``count`` on line 1624 apart from the ``const
count`` on line 1623, so a local became a reference edge to whichever
repo symbol shared its spelling. The extractor now tags each reference
with its innermost binding; the resolver decides what a tag costs.
"""

from pathlib import Path

import pytest

from dekko.core.extractor import extract_file
from dekko.core.languages import spec_for_path


def _bound(tmp_path: Path, name: str, source: str) -> dict[str, list]:
    """``{identifier: [bound, ...]}`` in source order, for one file."""
    (tmp_path / name).write_text(source)
    spec = spec_for_path(name)
    assert spec is not None
    fm = extract_file(tmp_path, name, spec)
    assert fm.error is None
    out: dict[str, list] = {}
    for ref in sorted(fm.refs, key=lambda r: r.line):
        out.setdefault(ref.name, []).append(ref.bound)
    return out


# --- JS / TS ------------------------------------------------------------


def test_ts_parameters_of_every_shape_are_params(tmp_path: Path) -> None:
    got = _bound(
        tmp_path,
        "a.ts",
        "export function f(count: number, { a, b: [c, ...rest] }: any,\n"
        "                  opt = 1, maybe?: string) {\n"
        "  use(count, a, c, rest, opt, maybe)\n"
        "}\n"
        "const one = x => use(x)\n",
    )
    for name in ("count", "a", "c", "rest", "opt", "maybe", "x"):
        assert got[name] == ["param"], name


def test_ts_const_let_are_block_scoped(tmp_path: Path) -> None:
    got = _bound(
        tmp_path,
        "a.ts",
        "export function f() {\n"
        "  { let blocky = 2; use(blocky) }\n"
        "  use(blocky)\n"
        "}\n",
    )
    # Inside the block it is the local. Outside, that `let` is out of
    # scope, so the name is free again.
    assert got["blocky"] == ["local", None]


def test_js_var_hoists_out_of_its_block(tmp_path: Path) -> None:
    got = _bound(
        tmp_path,
        "a.js",
        "function f(x) {\n"
        "  use(hoisted)\n"
        "  if (x) { var hoisted = 1 }\n"
        "  use(hoisted)\n"
        "}\n",
    )
    # `var` binds the whole function, including lines above it.
    assert got["hoisted"] == ["local", "local"]


def test_ts_catch_and_for_of_bindings(tmp_path: Path) -> None:
    got = _bound(
        tmp_path,
        "a.ts",
        "export function f(items: string[]) {\n"
        "  try { go() } catch (err) { use(err) }\n"
        "  for (const item of items) { use(item) }\n"
        "  for (existing of items) { use(existing) }\n"
        "}\n",
    )
    assert got["err"] == ["local"]
    assert got["item"] == ["local"]
    # No const/let/var: an assignment to an outer name, binds nothing.
    assert got["existing"] == [None]


def test_ts_imported_then_shadowed(tmp_path: Path) -> None:
    # claude-code `utils/ide.ts`: the shape the visibility veto could
    # never see, because the import makes the cross-file edge possible.
    got = _bound(
        tmp_path,
        "a.ts",
        "import { errorMessage } from './errors'\n"
        "export function f() {\n"
        "  use(errorMessage)\n"
        "  try { go() } catch (e) {\n"
        "    const errorMessage = String(e)\n"
        "    use(errorMessage)\n"
        "  }\n"
        "}\n",
    )
    assert got["errorMessage"] == [None, "local"]


def test_ts_module_level_names_are_never_tagged(tmp_path: Path) -> None:
    got = _bound(
        tmp_path,
        "a.ts",
        "import { keep } from './keep'\n"
        "const TOP = 1\n"
        "const { spread } = cfg\n"
        "export function f() { use(keep, TOP, spread) }\n",
    )
    assert got == {
        "cfg": [None],
        "keep": [None],
        "TOP": [None],
        "spread": [None],
    }


def test_ts_nested_definition_is_a_symbol_not_a_local(tmp_path: Path) -> None:
    got = _bound(
        tmp_path,
        "a.ts",
        "export function f() {\n"
        "  const helper = () => 1\n"
        "  function inner() { return 2 }\n"
        "  use(helper, inner)\n"
        "}\n",
    )
    # Both are indexed symbols; the ladder resolves them same-file.
    assert got == {"helper": [None], "inner": [None]}


def test_ts_nested_definition_stops_an_outer_parameter(
    tmp_path: Path,
) -> None:
    got = _bound(
        tmp_path,
        "a.ts",
        "export function f(handler: number) {\n"
        "  {\n"
        "    const handler = () => 1\n"
        "    use(handler)\n"
        "  }\n"
        "}\n",
    )
    # Innermost binding wins, and it is a definition.
    assert got["handler"] == [None]


def test_ts_function_local_lazy_imports_are_imports(tmp_path: Path) -> None:
    # Measured on claude-code: ~40 sites a naive scope walk calls
    # "shadowed destructure" are these, every one a true edge.
    got = _bound(
        tmp_path,
        "a.ts",
        "export async function f() {\n"
        "  const { lazy, renamed: alias } = await import('./lazy')\n"
        "  const req = require('./req')\n"
        "  const notImport = t('some.key')\n"
        "  use(lazy, alias, req, notImport)\n"
        "}\n",
    )
    assert got["lazy"] == [None]
    assert got["alias"] == [None]
    assert got["req"] == [None]
    assert got["notImport"] == ["local"]


def test_tsx_component_props_are_params(tmp_path: Path) -> None:
    got = _bound(
        tmp_path,
        "a.tsx",
        "export function Row({ onClick, label }: Props) {\n"
        "  return <Box onClick={onClick}>{label}</Box>\n"
        "}\n",
    )
    assert got["onClick"] == ["param"]
    assert got["label"] == ["param"]


def test_ts_abstract_class_name_is_not_a_local(tmp_path: Path) -> None:
    got = _bound(
        tmp_path,
        "a.ts",
        "export function make() {\n"
        "  abstract class Base { static inst: Base | null = null }\n"
        "  return use(Base)\n"
        "}\n",
    )
    assert got["Base"] == [None]


_ERROR_ROOT_TSX = (
    "import { keep } from './keep'\n"
    "export function f(p: number) { use(keep, p) }\n"
    'vi.mock("m", async (orig) => {\n'
    '  const a = await orig<typeof import("m")>()\n'
    "  return { D: ({ c }: { c?: R }) => (\n"
)


def test_error_root_file_still_has_a_module_scope(tmp_path: Path) -> None:
    # On a file cut off mid-expression tree-sitter's *root* is `ERROR`,
    # not `program` (2 of cline's 2,710 files). The module scope is
    # "ran out of parents", never a node type: a walker that stops at
    # `program` would find no module scope here at all.
    from tree_sitter import Parser

    from dekko.core.grammars import get_grammar

    tree = Parser(get_grammar("tsx")).parse(_ERROR_ROOT_TSX.encode())
    assert tree.root_node.type == "ERROR"  # the premise of this test

    got = _bound(tmp_path, "a.tsx", _ERROR_ROOT_TSX)
    assert got["keep"] == [None]
    assert got["p"] == ["param"]


# --- Python -------------------------------------------------------------


def test_python_parameters_of_every_shape_are_params(
    tmp_path: Path,
) -> None:
    got = _bound(
        tmp_path,
        "a.py",
        "def f(a, b=1, *c, d: int = 2, e: str, **g):\n"
        "    use(a, b, c, d, e, g)\n"
        "lam = lambda u: use(u)\n",
    )
    for name in ("a", "b", "c", "d", "e", "g", "u"):
        assert got[name] == ["param"], name


def test_python_annotation_names_bind_nothing(tmp_path: Path) -> None:
    got = _bound(
        tmp_path,
        "a.py",
        "def f(a: Path):\n    use(Path)\n",
    )
    assert got["Path"] == [None]


def test_python_local_binding_shapes(tmp_path: Path) -> None:
    got = _bound(
        tmp_path,
        "a.py",
        "def f(src):\n"
        "    x, (y, z) = 1, (2, 3)\n"
        "    total = 0\n"
        "    total += 1\n"
        "    for k, v in src: pass\n"
        "    with open(src) as fh, g() as (p, q): pass\n"
        "    try: pass\n"
        "    except E as err: pass\n"
        "    if (n := 5): pass\n"
        "    use(x, y, z, total, k, v, fh, p, q, err, n)\n",
    )
    for name in ("x", "y", "z", "total", "k", "v", "fh", "err", "n"):
        assert got[name] == ["local"], name
    # `as (p, q)` is a plain `tuple` node, so the reference query sees
    # the targets themselves as tuple elements too. Harmless: they are
    # tagged like every other use of the name.
    assert set(got["p"]) == {"local"}
    assert set(got["q"]) == {"local"}


def test_python_attribute_and_subscript_targets_bind_nothing(
    tmp_path: Path,
) -> None:
    got = _bound(
        tmp_path,
        "a.py",
        "def f(self):\n"
        "    self.handler = 1\n"
        "    table[key] = 2\n"
        "    use(handler, key)\n",
    )
    assert got["handler"] == [None]
    assert got["key"] == [None]


def test_python_comprehension_variable_does_not_leak(
    tmp_path: Path,
) -> None:
    got = _bound(
        tmp_path,
        "a.py",
        "def f(xs):\n    ys = [use(i) for i in xs]\n    use(i)\n",
    )
    assert got["i"] == ["local", None]


def test_python_global_and_nonlocal_unbind(tmp_path: Path) -> None:
    got = _bound(
        tmp_path,
        "a.py",
        "def outer():\n"
        "    shared = 1\n"
        "    def inner():\n"
        "        global G\n"
        "        nonlocal shared\n"
        "        G = 2\n"
        "        shared = 3\n"
        "        use(G, shared)\n",
    )
    # `G` is the module's. `shared` is still a local, but outer()'s.
    assert got["G"] == [None]
    assert got["shared"] == ["local"]


def test_python_function_level_import_is_an_import(tmp_path: Path) -> None:
    got = _bound(
        tmp_path,
        "a.py",
        "def f():\n"
        "    import os.path\n"
        "    import numpy as np\n"
        "    from m import late, other as alias\n"
        "    use(os, np, late, alias)\n",
    )
    assert got == {
        "os": [None],
        "np": [None],
        "late": [None],
        "alias": [None],
    }


def test_python_class_body_names_are_invisible_to_methods(
    tmp_path: Path,
) -> None:
    got = _bound(
        tmp_path,
        "a.py",
        "class C:\n"
        "    attr = 1\n"
        "    others = [attr]\n"
        "    def m(self):\n"
        "        use(attr)\n",
    )
    # In the class body `attr` is the class's own name. Inside a
    # method it is not in scope at all, so it's a free name again.
    assert got["attr"] == ["local", None]


def test_python_nested_def_and_method_names_are_symbols(
    tmp_path: Path,
) -> None:
    got = _bound(
        tmp_path,
        "a.py",
        "def f():\n"
        "    def nested(): ...\n"
        "    use(nested)\n"
        "class C:\n"
        "    def handler(self): ...\n"
        "    table = [handler]\n",
    )
    assert got == {"nested": [None], "handler": [None]}


def test_python_fixture_parameter_is_tagged_param(tmp_path: Path) -> None:
    # The extractor can't know what a fixture is; it says "param" and
    # `resolver._fixture_param_target` takes it from there.
    got = _bound(
        tmp_path,
        "test_a.py",
        "import pytest\n"
        "@pytest.fixture\n"
        "def short_root(tmp_path): ...\n"
        "def test_x(short_root):\n"
        "    run(short_root)\n",
    )
    assert got["short_root"] == ["param"]


# --- languages with no binding query ------------------------------------


@pytest.mark.parametrize(
    ("name", "source"),
    [
        ("a.go", "package a\nfunc f(x T) T { var y T; return y }\n"),
        (
            "A.java",
            "class A { void f(Runnable r) { use(this::f); } }\n",
        ),
    ],
)
def test_other_languages_are_never_tagged(
    tmp_path: Path, name: str, source: str
) -> None:
    got = _bound(tmp_path, name, source)
    assert all(b is None for bounds in got.values() for b in bounds)
