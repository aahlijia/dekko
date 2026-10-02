"""A C++ definition's arity takes the defaults its header declares.

A default argument belongs to the first declaration, and the ``.cc``
definition can't repeat it. When a prototype with the same qualname
and parameter count exists, the resolver reads the definition with
the prototype's trailing defaults: a call that leaves them out fits,
and a call that can't fit even then isn't the definition's.
Definitions with no prototype resolve the way they always have.
"""

from pathlib import Path

from dekko.core.model import CallGraph
from dekko.core.resolver import resolve
from dekko.repo_ops import map_repository


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
    return resolve(files)


def _pairs(graph: CallGraph, caller: str) -> set[str]:
    return {e.callee for e in graph.edges if e.caller == caller}


def _ambiguous_for(graph: CallGraph, caller: str) -> dict[str, list[str]]:
    return {name: ids for c, name, ids in graph.ambiguous if c == caller}


def _externals(graph: CallGraph, caller: str) -> set[str]:
    return {e.callee for e in graph.external if e.caller == caller}


_USE = "u/user.cc::app.Use"


def _user(body: str, includes: str = "") -> str:
    return f"{includes}namespace app {{\nvoid Use() {{\n{body}\n}}\n}}\n"


_SHAPE_H = (
    "namespace tf {\nint GetType(int shape, int type, int encoding = 0);\n}\n"
)
_SHAPE_CC = (
    "namespace tf {\nint GetType(int shape, int type, int encoding) {}\n}\n"
)
_GET_TYPE = "lib/shape.cc::tf.GetType"


def test_call_leaving_out_a_header_default_reaches_the_definition(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            "lib/shape.h": _SHAPE_H,
            "lib/shape.cc": _SHAPE_CC,
            "u/user.cc": _user("  GetType(1, 2);"),
        },
    )
    assert _pairs(graph, _USE) == {_GET_TYPE}


def test_call_below_the_declared_minimum_is_still_external(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            "lib/shape.h": _SHAPE_H,
            "lib/shape.cc": _SHAPE_CC,
            "u/user.cc": _user("  GetType(1);"),
        },
    )
    assert _pairs(graph, _USE) == set()
    assert _externals(graph, _USE) == {"GetType"}


def test_the_prototype_declaring_the_most_defaults_wins(
    tmp_path: Path,
) -> None:
    # One class declared under two build configurations.
    graph = _graph(
        tmp_path,
        {
            "lib/a.h": "namespace tf {\nint F(int a, int b);\n}\n",
            "lib/b.h": "namespace tf {\nint F(int a, int b = 1);\n}\n",
            "lib/f.cc": "namespace tf {\nint F(int a, int b) {}\n}\n",
            "u/user.cc": _user("  F(1);"),
        },
    )
    assert _pairs(graph, _USE) == {"lib/f.cc::tf.F"}


def test_method_through_a_receiver_takes_its_member_prototype(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            "lib/scope.h": (
                "namespace tf {\nclass Scope {\n public:\n"
                "  int ToGraph(int* g, int opts = 0);\n};\n}\n"
            ),
            "lib/scope.cc": (
                "namespace tf {\nint Scope::ToGraph(int* g, int opts) {}\n}\n"
            ),
            "u/user.cc": _user("  scope.ToGraph(&g);"),
        },
    )
    assert _pairs(graph, _USE) == {"lib/scope.cc::tf.Scope.ToGraph"}


def test_nested_namespace_spellings_join(tmp_path: Path) -> None:
    # ``namespace a::b {`` in the header, two blocks in the ``.cc``.
    graph = _graph(
        tmp_path,
        {
            "lib/f.h": "namespace a::b {\nint F(int x, int y = 0);\n}\n",
            "lib/f.cc": (
                "namespace a { namespace b {\nint F(int x, int y) {}\n} }\n"
            ),
            "u/user.cc": _user("  F(1);"),
        },
    )
    assert _pairs(graph, _USE) == {"lib/f.cc::a.b.F"}


_IDENTITY_H = (
    "namespace tf { namespace ops {\n"
    "int Identity(int ctx, int in, int out, int name = 0);\n"
    "} }\n"
)
_IDENTITY_CC = (
    "namespace tf { namespace ops {\n"
    "int Identity(int ctx, int in, int out, int name) {}\n"
    "} }\n"
)


