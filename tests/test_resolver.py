"""End-to-end resolution tests over the language fixtures."""

from pathlib import Path

from lidar_map.cli import map_repository
from lidar_map.model import FileMap, RawCall, Symbol
from lidar_map.resolver import resolve

FIXTURES = Path(__file__).parent / "fixtures"


def _fn(
    path: str, name: str, qual: str | None = None, line: int = 1
) -> Symbol:
    qual = qual or name
    return Symbol(
        id=f"{path}::{qual}",
        name=name,
        qualname=qual,
        kind="method" if "." in qual else "function",
        path=path,
        language="python",
        start_line=line,
        end_line=line + 1,
    )


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


def test_common_name_resolves_same_file_not_ambiguous() -> None:
    # ``run`` is defined in many files; a same-file call must still bind
    # to the local definition, not become ambiguous.
    files = [
        FileMap(
            path=f"mod{i}.py",
            language="python",
            symbols=[_fn(f"mod{i}.py", "run")],
        )
        for i in range(20)
    ]
    files.append(
        FileMap(
            path="caller.py",
            language="python",
            symbols=[
                _fn("caller.py", "run"),
                _fn("caller.py", "entry", line=5),
            ],
            calls=[
                RawCall(
                    caller_id="caller.py::entry",
                    path="caller.py",
                    text="run",
                    name="run",
                    line=6,
                )
            ],
        )
    )
    graph = resolve(files)
    edges = {(e.caller, e.callee) for e in graph.edges}
    assert ("caller.py::entry", "caller.py::run") in edges
    assert ("caller.py::entry", "mod0.py::run") not in edges
    assert graph.ambiguous == []


def test_self_container_resolves_with_like_named_elsewhere() -> None:
    # ``self.h()`` resolves to the calling class's method even when ``h``
    # exists elsewhere in the repo.
    cls = FileMap(
        path="c.py",
        language="python",
        symbols=[
            _fn("c.py", "C", "C"),
            _fn("c.py", "h", "C.h", line=2),
            _fn("c.py", "m", "C.m", line=4),
        ],
        calls=[
            RawCall(
                caller_id="c.py::C.m",
                path="c.py",
                text="self.h",
                name="h",
                receiver="self",
                line=5,
            )
        ],
    )
    other = FileMap(
        path="other.py", language="python", symbols=[_fn("other.py", "h")]
    )
    graph = resolve([cls, other])
    edges = {(e.caller, e.callee) for e in graph.edges}
    assert ("c.py::C.m", "c.py::C.h") in edges


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
