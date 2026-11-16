"""A bare C/C++ type name reads a nested type only where C++ sees it.

A member type is visible by its bare name inside its enclosing class,
in a class derived from it, and in a file that defines the enclosing
class's members. Anywhere else the bare name is some other type:
protobuf's ``NodeDef`` is not tflite's ``GraphFloat32::NodeDef``, so a
call on it must not land on the repo's lone method of that name.
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


# The repo's only ``NodeDef`` is nested in a tflite class, and its only
# ``add_input`` is an unrelated class's method.
_NESTED_NODEDEF = {
    "model.h": (
        "namespace tflite {\n"
        "class GraphFloat32 {\n"
        " public:\n"
        "  struct NodeDef {\n"
        "    int Id() { return 0; }\n"
        "  };\n"
        "};\n"
        "}\n"
    ),
    "node.h": (
        "namespace data {\n"
        "class Node {\n"
        " public:\n"
        "  void add_input(int x) {}\n"
        "};\n"
        "}\n"
    ),
}


def test_nested_type_is_not_read_from_an_unrelated_scope(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            **_NESTED_NODEDEF,
            "rewrite.cc": (
                '#include "model.h"\n'
                '#include "node.h"\n'
                "namespace tf {\n"
                "void Rewrite(NodeDef* d) { d->add_input(1); }\n"
                "void Local() {\n"
                "  NodeDef x;\n"
                "  x.add_input(2);\n"
                "}\n"
                "}\n"
            ),
        },
    )
    assert _callees(g, "rewrite.cc::tf.Rewrite") == set()
    assert _callees(g, "rewrite.cc::tf.Local") == set()


def test_nested_type_is_read_in_its_class_and_a_derived_class(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            "graph.h": (
                "namespace tflite {\n"
                "class GraphFloat32 {\n"
                " public:\n"
                "  struct NodeDef {\n"
                "    void Attach() {}\n"
                "  };\n"
                "  void Own() {\n"
                "    NodeDef n;\n"
                "    n.Attach();\n"
                "  }\n"
                "};\n"
                "}\n"
            ),
            "sub.h": (
                '#include "graph.h"\n'
                "namespace tflite {\n"
                "class SubGraph : public GraphFloat32 {\n"
                " public:\n"
                "  void Use() {\n"
                "    NodeDef n;\n"
                "    n.Attach();\n"
                "  }\n"
                "};\n"
                "}\n"
            ),
        },
    )
    attach = "graph.h::tflite.GraphFloat32.NodeDef.Attach"
    assert _callees(g, "graph.h::tflite.GraphFloat32.Own") == {attach}
    assert _callees(g, "sub.h::tflite.SubGraph.Use") == {attach}


def test_nested_type_is_read_where_its_class_is_defined(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            "holder.h": (
                "namespace a {\n"
                "class Holder {\n"
                " public:\n"
                "  struct Part {\n"
                "    void Go() {}\n"
                "  };\n"
                "  void Run();\n"
                "  Part* part_;\n"
                "};\n"
                "}\n"
            ),
            "holder.cc": (
                '#include "holder.h"\n'
                "namespace a {\n"
                "void Holder::Run() { part_->Go(); }\n"
                "}\n"
            ),
        },
    )
    assert _callees(g, "holder.cc::a.Holder.Run") == {
        "holder.h::a.Holder.Part.Go"
    }


def test_file_with_an_out_of_line_class_keeps_nested_types(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            "base.h": (
                "namespace d {\n"
                "class Base {\n"
                " public:\n"
                "  class Builder {\n"
                "   public:\n"
                "    void Add() {}\n"
                "  };\n"
                "};\n"
                "}\n"
            ),
            "op.cc": (
                '#include "base.h"\n'
                "namespace d {\n"
                "class Op {\n"
                " public:\n"
                "  class Dataset;\n"
                "};\n"
                "class Op::Dataset : public Base {\n"
                " public:\n"
                "  void Make(Builder* b) { b->Add(); }\n"
                "};\n"
                "}\n"
            ),
        },
    )
    callers = {e.caller for e in g.edges if e.callee.endswith("Builder.Add")}
    assert callers == {"op.cc::d.Op::Dataset.Make"}


def test_member_class_with_no_symbol_is_not_another_classes_type(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            "other.h": (
                "namespace o {\n"
                "class Other {\n"
                " public:\n"
                "  class Impl {\n"
                "   public:\n"
                "    int M() const { return 2; }\n"
                "  };\n"
                "};\n"
                "}\n"
            ),
            "client.h": (
                "namespace c {\n"
                "class ClientSession {\n"
                " public:\n"
                "  void Run();\n"
                "\n"
                " private:\n"
                "  class Impl;\n"
                "  Impl* impl_;\n"
                "};\n"
                "}\n"
            ),
            "client.cc": (
                '#include "client.h"\n'
                '#include "other.h"\n'
                "namespace c {\n"
                "class ClientSession::Impl {\n"
                " public:\n"
                "  int M() const { return 1; }\n"
                "};\n"
                "void ClientSession::Run() { impl_->M(); }\n"
                "}\n"
            ),
        },
    )
    callees = _callees(g, "client.cc::c.ClientSession.Run")
    assert "other.h::o.Other.Impl.M" not in callees
    assert {c.split("::", 1)[0] for c in callees} == {"client.cc"}


def test_name_no_repo_type_has_stays_outside(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "canvas.h": (
                "namespace ui {\n"
                "class Canvas {\n"
                " public:\n"
                "  void Draw() {}\n"
                "};\n"
                "}\n"
            ),
            "paint.cc": (
                '#include "canvas.h"\n'
                "namespace ui {\n"
                "void Paint(Widget* w) { w->Draw(); }\n"
                "}\n"
            ),
        },
    )
    assert _callees(g, "paint.cc::ui.Paint") == set()
