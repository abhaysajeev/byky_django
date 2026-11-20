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

Customer targets (section 9, owner 17 Nov 2026) share the table with kind
"customers": a whole number of unique customers on the same counted orders --
no days, no freezing; "On track" compares pro rata.

Target Profiles (design/11-target-profiles.md): every target belongs to a
profile and is one of its periods. sync_profile() makes them; the same orders
count toward every profile covering them (P2).
"""

import datetime
from collections import defaultdict
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from django.db import IntegrityError, transaction
from django.db.models import Count, DateTimeField, ExpressionWrapper, F, Q, Sum, Value
from django.db.models.functions import TruncDate
from django.utils import timezone

from apps.rental.counted import counted
from apps.rental.models import CreditNote, CreditNoteStatus, Order, OrderStatus
from apps.targets import periods as P
from apps.targets.models import (
    PeriodType,
    ProfileStatus,
    Target,
    TargetDay,
    TargetKind,
    TargetProfile,
    TargetProfileBranch,
    TargetProfileMonth,
)
from core.money import places_for
from core.timezones import business_day_of, business_window, day_start_for, zone_for

ZERO = Decimal("0")
HUNDRED = Decimal("100")


class TargetError(Exception):
    """A refused change; `field` and `message` are shown to the person."""

    def __init__(self, field, message):
        super().__init__(message)
        self.field, self.message = field, message


# -- Dates and money ----------------------------------------------------------------------


month_bounds = P.month_bounds
days_of = P.days_of
split_even = P.split_even
target_label = P.target_label


def today_for(company, now=None):
    return business_day_of(company, now or timezone.now())


def _step(places):
    return Decimal(1).scaleb(-places)


def required_for(amount, achieved_before, days_left, places):
    """What a day must bring in for the period to still be met (T5)."""
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


def customers_by_branch(company, branch_ids, first, last):
    """{branch_id: unique customers} on the orders that count for sales (C2):
    completed, never cancelled, booked within the business days first..last."""
    branch_ids = list(branch_ids)
    if not branch_ids or last < first:
        return {}
    since, until = business_window(company, first, last, day_start_for(company))
    orders = counted(Order.objects.filter(
        company=company, branch_id__in=branch_ids, status=OrderStatus.COMPLETED,
        booked_at__gte=since, booked_at__lt=until), path="")
    return dict(orders.values("branch_id").annotate(n=Count("customer_id", distinct=True)).order_by()
                .values_list("branch_id", "n"))


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
    targets = [t for t in targets if t.period_start <= today and t.kind == TargetKind.SALES]
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


def weights_of(profile):
    """An annual profile's {month_index: weight}; None = proportional to days."""
    if profile is None or profile.period_type != PeriodType.ANNUAL:
        return None
    found = {m.month_index: m.weight for m in profile.months.all()}
    return found if P.weights_valid(found) else None


def _create_days(target, places, weights=None):
    """Sales targets plan every day; customer targets have no days (C3)."""
    if target.kind != TargetKind.SALES:
        return
    split = P.plan(target.period_type, target.amount, target.period_start, target.period_end, places, weights)
    TargetDay.objects.bulk_create([TargetDay(target=target, date=d, planned_amount=a) for d, a in split.items()])


def clash(branch, kind, period_type, first, last, exclude_profile=None):
    """The target that would duplicate this one (same type + metric over
    overlapping dates on the station -- 11 P3), or None."""
    found = Target.objects.filter(branch=branch, kind=kind, period_type=period_type,
                                  period_start__lte=last, period_end__gte=first)
    if exclude_profile is not None:
        found = found.exclude(profile=exclude_profile)
    return found.select_related("profile").first()


