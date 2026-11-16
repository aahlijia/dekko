"""An unqualified C/C++ type name is looked up from where it is written.

``Status s;`` in a ``tf::data`` file names what C++ lookup finds from
there: a ``using ns::Status;`` in the file, else a ``Status`` in the
same file, else the one in the innermost scope the file's definitions
sit in (``tf``'s alias, not ``tf::exp``'s class). A name with a scope
in front of it (``model::Node``) keeps its by-path reading, and a name
no visible scope declares keeps the old nearest-by-path pick.
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


# ``tf::Status`` is an alias of the outside ``absl::Status``; the repo
# also has an unrelated class ``tf::exp::Status``.
_STATUS = {
    "status.h": (
        "namespace absl { class Status; }\n"
        "namespace tf {\n"
        "using Status = ::absl::Status;\n"
        "}\n"
    ),
    "exp_status.h": (
        "namespace tf {\n"
        "namespace exp {\n"
        "class Status {\n"
        " public:\n"
        "  bool ok() { return true; }\n"
        "};\n"
        "}\n"
        "}\n"
    ),
}


def test_bare_name_takes_the_innermost_visible_scope(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            **_STATUS,
            "data.cc": (
                '#include "status.h"\n'
                '#include "exp_status.h"\n'
                "namespace tf {\n"
                "namespace data {\n"
                "void Run() {\n"
                "  Status s;\n"
                "  s.ok();\n"
                "}\n"
                "}\n"
                "}\n"
            ),
            "exp.cc": (
                '#include "status.h"\n'
                '#include "exp_status.h"\n'
                "namespace tf {\n"
                "namespace exp {\n"
                "void Use() {\n"
                "  Status s;\n"
                "  s.ok();\n"
                "}\n"
                "}\n"
                "}\n"
            ),
        },
    )
    assert _callees(g, "data.cc::tf.data.Run") == set()
    assert _external(g, "data.cc::tf.data.Run") == {"s.ok"}
    assert _callees(g, "exp.cc::tf.exp.Use") == {
        "exp_status.h::tf.exp.Status.ok"
    }


def test_no_visible_scope_keeps_the_nearest_pick(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "lib/exp_status.h": _STATUS["exp_status.h"],
            "lib/use.cc": (
                '#include "exp_status.h"\n'
                "using namespace tf::exp;\n"
                "void Use() {\n"
                "  Status s;\n"
                "  s.ok();\n"
                "}\n"
            ),
        },
    )
    assert _callees(g, "lib/use.cc::Use") == {
        "lib/exp_status.h::tf.exp.Status.ok"
    }


def test_file_using_declaration_wins(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "tensor.h": (
                "namespace tf {\n"
                "class Tensor {\n"
                " public:\n"
                "  int dtype() { return 0; }\n"
                "};\n"
                "namespace exp {\n"
                "class Tensor {\n"
                " public:\n"
                "  int dtype() { return 1; }\n"
                "};\n"
                "}\n"
                "}\n"
            ),
            "handle_test.cc": (
                '#include "tensor.h"\n'
                "using tf::exp::Tensor;\n"
                "namespace tf {\n"
                "void T() {\n"
                "  Tensor t;\n"
                "  t.dtype();\n"
                "}\n"
                "}\n"
            ),
        },
    )
    assert _callees(g, "handle_test.cc::tf.T") == {
        "tensor.h::tf.exp.Tensor.dtype"
    }


def test_scoped_token_keeps_its_path_reading(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "core/graph.h": (
                "namespace tf {\n"
                "class Node {\n"
                " public:\n"
                "  int name() { return 0; }\n"
                "};\n"
                "}\n"
            ),
            "data/model.h": (
                "namespace tf {\n"
                "namespace model {\n"
                "class Node {\n"
                " public:\n"
                "  int name() { return 1; }\n"
                "};\n"
                "}\n"
                "}\n"
            ),
            "data/use.cc": (
                "#include <memory>\n"
                '#include "core/graph.h"\n'
                '#include "data/model.h"\n'
                "namespace tf {\n"
                "void T(std::shared_ptr<model::Node> n) {\n"
                "  n->name();\n"
                "}\n"
                "}\n"
            ),
        },
    )
    assert _callees(g, "data/use.cc::tf.T") == {
        "data/model.h::tf.model.Node.name"
    }
