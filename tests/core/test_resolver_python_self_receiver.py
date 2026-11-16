"""A Python ``self``/``cls`` call is decided by the caller's class.

``self.skipTest()`` in a ``unittest.TestCase`` subclass is unittest's
method, not the repo's one ``skipTest`` on an unrelated class: the
class is walked (own members, inherited, a subclass's for a template
hook), and a method none of them has is external. A supertype written
``mod.Name`` is read in the module ``mod`` names, through its own
imports and star imports, never by the bare ``Name`` across the repo.
A class whose supertype the map can't place keeps the old reading.
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


_OTHER = (
    "class Other:\n"
    "    def skipTest(self, why):\n"
    "        return why\n"
    "\n"
    "    def evaluate(self, x):\n"
    "        return x\n"
)


def test_an_outside_base_class_method_is_external(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "other.py": _OTHER,
            "t_test.py": (
                "import unittest\n"
                "\n"
                "\n"
                "class T2(unittest.TestCase):\n"
                "    def test_it(self):\n"
                '        self.skipTest("slow")\n'
            ),
        },
    )
    caller = "t_test.py::T2.test_it"

    assert _callees(g, caller) == set()
    assert "self.skipTest" in _external(g, caller)


def test_a_supertype_is_read_through_its_module_reexport(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            "other.py": _OTHER,
            "pkg/__init__.py": "",
            "pkg/framework/__init__.py": "",
            "pkg/framework/test_util.py": (
                "class TensorFlowTestCase:\n"
                "    def evaluate(self, x):\n"
                "        return x\n"
            ),
            "pkg/platform/__init__.py": "",
            "pkg/platform/test.py": (
                "from pkg.framework.test_util import TensorFlowTestCase "
                "as TestCase\n"
            ),
            "pkg/lookalike.py": (
                "class TestCase:\n"
                "    def evaluate(self, x):\n"
                "        return x\n"
            ),
            "pkg/ops_test.py": (
                "from pkg.platform import test\n"
                "\n"
                "\n"
                "class OpsTest(test.TestCase):\n"
                "    def test_it(self):\n"
                "        self.evaluate(1)\n"
            ),
        },
    )

    assert _callees(g, "pkg/ops_test.py::OpsTest.test_it") == {
        "pkg/framework/test_util.py::TensorFlowTestCase.evaluate"
    }


def test_a_supertype_from_an_outside_star_import_is_outside(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            "other.py": _OTHER,
            "pkg/__init__.py": "",
            "pkg/platform/__init__.py": "",
            "pkg/platform/googletest.py": (
                "from absl.testing.absltest import *\n"
            ),
            "pkg/platform/test.py": (
                "from pkg.platform.googletest import TestCase\n"
            ),
            "pkg/ops_test.py": (
                "from pkg.platform import test\n"
                "\n"
                "\n"
                "class OpsTest(test.TestCase):\n"
                "    def test_it(self):\n"
                '        self.skipTest("slow")\n'
            ),
        },
    )
    caller = "pkg/ops_test.py::OpsTest.test_it"

    assert _callees(g, caller) == set()
    assert "self.skipTest" in _external(g, caller)


def test_a_mixin_reaches_a_method_its_host_inherits(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "other.py": (
                "class Other:\n"
                "    def assertProtoEquals(self, a, b):\n"
                "        return a == b\n"
            ),
            "base.py": (
                "class TfCase:\n"
                "    def assertProtoEquals(self, a, b):\n"
                "        return a == b\n"
            ),
            "writer_test.py": (
                "from base import TfCase\n"
                "\n"
                "\n"
                "class WriterTestBase(object):\n"
                "    def check(self):\n"
                "        self.assertProtoEquals(1, 1)\n"
                "\n"
                "\n"
                "class WriterTest(WriterTestBase, TfCase):\n"
                "    pass\n"
            ),
        },
    )

    assert _callees(g, "writer_test.py::WriterTestBase.check") == {
        "base.py::TfCase.assertProtoEquals"
    }


def test_a_template_hook_reaches_the_subclass(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "other.py": (
                "class Other:\n    def hook(self):\n        return 0\n"
            ),
            "base.py": (
                "class Base:\n"
                "    def run(self):\n"
                "        return self.hook()\n"
                "\n"
                "\n"
                "class Impl(Base):\n"
                "    def hook(self):\n"
                "        return 1\n"
            ),
        },
    )

    assert _callees(g, "base.py::Base.run") == {"base.py::Impl.hook"}


def test_a_nested_class_built_through_self_keeps_its_edge(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            "coord.py": (
                "class Coordinator:\n"
                "    class _Creator:\n"
                "        def __init__(self, n):\n"
                "            self.n = n\n"
                "\n"
                "    def make(self):\n"
                "        return self._Creator(1)\n"
            ),
        },
    )
    caller = "coord.py::Coordinator.make"

    assert _external(g, caller) == set()
    assert _callees(g, caller) & {
        "coord.py::Coordinator._Creator",
        "coord.py::Coordinator._Creator.__init__",
    }


def test_an_unread_supertype_keeps_the_old_reading(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "other.py": _OTHER,
            "t_test.py": (
                "helpers = __import__('helpers')\n"
                "\n"
                "\n"
                "class T3(helpers.Base):\n"
                "    def test_it(self):\n"
                "        self.evaluate(1)\n"
            ),
        },
    )

    assert _callees(g, "t_test.py::T3.test_it") == {"other.py::Other.evaluate"}


def test_a_template_hook_reaches_a_grandchild(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "other.py": (
                "class Other:\n    def _add_loss(self, x):\n        return x\n"
            ),
            "layers.py": (
                "class Layer:\n"
                "    def add_loss(self, x):\n"
                "        return self._add_loss(x)\n"
                "\n"
                "\n"
                "class Model(Layer):\n"
                "    pass\n"
                "\n"
                "\n"
                "class Functional(Model):\n"
                "    def _add_loss(self, x):\n"
                "        return x\n"
            ),
        },
    )

    assert _callees(g, "layers.py::Layer.add_loss") == {
        "layers.py::Functional._add_loss"
    }
