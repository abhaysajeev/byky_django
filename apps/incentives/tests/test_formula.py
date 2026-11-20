"""The formula language (design/09-incentives.md 4.1): what it allows, what it
refuses, and that it computes in Decimal."""

from decimal import Decimal

import pytest

from apps.incentives import formula
from apps.incentives.formula import FormulaError

D = Decimal


@pytest.mark.parametrize(("text", "values", "result"), [
    ("basis * 1%", {"basis": D("1250")}, D("12.50")),
    ("basis * 1% / max(tier2, 2)", {"basis": D("1250"), "tier2": D("1")}, D("6.25")),
    ("basis * 1% / max(tier2, 2)", {"basis": D("1250"), "tier2": D("3")}, D("1250") / 100 / 3),
    ("0.5 %", {}, D("0.005")),
    ("achieved >= target", {"achieved": D("100"), "target": D("100")}, D(1)),
    ("achieved >= target", {"achieved": D("99.99"), "target": D("100")}, D(0)),
    ("10 if role == 'primary' else 5", {"role": "labour"}, D(5)),
    ("round(10 / 3, 2)", {}, D("3.33")),
    ("abs(-2) + min(1, 2) - max(0, 1)", {}, D(2)),
    ("is_labour and tier2 > 1", {"is_labour": D(1), "tier2": D(2)}, D(1)),
    ("not is_primary", {"is_primary": D(1)}, D(0)),
    ("(1 + 2) * 3", {}, D(9)),
])
def test_it_computes(text, values, result):
    assert formula.evaluate(text, values) == result


def test_decimal_is_exact():
    assert formula.evaluate("0.1 + 0.2", {}) == D("0.3")


@pytest.mark.parametrize(("text", "message"), [
    ("__import__('os')", "Only these functions"),
    ("basis.__class__", "can't be used"),
    ("[1, 2][0]", "can't be used"),
    ("basis ** 2", r"Only \+ - \* /"),
    ("open('x')", "Only these functions"),
    ("lambda: 1", "can't be used"),
    ("basis % 2", r"Only \+ - \* /"),
    ("(x for x in y)", "can't be used"),
    ("basis *", "not complete"),
    ("", "Enter a formula"),
    ("True", "Only numbers"),
    ("min()", "Give min"),
])
def test_it_refuses(text, message):
    with pytest.raises(FormulaError, match=message):
        formula.parse(text)


def test_unknown_names_are_named():
    with pytest.raises(FormulaError, match="Unknown variable: basiss"):
        formula.check("basiss * 1%", {"basis"})


def test_divide_by_zero_is_a_message_not_a_crash():
    with pytest.raises(FormulaError, match="Divides by zero"):
        formula.evaluate("basis / work_days", {"basis": D(10), "work_days": D(0)})


def test_text_is_only_compared():
    with pytest.raises(FormulaError, match="only be compared"):
        formula.evaluate("role * 2", {"role": "labour"})
