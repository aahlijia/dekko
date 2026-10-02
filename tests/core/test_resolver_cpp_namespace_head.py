"""A one-scope C++ ``ns::Name`` call only reaches what ``ns`` holds.

When the written scope is a namespace, a candidate counts only when
its qualname ends with ``ns.Name`` or a namespace-scope
``using``-declaration re-exports it into ``ns``; the ladder then picks
among those. ``std::`` never reaches the repo. A lone survivor has to
fit the call's argument count. Type heads and heads the map never saw
resolve the way they always have.
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


_USE = "u/user.cc::tensorflow.Use"


def _user(body: str, includes: str = "") -> str:
    return (
        f"{includes}namespace tensorflow {{\nvoid Use() {{\n{body}\n}}\n}}\n"
    )


# ``absl`` is a namespace the repo knows (it defines a type there), but
# ``OkStatus`` isn't in it.
_ABSL = "namespace absl { struct Hasher {}; }\n"


def test_foreign_namespace_does_not_reach_a_repo_namesake(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            "lib/absl_hash.h": _ABSL,
            "lib/status.cc": "namespace tensorflow { int OkStatus() {} }\n",
            "u/user.cc": _user("  absl::OkStatus();"),
        },
    )
    assert _pairs(graph, _USE) == set()
    assert _externals(graph, _USE) == {"absl::OkStatus"}


def test_std_call_never_reaches_a_repo_specialization(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        {
            "lib/types.h": (
                "namespace std {\n"
                "template <> class numeric_limits<Half> {\n"
                " public:\n"
                "  static Half max() {}\n"
                "};\n"
                "}\n"
            ),
            # The include is what lets the stem rung pick it today.
            "u/user.cc": _user(
                "  std::max(1, 2);", includes='#include "lib/types.h"\n'
            ),
        },
    )
    assert _pairs(graph, _USE) == set()
    assert _externals(graph, _USE) == {"std::max"}


def test_call_picks_the_namesake_in_the_written_namespace(
    tmp_path: Path,
) -> None:
    # The same-file optimized version would win the ladder; the call
    # names reference_ops.
    graph = _graph(
        tmp_path,
        {
            "lib/ref.h": (
                "namespace tflite { namespace reference_ops {\n"
                "void Resize(int a, int b) {}\n"
                "} }\n"
            ),
            "u/user.cc": (
                "namespace tflite { namespace optimized_ops {\n"
                "void Resize(int a, int b) {}\n"
                "void Use() {\n"
                "  reference_ops::Resize(1, 2);\n"
                "}\n"
                "} }\n"
            ),
        },
    )
    caller = "u/user.cc::tflite.optimized_ops.Use"
    assert _pairs(graph, caller) == {"lib/ref.h::tflite.reference_ops.Resize"}


def test_lone_namesake_fitting_its_header_default_becomes_the_edge(
    tmp_path: Path,
) -> None:
    # The header gives ``encoding`` a default the ``.cc`` definition
    # doesn't show; read with it, a 2-argument call fits.
    graph = _graph(
        tmp_path,
        {
            "tensorflow/utils/dynamic_shape_utils.h": (
                "namespace tensorflow {\n"
                "int GetType(int shape, int type, int encoding = 0);\n"
                "}\n"
            ),
            "tensorflow/utils/dynamic_shape_utils.cc": (
                "namespace tensorflow {\n"
                "int GetType(int shape, int type, int encoding) {}\n"
                "}\n"
            ),
            "lib/other.cc": "namespace other { int GetType(int a) {} }\n",
            "u/user.cc": (
                '#include "tensorflow/utils/dynamic_shape_utils.h"\n'
                "namespace mlir {\n"
                "void Use() {\n  tensorflow::GetType(1, 2);\n}\n"
                "}\n"
            ),
        },
    )
    assert _pairs(graph, "u/user.cc::mlir.Use") == {
        "tensorflow/utils/dynamic_shape_utils.cc::tensorflow.GetType"
    }


def test_unnarrowed_pick_outside_the_namespace_is_external(
    tmp_path: Path,
) -> None:
    # The generated ``ops::Identity(scope, x)`` isn't in the repo. The
    # one on its path takes five arguments, so the ladder runs over
    # every namesake, and the include hands it a test helper in another
    # namespace, which the written ``ops`` rules out.
    graph = _graph(
        tmp_path,
        {
            "tensorflow/c/ops/array_ops.cc": (
                "namespace tensorflow { namespace ops {\n"
                "int Identity(int ctx, int in, int out, int n, int d) {}\n"
                "} }\n"
            ),
            "tensorflow/graph/testlib.h": (
                "namespace tensorflow { namespace test { namespace graph {\n"
                "int Identity(int g, int x) {}\n"
                "} } }\n"
            ),
            "u/user.cc": _user(
                "  ops::Identity(s, x);",
                includes='#include "tensorflow/graph/testlib.h"\n',
            ),
        },
    )
    assert _pairs(graph, _USE) == set()
    assert _externals(graph, _USE) == {"ops::Identity"}


def test_lone_namesake_that_fits_becomes_the_edge(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        {
            "lib/tensor_util.cc": (
                "namespace tensorflow { namespace tensor {\n"
                "int Split(int a, int b, int c) {}\n"
                "} }\n"
            ),
            "lib/other.cc": "namespace other { int Split(int a) {} }\n",
            "lib/more.cc": "namespace more { int Split(int a, int b) {} }\n",
            "u/user.cc": _user("  tensor::Split(1, 2, 3);"),
        },
    )
    assert _pairs(graph, _USE) == {
        "lib/tensor_util.cc::tensorflow.tensor.Split"
    }
    assert _ambiguous_for(graph, _USE) == {}


def test_using_declaration_carries_a_name_into_the_namespace(
    tmp_path: Path,
) -> None:
    sources = {
        "lib/tsl_status.cc": "namespace tsl { int StatusFromTF(int s) {} }\n",
        "lib/other.cc": "namespace other { int StatusFromTF(int s) {} }\n",
        "u/user.cc": _user("  tensorflow::StatusFromTF(1);"),
    }
    reexport = (
        "namespace tensorflow {\nusing tsl::StatusFromTF;  // NOLINT\n}\n"
    )
    graph = _graph(tmp_path / "with", {**sources, "lib/helper.h": reexport})
    assert _pairs(graph, _USE) == {"lib/tsl_status.cc::tsl.StatusFromTF"}

    graph = _graph(tmp_path / "without", sources)
    assert _pairs(graph, _USE) == set()
    assert _externals(graph, _USE) == {"tensorflow::StatusFromTF"}


def test_no_candidate_in_the_namespace_leaves_no_ambiguous_row(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            "lib/xla_ns.h": "namespace xla { struct Shape {}; }\n",
            "lib/a.cc": "namespace a { int Mul(int x, int y) {} }\n",
            "lib/b.cc": "namespace b { int Mul(int x, int y) {} }\n",
            "u/user.cc": _user("  xla::Mul(1, 2);"),
        },
    )
    assert _pairs(graph, _USE) == set()
    assert _ambiguous_for(graph, _USE) == {}
    assert _externals(graph, _USE) == {"xla::Mul"}


def test_overloads_in_the_namespace_are_the_only_ambiguous_candidates(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            "lib/t1.cc": "namespace tensorflow { namespace test {\n"
            "int Close(int a, int b) {}\n} }\n",
            "lib/t2.cc": "namespace tensorflow { namespace test {\n"
            "int Close(int a, int b) {}\n} }\n",
            "lib/elsewhere.cc": "namespace x { int Close(int a, int b) {} }\n",
            "u/user.cc": _user("  test::Close(1, 2);"),
        },
    )
    assert _pairs(graph, _USE) == set()
    assert _ambiguous_for(graph, _USE) == {
        "Close": [
            "lib/t1.cc::tensorflow.test.Close",
            "lib/t2.cc::tensorflow.test.Close",
        ]
    }


def test_class_known_only_by_its_out_of_line_methods_counts(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            "lib/device_factory.cc": (
                "namespace tensorflow {\n"
                "int DeviceFactory::GetFactory(int t) {}\n"
                "}\n"
            ),
            "lib/other.cc": (
                "namespace tensorflow {\n"
                "int OtherRegistry::GetFactory(int t) {}\n"
                "}\n"
            ),
            "u/user.cc": _user("  DeviceFactory::GetFactory(1);"),
        },
    )
    assert _pairs(graph, _USE) == {
        "lib/device_factory.cc::tensorflow.DeviceFactory.GetFactory"
    }


def test_construction_through_a_namespace_reaches_the_class(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            "lib/shape.h": (
                "namespace tensorflow {\n"
                "class TensorShape {\n"
                " public:\n"
                "  TensorShape(int a, int b) {}\n"
                "};\n"
                "}\n"
            ),
            "lib/other.h": "namespace other { class TensorShape {}; }\n",
            "u/user.cc": _user("  tensorflow::TensorShape(1, 2);"),
        },
    )
    assert "lib/shape.h::tensorflow.TensorShape" in _pairs(graph, _USE)
    assert "lib/other.h::other.TensorShape" not in _pairs(graph, _USE)


def test_type_head_resolves_the_way_it_always_has(tmp_path: Path) -> None:
    # A static inherited from a base: ``TensorShape`` is a type, so the
    # namespace rule stays out of it.
    graph = _graph(
        tmp_path,
        {
            "lib/shape.h": (
                "namespace tensorflow {\n"
                "class TensorShapeBase {\n"
                " public:\n"
                "  static bool IsValid(int p) { return true; }\n"
                "};\n"
                "class TensorShape : public TensorShapeBase {};\n"
                "}\n"
            ),
            "u/user.cc": _user("  TensorShape::IsValid(1);"),
        },
    )
    assert _pairs(graph, _USE) == {
        "lib/shape.h::tensorflow.TensorShapeBase.IsValid"
    }


def test_unknown_head_resolves_the_way_it_always_has(tmp_path: Path) -> None:
    # ``f`` is a namespace alias the map doesn't follow; the ladder
    # still finds the only NDef.
    graph = _graph(
        tmp_path,
        {
            "lib/fn.cc": (
                "namespace tensorflow { namespace test {\n"
                "namespace function { int NDef(int a, int b) {} }\n"
                "} }\n"
            ),
            "u/user.cc": (
                "namespace f = tensorflow::test::function;\n"
                + _user("  f::NDef(1, 2);")
            ),
        },
    )
    assert _pairs(graph, _USE) == {"lib/fn.cc::tensorflow.test.function.NDef"}


def test_member_call_on_a_chain_is_not_a_namespace_call(
    tmp_path: Path,
) -> None:
    graph = _graph(
        tmp_path,
        {
            "lib/reg.h": (
                "namespace tf {\n"
                "class Registry {\n"
                " public:\n"
                "  static Registry* Global() { return nullptr; }\n"
                "  int LookUpOp(int x) { return x; }\n"
                "};\n"
                "}\n"
            ),
            "u/user.cc": _user("  tf::Registry::Global()->LookUpOp(1);"),
        },
    )
    assert "lib/reg.h::tf.Registry.LookUpOp" in _pairs(graph, _USE)


def test_same_file_out_of_line_constructor_still_wins(tmp_path: Path) -> None:
    # Two ``internal::Pool`` classes on the written path, in two
    # headers; the calling file defines one's constructor out of line,
    # and the ladder has always picked that one.
    pool = (
        "namespace {ns} {{ namespace internal {{\n"
        "class Pool {{\n public:\n  Pool(int a, int b);\n}};\n"
        "}} }}\n"
    )
    graph = _graph(
        tmp_path,
        {
            "tensorflow/run_handler.h": pool.format(ns="tensorflow"),
            "tfrt/run_handler.h": pool.format(ns="tfrt"),
            "tensorflow/run_handler.cc": (
                "namespace tensorflow {\n"
                "namespace internal {\n"
                "Pool::Pool(int a, int b) {}\n"
                "}\n"
                "void Use() {\n  new internal::Pool(1, 2);\n}\n"
                "}\n"
            ),
        },
    )
    caller = "tensorflow/run_handler.cc::tensorflow.Use"
    assert (
        "tensorflow/run_handler.cc::tensorflow.internal.Pool.Pool"
        in _pairs(graph, caller)
    )


def test_template_head_is_a_class_even_when_not_a_symbol(
    tmp_path: Path,
) -> None:
    # A specialization isn't extracted as a type, so only its methods'
    # qualnames carry its name; the call reaches its base's static.
    graph = _graph(
        tmp_path,
        {
            "lib/view.cc": (
                "namespace odml {\n"
                "class ViewBase {\n"
                " public:\n"
                "  static int Next(int i) { return i; }\n"
                "};\n"
                "template <typename T> class View;\n"
                "template <>\n"
                "class View<Attr> : ViewBase {\n"
                " public:\n"
                "  int Size() { return 0; }\n"
                "};\n"
                "void Use() {\n  View<Attr>::Next(1);\n}\n"
                "}\n"
            ),
        },
    )
    assert _pairs(graph, "lib/view.cc::odml.Use") == {
        "lib/view.cc::odml.ViewBase.Next"
    }
