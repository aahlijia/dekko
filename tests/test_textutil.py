"""Direct unit tests for ``textutil.signature``/``oneline``/``dir_of``.

These three functions had no direct unit-test call anywhere in the
suite (only wide indirect exercise through renderer/summary tests
that assert on whole rendered strings, per the sc:analyze
post-0.31.1 fixes plan item 4) -- ``estimate_tokens``/``count_lines``/
``Meter``/``fit_to_budget`` are already covered directly by
``test_tokenizer.py``/``test_budget.py``/``test_density.py``, so
they're out of scope here.
"""

from dekko.core.model import Param, Symbol
from dekko.textutil import dir_of, oneline, signature


def _symbol(**overrides: object) -> Symbol:
    """A minimal ``Symbol`` with sane defaults, one field overridden."""
    defaults: dict = {
        "id": "a.py::f",
        "name": "f",
        "qualname": "f",
        "kind": "function",
        "path": "a.py",
        "language": "python",
    }
    defaults.update(overrides)
    return Symbol(**defaults)


# --- signature() ------------------------------------------------------


def test_signature_type_kind() -> None:
    sym = _symbol(
        id="a.py::Widget",
        name="Widget",
        qualname="Widget",
        kind="class",
    )
    assert signature(sym) == "class Widget"


def test_signature_interface_type_kind() -> None:
    sym = _symbol(
        id="a.ts::Config",
        name="Config",
        qualname="Config",
        kind="interface",
        language="typescript",
    )
    assert signature(sym) == "interface Config"


def test_signature_variable() -> None:
    sym = _symbol(
        id="a.ts::jobs",
        name="jobs",
        qualname="jobs",
        kind="variable",
        language="typescript",
    )
    assert signature(sym) == "jobs"


def test_signature_module_anonymous_placeholder() -> None:
    # The synthetic <anonymous> placeholder for a module-caller bucket
    # entry (bug #4) -- see the inline comment at textutil.py:23-27.
    sym = _symbol(id="a.py::<module>", kind="module", path="a.py")
    assert signature(sym) == "<anonymous> (a.py)"


def test_signature_function_no_params_no_return() -> None:
    sym = _symbol()
    assert signature(sym) == "f()"


def test_signature_function_typed_params() -> None:
    sym = _symbol(
        params=[Param(name="x", type="int"), Param(name="y", type=None)]
    )
    assert signature(sym) == "f(x: int, y)"


def test_signature_function_with_return_type() -> None:
    sym = _symbol(
        params=[Param(name="x", type="int")],
        returns="str",
    )
    assert signature(sym) == "f(x: int) -> str"


def test_signature_method_kind_uses_qualname() -> None:
    sym = _symbol(
        id="a.py::Widget.render",
        name="render",
        qualname="Widget.render",
        kind="method",
    )
    assert signature(sym) == "Widget.render()"


# --- oneline() ----------------------------------------------------------


def test_oneline_empty_string() -> None:
    assert oneline("") == ""


def test_oneline_whitespace_only() -> None:
    assert oneline("   \n\t  ") == ""


def test_oneline_single_short_line() -> None:
    assert oneline("hello world") == "hello world"


def test_oneline_keeps_only_first_line() -> None:
    assert oneline("first line\nsecond line\nthird") == "first line"


def test_oneline_strips_leading_and_trailing_whitespace() -> None:
    assert oneline("  padded line  \nmore") == "padded line"


def test_oneline_truncates_at_limit_with_ellipsis() -> None:
    text = "x" * 100
    result = oneline(text, limit=10)
    assert result == "x" * 9 + "…"
    assert len(result) == 10


def test_oneline_custom_limit() -> None:
    result = oneline("abcdefghij", limit=5)
    assert result == "abcd…"
    assert len(result) == 5


def test_oneline_exactly_at_limit_is_not_truncated() -> None:
    text = "x" * 80
    assert oneline(text) == text


# --- dir_of() -------------------------------------------------------


def test_dir_of_root_level_file() -> None:
    assert dir_of("foo.py") == "."


def test_dir_of_nested_path() -> None:
    assert dir_of("src/dekko/foo.py") == "src/dekko"


def test_dir_of_single_level_nesting() -> None:
    assert dir_of("src/foo.py") == "src"


def test_dir_of_path_with_no_slash() -> None:
    assert dir_of("README.md") == "."
