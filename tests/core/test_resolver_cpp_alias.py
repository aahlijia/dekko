"""A C/C++ ``using``/``typedef`` alias is read through to what it names.

``using SumModel = BaseModel<3>; SumModel m; m.Invoke();`` is a call on
``BaseModel``. An alias is a symbol, but only the type walk sees it:
no call, reference, heritage clause or member lookup lands on one. It
is visible where C++ can see it (an alias in a source file only from
that file; a member alias by bare name only from inside its class), an
alias template's written arguments replace its parameters, and
``unused`` never lists one.
"""

from pathlib import Path

from dekko.core.model import CallGraph, FileMap
from dekko.core.resolver import resolve
from dekko.repo_ops import map_repository


def _files(root: Path, sources: dict[str, str]) -> list[FileMap]:
    for rel, text in sources.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    files, _ = map_repository(
        root,
        subpath=None,
        excludes=(),
        max_file_size=1_000_000,
    )
    return files


def _graph(root: Path, sources: dict[str, str]) -> CallGraph:
    return resolve(_files(root, sources), root=root)


def _callees(graph: CallGraph, caller: str) -> set[str]:
    return {e.callee for e in graph.edges if e.caller == caller}


def _external(graph: CallGraph, caller: str) -> set[str]:
    return {e.callee for e in graph.external if e.caller == caller}


_OTHER = (
    "class Other {\n"
    " public:\n"
    "  void Invoke() {}\n"
    "  void Run() {}\n"
    "  int Get() { return 0; }\n"
    "  int size() { return 0; }\n"
    "};\n"
)


def test_alias_of_a_template_reaches_its_base_method(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "other.h": _OTHER,
            "model.h": (
                "class SingleModel {\n"
                " public:\n"
                "  void Invoke() {}\n"
                "};\n"
                "template <int N>\n"
                "class BaseModel : public SingleModel {};\n"
            ),
            "reduce_test.cc": (
                '#include "model.h"\n'
                '#include "other.h"\n'
                "using SumModel = BaseModel<3>;\n"
                "void T() {\n"
                "  SumModel m;\n"
                "  m.Invoke();\n"
                "}\n"
            ),
        },
    )
    assert _callees(g, "reduce_test.cc::T") == {"model.h::SingleModel.Invoke"}


def test_namespace_typedef_reaches_the_type(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "other.h": _OTHER,
            "impl.h": (
                "namespace a {\n"
                "class Impl {\n"
                " public:\n"
                "  void Run() {}\n"
                "};\n"
                "typedef Impl Handle;\n"
                "}\n"
            ),
            "use.cc": (
                '#include "impl.h"\n'
                '#include "other.h"\n'
                "namespace a {\n"
                "void T(Handle* h) { h->Run(); }\n"
                "}\n"
            ),
        },
    )
    assert _callees(g, "use.cc::a.T") == {"impl.h::a.Impl.Run"}


def test_member_alias_written_as_a_path_reaches_the_type(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            "other.h": _OTHER,
            "outer.h": (
                "class Real {\n"
                " public:\n"
                "  int Get() { return 1; }\n"
                "};\n"
                "class Outer {\n"
                " public:\n"
                "  using Info = Real;\n"
                "};\n"
            ),
            "use.cc": (
                '#include "outer.h"\n'
                '#include "other.h"\n'
                "void T() {\n"
                "  Outer::Info i;\n"
                "  i.Get();\n"
                "}\n"
            ),
        },
    )
    assert _callees(g, "use.cc::T") == {"outer.h::Real.Get"}


def test_alias_of_a_std_template_is_read_as_a_template(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            "shape.h": (
                "#include <vector>\n"
                "class Shape {\n"
                " public:\n"
                "  int Dim() { return 0; }\n"
                "};\n"
                "using Shapes = std::vector<Shape>;\n"
            ),
            "use.cc": (
                '#include "shape.h"\n'
                "void T() {\n"
                "  Shapes v;\n"
                "  v.front().Dim();\n"
                "}\n"
            ),
        },
    )
    assert "shape.h::Shape.Dim" in _callees(g, "use.cc::T")


