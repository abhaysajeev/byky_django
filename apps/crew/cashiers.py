"""Primary and Secondary cashiers on the duty roster (owner, 13 Nov 2026).

  * A Working day worked as Cashier is Primary or Secondary; any other day is
    neither.
  * One Primary per branch per day (also a database rule).
  * A Secondary needs a Primary on its branch that day.
  * Choosing Primary for someone makes that branch-day's current Primary a
    Secondary -- so swapping who is Primary is one edit.
  * When a Primary stops being one (the day is cleared, worked as labour,
    turned into a day off or leave, moved to another branch), the earliest
    saved Secondary of that branch-day becomes Primary.

Every write of a roster day goes through `save_day` or calls `settle` for
the branch-days it emptied (services.py, leave.py), always inside the
caller's transaction.
"""

from django.db import IntegrityError, transaction

from apps.crew.models import CashierType, DutyRoster, DutyRosterDayType, RosterCategory

PRIMARY, SECONDARY = CashierType.PRIMARY, CashierType.SECONDARY


class CashierError(Exception):
    """A refused cashier type; `field` and `message` are shown to the person."""

    def __init__(self, field, message):
        super().__init__(message)
        self.field, self.message = field, message


def is_cashier_day(fields):
    return fields.get("day_type") == DutyRosterDayType.WORKING and fields.get("role") == RosterCategory.CASHIER


def wanted_type(value):
    """The posted cashier type: "primary", "secondary", or "" (let the rule pick); None when unreadable."""
    text = str(value or "").strip().lower()
    if text in ("", "auto"):
        return ""
    return text if text in CashierType.values else None


def _others(branch_id, on_date, employee_id):
    """The branch-day's other cashier rows, locked for this transaction."""
    return (DutyRoster.objects.select_for_update()
            .filter(branch_id=branch_id, date=on_date)
            .exclude(cashier_type="").exclude(employee_id=employee_id)
            .order_by("created_on", "id"))


def _place(row):
    return (row.branch_id, row.date) if row is not None and row.cashier_type else None


def save_day(employee, fields, user):
    """Upsert one checked day (services._check_day's `fields`, carrying the
    posted `cashier_type` wish) and keep the branch-day's cashiers right.
    Returns (row, created). Raises CashierError when refused."""
    fields = dict(fields)
    wish = fields.pop("cashier_type", "")
    on_date = fields["date"]
    with transaction.atomic():
        before = (DutyRoster.objects.select_for_update()
                  .filter(employee=employee, date=on_date).first())
        left_place = _place(before)
        if is_cashier_day(fields):
            others = list(_others(fields["branch"].pk, on_date, employee.pk))
            current = next((r for r in others if r.cashier_type == PRIMARY), None)
            kept = before.cashier_type if before is not None and _place(before) == (fields["branch"].pk, on_date) else ""
            kind = wish or kept or (SECONDARY if current else PRIMARY)
            if kind == SECONDARY and current is None:
                raise CashierError(
                    "Cashier type",
                    f"Set a primary cashier for {fields['branch'].name} on {on_date:%d %b} first.")
            if kind == PRIMARY and current is not None:
                current.cashier_type = SECONDARY
                current.save(update_fields=["cashier_type", "modified_on"])
            fields["cashier_type"] = kind
        else:
            fields["cashier_type"] = ""
        try:
            with transaction.atomic():
                row, created = DutyRoster.objects.update_or_create(
                    employee=employee, date=on_date, defaults={**fields, "modified_by": user})
        except IntegrityError as error:
            if "primary_cashier" in str(error):
                raise CashierError("Cashier type", "This branch already has a primary cashier that day.") from None
            raise
        if left_place and left_place != _place(row):
            settle([left_place])
        elif left_place and row.cashier_type != PRIMARY and before.cashier_type == PRIMARY:
            settle([left_place])
    return row, created


def places_of(rows):
    """The branch-days these rows hold a cashier in -- read before changing them."""
    return {(r.branch_id, r.date) for r in rows if r.cashier_type}


def settle(places):
    """For each (branch_id, date): if no Primary is left but a Secondary is,
    the earliest saved Secondary becomes Primary."""
    for branch_id, on_date in places:
        if branch_id is None:
            continue
        rows = list(DutyRoster.objects.select_for_update()
                    .filter(branch_id=branch_id, date=on_date).exclude(cashier_type="")
                    .order_by("created_on", "id"))
        if rows and not any(r.cashier_type == PRIMARY for r in rows):
            first = rows[0]
            first.cashier_type = PRIMARY
            first.save(update_fields=["cashier_type", "modified_on"])
