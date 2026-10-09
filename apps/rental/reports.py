"""The manager app's figures: station masters, collections by state and branch,
and a branch's orders (apps/rental/manager_api.py).

Rules agreed with the owner (9 Oct 2026):
- Collections are **settled** orders: the invoice's net, dated by when it was
  issued (the settle), company-local days, both dates included -- the same
  basis as the dashboard's Monthly Sales. Approved credit notes on those
  orders are returned beside the net, never subtracted. Every state / branch
  is listed, 0 when it sold nothing; an inactive branch only when it sold.
- Order details are the orders **booked** in the range at one branch -- running,
  settled and cancelled -- with a status summary.

Totals are worked out in the database (one grouped query each), so a long
range costs index reads, not rows in Python; the order list is paged and its
lines, payments and credit notes are fetched once per page.
"""

import datetime
from decimal import Decimal

from django.core.paginator import Paginator
from django.db.models import Case, CharField, Count, Exists, Max, OuterRef, Q, Sum, Value, When
from django.utils import timezone

from apps.company.models import Branch, State
from apps.rental.models import (
    CreditNote,
    CreditNoteStatus,
    Invoice,
    Order,
    OrderItem,
    OrderItemStatus,
    OrderStatus,
)

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
PAGE_SIZE = 50
ZERO = Decimal("0.000")

# The order statuses the manager app shows, derived from the order and its lines.
RUNNING, PARTIAL, AWAITING, RECEIVED, CANCELLED = (
    "running", "partially_received", "awaiting_settlement", "fully_received", "cancelled")
ORDER_STATUSES = (RUNNING, PARTIAL, AWAITING, RECEIVED, CANCELLED)

# The lines that bill: out now, or back. Replaced and removed lines do not.
_BILLING = (OrderItemStatus.ACTIVE, OrderItemStatus.RETURNED)


def day_bounds(start, end, zone):
    """[from 00:00, the day after `end` 00:00) in the company's zone."""
    return (datetime.datetime.combine(start, datetime.time.min, tzinfo=zone),
            datetime.datetime.combine(end + datetime.timedelta(days=1), datetime.time.min, tzinfo=zone))


def _local(moment, zone):
    return moment.astimezone(zone).strftime(TIME_FORMAT) if moment else None


def _money(value):
    return str(value if value is not None else ZERO)


# -- Masters ------------------------------------------------------------------------


def active_branches(company_ids):
    return (Branch.objects.filter(company_id__in=company_ids, is_active=True)
            .select_related("location__state"))


def states(company_ids):
    """The states the company has active branches in, with how many."""
    rows = (State.objects.filter(locations__branches__company_id__in=company_ids,
                                 locations__branches__is_active=True)
            .annotate(branch_count=Count("locations__branches", distinct=True)).order_by("name"))
    return [{"state_id": s.pk, "code": s.short_code, "name": s.name, "branch_count": s.branch_count}
            for s in rows]


def branches(company_ids, state_id=None):
    rows = active_branches(company_ids).order_by("name")
    if state_id:
        rows = rows.filter(location__state_id=state_id)
    return [{"branch_id": b.pk, "code": b.short_code, "name": b.name, "state_id": b.location.state_id,
             "state": b.location.state.name, "location": b.location.name} for b in rows]


# -- Collections ----------------------------------------------------------------------


def _collections_by_branch(company_ids, start, end, zone):
    """{branch_id: (orders, net, credit_notes)} for invoices issued in the range."""
    since, until = day_bounds(start, end, zone)
    invoices = Invoice.objects.filter(company_id__in=company_ids, issued_at__gte=since, issued_at__lt=until)
    sold = {row["branch_id"]: row for row in invoices.values("branch_id").annotate(
        orders=Count("id"), net=Sum("net_amount")).order_by()}
    credited = dict(
        CreditNote.objects.filter(status=CreditNoteStatus.APPROVED, invoice__in=invoices)
        .values("branch_id").annotate(total=Sum("net_amount")).order_by().values_list("branch_id", "total")
    )
    return {branch_id: (row["orders"], row["net"] or ZERO, credited.get(branch_id) or ZERO)
            for branch_id, row in sold.items()} | {
        branch_id: (0, ZERO, total) for branch_id, total in credited.items() if branch_id not in sold}


def _listed_branches(company_ids, figures):
    """Active branches, and inactive ones only when they have figures."""
    return (Branch.objects.filter(company_id__in=company_ids)
            .filter(Q(is_active=True) | Q(pk__in=list(figures)))
            .select_related("location__state"))


def _row(orders, net, credit_notes):
    return {"orders": orders, "net_amount": _money(net), "credit_notes": _money(credit_notes)}


def _sorted(rows):
    return sorted(rows, key=lambda r: (-Decimal(r["net_amount"]), r["name"]))


def _total(rows):
    return _row(sum(r["orders"] for r in rows), sum((Decimal(r["net_amount"]) for r in rows), ZERO),
                sum((Decimal(r["credit_notes"]) for r in rows), ZERO))


def collections_by_state(company_ids, start, end, zone):
    figures = _collections_by_branch(company_ids, start, end, zone)
    per_state = {}
    for branch in _listed_branches(company_ids, figures):
        state = branch.location.state
        orders, net, credit = figures.get(branch.pk, (0, ZERO, ZERO))
        entry = per_state.setdefault(state.pk, {"state_id": state.pk, "code": state.short_code,
                                                "name": state.name, "orders": 0, "net": ZERO, "credit": ZERO})
        entry["orders"] += orders
        entry["net"] += net
        entry["credit"] += credit
    rows = _sorted([{"state_id": e["state_id"], "code": e["code"], "name": e["name"],
                     **_row(e["orders"], e["net"], e["credit"])} for e in per_state.values()])
    return rows, _total(rows)


