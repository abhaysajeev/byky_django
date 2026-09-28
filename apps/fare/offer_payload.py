"""The offer form's JSON, both ways.

parse() turns what the browser posts into an OfferSpec plus the identifiers
the service needs (meta), reporting format problems in the message modal's
shape (same apps.fare.pricing.Issue that Fare's own payload.py uses).
serialise() turns a saved offer back into the same JSON, for the edit screen.

The shape:

  {pk, company, offer_code, offer_name, level, branch, location,
   valid_from, valid_to, promotion_for, inventory_type,
   lower_value, upper_value, promotion_type,
   time_slab_applicable, free_item_selectable, free_item_selectable_note,
   free_or_offer_price, is_active,
   items:      [{key, id, vehicle_type, package_minutes, value}],
   free_items: [{key, id, vehicle_type, package_minutes, value}],
   time_slabs: [{key, id, date_mode, specific_date, day, from, to,
                 vehicle_type, package_minutes, value}]}

Dates are ISO, times "HH:MM", money a decimal string. `key` names a row in
messages: "i12"/"f3"/"t7" for saved rows, anything else for new ones. There
is no lock_version -- Offer doesn't carry one (models.py's own note on why).
"""

import datetime
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from apps.fare.models import (
    FreeOrOfferPrice,
    InventoryType,
    OfferLevel,
    PromotionFor,
    PromotionType,
    SlabDateMode,
    SlabDay,
)
from apps.fare.pricing import Issue

MAX_MINUTES = 1440
TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
WHOLE = re.compile(r"^\d+$")
AMOUNT = re.compile(r"^-?\d+(\.\d+)?$")
MAX_MONEY = Decimal("9999999999.99")
CENTS = Decimal("0.01")


@dataclass(frozen=True)
class ItemRow:
    key: str
    vehicle_type: int
    package_minutes: int
    value: Decimal | None


@dataclass(frozen=True)
class FreeItemRow:
    key: str
    vehicle_type: int
    package_minutes: int
    value: Decimal


@dataclass(frozen=True)
class TimeSlabRow:
    key: str
    date_mode: str
    specific_date: datetime.date | None
    day: str
    start: datetime.time
    end: datetime.time
    vehicle_type: int
    package_minutes: int
    value: Decimal


@dataclass
class OfferSpec:
    offer_code: str
    offer_name: str
    level: str
    branch: int | None
    location: int | None
    valid_from: datetime.date
    valid_to: datetime.date
    promotion_for: str
    inventory_type: str
    lower_value: Decimal
    upper_value: Decimal
    promotion_type: str
    time_slab_applicable: bool
    free_item_selectable: bool
    free_item_selectable_note: str
    free_or_offer_price: str
    items: tuple
    free_items: tuple
    time_slabs: tuple


@dataclass
class Parsed:
    spec: OfferSpec | None
    errors: list
    meta: dict = field(default_factory=dict)


class _Reader:
    """Collects every format problem instead of stopping at the first (same
    pattern as apps.fare.payload._Reader)."""

    def __init__(self):
        self.errors = []

    def fail(self, key, where, message, *, missing=False):
        self.errors.append(Issue(key, where, message, missing))
        return None

    def text(self, value, key, where, *, max_length=None, required=True):
        text = str(value or "").strip()
        if required and not text:
            return self.fail(key, where, "This is required.", missing=True)
        if max_length and len(text) > max_length:
            return self.fail(key, where, f"Use at most {max_length} characters.")
        return text

    def whole(self, value, key, where, *, minimum=0, maximum=MAX_MINUTES):
        text = "" if value is None or isinstance(value, bool) else str(value).strip()
        if not WHOLE.match(text):
            return self.fail(key, where, "Enter a whole number, such as 10.", missing=not text)
        number = int(text)
        if not minimum <= number <= maximum:
            return self.fail(key, where, f"Enter {minimum} to {maximum}.")
        return number

    def money(self, value, key, where, *, required=True):
        text = "" if value is None or isinstance(value, bool) else str(value).strip()
        if not text:
            return self.fail(key, where, "Enter an amount, such as 50.00.", missing=True) if required else None
        if not AMOUNT.match(text):
            return self.fail(key, where, "Enter an amount, such as 50.00.")
        try:
            amount = Decimal(text)
        except InvalidOperation:
            return self.fail(key, where, "Enter an amount, such as 50.00.")
        if amount < 0:
            return self.fail(key, where, "The amount cannot be negative.")
        if amount.as_tuple().exponent < -2 and amount != amount.quantize(CENTS):
            return self.fail(key, where, "Use at most two decimal places.")
        if amount > MAX_MONEY:
            return self.fail(key, where, "That amount is too large.")
        return amount.quantize(CENTS)

    def date(self, value, key, where, *, required=True):
        if value in (None, ""):
            return self.fail(key, where, "Pick a date.", missing=True) if required else None
        try:
            return datetime.date.fromisoformat(str(value))
        except ValueError:
            return self.fail(key, where, "Pick a date.")

    def time(self, value, key, where):
        match = TIME.match(str(value or "").strip())
        if not match:
            return self.fail(key, where, "Use a 24-hour time such as 08:30.", missing=not str(value or "").strip())
        return datetime.time(int(match[1]), int(match[2]))

    def choice(self, value, key, where, choices, label):
        if value not in choices:
            return self.fail(key, where, f"Choose {label}.", missing=not value)
        return value

    def vehicle_type(self, value, key, where):
        number = _id(value)
        if number is None:
            return self.fail(key, where, "Choose a vehicle type.", missing=True)
        return number


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


