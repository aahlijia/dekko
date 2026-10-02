"""C++ construction sites: ``new T(...)``, ``make_unique<T>``, ``= delete``.

A C++ class is constructed through ``new``, the standard smart-pointer
factories, or a call-shaped temporary. The first two are not
``call_expression``s with the class as callee, so each needs its own
extraction rule; a deleted function declares it can't be called and is
not a symbol at all.
"""

from pathlib import Path

import pytest

from dekko.core import languages
from dekko.core.extractor import extract_file
from dekko.core.model import FileMap, RawCall


def _extract(tmp_path: Path, body: str) -> FileMap:
    spec = languages.spec_for_path("user.cc")
    assert spec is not None
    (tmp_path / "user.cc").write_text(f"void Build() {{\n{body}\n}}\n")
    fm = extract_file(tmp_path, "user.cc", spec)
    assert fm.error is None
    return fm


def _calls(fm: FileMap) -> list[tuple[str, str | None, int | None]]:
    return [(c.name, c.receiver, c.arg_count) for c in fm.calls]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("new T(a);", ("T", None, 1)),
        ("new ns::T(a);", ("T", "ns", 1)),
        ("new T<int>(a);", ("T", None, 1)),
        ("new ns::T<int>(a);", ("T", "ns", 1)),
        ("new T{a, b};", ("T", None, 2)),
        ("new T;", ("T", None, 0)),
        ("new T[4];", ("T", None, 0)),
        ("new (buf) T(a);", ("T", None, 1)),
    ],
)
def test_new_expression_is_a_construction_call(
    tmp_path: Path,
    source: str,
    expected: tuple[str, str | None, int | None],
) -> None:
    fm = _extract(tmp_path, f"  auto* p = {source}")
    assert _calls(fm) == [expected]


def test_new_of_a_qualified_template_drops_its_arguments_from_the_text(
    tmp_path: Path,
) -> None:
    fm = _extract(tmp_path, "  auto* p = new ns::Box<int>(3);")
    assert [c.text for c in fm.calls] == ["ns::Box"]


@pytest.mark.parametrize("source", ["new int[5];", "new unsigned long;"])
def test_new_of_a_builtin_type_is_not_a_call(
    tmp_path: Path, source: str
) -> None:
    fm = _extract(tmp_path, f"  auto* p = {source}")
    assert fm.calls == []


def _constructions(fm: FileMap) -> list[RawCall]:
    return [c for c in fm.calls if "make_" not in c.name]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("std::make_unique<T>(a, b)", ("T", None, 2)),
        ("absl::make_unique<T>()", ("T", None, 0)),
        ("std::make_shared<ns::T<int>>(x)", ("T", "ns", 1)),
    ],
)
def test_standard_factory_also_constructs_its_type_argument(
    tmp_path: Path,
    source: str,
    expected: tuple[str, str | None, int | None],
) -> None:
    fm = _extract(tmp_path, f"  auto p = {source};")
    factories = [c for c in fm.calls if "make_" in c.name]
    assert len(factories) == 1
    assert factories[0].receiver in ("std", "absl")
    built = _constructions(fm)
    assert [(c.name, c.receiver, c.arg_count) for c in built] == [expected]
    assert built[0].line == factories[0].line
    assert built[0].caller_id == factories[0].caller_id


@pytest.mark.parametrize(
    "source",
    [
        "foo::make_unique<T>()",
        "std::make_unique<T, U>()",
        "std::make_unique<int>(3)",
        "std::make_unique<T[]>(3)",
        "std::make_pair<T>(a)",
    ],
)
def test_other_factory_shapes_construct_nothing(
    tmp_path: Path, source: str
) -> None:
    fm = _extract(tmp_path, f"  auto p = {source};")
    assert _constructions(fm) == []


def _symbols(tmp_path: Path, body: str) -> list[str]:
    spec = languages.spec_for_path("graph.hpp")
    assert spec is not None
    (tmp_path / "graph.hpp").write_text(f"class Graph {{\n{body}\n}};\n")
    fm = extract_file(tmp_path, "graph.hpp", spec)
    assert fm.error is None
    return [s.qualname for s in fm.symbols]


def test_deleted_functions_are_not_symbols(tmp_path: Path) -> None:
    qualnames = _symbols(
        tmp_path,
        "  Graph(const Graph&) = delete;\n"
        "  void operator=(const Graph&) = delete;\n",
    )
    assert qualnames == ["Graph"]


def test_defaulted_functions_stay_symbols(tmp_path: Path) -> None:
    qualnames = _symbols(tmp_path, "  Graph(const Graph&) = default;\n")
    assert qualnames == ["Graph", "Graph.Graph"]
