"""The dashboard's figures: the branch live carousel, Monthly Sales and the
overdue watch.

Every function runs a fixed number of grouped queries, however many stations
there are -- never one query per branch. Days are the company's (Asia/Dubai
unless the company says otherwise).

A station's **today** starts when its first shift of the day starts and runs
to local midnight, so a bill settled just after closing still counts for the
day. Shifts never cross midnight (company.services.validate_schedule), so the
business day always sits inside one calendar day. A station with no shifts
today counts the whole calendar day.
"""

import datetime
import re
from collections import defaultdict
from decimal import Decimal

from django.core.cache import cache
from django.db.models import Count, Q, Sum
from django.db.models.functions import Coalesce, TruncDate
from django.urls import reverse
from django.utils import timezone

from apps.company.models import BranchWorkingTime
from apps.company.scoping import branches_for
from apps.crew.models import Attendance, PunchType
from apps.devices.models import DeviceMapping, DeviceStatus
from apps.rental import scoping as rental_scoping
from apps.rental.models import OrderItem, OrderItemStatus
from core.timezones import zone_for

LIVE_CACHE_SECONDS = 30
OVERDUE_ROWS = 50           # the card shows pages of 5; more than this is a list, not a glance

OPEN, CLOSED, NOT_SET = "open", "closed", "not_set"


def _zone(user):
    return zone_for(getattr(user, "company", None))


def _midnight(day, zone):
    return datetime.datetime.combine(day, datetime.time.min, tzinfo=zone)


def _aed(amount):
    return f"{(amount or Decimal('0')):,.3f}"


def format_mobile(mobile):
    """`971501234567` -> `+971 50 123 4567`. Anything else is shown as stored."""
    match = re.fullmatch(r"\+?971(\d{2})(\d{3})(\d{4})", (mobile or "").strip())
    if not match:
        return mobile or ""
    return "+971 {} {} {}".format(*match.groups())


# -- The branch carousel --------------------------------------------------------------


def branch_days(branch_ids, now, zone):
    """{branch_id: {"hours", "state", "starts"}} for today.

    `hours` is the raw shift text ("07:00–23:00", shifts joined by " · "),
    `starts` the moment today's counting begins.
    """
    local = now.astimezone(zone)
    midnight = _midnight(local.date(), zone)
    minute = local.time().replace(second=0, microsecond=0)

    today = defaultdict(list)
    for branch_id, start, end in (
        BranchWorkingTime.objects
        .filter(branch_id__in=branch_ids, is_active=True, week_day=local.weekday())
        .order_by("branch_id", "shift_number")
        .values_list("branch_id", "start_time", "end_time")
    ):
        today[branch_id].append((start, end))
    has_hours = set(
        BranchWorkingTime.objects.filter(branch_id__in=branch_ids, is_active=True)
        .values_list("branch_id", flat=True).distinct()
    )

    days = {}
    for branch_id in branch_ids:
        shifts = today.get(branch_id)
        if shifts:
            days[branch_id] = {
                "hours": " · ".join(f"{start:%H:%M}–{end:%H:%M}" for start, end in shifts),
                # Start and end minutes both count, as in company.services.branch_open_state.
                "state": OPEN if any(start <= minute <= end for start, end in shifts) else CLOSED,
                "starts": datetime.datetime.combine(local.date(), shifts[0][0], tzinfo=zone),
            }
        elif branch_id in has_hours:
            days[branch_id] = {"hours": "Closed today", "state": CLOSED, "starts": midnight}
        else:
            days[branch_id] = {"hours": "No hours set", "state": NOT_SET, "starts": midnight}
    return days


def _since_each_start(field, branch_field, days):
    """One Q matching `field >= that branch's start`, grouped by start so the
    usual case (everyone opens at midnight) is a single condition."""
    by_start = defaultdict(list)
    for branch_id, day in days.items():
        by_start[day["starts"]].append(branch_id)
    condition = Q(pk__in=[])
    for start, ids in by_start.items():
        condition |= Q(**{f"{branch_field}__in": ids, f"{field}__gte": start})
    return condition


def branch_live(user, now=None):
    """One slide per active station, busiest first: vehicles on rent now,
    today's invoices and revenue (what customers paid), tablets seen today and
    staff punched in and not yet out."""
    now = now or timezone.now()
    zone = _zone(user)
    branches = list(branches_for(user).filter(is_active=True).order_by("name").values("id", "name"))
    if not branches:
        return []
    ids = [b["id"] for b in branches]
    days = branch_days(ids, now, zone)
    midnight = _midnight(now.astimezone(zone).date(), zone)

    on_rent = dict(
        OrderItem.objects.filter(status=OrderItemStatus.ACTIVE, order__branch_id__in=ids)
        .values("order__branch_id").annotate(n=Count("id")).values_list("order__branch_id", "n")
    )
    billed = {
        row["branch_id"]: row
        for row in rental_scoping.invoices_for(user)
        .filter(_since_each_start("issued_at", "branch_id", days))
        .values("branch_id").annotate(n=Count("id"), total=Sum("net_amount"))
    }
    # A tablet counts at the station it is mapped to now, if it has been
    # heard from since that station's day began.
    devices = dict(
        DeviceMapping.objects.filter(to_date__isnull=True, device__status=DeviceStatus.APPROVED)
        .filter(_since_each_start("device__last_seen_at", "branch_id", days))
        .values("branch_id").annotate(n=Count("device_id", distinct=True))
        .values_list("branch_id", "n")
    )
    # Staff may punch in before opening, so on duty looks at the whole
    # calendar day. The station is where the punch was scanned; a manager's
    # own punch has no scanning station, so it falls back to their branch.
    staff = dict(
        Attendance.objects.filter(punch_type=PunchType.PUNCH_IN, punch_out__isnull=True,
                                  rms_scan_time__gte=midnight)
        .annotate(station=Coalesce("rms_branch_id", "employee_branch_id"))
        .filter(station__in=ids)
        .values("station").annotate(n=Count("employee_id", distinct=True))
        .values_list("station", "n")
    )

    slides = []
    for branch in branches:
        bid = branch["id"]
        bill = billed.get(bid, {})
        revenue = bill.get("total") or Decimal("0")
        slides.append({
            "id": bid,
            "name": branch["name"],
            "hours": days[bid]["hours"],
            "state": days[bid]["state"],
            "on_rent": on_rent.get(bid, 0),
            "invoices": bill.get("n", 0),
            "revenue": _aed(revenue),
            "devices": devices.get(bid, 0),
            "staff": staff.get(bid, 0),
            "_revenue": revenue,
        })
    slides.sort(key=lambda s: (-s["on_rent"], -s["_revenue"], s["name"]))
    for slide in slides:
        del slide["_revenue"]
    return slides