def _make_target(profile, branch, first, last, amount, remarks, user, weights=None, overridden=False):
    """One period's target and its days, or None when the database refuses a
    duplicate that slipped past clash() (two saves at once)."""
    try:
        with transaction.atomic():
            target = Target.objects.create(
                company=profile.company, branch=branch, profile=profile, kind=profile.metric,
                period_type=profile.period_type, period_start=first, period_end=last, amount=amount,
                amount_overridden=overridden, remarks=remarks, created_by=user, modified_by=user)
            _create_days(target, places_for(profile.company, branch), weights)
    except IntegrityError:
        return None
    return target


DEFAULT_NAMES = {TargetKind.SALES: "Monthly sales", TargetKind.CUSTOMERS: "Monthly customers"}


def default_profile(company, kind, first, last, user):
    """The company's monthly profile for a metric -- where a plain "this
    month's target" lives (the manager app, older screens). Made when missing;
    its range grows to cover first..last."""
    profile = (TargetProfile.objects.select_for_update()
               .filter(company=company, metric=kind, period_type=PeriodType.MONTHLY, status=ProfileStatus.ACTIVE)
               .order_by("pk").first())
    if profile is None:
        name = DEFAULT_NAMES[kind]
        while TargetProfile.objects.filter(company=company, name=name).exists():
            name += " (2)"
        return TargetProfile.objects.create(company=company, name=name, metric=kind,
                                            period_type=PeriodType.MONTHLY, starts_on=first, ends_on=last,
                                            status=ProfileStatus.ACTIVE, created_by=user, modified_by=user)
    if first < profile.starts_on or last > profile.ends_on:
        profile.starts_on, profile.ends_on = min(first, profile.starts_on), max(last, profile.ends_on)
        profile.modified_by = user
        profile.save(update_fields=["starts_on", "ends_on", "modified_by", "modified_on"])
    return profile


@transaction.atomic
def create_targets(company, branches, year, month, amount, remarks, user, now=None, kind=TargetKind.SALES):
    """Add one month's target for each station, on the company's monthly
    profile for `kind`. Returns (saved, refused): a station that already has a
    monthly target for the month, or is inactive, is refused by name."""
    first, last = month_bounds(year, month)
    today = today_for(company, now)
    if last < today:
        raise TargetError("Month", "That month has ended; a target can only be set for this month or later.")
    profile = default_profile(company, kind, first, last, user)
    saved, refused = [], []
    for branch in branches:
        if not branch.is_active:
            refused.append((branch, "the branch is inactive"))
            continue
        if clash(branch, kind, PeriodType.MONTHLY, first, last):
            refused.append((branch, "it already has a target for this month"))
            continue
        assignment = TargetProfileBranch.objects.filter(profile=profile, branch=branch).first()
        if assignment is None:
            assignment = TargetProfileBranch.objects.create(profile=profile, branch=branch, amount=amount)
        elif not assignment.active:
            assignment.active, assignment.amount = True, amount
            assignment.save(update_fields=["active", "amount"])
        target = _make_target(profile, branch, first, last, amount, remarks, user,
                              overridden=amount != assignment.amount)
        if target is None:
            refused.append((branch, "it already has a target for this month"))
            continue
        saved.append(target)
    ensure_frozen(saved, today)
    return saved, refused


def _replan(target, amount, today, places, weights=None):
    """Days before today keep their plan; today and later share what is left,
    in the proportions the period's plan gives them (even, or by month weight)."""
    days = list(target.days.order_by("date"))
    past = [d for d in days if d.date < today]
    ahead = [d for d in days if d.date >= today]
    if not ahead:
        return
    left = max(amount - sum((d.planned_amount for d in past), ZERO), ZERO)
    if target.period_type != PeriodType.ANNUAL:                 # evenly, exactly as T7
        split = split_even(left, [d.date for d in ahead], places)
        for row in ahead:
            row.planned_amount = split[row.date]
            if row.date == today:
                row.required_amount = None
        TargetDay.objects.bulk_update(ahead, ["planned_amount", "required_amount"])
        return
    shape = P.plan(target.period_type, amount, target.period_start, target.period_end, places, weights)
    base = sum((shape[d.date] for d in ahead), ZERO)
    step = _step(places)
    used = ZERO
    for row in ahead:
        share = (left * shape[row.date] / base) if base else left / len(ahead)
        row.planned_amount = share.quantize(step, rounding=ROUND_DOWN)
        used += row.planned_amount
        if row.date == today:
            row.required_amount = None                    # frozen again, with the new amount
    ahead[-1].planned_amount += left - used
    TargetDay.objects.bulk_update(ahead, ["planned_amount", "required_amount"])


