"""Rental -> Live Monitoring: the day's orders, and the push that keeps the
screen current.

Rebuilds the legacy RMS Live Monitoring grid (DMSGetAllStatusTrackingDetails-
ForPartialView_New: Rent Bill; LiveMonitoring_DirectBillSearch: Direct Bill)
with the same fields, grouped Running / Closed; a row opens the order's own
detail page, which holds the full bill. Every order change publishes
`order.changed` with the order's id (`order_changed`, called from
services.run_once and requests.cancel_order); the open screens fetch that one
row back through `rows_json`, so scoping and formatting live only here.
"""

import datetime
from decimal import Decimal

from django.db.models import Prefetch, Q, Sum
from django.urls import reverse

from apps.rental import scoping
from apps.rental.models import (
    OrderAction,
    OrderEvent,
    OrderItem,
    OrderItemStatus,
    OrderStatus,
    Payment,
    PaymentKind,
)
from core.realtime import publish

ZERO = Decimal("0")
STATUS_LABEL = {OrderStatus.ACTIVE: "Running", OrderStatus.COMPLETED: "Completed", OrderStatus.CANCELLED: "Cancelled"}
ITEM_STATE = {
    OrderItemStatus.ACTIVE: "out", OrderItemStatus.RETURNED: "back", OrderItemStatus.REPLACED: "replaced",
    OrderItemStatus.REMOVED: "removed", OrderItemStatus.CANCELLED: "cancelled",
}
_ACTION_LABEL = dict(OrderAction.choices)


def company_group(company_id):
    return f"orders.company.{company_id}"


def order_changed(order):
    """Tell every open Live Monitoring screen of this company that `order`
    changed. Sent after commit; never raises (core.realtime.publish)."""
    publish(company_group(order.company_id), {"type": "order.changed", "order": str(order.pk)})


# -- Which orders -----------------------------------------------------------------------


def day_bounds(day, zone):
    start = datetime.datetime.combine(day, datetime.time.min, tzinfo=zone)
    return start, start + datetime.timedelta(days=1)


def _scope(user, params):
    """The user's orders at the chosen stations (station / emirate filters)."""
    orders = scoping.orders_for(user)
    if params.get("branch", "").isdigit():
        orders = orders.filter(branch_id=int(params["branch"]))
    if params.get("state", "").isdigit():
        orders = orders.filter(branch__location__state_id=int(params["state"]))
    return orders


def orders_for_day(user, params, day, zone, *, today):
    """Booked on `day`, plus -- when `day` is today -- anything still running
    from an earlier day (legacy listed running orders whatever their date)."""
    start, end = day_bounds(day, zone)
    booked = Q(booked_at__gte=start, booked_at__lt=end)
    if today:
        booked |= Q(status=OrderStatus.ACTIVE, booked_at__lt=end)
    orders = _scope(user, params).filter(booked)
    orders = orders.filter(is_direct_bill=params.get("tab") == "direct")
    if params.get("status") == "active":
        orders = orders.filter(status=OrderStatus.ACTIVE)
    text = params.get("q", "").strip()
    if text:
        orders = orders.filter(
            Q(order_no__icontains=text) | Q(customer_name__icontains=text) | Q(customer_mobile__icontains=text)
            | Q(items__vehicle__vehicle_name__icontains=text)
        ).distinct()
    return orders


def with_rows(orders):
    """Everything a row needs, in a fixed number of queries."""
    return orders.select_related("branch__location__state", "created_by").prefetch_related(
        Prefetch("items", queryset=OrderItem.objects.select_related("vehicle").order_by("start_time", "created_on")),
        Prefetch("events", queryset=OrderEvent.objects.select_related("user").order_by("happened_at", "received_at")),
    )


# -- One row ------------------------------------------------------------------------------


def _money(value):
    return f"{value:.2f}" if value is not None else None


def _local(moment, zone):
    return moment.astimezone(zone) if moment else None


def _closed_by(events):
    """Legacy ReceivedBy: who closed the order -- the user on its settle."""
    for event in reversed(events):
        if event.action == OrderAction.SETTLE:
            return event.user.display_name
    return ""


def _last(order, events, zone):
    if events:
        event = events[-1]
        return _ACTION_LABEL.get(event.action, event.action), event.happened_at
    if order.status == OrderStatus.CANCELLED:
        return "Cancelled", order.cancelled_at or order.modified_on
    return "Booked", order.booked_at


def row(order, zone):
    items, events = list(order.items.all()), list(order.events.all())
    active = [i for i in items if i.status == OrderItemStatus.ACTIVE]
    last_label, last_at = _last(order, events, zone)
    ends = [i.expected_end_time for i in active]
    booked, start = _local(order.booked_at, zone), _local(order.start_time, zone)
    closed_at = order.completed_at or order.cancelled_at
    return {
        "id": str(order.pk),
        "order_no": order.order_no,
        "booked": f"{booked:%H:%M} · {booked:%d %b}",
        "station": order.branch.name,
        "emirate": order.branch.location.state.name,
        "customer": order.customer_name or "—",
        "mobile": order.customer_mobile,
        "vehicles": [{"no": i.vehicle.vehicle_name, "state": ITEM_STATE[i.status]} for i in items],
        "out": len(active),
        "back": sum(1 for i in items if i.status == OrderItemStatus.RETURNED),
        "start": order.start_time.isoformat(),
        "start_label": f"{start:%H:%M}",
        "due": min(ends).isoformat() if ends else None,
        "closed_at": closed_at.isoformat() if closed_at else None,
        "opened_by": order.created_by.display_name if order.created_by_id else "—",
        "closed_by": _closed_by(events) or "—",
        "status": order.status,
        "status_label": STATUS_LABEL[order.status],
        "collected": _money(order.paid_amount),
        "net": _money(order.net_amount),
        "tax": _money(order.tax_amount),
        "discount": _money(order.discount_amount) if order.discount_amount else None,
        "last": last_label,
        "last_at": last_at.isoformat() if last_at else None,
        "direct": order.is_direct_bill,
        "url": reverse("rental-order-detail", args=[order.pk]),
    }


# -- The day's totals -------------------------------------------------------------------------


def kpis(user, params, day, zone):
    """Legacy's totals: running vehicles, running orders, orders, cancelled,
    amount collected -- for the chosen stations, both tabs together."""
    orders = _scope(user, params)
    start, end = day_bounds(day, zone)
    payments = Payment.objects.filter(order__in=orders, paid_at__gte=start, paid_at__lt=end)
    taken = payments.exclude(kind=PaymentKind.REFUND).aggregate(t=Sum("amount"))["t"] or ZERO
    refunded = payments.filter(kind=PaymentKind.REFUND).aggregate(t=Sum("amount"))["t"] or ZERO
    booked = orders.filter(booked_at__gte=start, booked_at__lt=end)
    return {
        "vehicles": OrderItem.objects.filter(order__in=orders, status=OrderItemStatus.ACTIVE).count(),
        "running": orders.filter(status=OrderStatus.ACTIVE).count(),
        "today": booked.count(),
        "cancelled": orders.filter(status=OrderStatus.CANCELLED, cancelled_at__gte=start, cancelled_at__lt=end).count(),
        "collected": f"{taken - refunded:,.2f}",
    }


def counts(user, params, day, zone, *, today):
    """Orders per tab, under the other filters."""
    base = {k: v for k, v in params.items() if k != "tab"}
    return {
        tab: orders_for_day(user, {**base, "tab": tab}, day, zone, today=today).count()
        for tab in ("rent", "direct")
    }
