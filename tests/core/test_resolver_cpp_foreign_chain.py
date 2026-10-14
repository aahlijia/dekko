"""A C/C++ chain off a value typed outside the repo stays outside.

``v.getType().getElementType()`` on an ``mlir::Value v`` calls MLIR's
``getType()`` and then a method of what it returns, which is MLIR's
too: a call on an outside type without template arguments hands back
another outside value, so the chain's call is external instead of
landing on the repo's one ``getElementType``. A template
(``std::vector<Foo>``) can hand back its argument, and a field of the
outside value can be anything, so those chains still go to the
ladder.
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
    return resolve(files, root=root)


def _callees(graph: CallGraph, caller: str) -> set[str]:
    return {e.callee for e in graph.edges if e.caller == caller}


def _external(graph: CallGraph, caller: str) -> set[str]:
    return {e.callee for e in graph.external if e.caller == caller}


_REPO_TYPES = (
    "namespace tf {\n"
    "class MlirTensor {\n"
    " public:\n"
    "  int getElementType() { return 0; }\n"
    "};\n"
    "class Foo {\n"
    " public:\n"
    "  int bar() { return 1; }\n"
    "  int handle() { return 2; }\n"
    "};\n"
    "}\n"
)


def test_calls_off_an_outside_value_stay_outside(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "t/types.h": _REPO_TYPES,
            "u/user.cc": (
                '#include "mlir/IR/Value.h"\n'
                '#include "t/types.h"\n'
                "int A(mlir::Value v) {\n"
                "  return v.getType().getElementType();\n"
                "}\n"
                "int B(const AttrValue& attr) {\n"
                "  return attr.list().shape().handle();\n"
                "}\n"
            ),
        },
    )
    assert _callees(g, "u/user.cc::A") == set()
    assert any(
        n.endswith("getElementType") for n in _external(g, "u/user.cc::A")
    )
    assert _callees(g, "u/user.cc::B") == set()
    assert any(n.endswith("handle") for n in _external(g, "u/user.cc::B"))


def test_template_and_field_chains_keep_the_ladder(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "t/types.h": _REPO_TYPES,
            "u/user.cc": (
                "#include <vector>\n"
                '#include "mlir/IR/Value.h"\n'
                '#include "t/types.h"\n'
                "namespace tf {\n"
                "int A(std::vector<Foo>& v) {\n"
                "  return v.front().bar();\n"
                "}\n"
                "int B(mlir::Value v) {\n"
                "  return v.impl.handle();\n"
                "}\n"
                "}\n"
            ),
        },
    )
    assert _callees(g, "u/user.cc::tf.A") == {"t/types.h::tf.Foo.bar"}
    assert _callees(g, "u/user.cc::tf.B") == {"t/types.h::tf.Foo.handle"}