@transaction.atomic
def update_target(target, amount, remarks, user, now=None, override=True):
    """Change one period's amount or remarks (T7): refused once it has ended.
    Days before today keep their plan; today and later share what is left.
    Today's frozen required amount follows the new amount. An edit made here
    marks the period as set on its own, so a later change to the profile's
    amount leaves it alone (11 §5)."""
    today = today_for(target.company, now)
    if is_locked(target, today):
        raise TargetError("Period", "This period has ended; its target can't be changed.")
    fields = ["amount", "remarks", "modified_by", "modified_on"]
    if override and amount != target.amount:
        target.amount_overridden = True
        fields.append("amount_overridden")
    if target.kind == TargetKind.SALES:
        _replan(target, amount, today, places_for(target.company, target.branch), weights_of(target.profile))
    target.amount, target.remarks, target.modified_by = amount, remarks, user
    target.save(update_fields=fields)
    ensure_frozen([target], today)
    return target


def delete_target(target, now=None):
    if is_locked(target, today_for(target.company, now)):
        raise TargetError("Period", "This period has ended; its target can't be deleted.")
    if target.period_start <= today_for(target.company, now) and _has_run(target):
        raise TargetError("Period", "An incentive has been worked out on this target; it can't be deleted.")
    target.delete()


def _has_run(target):
    from apps.incentives.models import Run

    return Run.objects.filter(branch_id=target.branch_id, profile_id=target.profile_id,
                              period_start=target.period_start).exists()


# -- Profiles -----------------------------------------------------------------------------


@dataclass
class Refusal:
    branch: object
    reason: str

    def __iter__(self):                                   # (branch, reason), as create_targets gives
        return iter((self.branch, self.reason))


def _clash_reason(found, profile):
    label = P.target_label(found)
    return (f"{label} overlaps its {found.get_period_type_display().lower()} "
            f"{found.get_kind_display().lower()} target on “{found.profile.name}”")


@transaction.atomic
def sync_profile(profile, user, now=None):
    """Make the profile's targets match its stations and amounts. Idempotent.

    For each active station, every period gets a target: periods that have
    ended are only filled the first time a profile makes targets (setting one
    up late), never afterwards. An amount change moves every period not yet
    ended and not edited on its own (the T7 way); ended periods never change.
    A duplicate (same type + metric over overlapping dates, 11 P3) refuses that
    period, named; the others still save. Returns (made, changed, refusals)."""
    profile = TargetProfile.objects.select_for_update().select_related("company").get(pk=profile.pk)
    if profile.status != ProfileStatus.ACTIVE:
        return 0, 0, []
    today = today_for(profile.company, now)
    first_time = not profile.targets.exists()
    weights = weights_of(profile)
    spans = P.profile_periods(profile)
    made, changed, refusals, touched = 0, 0, [], []
    existing = defaultdict(dict)
    for t in profile.targets.all():
        existing[t.branch_id][t.period_start] = t
    for assignment in profile.assignments.filter(active=True).select_related("branch"):
        branch = assignment.branch
        if not branch.is_active:
            refusals.append(Refusal(branch, "the branch is inactive"))
            continue
        problems = []
        for first, last in spans:
            target = existing[branch.pk].get(first)
            if target is not None:
                if (target.amount != assignment.amount and not target.amount_overridden
                        and not is_locked(target, today)):
                    update_target(target, assignment.amount, target.remarks, user, now=now, override=False)
                    changed += 1
                continue
            if last < today and not first_time:
                continue
            found = clash(branch, profile.metric, profile.period_type, first, last, exclude_profile=profile)
            if found is not None:
                problems.append(_clash_reason(found, profile))
                continue
            target = _make_target(profile, branch, first, last, assignment.amount, "", user, weights)
            if target is None:
                problems.append(f"{P.period_label(profile.period_type, first, last)} overlaps another target")
                continue
            made += 1
            touched.append(target)
        if problems:
            more = f" (and {len(problems) - 3} more)" if len(problems) > 3 else ""
            refusals.append(Refusal(branch, "; ".join(problems[:3]) + more))
    ensure_frozen([t for t in touched if t.period_start <= today], today)
    return made, changed, refusals


