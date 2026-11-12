"""Station targets: every rule of design/08-targets.md lives here, so the web
screens now and the manager-app API later read the same figures.

  planned   the month's amount split evenly over all its days, the remainder
            on the last day (T4). Stored once; a mid-month edit re-splits
            today onward only (T7).
  required  for day d: max(0, amount - achieved before d) / days left
            including d (T5). Frozen at the day's start: by the beat task
            (tasks.freeze_due), or by the first read that finds it missing.
  achieved  per order: net_amount minus that order's approved credit notes,
            on the order's BOOKED business day; completed orders only, and
            never a cancelled one (apps/rental/counted.py) (T6). Never stored.

A day is the company's business day (Configurations: report day start, T3).
"""

import calendar
import datetime
from collections import defaultdict
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import DateTimeField, ExpressionWrapper, F, Sum, Value
from django.db.models.functions import TruncDate
from django.utils import timezone

from apps.rental.counted import counted
from apps.rental.models import CreditNote, CreditNoteStatus, Order, OrderStatus
from apps.targets.models import Target, TargetDay
from core.money import places_for
from core.timezones import business_day_of, business_window, day_start_for, zone_for

ZERO = Decimal("0")
HUNDRED = Decimal("100")
NEAR = Decimal("70")          # % at which a month in progress shows amber rather than red


class TargetError(Exception):
    """A refused change; `field` and `message` are shown to the person."""

    def __init__(self, field, message):
        super().__init__(message)
        self.field, self.message = field, message


# -- Dates and money ----------------------------------------------------------------------


def month_bounds(year, month):
    """(first, last) day of a calendar month."""
    return datetime.date(year, month, 1), datetime.date(year, month, calendar.monthrange(year, month)[1])


def days_of(first, last):
    return [first + datetime.timedelta(days=n) for n in range((last - first).days + 1)]


def today_for(company, now=None):
    return business_day_of(company, now or timezone.now())


def _step(places):
    return Decimal(1).scaleb(-places)


def split_even(amount, days, places):
    """{day: amount}: `amount` over `days` evenly, rounded down to the money
    places, the remainder on the last day -- so the days add up exactly."""
    if not days:
        return {}
    amount = max(amount, ZERO)
    each = (amount / len(days)).quantize(_step(places), rounding=ROUND_DOWN)
    split = dict.fromkeys(days, each)
    split[days[-1]] = amount - each * (len(days) - 1)
    return split


def required_for(amount, achieved_before, days_left, places):
    """What a day must bring in for the month to still be met (T5)."""
    if days_left <= 0:
        return ZERO
    left = max(amount - achieved_before, ZERO)
    return (left / days_left).quantize(_step(places), rounding=ROUND_HALF_UP)


def is_locked(target, today):
    """A period that has ended can't change (T7)."""
    return target.period_end < today


def percent(achieved, amount):
    if not amount:
        return ZERO
    return (achieved * HUNDRED / amount).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def tone(pct):
    """met / near / behind, for the % chip."""
    if pct >= HUNDRED:
        return "met"
    return "near" if pct >= NEAR else "behind"


# -- Achieved -----------------------------------------------------------------------------


def _business_day(path, company, day_start):
    """SQL: the business day of the timestamp at `path` (shift back by the day
    start, then take the date in the company's zone)."""
    shift = datetime.timedelta(hours=day_start.hour, minutes=day_start.minute)
    moved = ExpressionWrapper(F(path) - Value(shift), output_field=DateTimeField())
    return TruncDate(moved, tzinfo=zone_for(company))


def achieved_by_day(company, branch_ids, first, last):
    """{(branch_id, day): achieved} for the business days first..last.

    Completed orders booked in the window, never a cancelled one: net amount,
    less each order's approved credit notes, on the order's booked day (T6)."""
    branch_ids = list(branch_ids)
    if not branch_ids or last < first:
        return {}
    day_start = day_start_for(company)
    since, until = business_window(company, first, last, day_start)
    orders = counted(Order.objects.filter(
        company=company, branch_id__in=branch_ids, status=OrderStatus.COMPLETED,
        booked_at__gte=since, booked_at__lt=until), path="")
    found = defaultdict(lambda: ZERO)
    for branch_id, day, total in (orders.annotate(day=_business_day("booked_at", company, day_start))
                                  .values("branch_id", "day").annotate(total=Sum("net_amount")).order_by()
                                  .values_list("branch_id", "day", "total")):
        found[(branch_id, day)] += total or ZERO
    notes = counted(CreditNote.objects.filter(order__in=orders.values("pk"), status=CreditNoteStatus.APPROVED))
    for branch_id, day, total in (notes.annotate(day=_business_day("order__booked_at", company, day_start))
                                  .values("order__branch_id", "day").annotate(total=Sum("net_amount")).order_by()
                                  .values_list("order__branch_id", "day", "total")):
        found[(branch_id, day)] -= total or ZERO
    return dict(found)


