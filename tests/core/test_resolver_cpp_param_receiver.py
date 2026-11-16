"""A C/C++ call on a typed parameter or a cast is walked from its type.

``result_type.getElementType()`` on a ``ShapedType result_type``
parameter is MLIR's method, not the repo's one ``getElementType``: a
one-segment receiver that is one of the caller's typed parameters is
read from the declared type, so an outside type sends the call
external and an in-repo type narrows it to that type's members, even
for a namespaced type whose qualname the typed-parameter rung can't
match. A cast receiver (``llvm::cast<T>(v)``, ``reinterpret_cast<T*>
(p)``) is typed by its template argument the same way.
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


_MLIR_TENSOR = (
    "namespace tf {\n"
    "class MlirTensor {\n"
    " public:\n"
    "  int getElementType() { return 0; }\n"
    "};\n"
    "}\n"
)


def test_outside_typed_parameters_and_casts_go_external(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            "t/mlir_tensor.h": _MLIR_TENSOR,
            "u/user.cc": (
                '#include "mlir/IR/BuiltinTypes.h"\n'
                "using mlir::ShapedType;\n"
                "void A(ShapedType result_type) {\n"
                "  result_type.getElementType();\n"
                "}\n"
                "void D(mlir::Value v) {\n"
                "  llvm::cast<mlir::TensorType>(v).getElementType();\n"
                "}\n"
                "void F(mlir::Value v) {\n"
                "  v->getElementType();\n"
                "}\n"
            ),
        },
    )
    for caller in ("u/user.cc::A", "u/user.cc::D", "u/user.cc::F"):
        assert _callees(g, caller) == set()
        assert any(n.endswith("getElementType") for n in _external(g, caller))


_DATA_TYPE_SET = (
    "#include <cstddef>\n"
    "class DataTypeSet {\n"
    " public:\n"
    "  size_t size() const { return n_; }\n"
    " private:\n"
    "  size_t n_ = 0;\n"
    "};\n"
)


def test_std_parameters_go_external_past_an_include(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            "types.h": _DATA_TYPE_SET,
            "user.cc": (
                "#include <vector>\n"
                "#include <string>\n"
                '#include "types.h"\n'
                "void A(const std::vector<int>& v) {\n"
                "  if (v.size() > 3) {}\n"
                "}\n"
                "void B(std::string key) {\n"
                "  if (key.size() == 3) {}\n"
                "}\n"
                "void D(DataTypeSet s) {\n"
                "  s.size();\n"
                "}\n"
            ),
        },
    )
    assert _callees(g, "user.cc::A") == set()
    assert _callees(g, "user.cc::B") == set()
    assert _external(g, "user.cc::A") == {"v.size"}
    assert _external(g, "user.cc::B") == {"key.size"}
    assert _callees(g, "user.cc::D") == {"types.h::DataTypeSet.size"}


def test_namespaced_parameter_type_narrows_to_its_member(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            "fw/tensor.h": (
                "namespace tensorflow {\n"
                "class Tensor {\n"
                " public:\n"
                "  int dtype() const { return 0; }\n"
                "};\n"
                "class Variant {\n"
                " public:\n"
                "  int dtype() const { return 1; }\n"
                "};\n"
                "}\n"
            ),
            "k/kernel.cc": (
                '#include "fw/tensor.h"\n'
                "namespace tensorflow {\n"
                "int Kind(const Tensor& t) { return t.dtype(); }\n"
                "}\n"
            ),
        },
    )
    assert _callees(g, "k/kernel.cc::tensorflow.Kind") == {
        "fw/tensor.h::tensorflow.Tensor.dtype"
    }


def test_cast_to_a_repo_type_reaches_its_member(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "fw/ctx.h": (
                "namespace tf {\n"
                "class OpContext {\n"
                " public:\n"
                "  int device() { return 0; }\n"
                "};\n"
                "class Env {\n"
                " public:\n"
                "  int device() { return 1; }\n"
                "};\n"
                "}\n"
            ),
            "k/run.cc": (
                '#include "fw/ctx.h"\n'
                "namespace tf {\n"
                "int Run(void* p) {\n"
                "  return reinterpret_cast<OpContext*>(p)->device();\n"
                "}\n"
                "int Peek(const void* p) {\n"
                "  return static_cast<const OpContext*>(p)\n"
                "      ->device();\n"
                "}\n"
                "}\n"
            ),
        },
    )
    for caller in ("k/run.cc::tf.Run", "k/run.cc::tf.Peek"):
        assert _callees(g, caller) == {"fw/ctx.h::tf.OpContext.device"}
