"""The order's display status: one list for every screen, report and API
(owner, 3 Nov 2026 -- client change request 5).

The stored Order.status (active / completed / cancelled) is unchanged: the
business rules run on it. This is what people are shown, worked out from the
order and its lines:

    1  stored cancelled                 -> Cancelled
    2  stored completed                 -> Replaced & Received if a line was replaced, else Received
    3  nothing out (all back / removed) -> Awaiting Settlement
    4  out, nothing back                -> Replaced if a line was replaced, else Running
    5  out and back                     -> Replaced & Partially Received, else Partially Received

A line is *out* while active with no return_pending_at; *back* once returned,
or active with return_pending_at (a parked return -- the vehicle is back at the
station, the call is still open). Running and Replaced stay separate (owner,
3 Nov 2026): a swap stays visible at every stage.

with_display_status (SQL, for filters and counts) and display_status (Python,
on loaded lines) must agree; test_order_display_status.py holds them together.
"""

from django.db.models import Case, CharField, Exists, OuterRef, Q, Value, When

from apps.rental.models import OrderItem, OrderItemStatus, OrderStatus

RUNNING = "running"
REPLACED = "replaced"
PARTIALLY_RECEIVED = "partially_received"
REPLACED_PARTIALLY_RECEIVED = "replaced_partially_received"
AWAITING_SETTLEMENT = "awaiting_settlement"
RECEIVED = "received"
REPLACED_RECEIVED = "replaced_received"
CANCELLED = "cancelled"

CHOICES = (
    (RUNNING, "Running"),
    (REPLACED, "Replaced"),
    (PARTIALLY_RECEIVED, "Partially Received"),
    (REPLACED_PARTIALLY_RECEIVED, "Replaced & Partially Received"),
    (AWAITING_SETTLEMENT, "Awaiting Settlement"),
    (RECEIVED, "Received"),
    (REPLACED_RECEIVED, "Replaced & Received"),
    (CANCELLED, "Cancelled"),
)
VALUES = tuple(v for v, _ in CHOICES)
LABELS = dict(CHOICES)

# Badge tone per status, for the web badge partial and Live Monitoring.
TONES = {
    RUNNING: "running", REPLACED: "replaced",
    PARTIALLY_RECEIVED: "partial", REPLACED_PARTIALLY_RECEIVED: "partial",
    AWAITING_SETTLEMENT: "awaiting",
    RECEIVED: "received", REPLACED_RECEIVED: "received",
    CANCELLED: "cancelled",
}

# The web badge class (scr-badge-*) per status.
BADGES = {
    RUNNING: "anytime", REPLACED: "replaced",
    PARTIALLY_RECEIVED: "pending", REPLACED_PARTIALLY_RECEIVED: "pending",
    AWAITING_SETTLEMENT: "pending",
    RECEIVED: "active", REPLACED_RECEIVED: "active",
    CANCELLED: "inactive",
}

_OUT = Q(status=OrderItemStatus.ACTIVE, return_pending_at__isnull=True)
_BACK = Q(status=OrderItemStatus.RETURNED) | Q(status=OrderItemStatus.ACTIVE, return_pending_at__isnull=False)
_REPLACED = Q(status=OrderItemStatus.REPLACED)


def _lines(q):
    return Exists(OrderItem.objects.filter(q, order=OuterRef("pk")))


def with_display_status(orders, name="display_status"):
    """`orders` annotated with `name` (one of VALUES), in SQL."""
    return orders.annotate(
        _ds_out=_lines(_OUT), _ds_back=_lines(_BACK), _ds_replaced=_lines(_REPLACED),
    ).annotate(**{name: Case(
        When(status=OrderStatus.CANCELLED, then=Value(CANCELLED)),
        When(status=OrderStatus.COMPLETED, _ds_replaced=True, then=Value(REPLACED_RECEIVED)),
        When(status=OrderStatus.COMPLETED, then=Value(RECEIVED)),
        When(_ds_out=False, then=Value(AWAITING_SETTLEMENT)),
        When(_ds_back=False, _ds_replaced=True, then=Value(REPLACED)),
        When(_ds_back=False, then=Value(RUNNING)),
        When(_ds_replaced=True, then=Value(REPLACED_PARTIALLY_RECEIVED)),
        default=Value(PARTIALLY_RECEIVED), output_field=CharField(),
    )})


def is_out(item):
    return item.status == OrderItemStatus.ACTIVE and item.return_pending_at is None


def is_back(item):
    return item.status == OrderItemStatus.RETURNED or (
        item.status == OrderItemStatus.ACTIVE and item.return_pending_at is not None)


def display_status(order, items=None):
    """The same status in Python. `items` defaults to order.items.all() (use a
    prefetch to avoid a query per order)."""
    if order.status == OrderStatus.CANCELLED:
        return CANCELLED
    items = list(order.items.all() if items is None else items)
    replaced = any(i.status == OrderItemStatus.REPLACED for i in items)
    if order.status == OrderStatus.COMPLETED:
        return REPLACED_RECEIVED if replaced else RECEIVED
    out = any(is_out(i) for i in items)
    back = any(is_back(i) for i in items)
    if not out:
        return AWAITING_SETTLEMENT
    if not back:
        return REPLACED if replaced else RUNNING
    return REPLACED_PARTIALLY_RECEIVED if replaced else PARTIALLY_RECEIVED


def label(value):
    return LABELS.get(value, "")