def test_alias_template_arguments_replace_its_parameters(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            "other.h": _OTHER,
            "trt.h": (
                "#include <memory>\n"
                "template <typename T>\n"
                "struct TrtDestroyer {\n"
                "  void operator()(T* t) {}\n"
                "};\n"
                "template <typename T>\n"
                "using TrtUniquePtrType =\n"
                "    std::unique_ptr<T, TrtDestroyer<T>>;\n"
                "class Engine {\n"
                " public:\n"
                "  int size() { return 1; }\n"
                "};\n"
            ),
            "use.cc": (
                '#include "trt.h"\n'
                '#include "other.h"\n'
                "void T() {\n"
                "  TrtUniquePtrType<Engine> e;\n"
                "  e->size();\n"
                "}\n"
            ),
        },
    )
    assert _callees(g, "use.cc::T") == {"trt.h::Engine.size"}


def test_typedef_struct_keeps_the_struct_id(tmp_path: Path) -> None:
    files = _files(
        tmp_path,
        {
            "point.hpp": (
                "typedef struct Point {\n"
                "  int x;\n"
                "} Point;\n"
                "typedef struct {\n"
                "  int y;\n"
                "} Anon;\n"
                "typedef Point Spot;\n"
            ),
        },
    )
    ids = {s.id: s.kind for fm in files for s in fm.symbols}
    assert ids["point.hpp::Point"] == "struct"
    assert "point.hpp::Point#2" not in ids
    assert "point.hpp::Anon" not in ids
    assert ids["point.hpp::Spot"] == "type_alias"


def test_alias_loop_and_long_chain_stay_unknown(tmp_path: Path) -> None:
    chain = (
        "".join(f"using A{i} = A{i + 1};\n" for i in range(1, 8))
        + "using A8 = Real;\n"
    )
    g = _graph(
        tmp_path,
        {
            "other.h": _OTHER,
            "real.h": (
                "class Real {\n"
                " public:\n"
                "  void Run() {}\n"
                "};\n"
                "using Loop = Back;\n"
                "using Back = Loop;\n"
                "using Short = Mid;\n"
                "using Mid = Real;\n" + chain
            ),
            "use.cc": (
                '#include "real.h"\n'
                '#include "other.h"\n'
                "void L(Loop x) { x.Run(); }\n"
                "void S(Short x) { x.Run(); }\n"
                "void C(A1 x) { x.Run(); }\n"
            ),
        },
    )
    assert _callees(g, "use.cc::S") == {"real.h::Real.Run"}
    for caller in ("use.cc::L", "use.cc::C"):
        assert _callees(g, caller) == set()
        assert _external(g, caller) == set()


def test_alias_is_never_a_call_or_heritage_target(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "base.h": (
                "class Base {\n"
                " public:\n"
                "  void Ref() {}\n"
                "};\n"
                "using BaseAlias = Base;\n"
                "class Holder {\n"
                " public:\n"
                "  typedef Base Ref;\n"
                "};\n"
                "class D : public BaseAlias {};\n"
            ),
            "use.cc": ('#include "base.h"\nvoid T(Base* h) { h->Ref(); }\n'),
        },
    )
    assert _callees(g, "use.cc::T") == {"base.h::Base.Ref"}
    assert not any(e.supertype_id == "base.h::BaseAlias" for e in g.heritage)


def test_alias_is_visible_only_where_cpp_sees_it(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "other.h": _OTHER,
            "dev.h": (
                "class Device {\n"
                " public:\n"
                "  void Sync() {}\n"
                "};\n"
                "class GPUDevice {\n"
                " public:\n"
                "  void Sync() {}\n"
                "};\n"
                "class Kernel {\n"
                " public:\n"
                "  typedef GPUDevice Device;\n"
                "  void K(Device* d) { d->Sync(); }\n"
                "};\n"
            ),
            "a.cc": (
                '#include "dev.h"\n'
                "using Device = Other;\n"
                "void A(Device* d) { d->Run(); }\n"
            ),
            "b.cc": ('#include "dev.h"\nvoid B(Device* d) { d->Sync(); }\n'),
        },
    )
    assert _callees(g, "a.cc::A") == {"other.h::Other.Run"}
    assert _callees(g, "b.cc::B") == {"dev.h::Device.Sync"}
