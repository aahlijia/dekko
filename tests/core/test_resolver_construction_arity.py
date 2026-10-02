"""A construction's argument count is checked against the constructor.

``new Base(1)`` names the class, and a JS/TS or Python class symbol
has no parameters of its own: they are on ``constructor`` /
``__init__``. When the class is the only candidate and no import ties
the call to its file (the import goes through a barrel, or there is
none), the written argument count has to be read against the
constructor, or every construction with an argument is thrown out.

A bare npm specifier (``"vscode"``, ``"http"``) is a package, whatever
repo file happens to be named like it.
"""

import json
from pathlib import Path

from dekko.core.model import CallGraph
from dekko.core.resolver import resolve
from dekko.repo_ops import map_repository

_BASE_TS = (
    "export class Base {\n"
    "  constructor(a: number, b?: string) {}\n"
    "}\n"
    "export class Sub extends Base {}\n"
    "export class NoCtor {}\n"
    "export class Opt {\n"
    "  constructor(a?: number) {}\n"
    "}\n"
)
_INDEX_TS = 'export * from "./base";\n'

_IMPL_PY = (
    "class Widget:\n"
    "    def __init__(self, a, b=None):\n"
    "        self.a = a\n"
    "\n"
    "\n"
    "class Child(Widget):\n"
    "    pass\n"
)
_INIT_PY = "from pkg.impl import Child, Widget\n"


def _graph(root: Path, sources: dict[str, str]) -> CallGraph:
    for rel, text in sources.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    files, _ = map_repository(
        root,
        subpath=None,
        excludes=(),
        max_file_size=1_000_000,
    )
    return resolve(files, root=root)


def _callees(graph: CallGraph, caller: str) -> set[str]:
    return {e.callee for e in graph.edges if e.caller == caller}


def _externals(graph: CallGraph, caller: str) -> set[str]:
    return {x.callee for x in graph.external if x.caller == caller}


def _ts(root: Path, body: str, imports: str = "") -> CallGraph:
    user = f"{imports}export function run() {{\n  {body}\n}}\n"
    return _graph(
        root,
        {"lib/base.ts": _BASE_TS, "lib/index.ts": _INDEX_TS, "user.ts": user},
    )


def _py(root: Path, body: str, imports: str = "") -> CallGraph:
    user = f"{imports}\n\ndef run():\n    {body}\n"
    return _graph(
        root,
        {
            "pkg/impl.py": _IMPL_PY,
            "pkg/__init__.py": _INIT_PY,
            "user.py": user,
        },
    )


_BARREL = 'import { Base, Sub, NoCtor, Opt } from "./lib/index";\n'
_RUN_TS = "user.ts::run"
_RUN_PY = "user.py::run"


def test_construction_through_a_barrel_fits_the_constructor(
    tmp_path: Path,
) -> None:
    graph = _ts(tmp_path, "return new Base(1);", _BARREL)
    assert _callees(graph, _RUN_TS) == {
        "lib/base.ts::Base",
        "lib/base.ts::Base.constructor",
    }
    assert _externals(graph, _RUN_TS) == set()


def test_construction_with_no_import_fits_the_constructor(
    tmp_path: Path,
) -> None:
    graph = _ts(tmp_path, 'return new Base(2, "x");')
    assert _callees(graph, _RUN_TS) == {
        "lib/base.ts::Base",
        "lib/base.ts::Base.constructor",
    }


def test_construction_no_constructor_fits_stays_external(
    tmp_path: Path,
) -> None:
    # Three arguments against ``(a, b?)``: the name matched a class
    # this call cannot be constructing.
    graph = _ts(tmp_path, 'return new Base(1, "x", 3);')
    assert _callees(graph, _RUN_TS) == set()
    assert _externals(graph, _RUN_TS) == {"Base"}


