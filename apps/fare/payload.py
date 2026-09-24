"""The fare form's JSON, both ways.

parse() turns what the browser posts into a pricing.FareSpec plus the
identifiers the service needs (meta), reporting format problems -- a date that
is not a date, a time that is not HH:MM -- in the message modal's shape.
serialise() turns a saved fare back into the same JSON, for the edit screen.

The shape:

  {pk, lock_version, company, vehicle_type, level, branches: [id],
   valid_from, valid_to, is_active, package_minutes, base: PRICE,
   rules:   [{key, id, kind, weekdays: [0-6], on_date, start, end, price: PRICE}],
   seasons: [{key, id, name, start_date, end_date, base: PRICE, rules: [...]}]}

  PRICE = {base_fare, grace_minutes, concurrent_interval_minutes,
           concurrent_fare, concurrent_grace_minutes}

Dates are ISO, times "HH:MM" ("24:00", or "00:00" as an end, is midnight),
money a decimal string, weekdays company.WeekDay (Monday is 0). `key` names a
row in messages: "r12"/"s3" for saved rows, anything else for new ones.
"""

import datetime
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from apps.fare import pricing
from apps.fare.models import FareLevel, RuleKind
from apps.fare.pricing import MIDNIGHT, FareSpec, Issue, Price, Rule, Season

PRICE_FIELDS = (
    ("base_fare", "Basic fare", "money"),
    ("grace_minutes", "Grace period", "minutes"),
    ("concurrent_interval_minutes", "Concurrent interval", "minutes"),
    ("concurrent_fare", "Concurrent fare", "money"),
    ("concurrent_grace_minutes", "Concurrent grace", "minutes"),
)
TIME = re.compile(r"^([01]\d|2[0-4]):([0-5]\d)$")
MAX_MONEY = Decimal("9999999999.99")   # DecimalField(max_digits=12, decimal_places=2)
CENTS = Decimal("0.01")


@dataclass
class Parsed:
    spec: FareSpec | None
    errors: list
    meta: dict = field(default_factory=dict)


class _Reader:
    """Collects every format problem instead of stopping at the first."""

    def __init__(self):
        self.errors = []

    def fail(self, key, where, message):
        self.errors.append(Issue(key, where, message))
        return None

    def whole(self, value, key, where, *, minimum=0, maximum=32767):
        try:
            number = int(str(value).strip())
        except (TypeError, ValueError):
            return self.fail(key, where, "Enter a whole number of minutes.")
        if not minimum <= number <= maximum:
            return self.fail(key, where, f"Enter a number from {minimum} to {maximum}.")
        return number

    def money(self, value, key, where):
        try:
            amount = Decimal(str(value).strip())
        except (InvalidOperation, TypeError, ValueError):
            return self.fail(key, where, "Enter an amount, such as 50.00.")
        if not amount.is_finite() or amount.as_tuple().exponent < -2:
            return self.fail(key, where, "Use at most two decimal places.")
        if abs(amount) > MAX_MONEY:
            return self.fail(key, where, "That amount is too large.")
        return amount.quantize(CENTS)                 # "80" and "80.00" are one price

    def date(self, value, key, where, *, required=True):
        if value in (None, ""):
            return self.fail(key, where, "Pick a date.") if required else None
        try:
            return datetime.date.fromisoformat(str(value))
        except ValueError:
            return self.fail(key, where, "Pick a date.")

    def minute(self, value, key, where, *, end=False):
        match = TIME.match(str(value or "").strip())
        if not match:
            return self.fail(key, where, "Use a 24-hour time such as 08:30.")
        minutes = int(match[1]) * 60 + int(match[2])
        if minutes > MIDNIGHT or (minutes == MIDNIGHT and not end):
            return self.fail(key, where, "Use a 24-hour time such as 08:30.")
        if end and minutes == 0:
            return MIDNIGHT                      # "00:00" as an end means midnight
        return minutes

    def price(self, data, key, where):
        data = data if isinstance(data, dict) else {}
        values = {}
        for name, label, kind in PRICE_FIELDS:
            place = f"{where} · {label}"
            if kind == "money":
                values[name] = self.money(data.get(name), key, place)
            else:
                values[name] = self.whole(data.get(name), key, place)
        if any(v is None for v in values.values()):
            return None
        return Price(**values)


def _key(row, prefix):
    key = str(row.get("key") or "").strip()
    if key:
        return key
    return f"{prefix}{row.get('id')}" if row.get("id") else ""


