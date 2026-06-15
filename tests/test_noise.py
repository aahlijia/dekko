"""B6: minor-file collapse, test grouping, and ``--order``."""

from dekko.model import CallGraph, Edge, FileMap, Symbol
from dekko.render_md import render_markdown


def _sym(path: str, name: str, line: int = 1) -> Symbol:
    return Symbol(
        id=f"{path}::{name}",
        name=name,
        qualname=name,
        kind="function",
        path=path,
        language="python",
        start_line=line,
        end_line=line + 1,
    )


# --- minor-file collapse ---------------------------------------------------


def test_minor_files_collapse_to_also_present() -> None:
    core = FileMap("pkg/core.py", "python", symbols=[_sym("pkg/core.py", "f")])
    data = FileMap("pkg/data.txt", "text", symbols=[])  # no syms/doc/error
    text = render_markdown([core, data], CallGraph(), "demo")
    assert "*also present:* `data.txt`" in text
    # The minor file gets no `##` section of its own.
    assert "`pkg/data.txt`" not in text


def test_docless_zero_symbol_is_minor_but_doc_keeps_section() -> None:
    documented = FileMap(
        "pkg/__init__.py", "python", symbols=[], doc="Package root."
    )
    text = render_markdown([documented], CallGraph(), "demo")
    # A 0-symbol file with a docstring is NOT minor: it keeps a bullet
    # (carrying the purpose) and a section.
    assert "*also present:*" not in text
    assert "Package root." in text


# --- test grouping ---------------------------------------------------------


def test_test_files_grouped_in_details() -> None:
    prod = FileMap("src/a.py", "python", symbols=[_sym("src/a.py", "f")])
    test = FileMap(
        "tests/test_a.py",
        "python",
        symbols=[_sym("tests/test_a.py", "test_f")],
    )
    text = render_markdown([prod, test], CallGraph(), "demo")
    assert "<details><summary>tests (1 files)</summary>" in text
    at = text.index("<details>")
    # Production file is in the open list; the test file is inside the
    # collapsed block.
    assert text.index("`a.py`") < at
    assert text.index("`test_a.py`") > at


# --- ordering --------------------------------------------------------------


def test_default_path_order_preserves_input() -> None:
    fz = FileMap("z.py", "python", symbols=[_sym("z.py", "f")])
    fa = FileMap("a.py", "python", symbols=[_sym("a.py", "g")])
    text = render_markdown([fz, fa], CallGraph(), "demo")
    assert text.index('id="z-py"') < text.index('id="a-py"')


def test_order_name_sorts_sections_by_basename() -> None:
    fz = FileMap("z.py", "python", symbols=[_sym("z.py", "f")])
    fa = FileMap("a.py", "python", symbols=[_sym("a.py", "g")])
    text = render_markdown([fz, fa], CallGraph(), "demo", order="name")
    assert text.index('id="a-py"') < text.index('id="z-py"')


def test_order_fan_in_orders_files_and_symbols() -> None:
    hub = FileMap(
        "hub.py",
        "python",
        symbols=[_sym("hub.py", "helper", 1), _sym("hub.py", "core", 5)],
    )
    leaf = FileMap(
        "leaf.py",
        "python",
        symbols=[_sym("leaf.py", "c1", 1), _sym("leaf.py", "c2", 5)],
    )
    # calls_in/out are resolver-populated in the real flow, not derived
    # from edges; build them so core has fan-in 2 and helper 0.
    graph = CallGraph(
        edges=[
            Edge(caller="leaf.py::c1", callee="hub.py::core", lines=[2]),
            Edge(caller="leaf.py::c2", callee="hub.py::core", lines=[6]),
        ],
        calls_out={
            "leaf.py::c1": ["hub.py::core"],
            "leaf.py::c2": ["hub.py::core"],
        },
        calls_in={"hub.py::core": ["leaf.py::c1", "leaf.py::c2"]},
    )
    # Input order is leaf-then-hub; fan-in must flip it and float the
    # load-bearing symbol to the top of its file.
    text = render_markdown([leaf, hub], graph, "demo", order="fan-in")
    assert text.index('id="hub-py"') < text.index('id="leaf-py"')
    assert text.index('id="hub-py-core"') < text.index('id="hub-py-helper"')
