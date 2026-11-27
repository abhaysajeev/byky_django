"""Suspense entries (owner, 26 Nov 2026): a branch's collection came in short
or in excess and the field team could not say against which order.

A web user with the page privilege records the branch, the business day, the
type, a positive amount and -- optionally -- the orders they suspect. The admin
team works it out by hand, outside this screen, and marks the entry Resolved:
a label only; no order or invoice is touched. An entry is edited while Open and
never deleted -- it is Cancelled, with a reason. Several entries per branch and
day are fine.

New: the legacy had no such record.
"""

import datetime
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from apps.company.scoping import branches_for
from apps.rental import order_status
from apps.rental.models import SuspenseEntry, SuspenseKind, SuspenseStatus
from apps.rental.scoping import orders_for
from core import money
from core.ids import uuid7
from core.timezones import business_day_of, business_window, zone_for


class SuspenseInvalid(Exception):
    """Every problem with a posted entry at once, as {field, message} pairs."""

    def __init__(self, errors):
        super().__init__("; ".join(e["message"] for e in errors))
        self.errors = errors


def today_for(company):
    """Today's business day for the company (04:00 to 04:00 at BYKY)."""
    return business_day_of(company, timezone.now())


def orders_for_day(user, branch, day):
    """The branch's orders booked on business day `day`, newest first, with
    their display status -- the Suspected orders picker."""
    start, end = business_window(branch.company, day, day)
    orders = (orders_for(user).filter(branch=branch, booked_at__gte=start, booked_at__lt=end)
              .order_by("-booked_at"))
    return order_status.with_display_status(orders)


def order_option(order, zone, places):
    """One picker option: "ORD-123 · 14:05 · AED 45.00 · Received"."""
    at = timezone.localtime(order.booked_at, zone).strftime("%H:%M")
    parts = [order.order_no, at]
    if order.net_amount is not None:
        parts.append(f"AED {money.fmt(order.net_amount, places)}")
    status = order_status.label(getattr(order, "display_status", ""))
    if status:
        parts.append(status)
    return {"id": str(order.pk), "name": " · ".join(parts)}


def _parse_date(text):
    """ISO, or "26 Nov 2026" as the drawer's date picker sends it."""
    text = str(text or "").strip()
    for parse in (datetime.date.fromisoformat, lambda t: datetime.datetime.strptime(t, "%d %b %Y").date()):
        try:
            return parse(text)
        except ValueError:
            continue
    return None


def _parse_amount(text):
    try:
        value = Decimal(str(text or "").strip())
    except (InvalidOperation, ValueError):
        return None
    return value if value.is_finite() else None


def _check(user, data):
    """(fields, orders) from a posted entry, or SuspenseInvalid listing every problem."""
    errors = []
    branch = branches_for(user).filter(pk=_int(data.get("branch")), is_active=True).select_related("company").first()
    if branch is None:
        errors.append({"field": "Branch", "message": "Choose one of your branches."})

    day = _parse_date(data.get("entry_date"))
    if day is None:
        errors.append({"field": "Date", "message": "Choose the collection's date."})
    elif branch is not None and day > today_for(branch.company):
        errors.append({"field": "Date", "message": "The date cannot be in the future."})

    kind = str(data.get("kind") or "")
    if kind not in SuspenseKind.values:
        errors.append({"field": "Type", "message": "Choose Excess or Short."})

    amount = _parse_amount(data.get("amount"))
    if amount is None:
        errors.append({"field": "Amount", "message": "Enter the amount."})
    elif amount <= 0:
        errors.append({"field": "Amount", "message": "Enter an amount greater than zero; choose Short or Excess for its direction."})
    elif branch is not None:
        places = money.places_for(branch.company, branch)
        if money.places_of(amount) > places:
            errors.append({"field": "Amount", "message": money.too_precise_message([("Amount", amount)], places)})

    ids = data.get("suspected_orders") or []
    if not isinstance(ids, list):
        ids = [ids]
    ids = {str(i) for i in ids if str(i).strip()}
    orders = []
    if ids and branch is not None and day is not None:
        orders = list(orders_for_day(user, branch, day).filter(pk__in=ids))
        if len(orders) != len(ids):
            errors.append({"field": "Suspected orders",
                           "message": "Pick only orders booked at this branch on this date."})

    if errors:
        raise SuspenseInvalid(errors)
    fields = {"company": branch.company, "branch": branch, "entry_date": day, "kind": kind,
              "amount": amount, "remarks": str(data.get("remarks") or "").strip()}
    return fields, orders


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def save(user, data, entry=None):
    """Create an entry, or edit one that is still Open."""
    if entry is not None and entry.status != SuspenseStatus.OPEN:
        raise SuspenseInvalid([{"field": "Status", "message": "Only an open entry can be edited."}])
    fields, orders = _check(user, data)
    with transaction.atomic():
        if entry is None:
            entry = SuspenseEntry(id=uuid7(), created_by=user, **fields)
        else:
            for name, value in fields.items():
                setattr(entry, name, value)
            entry.modified_by = user
        entry.save()
        entry.suspected_orders.set(orders)
    return entry


def resolve(user, entry, remarks=""):
    """A label only: the fix was made by hand, outside this screen."""
    if entry.status != SuspenseStatus.OPEN:
        raise SuspenseInvalid([{"field": "Status", "message": "Only an open entry can be resolved."}])
    entry.status = SuspenseStatus.RESOLVED
    entry.resolved_by, entry.resolved_at = user, timezone.now()
    entry.resolution_remarks = str(remarks or "").strip()
    entry.modified_by = user
    entry.save()
    return entry


def cancel(user, entry, reason):
    """Entries are never deleted: cancelled, with a reason."""
    reason = str(reason or "").strip()
    if entry.status != SuspenseStatus.OPEN:
        raise SuspenseInvalid([{"field": "Status", "message": "Only an open entry can be cancelled."}])
    if not reason:
        raise SuspenseInvalid([{"field": "Reason", "message": "Say why the entry is cancelled."}])
    entry.status = SuspenseStatus.CANCELLED
    entry.cancelled_by, entry.cancelled_at = user, timezone.now()
    entry.cancel_reason = reason
    entry.modified_by = user
    entry.save()
    return entry


def zone_and_places(company, branch=None):
    return zone_for(company), money.places_for(company, branch)