def _id(value):
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _rules(reader, rows, owner, meta, season_key=None):
    rules = []
    for index, row in enumerate(rows if isinstance(rows, list) else []):
        if not isinstance(row, dict):
            reader.fail("", owner, "A special price could not be read.")
            continue
        key = _key(row, "r") or f"{season_key or 'fare'}-new-{index}"
        kind = row.get("kind")
        where = f"{owner} · {pricing.KIND_LABELS.get(kind, 'Special price')}"
        if kind not in RuleKind.values:
            reader.fail(key, where, "Choose when this price applies.")
            continue
        weekdays = set()
        if kind == RuleKind.SELECTED_DAYS:
            for day in row.get("weekdays") or []:
                number = _id(day)
                if number is None or not 0 <= number <= 6:
                    reader.fail(key, where, "A day could not be read.")
                else:
                    weekdays.add(number)
        on_date = reader.date(row.get("on_date"), key, where) if kind == RuleKind.SINGLE_DATE else None
        start = reader.minute(row.get("start"), key, f"{where} · From")
        end = reader.minute(row.get("end"), key, f"{where} · To", end=True)
        price = reader.price(row.get("price"), key, where)
        meta["rule_ids"][key] = _id(row.get("id"))
        meta["rule_season"][key] = season_key
        if None in (start, end, price) or (kind == RuleKind.SINGLE_DATE and on_date is None):
            continue
        rules.append(Rule(key, kind, start, end, price, frozenset(weekdays), on_date))
    return tuple(rules)


def parse(data):
    data = data if isinstance(data, dict) else {}
    reader = _Reader()
    level = data.get("level")
    meta = {
        "pk": _id(data.get("pk")),
        "lock_version": _id(data.get("lock_version")) or 0,
        "company": _id(data.get("company")),
        "vehicle_type": _id(data.get("vehicle_type")),
        "level": level,
        "branches": sorted({b for b in (_id(x) for x in data.get("branches") or []) if b}),
        "is_active": data.get("is_active") not in (False, "false", "0", 0, None),
        "rule_ids": {}, "rule_season": {}, "season_ids": {},
    }
    if not meta["vehicle_type"]:
        reader.fail("", "Vehicle type", "Choose a vehicle type.")
    if level not in FareLevel.values:
        reader.fail("", "Fare level", "Choose company or branch level.")
    elif level == FareLevel.BRANCH and not meta["branches"]:
        reader.fail("", "Branches", "Select at least one branch.")
    if level == FareLevel.COMPANY:
        meta["branches"] = []

    valid_from = reader.date(data.get("valid_from"), "", "Valid from")
    valid_to = reader.date(data.get("valid_to"), "", "Valid to")
    package = reader.whole(data.get("package_minutes"), "", "Package time")
    base = reader.price(data.get("base"), "", "Base fare")
    rules = _rules(reader, data.get("rules"), "Special pricing", meta)

    seasons = []
    for index, row in enumerate(data.get("seasons") if isinstance(data.get("seasons"), list) else []):
        if not isinstance(row, dict):
            reader.fail("", "Seasons", "A season could not be read.")
            continue
        key = _key(row, "s") or f"season-new-{index}"
        name = str(row.get("name") or "").strip()
        label = name or "Season"
        meta["season_ids"][key] = _id(row.get("id"))
        start = reader.date(row.get("start_date"), key, f"{label} · From date")
        end = reader.date(row.get("end_date"), key, f"{label} · To date")
        season_base = reader.price(row.get("base"), key, f"{label} · base fare")
        season_rules = _rules(reader, row.get("rules"), label, meta, season_key=key)
        if None in (start, end, season_base):
            continue
        seasons.append(Season(key, name, start, end, season_base, season_rules))

    # Build what can be built even when a row failed to read, so the fare's
    # own checks still run and the screen lists every problem at once.
    if None in (valid_from, valid_to, package, base):
        return Parsed(None, reader.errors, meta)
    return Parsed(FareSpec(valid_from, valid_to, package, base, rules, tuple(seasons)), reader.errors, meta)


# -- Saved fare -> JSON -----------------------------------------------------------------


def _hhmm(minute):
    return pricing.hhmm(minute)


def _price(row):
    return {
        "base_fare": str(row.base_fare), "grace_minutes": row.grace_minutes,
        "concurrent_interval_minutes": row.concurrent_interval_minutes,
        "concurrent_fare": str(row.concurrent_fare), "concurrent_grace_minutes": row.concurrent_grace_minutes,
    }


def _rule(row):
    return {
        "key": f"r{row.pk}", "id": row.pk, "kind": row.kind, "weekdays": sorted(row.weekdays),
        "on_date": row.on_date.isoformat() if row.on_date else "",
        "start": _hhmm(row.start_minute), "end": _hhmm(row.end_minute), "price": _price(row),
    }


def serialise(fare):
    """A saved fare in parse()'s shape. Expects seasons and rules prefetched."""
    rules = list(fare.rules.all())
    return {
        "pk": fare.pk, "lock_version": fare.lock_version, "company": fare.company_id,
        "vehicle_type": fare.vehicle_type_id, "level": fare.level,
        "branches": sorted(link.branch_id for link in fare.branch_links.all()),
        "valid_from": fare.valid_from.isoformat(), "valid_to": fare.valid_to.isoformat(),
        "is_active": fare.is_active, "package_minutes": fare.package_minutes, "base": _price(fare),
        "rules": [_rule(r) for r in rules if r.season_id is None],
        "seasons": [
            {
                "key": f"s{season.pk}", "id": season.pk, "name": season.name,
                "start_date": season.start_date.isoformat(), "end_date": season.end_date.isoformat(),
                "base": _price(season),
                "rules": [_rule(r) for r in rules if r.season_id == season.pk],
            }
            for season in fare.seasons.all()
        ],
    }