@transaction.atomic
def remove_branch(profile, branch, now=None):
    """Take a station off a profile: its targets not yet started go; started
    and ended ones stay as history, and the station stays listed, inactive."""
    today = today_for(profile.company, now)
    profile.targets.filter(branch=branch, period_start__gt=today).delete()
    kept = profile.targets.filter(branch=branch).exists()
    assignment = TargetProfileBranch.objects.filter(profile=profile, branch=branch).first()
    if assignment is None:
        return
    if kept:
        assignment.active = False
        assignment.save(update_fields=["active"])
    else:
        assignment.delete()


def set_weights(profile, weights):
    """Store an annual profile's 12 month weights (they must total 100)."""
    if not P.weights_valid(weights):
        raise TargetError("Month weights", "The 12 month weights must each be 0 or more and add up to 100%.")
    TargetProfileMonth.objects.filter(profile=profile).delete()
    TargetProfileMonth.objects.bulk_create([TargetProfileMonth(profile=profile, month_index=i, weight=w)
                                            for i, w in sorted(weights.items())])


def can_delete_profile(profile, now=None):
    """Only while nothing has happened on it: no target started, no incentive."""
    today = today_for(profile.company, now)
    return not profile.targets.filter(period_start__lte=today).exists() and not profile.runs.exists() \
        and not profile.schemes.exists()


def profile_locked_fields(profile):
    """Type and metric are fixed once a profile has made targets (11 §3)."""
    return {"metric", "period_type"} if profile.pk and profile.targets.exists() else set()


def shrink_allowed(profile, starts_on, ends_on, now=None):
    """The range may grow; it may shrink only over periods not yet started."""
    today = today_for(profile.company, now)
    lost = profile.targets.filter(Q(period_end__lt=starts_on) | Q(period_start__gt=ends_on))
    return not lost.filter(period_start__lte=today).exists()


def drop_outside(profile, now=None):
    """After a range change: periods now outside it, not yet started, go."""
    today = today_for(profile.company, now)
    profile.targets.filter(Q(period_end__lt=profile.starts_on) | Q(period_start__gt=profile.ends_on),
                           period_start__gt=today).delete()


# -- Reading ------------------------------------------------------------------------------
#
# Statuses are words, never colour alone (design §6):
#   period Met | On track (achieved >= planned through yesterday) | Behind | Missed (ended) | Not started
#   day    Met | Short (past, below its day's target) | In progress (today) | Upcoming

MONTH_STATUS = {"met": "Met", "track": "On track", "behind": "Behind", "missed": "Missed", "idle": "Not started"}
DAY_STATUS = {"met": "Met", "short": "Short", "live": "In progress", "idle": "Upcoming"}
# A Daily profile's target is one day: progress only (11 P4).
CARD_STATUS = {"met": "Met", "live": "In progress", "short": "Not met", "idle": "Upcoming"}


@dataclass
class Figures:
    amount: Decimal
    achieved: Decimal
    percent: Decimal
    remaining: Decimal


def figures(amount, achieved):
    return Figures(amount, achieved, percent(achieved, amount), max(amount - achieved, ZERO))


def _status(key, labels):
    return {"key": key, "label": labels[key]}