def _items(reader, rows, label, meta, *, value_required):
    parsed = []
    for index, row in enumerate(rows if isinstance(rows, list) else []):
        if not isinstance(row, dict):
            reader.fail("", label, "A row could not be read.")
            continue
        key = _key(row, "i") or f"item-new-{index}"
        where = f"{label} · row {index + 1}"
        vehicle_type = reader.vehicle_type(row.get("vehicle_type"), key, where)
        package_minutes = reader.whole(row.get("package_minutes"), key, f"{where} · Package", minimum=1)
        value = reader.money(row.get("value"), key, f"{where} · Value", required=value_required)
        meta["item_ids"][key] = _id(row.get("id"))
        if None in (vehicle_type, package_minutes) or (value_required and value is None):
            continue
        parsed.append(ItemRow(key, vehicle_type, package_minutes, value))
    return tuple(parsed)


def _free_items(reader, rows, label, meta):
    parsed = []
    for index, row in enumerate(rows if isinstance(rows, list) else []):
        if not isinstance(row, dict):
            reader.fail("", label, "A row could not be read.")
            continue
        key = _key(row, "f") or f"free-new-{index}"
        where = f"{label} · row {index + 1}"
        vehicle_type = reader.vehicle_type(row.get("vehicle_type"), key, where)
        package_minutes = reader.whole(row.get("package_minutes"), key, f"{where} · Package", minimum=1)
        value = reader.money(row.get("value"), key, f"{where} · Value")
        meta["free_item_ids"][key] = _id(row.get("id"))
        if None in (vehicle_type, package_minutes, value):
            continue
        parsed.append(FreeItemRow(key, vehicle_type, package_minutes, value))
    return tuple(parsed)


def _time_slabs(reader, rows, label, meta):
    parsed = []
    for index, row in enumerate(rows if isinstance(rows, list) else []):
        if not isinstance(row, dict):
            reader.fail("", label, "A row could not be read.")
            continue
        key = _key(row, "t") or f"slab-new-{index}"
        where = f"{label} · row {index + 1}"
        date_mode = reader.choice(row.get("date_mode"), key, where, SlabDateMode.values, "a date mode")
        specific_date = None
        if date_mode == SlabDateMode.SPECIFIC_DATE:
            specific_date = reader.date(row.get("specific_date"), key, f"{where} · Date")
        day = reader.choice(row.get("day"), key, f"{where} · Day", SlabDay.values, "a day")
        start = reader.time(row.get("from"), key, f"{where} · From")
        end = reader.time(row.get("to"), key, f"{where} · To")
        vehicle_type = reader.vehicle_type(row.get("vehicle_type"), key, where)
        package_minutes = reader.whole(row.get("package_minutes"), key, f"{where} · Package", minimum=1)
        value = reader.money(row.get("value"), key, f"{where} · Value")
        meta["time_slab_ids"][key] = _id(row.get("id"))
        if (None in (date_mode, day, start, end, vehicle_type, package_minutes, value)
                or (date_mode == SlabDateMode.SPECIFIC_DATE and specific_date is None)):
            continue
        if end <= start:
            reader.fail(key, f"{where} · To", "End time must be after start time.")
            continue
        parsed.append(TimeSlabRow(key, date_mode, specific_date, day, start, end,
                                   vehicle_type, package_minutes, value))
    return tuple(parsed)


