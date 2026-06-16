"""Unit tests for the shared budgeting seam (Meter + fit_to_budget)."""

from dekko.textutil import Meter, fit_to_budget

# Each line is 40 chars => ~10 estimated tokens (len // 4).
LINES = ["x" * 40 for _ in range(5)]


def test_no_budget_no_limit_keeps_all() -> None:
    kept, meter = fit_to_budget(LINES, budget=None, limit=None)
    assert kept == LINES
    assert meter.returned == 5
    assert meter.total == 5
    assert meter.omitted == 0
    assert meter.truncated_by is None


def test_budget_trims_from_the_end() -> None:
    # line1 ~10 tok, line1+line2 ~20 tok, +line3 ~30 tok > 25.
    kept, meter = fit_to_budget(LINES, budget=25, limit=None)
    assert kept == LINES[:2]
    assert meter.returned == 2
    assert meter.omitted == 3
    assert meter.truncated_by == "budget"


def test_floor_keeps_one_row_under_tiny_budget() -> None:
    kept, meter = fit_to_budget(LINES, budget=1, limit=None)
    assert kept == LINES[:1]
    assert meter.returned == 1
    assert meter.truncated_by == "budget"


def test_limit_bites_before_budget() -> None:
    kept, meter = fit_to_budget(LINES, budget=None, limit=2)
    assert kept == LINES[:2]
    assert meter.truncated_by == "limit"


def test_budget_below_limit_reports_budget() -> None:
    _, meter = fit_to_budget(LINES, budget=25, limit=4)
    assert meter.returned == 2
    assert meter.truncated_by == "budget"


def test_empty_input() -> None:
    kept, meter = fit_to_budget([], budget=100, limit=10)
    assert kept == []
    assert meter.total == 0
    assert meter.omitted == 0
    assert meter.truncated_by is None


def test_prefix_counts_against_budget() -> None:
    # 80-char prefix ~20 tok already eats most of a 25-tok budget.
    _, meter = fit_to_budget(LINES, budget=25, limit=None, prefix="y" * 80)
    assert meter.returned == 1
    assert meter.tokens >= 20


def test_footer_without_omissions() -> None:
    meter = Meter(tokens=10, returned=5, total=5)
    assert meter.footer() == "(~10 tokens)"


def test_footer_with_omissions_names_the_cap() -> None:
    meter = Meter(tokens=20, returned=2, total=5, budget=25)
    assert meter.footer() == ("(~20 tokens · 3 of 5 omitted · raise --budget)")
    limited = Meter(tokens=20, returned=2, total=5, limit=2)
    assert "raise --limit" in limited.footer()


def test_as_dict_shape() -> None:
    meter = Meter(tokens=20, returned=2, total=5, budget=25, limit=4)
    assert meter.as_dict() == {
        "tokens": 20,
        "returned": 2,
        "total": 5,
        "budget": 25,
        "limit": 4,
        "truncated_by": "budget",
        "signals": 0,
        "tokens_per_signal": None,
    }