def test_namespace_call_that_cannot_fit_a_declared_arity_is_external(
    tmp_path: Path,
) -> None:
    # The generated ``ops::Identity(scope, x)`` isn't in the repo. The
    # one on its path needs three arguments even with its header's
    # default, so nothing in ``ops`` is the target, however the
    # include points.
    graph = _graph(
        tmp_path,
        {
            "tf/c/ops/array_ops.h": _IDENTITY_H,
            "tf/c/ops/array_ops.cc": _IDENTITY_CC,
            "u/user.cc": _user(
                "  ops::Identity(s, x);",
                includes='#include "tf/c/ops/array_ops.h"\n',
            ),
        },
    )
    assert _pairs(graph, _USE) == set()
    assert _ambiguous_for(graph, _USE) == {}
    assert _externals(graph, _USE) == {"ops::Identity"}


def test_namespace_call_with_no_prototype_keeps_the_ladder(
    tmp_path: Path,
) -> None:
    # Without a prototype the definition's arity may hide defaults, so
    # the include still settles it.
    graph = _graph(
        tmp_path,
        {
            "tf/c/ops/array_ops.h": "namespace tf { namespace ops {\n} }\n",
            "tf/c/ops/array_ops.cc": _IDENTITY_CC,
            "u/user.cc": _user(
                "  ops::Identity(s, x);",
                includes='#include "tf/c/ops/array_ops.h"\n',
            ),
        },
    )
    assert _pairs(graph, _USE) == {"tf/c/ops/array_ops.cc::tf.ops.Identity"}


def test_namespace_call_fitting_a_hidden_default_is_narrowed(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            "lib/shape.h": _SHAPE_H,
            "lib/shape.cc": _SHAPE_CC,
            "lib/other.cc": "namespace other { int GetType(int a) {} }\n",
            "u/user.cc": _user("  tf::GetType(1, 2);"),
        },
    )
    assert _pairs(graph, _USE) == {_GET_TYPE}
    assert _ambiguous_for(graph, _USE) == {}


_DEVICE_H = (
    "namespace tf {\nclass Device {\n public:\n"
    "  Device(int* env, int attrs);\n};\n}\n"
)
_DEVICE_CC = "namespace tf {\nDevice::Device(int* env, int attrs) {}\n}\n"


def test_out_of_line_constructor_keeps_its_declared_minimum(
    tmp_path: Path,
) -> None:
    # A kernel builder's ``.Device(DEVICE_CPU)`` has one argument; the
    # constructor declares two, neither defaulted.
    graph = _graph(
        tmp_path,
        {
            "lib/device.h": _DEVICE_H,
            "lib/device.cc": _DEVICE_CC,
            "u/user.cc": _user("  Name(1).Device(2);"),
        },
    )
    assert "lib/device.cc::tf.Device.Device" not in _pairs(graph, _USE)


def test_out_of_line_constructor_takes_its_declared_default(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            "lib/foo.h": (
                "namespace tf {\nclass Foo {\n public:\n"
                "  Foo(int a, int b = 0);\n};\n}\n"
            ),
            "lib/foo.cc": "namespace tf {\nFoo::Foo(int a, int b) {}\n}\n",
            "u/user.cc": _user("  new tf::Foo(1);"),
        },
    )
    assert "lib/foo.cc::tf.Foo.Foo" in _pairs(graph, _USE)


def test_out_of_line_constructor_with_no_prototype_fits_any_count(
    tmp_path: Path,
) -> None:
    # No declaration of it was seen (a macro, a generated header), so
    # its defaults could be anything.
    graph = _graph(
        tmp_path,
        {
            "lib/foo.h": "namespace tf {\nclass Foo {\n  int x;\n};\n}\n",
            "lib/foo.cc": "namespace tf {\nFoo::Foo(int a, int b) {}\n}\n",
            "u/user.cc": _user("  new tf::Foo(1);"),
        },
    )
    assert "lib/foo.cc::tf.Foo.Foo" in _pairs(graph, _USE)


def test_c_files_resolve_the_way_they_always_have(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        {
            "lib/f.h": "int F(int a, int b);\n",
            "lib/f.c": "int F(int a, int b) {}\n",
            "u/user.c": "void Use(void) {\n  F(1);\n  F(1, 2);\n}\n",
        },
    )
    assert _pairs(graph, "u/user.c::Use") == {"lib/f.c::F"}
    assert _externals(graph, "u/user.c::Use") == {"F"}
