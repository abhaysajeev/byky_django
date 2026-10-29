"""Consolidated -- the client's sheet "Consolidated Report".

Orders **booked** in the range, one row per day x station x booking tablet:
how many, how many still running or cancelled, replaced vehicles, the first
and last receipt number, reprints, discounted and complimentary orders, and
the money of the **settled** ones among them -- bill (net less VAT), VAT,
discount, rounding, net, payments by mode, approved credit notes and the
actual collection (net less them).
"""

from collections import defaultdict

from django.db.models import Count, OuterRef, Q, Subquery
from django.db.models.functions import Coalesce

from apps.devices.services import running_number
from apps.rental.models import (
    DiscountSource,
    OrderItem,
    OrderItemStatus,
    OrderRequest,
    OrderRequestKind,
    OrderRequestStatus,
    OrderStatus,
)
from apps.rental.scoping import orders_for
from apps.reports import common
from apps.reports.base import (
    COUNT,
    DATE,
    GRAND,
    MONEY,
    TEXT,
    ZERO,
    Column,
    Kpi,
    Report,
    Row,
    paged,
    total_of,
)

PAY = {"cash": "Paid By Cash (AED)", "card": "Paid By Card (AED)", "tap": "Paid By Tap & Pay (AED)",
       "upi": "Paid By UPI (AED)", "other": "Paid By Other (AED)"}
_REPRINTS = (OrderRequestKind.REPRINT, OrderRequestKind.ORDER_REPRINT)
_MONEY = ("bill", "tax", "discount", "rounding", "net", "credit")


def _count(queryset):
    return Coalesce(Subquery(queryset.values("order").annotate(n=Count("pk")).values("n")[:1]), 0)


def device_name(device):
    return device.name or str(device)


