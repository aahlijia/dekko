"""A C++ call on a typed local is walked from the local's declared type.

``ShapedType t = ..; t.getElementType()`` is MLIR's method, not the
repo's one ``getElementType``. The extractor records the type a local
receiver was declared with (``RawCall.receiver_type``): the written
type, or ``T`` for ``auto x = cast<T>(..)``. A plain ``auto x = f()``
records nothing, the innermost declaration before the call wins, and
one after it doesn't count. The resolver walks that type like a typed
parameter's: an outside type sends the call external, an in-repo type
narrows it to that type's members.
"""

from dataclasses import asdict
from pathlib import Path

from dekko.core import languages
from dekko.core.extractor import extract_file
from dekko.core.model import CallGraph, RawCall
from dekko.core.resolver import resolve
from dekko.repo_ops import map_repository
from dekko.storage.cache import _rawcall_from_dict


def _calls(tmp_path: Path, source: str) -> dict[tuple[str, int], RawCall]:
    (tmp_path / "f.cc").write_text(source)
    fm = extract_file(tmp_path, "f.cc", languages.CPP)
    assert fm.error is None
    return {(c.text, c.line): c for c in fm.calls}


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


def test_a_local_records_its_written_type_or_cast_target(
    tmp_path: Path,
) -> None:
    calls = _calls(
        tmp_path,
        "void F(mlir::Value v, void* p) {\n"
        "  mlir::ShapedType t = v.getType();\n"
        "  t.getElementType();\n"
        "  auto c = llvm::cast<mlir::TensorType>(v);\n"
        "  c.getElementType();\n"
        "  auto* k = static_cast<const OpContext*>(p);\n"
        "  k->device();\n"
        "  auto r = Make();\n"
        "  r.run();\n"
        "}\n",
    )
    assert calls["t.getElementType", 3].receiver_type == "mlir::ShapedType"
    assert calls["c.getElementType", 5].receiver_type == "mlir::TensorType"
    assert calls["k->device", 7].receiver_type == "const OpContext*"
    assert calls["r.run", 9].receiver_type is None
    assert calls["v.getType", 2].receiver_type is None


def test_the_innermost_declaration_before_the_call_wins(
    tmp_path: Path,
) -> None:
    calls = _calls(
        tmp_path,
        "void F() {\n"
        "  x.early();\n"
        "  Outer x;\n"
        "  x.outer();\n"
        "  {\n"
        "    Inner x;\n"
        "    x.inner();\n"
        "    {\n"
        "      auto x = Make();\n"
        "      x.untyped();\n"
        "    }\n"
        "  }\n"
        "  x.again();\n"
        "}\n",
    )
    assert calls["x.early", 2].receiver_type is None
    assert calls["x.outer", 4].receiver_type == "Outer"
    assert calls["x.inner", 7].receiver_type == "Inner"
    assert calls["x.untyped", 10].receiver_type is None
    assert calls["x.again", 13].receiver_type == "Outer"


def test_receiver_type_survives_the_extraction_cache(
    tmp_path: Path,
) -> None:
    calls = _calls(
        tmp_path,
        "void F() {\n  Tensor t;\n  t.dtype();\n}\n",
    )
    call = calls["t.dtype", 3]
    assert _rawcall_from_dict(asdict(call)) == call


_MLIR_TENSOR = (
    "namespace tf {\n"
    "class MlirTensor {\n"
    " public:\n"
    "  int getElementType() { return 0; }\n"
    "};\n"
    "}\n"
)


def test_calls_on_outside_typed_locals_go_external(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "t/mlir_tensor.h": _MLIR_TENSOR,
            "u/user.cc": (
                '#include "mlir/IR/BuiltinTypes.h"\n'
                "void A(mlir::Value v) {\n"
                "  mlir::ShapedType t = v.getType();\n"
                "  t.getElementType();\n"
                "}\n"
                "void B(mlir::Value v) {\n"
                "  auto t = mlir::cast<mlir::TensorType>(v.getType());\n"
                "  t.getElementType();\n"
                "}\n"
                "void C(mlir::Operation* op) {\n"
                "  mlir::Value v = op->getResult(0);\n"
                "  v.getType().getElementType();\n"
                "}\n"
            ),
        },
    )
    for caller in ("u/user.cc::A", "u/user.cc::B", "u/user.cc::C"):
        assert _callees(g, caller) == set()
        assert any(n.endswith("getElementType") for n in _external(g, caller))


def test_a_repo_typed_local_narrows_to_its_member(tmp_path: Path) -> None:
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
            "k/kernel.cc": (
                '#include "fw/tensor.h"\n'
                "namespace tensorflow {\n"
                "int Kind() {\n"
                "  Tensor t;\n"
                "  return t.dtype();\n"
                "}\n"
                "int Run(void* p) {\n"
                "  auto* c = static_cast<OpContext*>(p);\n"
                "  return c->device();\n"
                "}\n"
                "}\n"
            ),
        },
    )
    assert _callees(g, "k/kernel.cc::tensorflow.Kind") == {
        "fw/tensor.h::tensorflow.Tensor.dtype"
    }
    assert _callees(g, "k/kernel.cc::tensorflow.Run") == {
        "fw/tensor.h::tensorflow.OpContext.device"
    }
