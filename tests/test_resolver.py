"""End-to-end resolution tests over the language fixtures."""

from pathlib import Path

from lidar import map_repository
from resolver import resolve

FIXTURES = Path(__file__).parent / "fixtures"


def _edges(root: Path) -> set[tuple[str, str]]:
    files, _ = map_repository(
        root,
        subpath=None,
        excludes=(),
        max_file_size=1_000_000,
    )
    graph = resolve(files)
    return {(e.caller, e.callee) for e in graph.edges}


def test_python_resolution() -> None:
    edges = _edges(FIXTURES / "python")
    assert ("main.py::run", "util.py::helper") in edges
    assert ("main.py::run", "util.py::Config") in edges
    assert ("main.py::run", "util.py::Config.validate") in edges
    assert ("util.py::Config.validate", "util.py::Config.load") in edges
    assert ("main.py::<module>", "main.py::run") in edges


def test_rust_resolution() -> None:
    edges = _edges(FIXTURES / "rust")
    assert ("main.rs::main", "lib.rs::Point.new") in edges
    assert ("main.rs::main", "lib.rs::Point.dist") in edges
    assert ("main.rs::main", "lib.rs::norm") in edges
    assert ("lib.rs::Point.dist", "lib.rs::norm") in edges


def test_external_calls_recorded() -> None:
    files, _ = map_repository(
        FIXTURES / "rust",
        subpath=None,
        excludes=(),
        max_file_size=1_000_000,
    )
    graph = resolve(files)
    externals = {text for _, text in graph.external}
    assert any("sqrt" in text for text in externals)