class Consolidated(Report):
    slug = "consolidated"
    title = "Consolidated"
    sheet_title = "Consolidated Report"
    page_code = "reports.consolidated"
    filters = ("state", "branch", "cashier", "device")
    unit = ("tablet-day", "tablet-days")
    empty = "No orders were booked in this range."
    description = "Orders booked per day, branch and tablet: counts, receipts and the money of the settled ones."

    def build(self, ctx, page=None, per_page=None):
        f = ctx.f
        orders = orders_for(ctx.user).filter(booked_at__gte=f.since, booked_at__lt=f.until)
        orders = common.cashier(common.place(orders, f), f, order="")
        if f.get("device"):
            orders = orders.filter(Q(device__name__icontains=f.get("device"))
                                   | Q(device__device_registration_id__icontains=f.get("device")))
        orders = list(
            orders.select_related("invoice", "device", "branch__location__state")
            .annotate(
                replaced=_count(OrderItem.objects.filter(order=OuterRef("pk"), status=OrderItemStatus.REPLACED)),
                reprints=_count(OrderRequest.objects.filter(order=OuterRef("pk"), kind__in=_REPRINTS,
                                                            status=OrderRequestStatus.USED)),
            )
        )
        settled = [o for o in orders if o.status == OrderStatus.COMPLETED and hasattr(o, "invoice")]
        paid = common.payments_by_order(o.pk for o in settled)
        credited = common.credit_notes_by_order(o.pk for o in settled)

        groups = defaultdict(lambda: {"orders": [], **dict.fromkeys(_MONEY, ZERO),
                                      **{f"pay_{k}": ZERO for k in common.PAY_KEYS}})
        for order in orders:
            key = (order.booked_at.astimezone(ctx.zone).date(), order.branch.location.state.name,
                   order.branch.name, device_name(order.device), order.device_id, order.branch_id)
            g = groups[key]
            g["orders"].append(order)
            invoice = getattr(order, "invoice", None)
            if order.status != OrderStatus.COMPLETED or invoice is None:
                continue
            g["bill"] += invoice.net_amount - invoice.tax_amount
            g["tax"] += invoice.tax_amount
            g["discount"] += invoice.discount_amount
            g["rounding"] += invoice.rounding_adjustment
            g["net"] += invoice.net_amount
            g["credit"] += credited.get(order.pk) or ZERO
            for k, v in paid[order.pk].items():
                g[f"pay_{k}"] += v

        with_other = any(g["pay_other"] for g in groups.values())
        columns = [
            Column("date", "Date", DATE, width=12),
            Column("state", "State", width=14),
            Column("branch", "Branch", width=24),
            Column("device", "Device Name", width=22),
            Column("orders", "Total Orders", COUNT),
            Column("running", "Total Running Orders", COUNT),
            Column("replaced", "No Of Replaced Vehicles", COUNT),
            Column("first_no", "Starting Order No", TEXT),
            Column("last_no", "Ending Order No", TEXT),
            Column("cancelled", "No Of Cancelled Orders", COUNT),
            Column("reprints", "No Of Reprint Orders", COUNT),
            Column("discounted", "No Of Discount Orders", COUNT),
            Column("complimentary", "No Of Complimentary Orders", COUNT),
            Column("bill", "Total Bill Amount (AED)", MONEY),
            Column("tax", "Tax Amount (AED)", MONEY),
            Column("discount", "Discount Amount (AED)", MONEY),
            Column("rounding", "Rounded Difference (AED)", MONEY),
            Column("net", "Net Amount (AED)", MONEY),
            *common.pay_columns(PAY, with_other),
            Column("credit", "Credit Note Amount (AED)", MONEY),
            Column("actual", "Actual Collection Amount (AED)", MONEY),
        ]

        rows = []
        for key in sorted(groups):
            day, state, branch, device, _, _ = key
            g = groups[key]
            members = g["orders"]
            first, last = _receipt_range(members)
            sources = [getattr(getattr(o, "invoice", None), "discount_source", "") for o in members
                       if o.status == OrderStatus.COMPLETED]
            rows.append(Row({
                "date": day, "state": state, "branch": branch, "device": device,
                "orders": len(members),
                "running": sum(1 for o in members if o.status == OrderStatus.ACTIVE),
                "replaced": sum(o.replaced for o in members),
                "first_no": first, "last_no": last,
                "cancelled": sum(1 for o in members if o.status == OrderStatus.CANCELLED),
                "reprints": sum(o.reprints for o in members),
                "discounted": sum(1 for s in sources if s in (DiscountSource.CARD, DiscountSource.MANAGER)),
                "complimentary": sum(1 for s in sources if s == DiscountSource.COMPLIMENTARY),
                **{k: g[k] for k in ("bill", "tax", "discount", "rounding", "net", "credit")},
                **{f"pay_{k}": g[f"pay_{k}"] for k in common.PAY_KEYS},
                "actual": g["net"] - g["credit"],
            }))

        grand = total_of(rows, columns)
        shown = rows + ([Row(grand, GRAND, label="TOTAL")] if rows else [])
        return paged(
            shown, page, per_page, columns=columns, total=grand, count=len(rows),
            kpis=[Kpi("Orders", grand.get("orders", 0), COUNT), Kpi("Running", grand.get("running", 0), COUNT),
                  Kpi("Net amount", grand.get("net", ZERO)),
                  Kpi("Actual collection", grand.get("actual", ZERO))],
        )


def _receipt_range(orders):
    """The first and last running number this tablet printed in the group.
    Within one tablet the numbers are zero-padded to one width, so the
    shortest-then-lowest receipt is the first; only those two are parsed."""
    if not orders:
        return None, None
    ordered = sorted(orders, key=lambda o: (len(o.order_no), o.order_no))
    lo, hi = ordered[0], ordered[-1]
    first = running_number(lo.device, lo.branch, lo.order_no)
    last = running_number(hi.device, hi.branch, hi.order_no)
    return (str(first) if first is not None else lo.order_no), (str(last) if last is not None else hi.order_no)