def collections_by_branch(company_ids, state_id, start, end, zone):
    figures = _collections_by_branch(company_ids, start, end, zone)
    rows = _sorted([
        {"branch_id": b.pk, "code": b.short_code, "name": b.name, **_row(*figures.get(b.pk, (0, ZERO, ZERO)))}
        for b in _listed_branches(company_ids, figures).filter(location__state_id=state_id)
    ])
    return rows, _total(rows)


# -- Order details ------------------------------------------------------------------------


def _has_line(status):
    return Exists(OrderItem.objects.filter(order=OuterRef("pk"), status=status))


def _with_status(orders):
    """Each order with its manager-app status, worked out in SQL from its lines
    (EXISTS, not counts, so the status can be grouped on for the summary)."""
    return orders.annotate(
        has_out=_has_line(OrderItemStatus.ACTIVE),
        has_back=_has_line(OrderItemStatus.RETURNED),
        has_replaced=_has_line(OrderItemStatus.REPLACED),
    ).annotate(
        app_status=Case(
            When(status=OrderStatus.CANCELLED, then=Value(CANCELLED)),
            When(status=OrderStatus.COMPLETED, then=Value(RECEIVED)),
            When(has_out=False, then=Value(AWAITING)),
            When(has_back=False, then=Value(RUNNING)),
            default=Value(PARTIAL), output_field=CharField(),
        ),
    )


def branch_orders(company_ids, branch_id, start, end, zone, *, status="all", page=1):
    """(branch, summary, rows, page) -- or None when the branch is not the user's."""
    branch = Branch.objects.filter(pk=branch_id, company_id__in=company_ids).first()
    if branch is None:
        return None
    since, until = day_bounds(start, end, zone)
    orders = _with_status(Order.objects.filter(branch=branch, booked_at__gte=since, booked_at__lt=until))

    counts = dict(orders.values("app_status").annotate(n=Count("id")).order_by().values_list("app_status", "n"))
    summary = {"all": sum(counts.values()), **{s: counts.get(s, 0) for s in ORDER_STATUSES},
               "replaced": orders.filter(has_replaced=True).count()}

    if status != "all":
        orders = orders.filter(app_status=status)
    listed = (orders.select_related("invoice").annotate(last_expected=Max("items__expected_end_time"))
              .prefetch_related("items__vehicle__vehicle_type", "payments__mode", "credit_notes")
              .order_by("-booked_at", "-id"))
    paged = Paginator(listed, PAGE_SIZE).get_page(page)
    now = timezone.now()
    return branch, summary, [_order_json(order, branch, zone, now) for order in paged.object_list], paged


def _minutes(item, now):
    """Run minutes: as billed once back, so far while out."""
    if item.run_minutes is not None:
        return item.run_minutes
    return max(int(((item.end_time or now) - item.start_time).total_seconds() // 60), 0)


def _order_json(order, branch, zone, now):
    lines = sorted((i for i in order.items.all() if i.status in _BILLING), key=lambda i: i.start_time)
    settled = order.status == OrderStatus.COMPLETED
    ends = [i.end_time for i in lines if i.end_time]
    end = (max(ends) if settled and ends else order.last_expected) if lines else None
    if settled:
        amount = order.net_amount
    else:   # what the lines come to so far: back at their total, out at their package price
        amount = sum((i.total_amount if i.status == OrderItemStatus.RETURNED and i.total_amount is not None
                      else i.base_fare for i in lines), ZERO)
    note = next((n for n in order.credit_notes.all() if n.status in (CreditNoteStatus.PENDING,
                                                                       CreditNoteStatus.APPROVED)), None)
    invoice = getattr(order, "invoice", None)
    return {
        "order_id": str(order.pk),
        "order_no": order.order_no,
        "station_name": branch.name,
        "customer_name": order.customer_name,
        "customer_mobile": order.customer_mobile,
        "booked_at": _local(order.booked_at, zone),
        "start_time": _local(order.start_time, zone),
        "end_time": _local(end, zone),
        "duration_minutes": max(int((end - order.start_time).total_seconds() // 60), 0) if end else None,
        "order_status": order.app_status,
        "amount": _money(amount),
        "advance_paid": _money(order.paid_amount),
        "invoice_no": invoice.invoice_no if invoice else None,
        "credit_note": {"credit_note_no": note.credit_note_no or None, "status": note.status,
                        "net_amount": str(note.net_amount) if note.net_amount is not None else None}
        if note else None,
        "vehicles": [
            {"vehicle_name": i.vehicle.vehicle_name, "vehicle_type": i.vehicle.vehicle_type.vehicle_type_name,
             "vehicle_status": "running" if i.status == OrderItemStatus.ACTIVE else i.status,
             "start_time": _local(i.start_time, zone), "end_time": _local(i.end_time, zone),
             "minutes": _minutes(i, now)}
            for i in sorted(order.items.all(), key=lambda i: i.start_time)
        ],
        "payments": [
            {"mode": p.mode.name, "kind": p.kind, "amount": str(p.amount), "reference_no": p.reference_no,
             "paid_at": _local(p.paid_at, zone)}
            for p in sorted(order.payments.all(), key=lambda p: (p.paid_at, p.created_on))
        ],
    }