def test_an_import_through_a_barrel_proves_the_class_like_a_direct_one(
    tmp_path: Path,
) -> None:
    # With an import that leads to the class's file the count is not
    # the evidence, the import is: a barrel reads as a direct import.
    direct = 'import { Base } from "./lib/base";\n'
    for label, imports in (("barrel", _BARREL), ("direct", direct)):
        root = tmp_path / label
        root.mkdir()
        graph = _ts(root, 'return new Base(1, "x", 3);', imports)
        assert _callees(graph, _RUN_TS) == {"lib/base.ts::Base"}, label


def test_optional_constructor_parameter_takes_one_argument(
    tmp_path: Path,
) -> None:
    graph = _ts(tmp_path, "return new Opt(1);", _BARREL)
    assert _callees(graph, _RUN_TS) == {
        "lib/base.ts::Opt",
        "lib/base.ts::Opt.constructor",
    }


def test_subclass_without_a_constructor_takes_arguments(
    tmp_path: Path,
) -> None:
    # ``Sub`` inherits its constructor; its own argument count is not
    # written anywhere on it.
    graph = _ts(tmp_path, "return new Sub(1);", _BARREL)
    assert _callees(graph, _RUN_TS) == {"lib/base.ts::Sub"}


def test_class_without_constructor_or_base_takes_no_arguments(
    tmp_path: Path,
) -> None:
    graph = _ts(tmp_path, "return new NoCtor(1);")
    assert _callees(graph, _RUN_TS) == set()
    assert _externals(graph, _RUN_TS) == {"NoCtor"}

    again = tmp_path / "zero"
    again.mkdir()
    graph = _ts(again, "return new NoCtor();", _BARREL)
    assert _callees(graph, _RUN_TS) == {"lib/base.ts::NoCtor"}


def test_subclass_on_a_receiver_that_is_not_its_file_stays_external(
    tmp_path: Path,
) -> None:
    # Nothing to check one argument against, and the receiver is an
    # import of another module: no evidence this ``Sub`` is meant.
    graph = _graph(
        tmp_path,
        {
            "lib/base.ts": _BASE_TS,
            "other/shapes.ts": "export const kind = 1;\n",
            "user.ts": (
                'import * as shapes from "./other/shapes";\n'
                "export function run() {\n"
                "  return new shapes.Sub(1);\n"
                "}\n"
            ),
        },
    )
    assert _callees(graph, _RUN_TS) == set()
    assert _externals(graph, _RUN_TS) == {"shapes.Sub"}


def test_python_construction_through_a_package_fits_init(
    tmp_path: Path,
) -> None:
    graph = _py(tmp_path, "return Widget(1)", "from pkg import Widget")
    assert _callees(graph, _RUN_PY) == {
        "pkg/impl.py::Widget",
        "pkg/impl.py::Widget.__init__",
    }


def test_python_construction_init_does_not_fit_stays_external(
    tmp_path: Path,
) -> None:
    graph = _py(tmp_path, "return Widget(1, 2, 3)", "from pkg import Widget")
    assert _callees(graph, _RUN_PY) == set()
    assert _externals(graph, _RUN_PY) == {"Widget"}


def test_python_class_without_init_resolves_on_a_bare_call(
    tmp_path: Path,
) -> None:
    graph = _py(tmp_path, "return Child(1)", "from pkg import Child")
    assert _callees(graph, _RUN_PY) == {"pkg/impl.py::Child"}


def test_python_class_without_init_on_another_modules_receiver(
    tmp_path: Path,
) -> None:
    # ``config_pb2.Child(..)``: the receiver is an import of a module
    # that is not the class's file (here a generated one that is not
    # in the repo at all), and the class has no ``__init__`` to check
    # the argument against.
    graph = _py(
        tmp_path,
        "return config_pb2.Child(1)",
        "from pkg.protobuf import config_pb2",
    )
    assert _callees(graph, _RUN_PY) == set()
    assert _externals(graph, _RUN_PY) == {"config_pb2.Child"}


def test_python_class_without_init_on_its_own_modules_receiver(
    tmp_path: Path,
) -> None:
    graph = _py(tmp_path, "return impl.Child(1)", "from pkg import impl")
    assert _callees(graph, _RUN_PY) == {"pkg/impl.py::Child"}


