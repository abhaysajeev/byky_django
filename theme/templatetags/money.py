"""{{ value|money }} -- money with the company's decimals (core/money.py,
CR1). Load with {% load money %}. Text that is not a number is shown as is."""

from decimal import Decimal, InvalidOperation

from django import template

from core import money as money_rules

register = template.Library()


@register.filter
def money(value):
    if value is None or value == "":
        return value
    try:
        return money_rules.fmt(Decimal(str(value).replace(",", "")))
    except (InvalidOperation, ValueError):
        return value


@register.simple_tag
def money_places():
    """This request's decimals, for data-money-places."""
    return money_rules.current_places()