def _sum(achieved, branch_id, first, last):
    return sum((v for (b, d), v in achieved.items() if b == branch_id and first <= d <= last), ZERO)


# -- Freezing "required" ------------------------------------------------------------------


def _freeze(target, days, achieved, today, places):
    """Fill required_amount for every day up to today that has none. Never
    changes a frozen one."""
    total_days = len(days)
    changed = []
    for index, row in enumerate(days):
        if row.date > today or row.required_amount is not None:
            continue
        before = _sum(achieved, target.branch_id, target.period_start, row.date - datetime.timedelta(days=1))
        row.required_amount = required_for(target.amount, before, total_days - index, places)
        changed.append(row)
    if changed:
        TargetDay.objects.bulk_update(changed, ["required_amount"])


def ensure_frozen(targets, today):
    """Freeze what is due for these targets of one company (lazy fill on read)."""
    targets = [t for t in targets if t.period_start <= today]
    if not targets:
        return
    company = targets[0].company
    first = min(t.period_start for t in targets)
    achieved = achieved_by_day(company, {t.branch_id for t in targets}, first, today - datetime.timedelta(days=1))
    days = defaultdict(list)
    for row in TargetDay.objects.filter(target__in=targets).order_by("date"):
        days[row.target_id].append(row)
    for t in targets:
        if any(r.required_amount is None and r.date <= today for r in days[t.pk]):
            _freeze(t, days[t.pk], achieved, today, places_for(t.company, t.branch))


def freeze_due(now=None):
    """The beat task's job: freeze today's required amount for every target
    whose business day has started. Idempotent. Returns how many targets."""
    from apps.company.models import Company

    now = now or timezone.now()
    done = 0
    for company in Company.objects.filter(pk__in=Target.objects.values("company_id")):
        today = today_for(company, now)
        due = list(Target.objects.filter(
            company=company, period_start__lte=today, period_end__gte=today,
            days__date__lte=today, days__required_amount__isnull=True,
        ).distinct().select_related("company", "branch"))
        ensure_frozen(due, today)
        done += len(due)
    return done


# -- Writing ------------------------------------------------------------------------------


def _create_days(target, places):
    split = split_even(target.amount, days_of(target.period_start, target.period_end), places)
    TargetDay.objects.bulk_create([TargetDay(target=target, date=d, planned_amount=a) for d, a in split.items()])


@transaction.atomic
def create_targets(company, branches, year, month, amount, remarks, user, now=None):
    """Add one month's target for each station. Returns (saved, refused):
    a station that already has a target overlapping the month, or is
    inactive, is refused by name; the others are saved (design §5)."""
    first, last = month_bounds(year, month)
    today = today_for(company, now)
    if last < today:
        raise TargetError("Month", "That month has ended; a target can only be set for this month or later.")
    saved, refused = [], []
    for branch in branches:
        if not branch.is_active:
            refused.append((branch, "the station is inactive"))
            continue
        if Target.objects.filter(branch=branch, period_start__lte=last, period_end__gte=first).exists():
            refused.append((branch, "it already has a target for this month"))
            continue
        target = Target.objects.create(company=company, branch=branch, period_start=first, period_end=last,
                                       amount=amount, remarks=remarks, created_by=user, modified_by=user)
        _create_days(target, places_for(company, branch))
        saved.append(target)
    ensure_frozen(saved, today)
    return saved, refused


@transaction.atomic
def update_target(target, amount, remarks, user, now=None):
    """Change a target's amount or remarks (T7): refused once its month has
    ended. Days before today keep their planned amount; today and later share
    what is left. Today's frozen required amount follows the new amount."""
    today = today_for(target.company, now)
    if is_locked(target, today):
        raise TargetError("Month", "This month has ended; its target can't be changed.")
    places = places_for(target.company, target.branch)
    days = list(target.days.order_by("date"))
    past = [d for d in days if d.date < today]
    ahead = [d for d in days if d.date >= today]
    split = split_even(amount - sum((d.planned_amount for d in past), ZERO), [d.date for d in ahead], places)
    for row in ahead:
        row.planned_amount = split[row.date]
        if row.date == today:
            row.required_amount = None                    # frozen again below, with the new amount
    TargetDay.objects.bulk_update(ahead, ["planned_amount", "required_amount"])
    target.amount, target.remarks, target.modified_by = amount, remarks, user
    target.save(update_fields=["amount", "remarks", "modified_by", "modified_on"])
    ensure_frozen([target], today)
    return target