def month_status(target, achieved, planned_to_date, today):
    """A period's status. `planned_to_date`: the plan for the days before today."""
    if target.period_start > today:
        return _status("idle", MONTH_STATUS)
    if achieved >= target.amount:
        return _status("met", MONTH_STATUS)
    if target.period_end < today:
        return _status("missed", MONTH_STATUS)
    return _status("track" if achieved >= planned_to_date else "behind", MONTH_STATUS)


period_status = month_status


def card_status(target, achieved, today):
    """A one-day target: Met, In progress (today), Not met (past), Upcoming."""
    if target.period_start > today:
        return _status("idle", CARD_STATUS)
    if achieved >= target.amount:
        return _status("met", CARD_STATUS)
    return _status("live" if target.period_end >= today else "short", CARD_STATUS)


def pro_rata(target, today):
    """A customer target's share for the days before today (C3: no daily plan)."""
    elapsed = (min(today, target.period_end + datetime.timedelta(days=1)) - target.period_start).days
    total = (target.period_end - target.period_start).days + 1
    return target.amount * max(elapsed, 0) / total


def day_target(row):
    """The day's target: frozen required amount, or the plan for a day not reached yet."""
    return row.required_amount if row.required_amount is not None else row.planned_amount


def day_status(row, achieved, today):
    if row.date > today:
        return _status("idle", DAY_STATUS)
    if row.date == today:
        return _status("met" if achieved >= day_target(row) else "live", DAY_STATUS)
    return _status("met" if achieved >= day_target(row) else "short", DAY_STATUS)


def achieved_for(targets, today):
    """{target_id: achieved so far} for many targets: AED for sales (one query
    per company), unique customers for customers -- counted over each
    target's own window, since overlapping profiles have different windows."""
    by_company = defaultdict(list)
    for t in targets:
        if t.period_start <= today:
            by_company[(t.company, t.kind)].append(t)
    out = {}
    for (company, kind), items in by_company.items():
        if kind == TargetKind.CUSTOMERS:
            windows = defaultdict(list)
            for t in items:
                windows[(t.period_start, min(t.period_end, today))].append(t)
            for (first, last), group in windows.items():
                counts = customers_by_branch(company, {t.branch_id for t in group}, first, last)
                for t in group:
                    out[t.pk] = Decimal(counts.get(t.branch_id, 0))
            continue
        first = min(t.period_start for t in items)
        last = min(max(t.period_end for t in items), today)
        achieved = achieved_by_day(company, {t.branch_id for t in items}, first, last)
        for t in items:
            out[t.pk] = _sum(achieved, t.branch_id, t.period_start, t.period_end)
    return out


def _days_by_target(targets):
    days = defaultdict(list)
    for row in TargetDay.objects.filter(target__in=targets).order_by("date"):
        days[row.target_id].append(row)
    return days


def is_card(target):
    """A Daily profile's target shows as one progress card, not a day table."""
    return target.period_type == PeriodType.DAILY


