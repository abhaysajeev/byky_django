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

from contextvars import ContextVar
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

# The setting for this request: set once by MoneyMiddleware (web, from the
# signed-in company) and by the app token's authentication (the session's
# station, else the company), so the format helpers need not be told.
# Outside a request -- tasks, the shell -- it stays DEFAULT.
_current = ContextVar("byky_money", default=DEFAULT)


def activate(setting):
    return _current.set(setting or DEFAULT)


def reset(token):
    _current.reset(token)


def current():
    return _current.get()


def current_places():
    return _current.get().decimal_places


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


def company_for_user(user):
    """The company whose settings a signed-in user's screens follow: their own,
    or -- for a system user, who has none -- the only company when there is
    exactly one (BYKY today). With several, None: the defaults apply, as
    there is no one right answer (owner, 3 Nov 2026)."""
    company = getattr(user, "company", None)
    if company is not None or not getattr(user, "is_authenticated", False):
        return company
    from core.enums import UserScope

    if getattr(user, "scope", None) != UserScope.SYSTEM:
        return None
    from apps.company.models import Company

    only = list(Company.objects.all()[:2])
    return only[0] if len(only) == 1 else None


def places_for(company, branch=None):
    return money_for(company, branch).decimal_places


def _exp(places):
    return Decimal(1).scaleb(-places)


def quantize(value, places=None):
    """`value` to `places` decimals (default: this request's), half up. None
    stays None."""
    if value is None:
        return None
    places = current_places() if places is None else places
    return Decimal(value).quantize(_exp(places), rounding=ROUND_HALF_UP)


def fmt(value, places=None):
    """`value` as a string with exactly `places` decimals ("75.00"; default:
    this request's); None stays None."""
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


# -- Incoming amounts (CR1) ------------------------------------------------------------
# A money field is a serializer DecimalField with 3 decimal places (the
# database's): percentages (2) and star ratings are left alone. With the
# company on 2 decimals, "75.255" is refused rather than silently cut.


def _too_precise(field, value, places, path):
    from rest_framework import serializers

    if value is None:
        return []
    if isinstance(field, serializers.ListSerializer | serializers.ListField):
        return [hit for i, item in enumerate(value)
                for hit in _too_precise(field.child, item, places, f"{path}[{i}]")]
    if isinstance(field, serializers.Serializer):
        found = []
        for name, child in field.fields.items():
            if isinstance(value, dict) and name in value:
                found += _too_precise(child, value[name], places, f"{path}.{name}" if path else name)
        return found
    if (isinstance(field, serializers.DecimalField) and field.decimal_places == DEFAULT_PLACES
            and isinstance(value, Decimal) and places_of(value) > places):
        return [path]
    return []


def refuse_too_precise(form, values=None, places=None):
    """Raise a DRF ValidationError for any money field in a validated `form`
    with more decimals than this request's setting allows."""
    from rest_framework.exceptions import ValidationError

    places = current_places() if places is None else places
    if places >= DEFAULT_PLACES:
        return
    found = _too_precise(form, form.validated_data if values is None else values, places, "")
    if found:
        raise ValidationError({path: [f"must have at most {places} decimal places"] for path in found})