def test_bare_local_name_does_not_reach_a_nested_class(
    tmp_path: Path,
) -> None:
    # ``Car`` is a local namedtuple here; the repo's only class named
    # ``Car`` is nested in another file's class and has no bare name.
    graph = _graph(
        tmp_path,
        {
            "shapes.py": (
                "class Garage:\n    class Car(Base):\n        pass\n"
            ),
            "user.py": (
                "import collections\n\n\n"
                "def run():\n"
                '    Car = collections.namedtuple("Car", ["size"])\n'
                "    return Car(1)\n"
            ),
        },
    )
    assert _callees(graph, _RUN_PY) == set()
    assert "Car" in _externals(graph, _RUN_PY)


def test_bare_local_name_does_not_reach_a_test_class(
    tmp_path: Path,
) -> None:
    sources = {
        "tests/test_values.py": "class Wrapped(Base):\n    pass\n",
        "user.py": (
            "import collections\n\n"
            'Wrapped = collections.namedtuple("Wrapped", ["t"])\n\n\n'
            "def run():\n"
            "    return Wrapped(1)\n"
        ),
    }
    graph = _graph(tmp_path, sources)
    assert _callees(graph, _RUN_PY) == set()
    assert _externals(graph, _RUN_PY) == {"Wrapped"}

    # From another test file the same class is a plausible target.
    again = tmp_path / "from-tests"
    again.mkdir()
    sources["tests/test_user.py"] = sources.pop("user.py")
    graph = _graph(again, sources)
    assert _callees(graph, "tests/test_user.py::run") == {
        "tests/test_values.py::Wrapped"
    }


_VSCODE_STUB = (
    "export class Position {\n"
    "  constructor(line: number, character: number) {}\n"
    "}\n"
    "export const commands = {\n"
    "  executeCommand(name: string) {},\n"
    "};\n"
)


def test_bare_npm_specifier_is_external_despite_a_file_named_like_it(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            "webview/src/vscode.ts": "export const api = 1;\n",
            "test/vscode-stub.ts": _VSCODE_STUB,
            "src/user.ts": (
                'import * as vscode from "vscode";\n'
                "export function run() {\n"
                '  vscode.commands.executeCommand("x");\n'
                "  return new vscode.Position(1, 2);\n"
                "}\n"
            ),
        },
    )
    assert _callees(graph, "src/user.ts::run") == set()
    assert _externals(graph, "src/user.ts::run") == {
        "vscode.commands.executeCommand",
        "vscode.Position",
    }


def test_bare_node_builtin_named_import_is_external(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        {
            "src/process.ts": "export const tag = 1;\n",
            "src/fs-ops.ts": (
                "export class Ops {\n  cwd(): string {\n"
                '    return "";\n  }\n}\n'
            ),
            "src/user.ts": (
                'import { cwd } from "process";\n'
                "export function run() {\n"
                "  return cwd();\n"
                "}\n"
            ),
        },
    )
    assert _callees(graph, "src/user.ts::run") == set()
    assert _externals(graph, "src/user.ts::run") == {"cwd"}


def test_workspace_package_named_like_a_file_still_resolves(
    tmp_path: Path,
) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "acme", "workspaces": ["packages/*"]})
    )
    (tmp_path / "packages/shapes").mkdir(parents=True)
    (tmp_path / "packages/shapes/package.json").write_text(
        json.dumps({"name": "shapes"})
    )
    graph = _graph(
        tmp_path,
        {
            "packages/shapes/src/index.ts": (
                "export class Circle {\n  constructor(r: number) {}\n}\n"
            ),
            "packages/app/src/shapes.ts": "export const tag = 1;\n",
            "packages/app/src/user.ts": (
                'import * as shapes from "shapes";\n'
                "export function run() {\n"
                "  return new shapes.Circle(1);\n"
                "}\n"
            ),
        },
    )
    assert _callees(graph, "packages/app/src/user.ts::run") == {
        "packages/shapes/src/index.ts::Circle",
        "packages/shapes/src/index.ts::Circle.constructor",
    }