def month_view(target, now=None):
    """Everything a target's detail (and the apps) show, for any period type.
    `card` is set for a Daily profile's target: progress only (11 P4)."""
    company = target.company
    today = today_for(company, now)
    base = {"target": target, "label": P.target_label(target), "locked": is_locked(target, today),
            "started": target.period_start <= today, "business_today": today, "card": is_card(target),
            "days_left": max(0, (target.period_end - max(today, target.period_start)).days + 1)
            if target.period_end >= today else 0}
    if target.kind == TargetKind.CUSTOMERS:
        got = achieved_for([target], today).get(target.pk, ZERO)
        status = (card_status(target, got, today) if base["card"]
                  else month_status(target, got, pro_rata(target, today), today))
        return {**base, "figures": figures(target.amount, got), "days": [], "today": None, "status": status}
    ensure_frozen([target], today)
    rows = list(target.days.order_by("date"))
    achieved = achieved_by_day(company, [target.branch_id], target.period_start, min(today, target.period_end))
    total = _sum(achieved, target.branch_id, target.period_start, target.period_end)
    if base["card"]:
        return {**base, "figures": figures(target.amount, total), "days": [], "today": None,
                "status": card_status(target, total, today)}
    days = []
    for row in rows:
        got = achieved.get((target.branch_id, row.date), ZERO) if row.date <= today else None
        goal = day_target(row)
        status = day_status(row, got or ZERO, today)
        days.append({"date": row.date, "planned": row.planned_amount, "required": row.required_amount,
                     "target": goal, "achieved": got, "gap": None if got is None else got - goal,
                     "status": status, "state": status["key"]})
    current = next((d for d in days if d["date"] == today), None)
    planned_to_date = sum((r.planned_amount for r in rows if r.date < today), ZERO)
    return {**base, "figures": figures(target.amount, total), "days": days, "today": current,
            "status": month_status(target, total, planned_to_date, today)}


target_view = month_view


def month_targets(branch_ids, year, month, kind=TargetKind.SALES):
    """The monthly-profile targets of a calendar month (the older screens and
    app calls that ask for "the month's target")."""
    first, _ = month_bounds(year, month)
    return (Target.objects.filter(branch_id__in=branch_ids, kind=kind, period_type=PeriodType.MONTHLY,
                                  period_start=first)
            .select_related("company", "branch", "profile", "modified_by"))


def targets_on(branch_ids, day, profile=None, kind=None):
    """Every target whose period covers `day`, optionally of one profile or metric."""
    found = Target.objects.filter(branch_id__in=branch_ids, period_start__lte=day, period_end__gte=day)
    if profile is not None:
        found = found.filter(profile=profile)
    if kind is not None:
        found = found.filter(kind=kind)
    return found.select_related("company", "branch", "profile", "modified_by")


def _rows(targets, now, kind):
    """Overview rows and totals for a set of targets (one per station)."""
    targets = list(targets)
    todays = {t.company_id: today_for(t.company, now) for t in targets}
    achieved = {}
    for cid in todays:
        achieved.update(achieved_for([t for t in targets if t.company_id == cid], todays[cid]))
    days = _days_by_target(targets)
    rows, totals = [], {"target": ZERO, "achieved": ZERO, "today": ZERO, "on_track": 0, "today_set": False}
    for t in sorted(targets, key=lambda t: (t.branch.name.lower(), t.period_start)):
        today = todays[t.company_id]
        got = achieved.get(t.pk, ZERO)
        if is_card(t):
            status = card_status(t, got, today)
        elif kind == TargetKind.CUSTOMERS:
            status = month_status(t, got, pro_rata(t, today), today)
        else:
            status = month_status(t, got, sum((d.planned_amount for d in days[t.pk] if d.date < today), ZERO), today)
        today_row = None if is_card(t) else next((d for d in days[t.pk] if d.date == today), None)
        today_goal = day_target(today_row) if today_row else None
        rows.append({"target": t, "figures": figures(t.amount, got), "status": status, "today": today_goal,
                     "label": P.target_label(t)})
        totals["target"] += t.amount
        totals["achieved"] += got
        totals["on_track"] += status["key"] in ("met", "track")
        if today_goal is not None:
            totals["today"] += today_goal
            totals["today_set"] = True
    totals["percent"] = percent(totals["achieved"], totals["target"])
    totals["stations"] = len(rows)
    # Still to win: each branch's own shortfall added up, so a branch far above
    # its target never hides the ones below theirs.
    totals["remaining"] = sum((r["figures"].remaining for r in rows), ZERO)
    return {"rows": rows, "totals": totals}


def overview(branches, year, month, now=None, kind=TargetKind.SALES):
    """Every station's monthly-profile target for the month, and the totals."""
    return _rows(month_targets([b.pk for b in branches], year, month, kind), now, kind)