def delete_target(target, now=None):
    if is_locked(target, today_for(target.company, now)):
        raise TargetError("Month", "This month has ended; its target can't be deleted.")
    target.delete()


# -- Reading ------------------------------------------------------------------------------


@dataclass
class Figures:
    amount: Decimal
    achieved: Decimal
    percent: Decimal
    tone: str
    remaining: Decimal


def figures(amount, achieved):
    pct = percent(achieved, amount)
    return Figures(amount, achieved, pct, tone(pct), max(amount - achieved, ZERO))


def month_view(target, now=None):
    """Everything the month view (and the manager API later) shows."""
    company = target.company
    today = today_for(company, now)
    ensure_frozen([target], today)
    rows = list(target.days.order_by("date"))
    achieved = achieved_by_day(company, [target.branch_id], target.period_start, min(today, target.period_end))
    total = _sum(achieved, target.branch_id, target.period_start, target.period_end)
    days = []
    for row in rows:
        got = achieved.get((target.branch_id, row.date), ZERO) if row.date <= today else None
        if row.date > today:
            state = "future"
        elif row.date == today:
            state = "today"
        else:
            state = "met" if got >= (row.required_amount or ZERO) else "missed"
        days.append({"date": row.date, "planned": row.planned_amount, "required": row.required_amount,
                     "achieved": got, "state": state,
                     "gap": (got - row.required_amount) if got is not None and row.required_amount is not None
                     else None})
    current = next((d for d in days if d["state"] == "today"), None)
    days_left = sum(1 for d in days if d["date"] >= today)
    return {"target": target, "figures": figures(target.amount, total), "days": days, "today": current,
            "days_left": days_left, "locked": is_locked(target, today), "started": target.period_start <= today,
            "business_today": today}


def year_targets(branch_ids, year):
    return (Target.objects.filter(branch_id__in=branch_ids, period_start__year=year)
            .select_related("company", "branch"))


def achieved_for(targets, today):
    """{target_id: achieved so far} for many targets, one query per company."""
    by_company = defaultdict(list)
    for t in targets:
        if t.period_start <= today:
            by_company[t.company].append(t)
    out = {}
    for company, items in by_company.items():
        first = min(t.period_start for t in items)
        last = min(max(t.period_end for t in items), today)
        achieved = achieved_by_day(company, {t.branch_id for t in items}, first, last)
        for t in items:
            out[t.pk] = _sum(achieved, t.branch_id, t.period_start, t.period_end)
    return out


def year_grid(branches, year, now=None):
    """The list screen: one row per station, 12 month cells."""
    branches = list(branches)
    targets = list(year_targets([b.pk for b in branches], year))
    companies = {b.company_id: b.company for b in branches}
    todays = {cid: today_for(c, now) for cid, c in companies.items()}
    started = [t for t in targets if t.period_start <= todays[t.company_id]]
    achieved = {}
    for cid in companies:
        achieved.update(achieved_for([t for t in started if t.company_id == cid], todays[cid]))
    by_cell = {(t.branch_id, t.period_start.month): t for t in targets}
    rows = []
    for b in branches:
        cells = []
        for month in range(1, 13):
            t = by_cell.get((b.pk, month))
            first, last = month_bounds(year, month)
            today = todays[b.company_id]
            cell = {"month": month, "key": f"{year}-{month:02d}", "target": t, "open": last >= today}
            if t is not None and t.pk in achieved:
                cell["figures"] = figures(t.amount, achieved[t.pk])
            cells.append(cell)
        rows.append({"branch": b, "cells": cells})
    return rows


def branch_year(branch, year, now=None):
    """The branch screen: the station's 12 months and the year's totals."""
    today = today_for(branch.company, now)
    targets = {t.period_start.month: t for t in year_targets([branch.pk], year)}
    achieved = achieved_for(targets.values(), today)
    months, amount, got = [], ZERO, ZERO
    for month in range(1, 13):
        first, last = month_bounds(year, month)
        t = targets.get(month)
        row = {"month": month, "first": first, "target": t, "open": last >= today, "started": first <= today}
        if t is not None:
            amount += t.amount
            if t.pk in achieved:
                got += achieved[t.pk]
                row["figures"] = figures(t.amount, achieved[t.pk])
        months.append(row)
    return {"months": months, "total": figures(amount, got) if amount else None}
