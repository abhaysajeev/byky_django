"""{{ value|amount }} -- money grouped in thousands at the company's decimals
("50,000.00"), for reading figures side by side. {{ value|signed }} adds a
"+" to a positive difference. Load with {% load targets %}."""

from decimal import Decimal, InvalidOperation

from django import template

from core import money as money_rules

register = template.Library()


def _grouped(value):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return value
    text = money_rules.fmt(number)
    whole, _, fraction = text.lstrip("-").partition(".")
    grouped = f"{int(whole):,}"
    sign = "-" if text.startswith("-") and number != 0 else ""
    return f"{sign}{grouped}.{fraction}" if fraction else f"{sign}{grouped}"


@register.filter
def amount(value):
    if value is None or value == "":
        return "—"
    return _grouped(value)


@register.filter
def signed(value):
    if value is None or value == "":
        return "—"
    text = _grouped(value)
    return f"+{text}" if Decimal(str(value)) > 0 else text.replace("-", "−", 1)


@register.filter
def bar(value):
    """A progress width, 0..100."""
    try:
        return max(0, min(100, float(value)))
    except (TypeError, ValueError):
        return 0


@register.filter
def figure(value, is_customers=False):
    """A target figure: a whole count of customers ("1,250"), else AED (|amount)."""
    if value is None or value == "":
        return "—"
    if is_customers:
        try:
            return f"{int(Decimal(str(value))):,}"
        except (InvalidOperation, ValueError):
            return value
    return _grouped(value)
