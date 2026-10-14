"""A C/C++ type written with a scope is read by that whole path.

``absl::Status`` is not the repo's ``tf::experimental::Status``: a
declared type that names its namespaces finds only the in-repo types
on that path. Several on it take the innermost one the writing file's
own scopes reach; none, and the path goes through an in-repo type
(a member alias) the walk can't read, so the ladder decides; none,
and the head namespace holds a type of that name (a namespace-level
alias), that type; otherwise the type is outside the repo. A
template is judged by its outer path, unless the outer name is a
wrapper whose argument is the type.
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


_REPO_STATUS = (
    "namespace tf {\n"
    "namespace experimental {\n"
    "class Status {\n"
    " public:\n"
    "  bool ok() const { return true; }\n"
    "};\n"
    "}\n"
    "}\n"
)


def test_outside_qualified_field_and_return_go_external(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            "lib/status.h": _REPO_STATUS,
            "w/writer.h": (
                "namespace tf {\n"
                "class Writer {\n"
                " public:\n"
                "  bool Done();\n"
                "  bool Flushed();\n"
                "  absl::Status Finish();\n"
                " private:\n"
                "  absl::Status status_;\n"
                "};\n"
                "}\n"
            ),
            "w/writer.cc": (
                '#include "w/writer.h"\n'
                "namespace tf {\n"
                "bool Writer::Done() { return status_.ok(); }\n"
                "bool Writer::Flushed() { return Finish().ok(); }\n"
                "}\n"
            ),
        },
    )
    for caller in (
        "w/writer.cc::tf.Writer.Done",
        "w/writer.cc::tf.Writer.Flushed",
    ):
        assert _callees(g, caller) == set()
        assert any(n.endswith(".ok") for n in _external(g, caller))


_GRAPH_VIEWS = {
    "g/view.h": (
        "namespace a {\n"
        "namespace internal {\n"
        "class GraphView {\n"
        " public:\n"
        "  int GetNode() { return 0; }\n"
        "};\n"
        "}\n"
        "}\n"
    ),
    "g/utils/view.h": (
        "namespace a {\n"
        "namespace utils {\n"
        "namespace internal {\n"
        "class GraphView {\n"
        " public:\n"
        "  int GetNode() { return 1; }\n"
        "};\n"
        "}\n"
        "}\n"
        "}\n"
    ),
}


def test_scoped_type_takes_the_innermost_enclosing_namespace(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            **_GRAPH_VIEWS,
            "g/user.h": (
                "namespace a {\n"
                "namespace utils {\n"
                "class User {\n"
                " public:\n"
                "  int Run();\n"
                " private:\n"
                "  internal::GraphView* view_;\n"
                "};\n"
                "}\n"
                "}\n"
            ),
            "g/user.cc": (
                '#include "g/user.h"\n'
                "namespace a {\n"
                "namespace utils {\n"
                "int User::Run() { return view_->GetNode(); }\n"
                "}\n"
                "}\n"
            ),
        },
    )
    assert _callees(g, "g/user.cc::a.utils.User.Run") == {
        "g/utils/view.h::a.utils.internal.GraphView.GetNode"
    }


def test_path_through_a_repo_class_is_left_to_the_ladder(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            "x/info.h": (
                "namespace tf {\n"
                "class RealInfo {\n"
                " public:\n"
                "  int Ids() const { return 0; }\n"
                "};\n"
                "class Outer {\n"
                " public:\n"
                "  using Info = RealInfo;\n"
                "};\n"
                "}\n"
            ),
            "y/use.h": (
                '#include "x/info.h"\n'
                "namespace tf {\n"
                "class Use {\n"
                " public:\n"
                "  int Run();\n"
                " private:\n"
                "  Outer::Info info_;\n"
                "};\n"
                "}\n"
            ),
            "y/use.cc": (
                '#include "y/use.h"\n'
                "namespace tf {\n"
                "int Use::Run() { return info_.Ids(); }\n"
                "}\n"
            ),
        },
    )
    assert _callees(g, "y/use.cc::tf.Use.Run") == {"x/info.h::tf.RealInfo.Ids"}


def test_head_namespace_alias_reaches_the_type_inside_it(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            "lite/core/interp.h": (
                "namespace tflite {\n"
                "namespace impl {\n"
                "class Interp {\n"
                " public:\n"
                "  int Run() { return 0; }\n"
                "};\n"
                "}\n"
                "}\n"
            ),
            "app/interp.h": (
                "namespace other {\n"
                "class Interp {\n"
                " public:\n"
                "  int Run() { return 1; }\n"
                "};\n"
                "}\n"
            ),
            "app/user.h": (
                "namespace app {\n"
                "class User {\n"
                " public:\n"
                "  int Go();\n"
                " private:\n"
                "  tflite::Interp* interp_;\n"
                "};\n"
                "}\n"
            ),
            "app/user.cc": (
                '#include "app/user.h"\n'
                "namespace app {\n"
                "int User::Go() { return interp_->Run(); }\n"
                "}\n"
            ),
        },
    )
    assert _callees(g, "app/user.cc::app.User.Go") == {
        "lite/core/interp.h::tflite.impl.Interp.Run"
    }


def test_outside_template_is_judged_by_its_outer_path(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            "rt/span.h": (
                "namespace mlrt {\n"
                "namespace bc {\n"
                "template <typename T>\n"
                "class Span {\n"
                " public:\n"
                "  int size() const { return 0; }\n"
                "};\n"
                "}\n"
                "}\n"
            ),
            "k/user.h": (
                "namespace k {\n"
                "class User {\n"
                " public:\n"
                "  int Count();\n"
                " private:\n"
                "  absl::Span<const int> ids_;\n"
                "};\n"
                "}\n"
            ),
            "k/user.cc": (
                '#include "k/user.h"\n'
                "namespace k {\n"
                "int User::Count() { return ids_.size(); }\n"
                "}\n"
            ),
        },
    )
    assert _callees(g, "k/user.cc::k.User.Count") == set()
    assert _external(g, "k/user.cc::k.User.Count") == {"ids_.size"}


def test_wrapper_template_still_reads_its_argument(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "m/foo.h": (
                "class Foo {\n public:\n  void Go() {}\n};\n"
                "class Bar {\n public:\n  void Go() {}\n};\n"
            ),
            "m/user.h": (
                '#include "m/foo.h"\n'
                "#include <memory>\n"
                "class User {\n"
                " public:\n"
                "  void Run();\n"
                " private:\n"
                "  std::unique_ptr<Foo> foo_;\n"
                "};\n"
            ),
            "m/user.cc": (
                '#include "m/user.h"\nvoid User::Run() { foo_->Go(); }\n'
            ),
        },
    )
    assert _callees(g, "m/user.cc::User.Run") == {"m/foo.h::Foo.Go"}