def parse(data):
    data = data if isinstance(data, dict) else {}
    reader = _Reader()
    meta = {
        "pk": _id(data.get("pk")),
        "company": _id(data.get("company")),
        "is_active": data.get("is_active") not in (False, "false", "0", 0, None),
        "item_ids": {}, "free_item_ids": {}, "time_slab_ids": {},
    }

    offer_code = reader.text(data.get("offer_code"), "", "Promotion Code", max_length=30)
    offer_name = reader.text(data.get("offer_name"), "", "Promotion Name", max_length=100)

    level = reader.choice(data.get("level"), "", "Promotion Level", OfferLevel.values, "a promotion level")
    branch = _id(data.get("branch"))
    location = _id(data.get("location"))
    if level == OfferLevel.BRANCH and not branch:
        reader.fail("", "Branch", "Choose a branch.", missing=True)
    if level == OfferLevel.LOCATION and not location:
        reader.fail("", "Location", "Choose a location.", missing=True)
    if level != OfferLevel.BRANCH:
        branch = None
    if level != OfferLevel.LOCATION:
        location = None

    valid_from = reader.date(data.get("valid_from"), "", "Valid From")
    valid_to = reader.date(data.get("valid_to"), "", "Valid To")
    if valid_from and valid_to and valid_to < valid_from:
        reader.fail("", "Valid To", "Valid To must be on or after Valid From.")

    promotion_for = reader.choice(data.get("promotion_for"), "", "Promotion For",
                                  PromotionFor.values, "Quantity or Amount")
    inventory_type = reader.choice(data.get("inventory_type"), "", "Inventory Type",
                                   InventoryType.values, "an inventory type")

    lower_value = reader.money(data.get("lower_value"), "", "Lower Promotion Value")
    upper_value = reader.money(data.get("upper_value"), "", "Upper Promotion Value")
    if lower_value is not None and upper_value is not None and upper_value < lower_value:
        reader.fail("", "Upper Promotion Value", "Upper value must be at or above the lower value.")

    promotion_type = reader.choice(data.get("promotion_type"), "", "Promotion Type",
                                   PromotionType.values, "a promotion type")

    time_slab_applicable = bool(data.get("time_slab_applicable"))
    free_item_selectable = bool(data.get("free_item_selectable"))
    free_item_selectable_note = str(data.get("free_item_selectable_note") or "").strip()
    free_or_offer_price = reader.choice(data.get("free_or_offer_price"), "", "Free / Offer Price",
                                        FreeOrOfferPrice.values, "Free or Offer Price")

    items = _items(reader, data.get("items"), "Promotion Items", meta,
                   value_required=promotion_type not in (None, PromotionType.QUANTITY))
    if not items:
        reader.fail("", "Promotion Items", "Add at least one promotion item.", missing=True)

    free_items = ()
    if promotion_type == PromotionType.QUANTITY and free_item_selectable:
        free_items = _free_items(reader, data.get("free_items"), "Free Promotion Items", meta)
        if not free_items:
            reader.fail("", "Free Promotion Items", "Add at least one free item.", missing=True)

    time_slabs = ()
    if time_slab_applicable:
        time_slabs = _time_slabs(reader, data.get("time_slabs"), "Free Item Time Slabs", meta)
        if not time_slabs:
            reader.fail("", "Free Item Time Slabs", "Add at least one time slab.", missing=True)

    required = (offer_code, offer_name, level, valid_from, valid_to, promotion_for, inventory_type,
               lower_value, upper_value, promotion_type, free_or_offer_price)
    if None in required or not items:
        return Parsed(None, reader.errors, meta)

    spec = OfferSpec(
        offer_code=offer_code, offer_name=offer_name, level=level, branch=branch, location=location,
        valid_from=valid_from, valid_to=valid_to, promotion_for=promotion_for, inventory_type=inventory_type,
        lower_value=lower_value, upper_value=upper_value, promotion_type=promotion_type,
        time_slab_applicable=time_slab_applicable, free_item_selectable=free_item_selectable,
        free_item_selectable_note=free_item_selectable_note, free_or_offer_price=free_or_offer_price,
        items=items, free_items=free_items, time_slabs=time_slabs,
    )
    return Parsed(spec, reader.errors, meta)


# -- Saved offer -> JSON -----------------------------------------------------------------


def _item_json(row):
    return {"key": f"i{row.pk}", "id": row.pk, "vehicle_type": row.vehicle_type_id,
           "package_minutes": row.package_minutes, "value": str(row.value) if row.value is not None else None}


def _free_item_json(row):
    return {"key": f"f{row.pk}", "id": row.pk, "vehicle_type": row.vehicle_type_id,
           "package_minutes": row.package_minutes, "value": str(row.value)}


def _time_slab_json(row):
    return {
        "key": f"t{row.pk}", "id": row.pk, "date_mode": row.date_mode,
        "specific_date": row.specific_date.isoformat() if row.specific_date else "",
        "day": row.day, "from": row.from_time.strftime("%H:%M"), "to": row.to_time.strftime("%H:%M"),
        "vehicle_type": row.vehicle_type_id, "package_minutes": row.package_minutes, "value": str(row.value),
    }


def serialise(offer):
    """A saved offer in parse()'s shape. Expects items/free_items/time_slabs
    prefetched (offer_services.with_children)."""
    return {
        "pk": offer.pk, "company": offer.company_id, "offer_code": offer.offer_code, "offer_name": offer.offer_name,
        "level": offer.level, "branch": offer.branch_id, "location": offer.location_id,
        "valid_from": offer.valid_from.isoformat(), "valid_to": offer.valid_to.isoformat(),
        "promotion_for": offer.promotion_for, "inventory_type": offer.inventory_type,
        "lower_value": str(offer.lower_value), "upper_value": str(offer.upper_value),
        "promotion_type": offer.promotion_type,
        "time_slab_applicable": offer.time_slab_applicable, "free_item_selectable": offer.free_item_selectable,
        "free_item_selectable_note": offer.free_item_selectable_note,
        "free_or_offer_price": offer.free_or_offer_price, "is_active": offer.is_active,
        "items": [_item_json(r) for r in offer.items.all()],
        "free_items": [_free_item_json(r) for r in offer.free_items.all()],
        "time_slabs": [_time_slab_json(r) for r in offer.time_slabs.all()],
    }