# -- Monthly Sales ------------------------------------------------------------------


def _month_start(day):
    return day.replace(day=1)


def monthly_sales(user, now=None):
    """This calendar month so far, against the same days of last month."""
    now = now or timezone.now()
    zone = _zone(user)
    local = now.astimezone(zone)
    today = local.date()
    first = _month_start(today)
    prev_first = _month_start(first - datetime.timedelta(days=1))
    prev_days = (first - prev_first).days

    # Last month up to the same moment: the same day number and time of day,
    # or the whole of last month when it is shorter than today's date.
    if today.day <= prev_days:
        prev_cut = datetime.datetime.combine(prev_first.replace(day=today.day), local.time(), tzinfo=zone)
    else:
        prev_cut = _midnight(first, zone)

    invoices = rental_scoping.invoices_for(user).filter(issued_at__gte=_midnight(prev_first, zone))
    totals = invoices.aggregate(
        current=Sum("net_amount", filter=Q(issued_at__gte=_midnight(first, zone))),
        previous=Sum("net_amount", filter=Q(issued_at__lt=prev_cut)),
    )
    by_day = dict(
        invoices.filter(issued_at__gte=_midnight(first, zone))
        .annotate(day=TruncDate("issued_at", tzinfo=zone))
        .values("day").annotate(total=Sum("net_amount")).values_list("day", "total")
    )

    total = totals["current"] or Decimal("0")
    previous = totals["previous"] or Decimal("0")
    if previous > 0:
        change = (total - previous) / previous * 100
        delta = {"text": f"{change:+.1f}%", "direction": "up" if change >= 0 else "down"}
    else:
        delta = {"text": "—", "direction": "none"}

    days = [first + datetime.timedelta(days=n) for n in range(today.day)]
    series = [float(by_day.get(day) or 0) for day in days]

    peak = best_day = ""
    if total > 0:
        peak_at = max(range(len(series)), key=lambda n: series[n])
        peak = f"Peak {days[peak_at]:%-d %b} · AED {series[peak_at]:,.0f}"
        sums, counts = defaultdict(float), defaultdict(int)
        for day, value in zip(days, series, strict=True):
            sums[day.weekday()] += value
            counts[day.weekday()] += 1
        weekday = max(sums, key=lambda wd: sums[wd] / counts[wd])
        best_day = f"Best day: {calendar_day_name(weekday)}"

    return {
        "title": f"{first:%B %Y} to date",
        "total": f"{total:,.0f}",
        "delta": delta,
        "daily_avg": f"{total / today.day:,.0f}",
        "labels": [f"{day:%-d %b}" for day in days],
        "series": series,
        "peak": peak,
        "best_day": best_day,
    }


def calendar_day_name(weekday):
    return ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")[weekday]


# -- The overdue watch ----------------------------------------------------------------


def overdue_watch(user, now=None):
    """Vehicles on rent now, and those running past their booked end time,
    longest overdue first."""
    now = now or timezone.now()
    items = OrderItem.objects.filter(order__in=rental_scoping.orders_for(user), status=OrderItemStatus.ACTIVE)
    counts = items.aggregate(on_rent=Count("id"), overdue=Count("id", filter=Q(expected_end_time__lt=now)))
    rows = (
        items.filter(expected_end_time__lt=now)
        .select_related("order__branch", "vehicle")
        .order_by("expected_end_time")[:OVERDUE_ROWS]
    )
    return {
        "on_rent": counts["on_rent"],
        "overdue": counts["overdue"],
        "rows": [
            {
                "vehicle": item.vehicle.vehicle_name,
                "station": item.order.branch.name,
                "customer": item.order.customer_name or "—",
                "mobile": format_mobile(item.order.customer_mobile) or "—",
                "due": item.expected_end_time.isoformat(),
                "url": reverse("rental-order-detail", args=[item.order_id]),
            }
            for item in rows
        ],
    }


# -- The live payload -----------------------------------------------------------------


def live_payload(user):
    """Everything the page refreshes, cached briefly per scope so several
    managers watching at once cost one calculation."""
    key = f"dashboard-live:{getattr(user, 'scope', '')}:{getattr(user, 'company_id', '')}"
    payload = cache.get(key)
    if payload is None:
        now = timezone.now()
        payload = {
            "generated_at": now.isoformat(),
            "branches": branch_live(user, now),
            "overdue": overdue_watch(user, now),
        }
        cache.set(key, payload, LIVE_CACHE_SECONDS)
    return payload
