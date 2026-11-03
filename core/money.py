"""Money: how many decimals, and the bill's round-off -- per company and
country (owner, 3 Nov 2026 -- client change request 1, the Configurations
page, apps/company/models.py CompanyMoneySetting).

Which setting applies: a station uses its own country's row
(branch.location.country); without a station (manager and employee apps, the
web) the company's country. No row: 3 decimals, upward, 0.25 -- the behaviour
before the setting existed.

Values are always Decimal; this module only says how many places to keep and
show, and checks what comes in. The database keeps 3 places, so 2 and 3 both
fit.
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

DEFAULT_PLACES = 3


@dataclass(frozen=True)
class Money:
    decimal_places: int = DEFAULT_PLACES
    round_off_mode: str = "upward"
    round_off_step: Decimal = Decimal("0.250")

    def as_json(self):
        return {"decimal_places": self.decimal_places, "round_off_mode": self.round_off_mode,
                "round_off_step": fmt(self.round_off_step, self.decimal_places)}


DEFAULT = Money()


def _country_id(company, branch):
    if branch is not None:
        location = getattr(branch, "location", None)
        if location is not None:
            return location.country_id
    return getattr(company, "country_id", None)


def money_for(company, branch=None):
    """The Money that applies to `company` (at `branch`, if given)."""
    from apps.company.models import CompanyMoneySetting

    if company is None:
        return DEFAULT
    country_id = _country_id(company, branch)
    row = (CompanyMoneySetting.objects.filter(company_id=company.pk, country_id=country_id).first()
           if country_id else None)
    if row is None:
        return DEFAULT
    return Money(row.decimal_places, row.round_off_mode, row.round_off_step)


def places_for(company, branch=None):
    return money_for(company, branch).decimal_places


def _exp(places):
    return Decimal(1).scaleb(-places)


def quantize(value, places=DEFAULT_PLACES):
    """`value` to `places` decimals, half up. None stays None."""
    if value is None:
        return None
    return Decimal(value).quantize(_exp(places), rounding=ROUND_HALF_UP)


def fmt(value, places=DEFAULT_PLACES):
    """`value` as a string with exactly `places` decimals ("75.00"); None
    stays None."""
    if value is None:
        return None
    return str(quantize(value, places))


def places_of(value):
    """How many decimals a Decimal carries, ignoring trailing zeros
    (75.500 -> 1)."""
    exponent = Decimal(value).normalize().as_tuple().exponent
    return max(-exponent, 0) if isinstance(exponent, int) else 0


def check_places(data, places, path=""):
    """[(field path, value)] for every Decimal in `data` (dicts and lists,
    nested) with more than `places` decimals."""
    found = []
    if isinstance(data, Decimal):
        if data.is_finite() and places_of(data) > places:
            found.append((path, data))
    elif isinstance(data, dict):
        for key, value in data.items():
            found += check_places(value, places, f"{path}.{key}" if path else str(key))
    elif isinstance(data, list | tuple):
        for index, value in enumerate(data):
            found += check_places(value, places, f"{path}[{index}]")
    return found


def too_precise_message(found, places):
    field, _ = found[0]
    return f"{field} must have at most {places} decimal places."


def check_round_off(net, adjustment, money):
    """None when the settle's round-off follows the setting, else why not:
    the net is a whole number of steps, and the adjustment goes the setting's
    way by less than a step (half a step, either way, for nearest)."""
    step = Decimal(money.round_off_step)
    net, adjustment = Decimal(net), Decimal(adjustment)
    if net % step != 0:
        return f"net_amount must be a multiple of the round-off step {fmt(step, money.decimal_places)}."
    mode = money.round_off_mode
    if mode == "upward" and not (Decimal(0) <= adjustment < step):
        return f"rounding_adjustment must be 0 or more and less than {fmt(step, money.decimal_places)} (upward)."
    if mode == "downward" and not (-step < adjustment <= Decimal(0)):
        return f"rounding_adjustment must be 0 or less and more than -{fmt(step, money.decimal_places)} (downward)."
    if mode == "nearest" and abs(adjustment) > step / 2:
        return f"rounding_adjustment must be within half a step ({fmt(step / 2, money.decimal_places + 1)}) (nearest)."
    return None