def profile_overview(profile, branches, day, now=None):
    """Overview for one profile: each station's period containing `day`."""
    found = targets_on([b.pk for b in branches], day, profile=profile)
    return _rows(found, now, profile.metric)


def station_targets(branch, day, now=None):
    """A station's page: every target, of any profile, overlapping the month of
    `day` -- the monthly target, that month's specials and campaigns, the
    annual year -- with figures. Sorted by period length, then profile."""
    first, last = month_bounds(day.year, day.month)
    found = list(Target.objects.filter(branch=branch, period_start__lte=last, period_end__gte=first)
                 .select_related("company", "branch", "profile"))
    order = {PeriodType.ANNUAL: 0, PeriodType.MONTHLY: 1, PeriodType.CUSTOM: 2, PeriodType.DAILY: 3}
    found.sort(key=lambda t: (order.get(t.period_type, 9), t.kind, t.period_start, t.profile.name.lower()))
    if not found:
        return []
    today = today_for(found[0].company, now)
    achieved = achieved_for(found, today)
    days = _days_by_target([t for t in found if t.kind == TargetKind.SALES and not is_card(t)])
    out = []
    for t in found:
        got = achieved.get(t.pk, ZERO)
        if is_card(t):
            status = card_status(t, got, today)
        elif t.kind == TargetKind.CUSTOMERS:
            status = month_status(t, got, pro_rata(t, today), today)
        else:
            status = month_status(t, got, sum((d.planned_amount for d in days[t.pk] if d.date < today), ZERO), today)
        out.append({"target": t, "label": P.target_label(t), "figures": figures(t.amount, got), "status": status})
    return out


def daily(branches, day, now=None, profile=None):
    """Daily Progress: every multi-day sales target covering `day` -- one row
    per station and profile (a Daily profile's target is a card, not a day)."""
    branches = list(branches)
    found = Target.objects.filter(branch__in=branches, kind=TargetKind.SALES, period_start__lte=day,
                                  period_end__gte=day).exclude(period_type=PeriodType.DAILY)
    if profile is not None:
        found = found.filter(profile=profile)
    targets = list(found.select_related("company", "branch", "profile"))
    by_company = defaultdict(list)
    for t in targets:
        by_company[t.company].append(t)
    achieved, todays = {}, {}
    for company, items in by_company.items():
        today = today_for(company, now)
        todays[company.pk] = today
        if day == today:
            ensure_frozen(items, today)
        if day <= today:
            achieved.update(achieved_by_day(company, {t.branch_id for t in items}, day, day))
    rows_by_target = {r.target_id: r for r in TargetDay.objects.filter(target__in=targets, date=day)}
    rows, totals = [], {"target": ZERO, "achieved": ZERO, "gap": ZERO, "met": 0}
    for t in sorted(targets, key=lambda t: (t.branch.name.lower(), t.profile.name.lower())):
        row = rows_by_target.get(t.pk)
        if row is None:
            continue
        today = todays[t.company_id]
        got = achieved.get((t.branch_id, day), ZERO) if day <= today else None
        goal = day_target(row)
        status = day_status(row, got or ZERO, today)
        rows.append({"target": t, "goal": goal, "achieved": got, "gap": None if got is None else got - goal,
                     "status": status, "label": P.target_label(t)})
        totals["target"] += goal
        if got is not None:
            totals["achieved"] += got
            totals["gap"] += got - goal
        totals["met"] += status["key"] == "met"
    totals["stations"] = len({r["target"].branch_id for r in rows})
    totals["reached"] = any(r["achieved"] is not None for r in rows)
    return {"rows": rows, "totals": totals}


def profile_rows(profiles, now=None):
    """The Target Profiles list: each profile with its range, stations and status."""
    out = []
    for profile in profiles:
        out.append({"profile": profile, "range": P.range_label(profile),
                    "stations": sum(1 for a in profile.assignments.all() if a.active),
                    "targets": profile.targets.count()})
    return out
