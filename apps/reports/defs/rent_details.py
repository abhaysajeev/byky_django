"""Rent Details -- the client's sheet "Rent Details Report".

Orders **booked** in the range, newest last, one block per order: the order's
columns once (merged down its vehicle rows in Excel), then one row per
vehicle line. Paged by order in SQL; the TOTAL is worked out in SQL over every
order the filters find, never from one page.

Rules agreed with the owner (29 Oct 2026):
- The discount is on the whole bill, so Supervisor Discount (a manager's
  discount, or a complimentary bill), Discount Card, Card Type and Card Grade
  are order columns, not vehicle ones.
- With a category, vehicle type or vehicle number filter, an order is listed
  when any of its lines matches and only the matching lines are shown; the
  order's amounts stay the whole bill, counted once in the TOTAL.
- A line's Initial and Total Fare are shown for billed lines only (out now, or
  back); a replaced, removed or cancelled line is not billed and shows a dash,
  so the fares add up to the order's Initial Order Amount.
- Status filter: Running, Partially Received (an order with every vehicle back
  but not yet settled counts here too), Received (settled), Replaced (has a
  replaced line), Cancelled, Complimentary, Direct Rent, Discounted (a card or
  manager discount on the bill).
"""

from django.db.models import Count, DecimalField, Exists, F, OuterRef, Prefetch, Q, Subquery, Sum
from django.db.models.functions import Coalesce

from apps.rental.models import (
    CreditNote,
    CreditNoteStatus,
    DiscountSource,
    OrderItem,
    OrderItemStatus,
    OrderStatus,
    Payment,
    PaymentKind,
)
from apps.rental.reports import AWAITING, CANCELLED, PARTIAL, RECEIVED, RUNNING, _with_status
from apps.rental.scoping import orders_for
from apps.reports import common
from apps.reports.base import (
    COUNT,
    DATETIME,
    GRAND,
    MONEY,
    PERCENT,
    ZERO,
    Column,
    Kpi,
    Report,
    Result,
    Row,
    local,
    window,
)

PAY = {"cash": "Paid By Cash", "card": "Paid By Card", "tap": "Paid By Tap & Pay", "upi": "Paid By UPI",
       "other": "Paid By Other"}
STATUS_LABELS = {RUNNING: "Running", PARTIAL: "Partially Received", AWAITING: "Partially Received",
                 RECEIVED: "Received", CANCELLED: "Cancelled"}
LINE_LABELS = {OrderItemStatus.ACTIVE: "Running", OrderItemStatus.RETURNED: "Received",
               OrderItemStatus.REPLACED: "Replaced", OrderItemStatus.REMOVED: "Removed",
               OrderItemStatus.CANCELLED: "Cancelled"}
_MANAGER = (DiscountSource.MANAGER, DiscountSource.COMPLIMENTARY)
_DEC = DecimalField(max_digits=13, decimal_places=3)
VEHICLE_NOTE = "Vehicle columns show the matching vehicles only; amounts are whole orders."


def _sum(queryset, field):
    """A per-order sum as a subquery, 0 when there is nothing."""
    return Coalesce(Subquery(queryset.values("order").annotate(s=Sum(field)).values("s")[:1]),
                    ZERO, output_field=_DEC)


class RentDetails(Report):
    slug = "rent-details"
    title = "Rent Details"
    sheet_title = "Rent Details Report"
    page_code = "reports.rent_details"
    filters = ("state", "branch", "cashier", "category", "vehicle_type", "customer", "phone", "order_no",
               "vehicle_no", "status")
    statuses = (("running", "Running"), ("partially_received", "Partially Received"), ("received", "Received"),
                ("replaced", "Replaced"), ("cancelled", "Cancelled"), ("complimentary", "Complimentary"),
                ("direct_rent", "Direct Rent"), ("discounted", "Discounted"))
    unit = ("order", "orders")
    empty = "No orders were booked in this range."
    description = "Every order booked in the range, with its bill, payments and each vehicle."

    def orders(self, ctx):
        f = ctx.f
        qs = orders_for(ctx.user).filter(booked_at__gte=f.since, booked_at__lt=f.until)
        qs = common.customer(common.cashier(common.place(qs, f), f, order=""), f, order="")
        if f.any_vehicle:
            qs = qs.filter(Exists(OrderItem.objects.filter(common.vehicle_q(f), order=OuterRef("pk"))))
        qs = _with_status(qs)
        status = f.get("status")
        if status:
            qs = qs.filter({
                "running": Q(app_status=RUNNING),
                "partially_received": Q(app_status__in=(PARTIAL, AWAITING)),
                "received": Q(app_status=RECEIVED),
                "cancelled": Q(app_status=CANCELLED),
                "replaced": Q(has_replaced=True),
                "complimentary": Q(invoice__discount_source=DiscountSource.COMPLIMENTARY),
                "direct_rent": Q(is_direct_bill=True),
                "discounted": Q(invoice__discount_source__in=(DiscountSource.CARD, DiscountSource.MANAGER)),
            }[status])
        return qs

    def size(self, ctx):
        return self.orders(ctx).count()

    def columns(self, with_other):
        order = "order"
        return [
            Column("branch", "Branch", part=order, width=22),
            Column("order_no", "Order Number", part=order, width=20),
            Column("booked", "Order Date And Time", DATETIME, part=order, width=21),
            Column("status", "Order Status", part=order, width=17),
            Column("open_by", "Open By", part=order, width=28),
            Column("close_by", "Close By", part=order, width=28),
            Column("customer", "Customer Name", part=order, width=20),
            Column("phone", "Phone No", part=order, width=16),
            Column("initial", "Initial Order Amount (AED)", MONEY, part=order),
            Column("collected", "Collected  Amount (AED)", MONEY, part=order),
            Column("deposit", "Deposit/Balance to Pay (AED)", MONEY, part=order),
            Column("rounding", "Rounded Difference", MONEY, part=order),
            Column("tax_pct", "Tax Percentage", PERCENT, part=order, width=10),
            Column("tax", "Tax Amount (AED)", MONEY, part=order),
            Column("net", "Net Amount (AED)", MONEY, part=order),
            Column("credit", "Credit Note Amount (AED)", MONEY, part=order),
            Column("actual", "Actual Collection Amount (AED)", MONEY, part=order),
            *common.pay_columns(PAY, with_other, part=order),
            Column("refs", "Referance Number", part=order, width=18),
            Column("vehicle_no", "Vehicle Number", part="line", width=14),
            Column("vehicle_status", "Vehicle Status", part="line", width=13),
            Column("start", "Start Date And Time", DATETIME, part="line", width=21),
            Column("exit", "Exit Date And Time", DATETIME, part="line", width=21),
            Column("initial_fare", "Initial Fare Amount (AED)", MONEY, part="line"),
            Column("total_fare", "Total Fare Amount (AED)", MONEY, part="line"),
            Column("supervisor", "SupervisorDiscount Amount (AED)", MONEY, part=order),
            Column("card_discount", "Discount Card (AED)", MONEY, part=order),
            Column("card_type", "Card Type", part=order, width=14),
            Column("card_grade", "Card Grade", part=order, width=22),
            Column("bill", "Bill Amount (AED)", MONEY, part=order),
        ]

    def build(self, ctx, page=None, per_page=None):
        f = ctx.f
        orders = self.orders(ctx)
        size = orders.count()
        page, pages, start, stop = window(size, page, per_page)
        ids = orders.values("pk")

        lines = OrderItem.objects.filter(order__in=ids)
        shown_lines = lines.filter(common.vehicle_q(f)) if f.any_vehicle else lines
        totals = self._totals(orders, ids, shown_lines)
        with_other = bool(totals["pay_other"])
        columns = self.columns(with_other)

        chunk = list(
            orders.order_by("booked_at", "pk")[start:stop]
            .select_related("branch", "invoice__issued_by__employee", "created_by__employee",
                            "discount_claim__card_type", "discount_claim__card_grade")
            .annotate(
                initial=_sum(OrderItem.objects.filter(order=OuterRef("pk"), status__in=common.BILLING), "base_fare"),
                advance=_sum(Payment.objects.filter(order=OuterRef("pk"), kind=PaymentKind.ADVANCE), "amount"),
            )
            .prefetch_related(Prefetch(
                "items", to_attr="shown",
                queryset=OrderItem.objects.filter(common.vehicle_q(f)).select_related("vehicle")
                .order_by("start_time", "pk"),
            ))
        )
        page_ids = [o.pk for o in chunk]
        paid = common.payments_by_order(page_ids)
        refs = common.references_by_order(page_ids)
        credited = common.credit_notes_by_order(page_ids)
        rows = [self._row(o, ctx, paid[o.pk], refs.get(o.pk, ""), credited.get(o.pk) or ZERO) for o in chunk]
        if rows and stop == size:
            rows.append(Row(totals, GRAND, label="TOTAL"))
        return Result(
            columns=columns, rows=rows, total=totals, count=size, page=page, pages=pages,
            first=start + 1 if stop > start else 0, last=stop, size=size,
            note=VEHICLE_NOTE if f.any_vehicle else "",
            kpis=[Kpi("Orders", size, COUNT), Kpi("Vehicles", totals["_vehicles"], COUNT),
                  Kpi("Net amount", totals["net"]), Kpi("Collected", totals["collected"])],
        )

    def _row(self, order, ctx, paid, refs, credit):
        invoice = getattr(order, "invoice", None)
        settled = order.status == OrderStatus.COMPLETED and invoice is not None
        source = invoice.discount_source if settled else ""
        claim = order.discount_claim if source == DiscountSource.CARD else None
        cells = {
            "branch": order.branch.name,
            "order_no": order.order_no,
            "booked": local(order.booked_at, ctx.zone),
            "status": STATUS_LABELS.get(order.app_status, order.app_status),
            "open_by": common.person(order.created_by),
            "close_by": common.person(invoice.issued_by) if settled else None,
            "customer": order.customer_name,
            "phone": order.customer_mobile,
            "initial": order.initial,
            "collected": order.paid_amount,
            "deposit": order.paid_amount - order.net_amount if settled else order.advance,
            "rounding": order.rounding_adjustment if settled else None,
            "tax_pct": order.tax_percentage if settled else None,
            "tax": order.tax_amount if settled else None,
            "net": order.net_amount if settled else None,
            "credit": credit if settled else None,
            "actual": order.net_amount - credit if settled else None,
            **common.pay_cells(paid),
            "refs": refs or None,
            "supervisor": (invoice.discount_amount if source in _MANAGER else ZERO) if settled else None,
            "card_discount": (invoice.discount_amount if source == DiscountSource.CARD else ZERO) if settled else None,
            "card_type": claim.card_type.name if claim else None,
            "card_grade": claim.card_grade.name if claim else None,
            "bill": order.net_amount - order.tax_amount if settled else None,
        }
        lines = []
        for item in order.shown:
            billed = item.status in common.BILLING
            lines.append({
                "vehicle_no": item.vehicle.vehicle_code,
                "vehicle_status": LINE_LABELS.get(item.status, item.status),
                "start": local(item.start_time, ctx.zone),
                "exit": local(item.end_time, ctx.zone),
                "initial_fare": item.base_fare if billed else None,
                "total_fare": item.total_amount if billed else None,
            })
        return Row(cells, lines=lines)

    def _totals(self, orders, ids, shown_lines):
        """Every order the filters find, each counted once; the line columns over
        the lines shown."""
        settled = Q(status=OrderStatus.COMPLETED)
        agg = orders.aggregate(
            collected=Coalesce(Sum("paid_amount"), ZERO, output_field=_DEC),
            rounding=Coalesce(Sum("rounding_adjustment", filter=settled), ZERO, output_field=_DEC),
            tax=Coalesce(Sum("tax_amount", filter=settled), ZERO, output_field=_DEC),
            net=Coalesce(Sum("net_amount", filter=settled), ZERO, output_field=_DEC),
            balance=Coalesce(Sum(F("paid_amount") - F("net_amount"), filter=settled), ZERO, output_field=_DEC),
            supervisor=Coalesce(Sum("invoice__discount_amount", filter=settled & Q(
                invoice__discount_source__in=_MANAGER)), ZERO, output_field=_DEC),
            card_discount=Coalesce(Sum("invoice__discount_amount", filter=settled & Q(
                invoice__discount_source=DiscountSource.CARD)), ZERO, output_field=_DEC),
        )
        advance = (Payment.objects.filter(order__in=orders.exclude(status=OrderStatus.COMPLETED).values("pk"),
                                          kind=PaymentKind.ADVANCE)
                   .aggregate(s=Coalesce(Sum("amount"), ZERO, output_field=_DEC))["s"])
        credit = (CreditNote.objects.filter(order__in=ids, status=CreditNoteStatus.APPROVED)
                  .aggregate(s=Coalesce(Sum("net_amount"), ZERO, output_field=_DEC))["s"])
        initial = (OrderItem.objects.filter(order__in=ids, status__in=common.BILLING)
                   .aggregate(s=Coalesce(Sum("base_fare"), ZERO, output_field=_DEC))["s"])
        fares = shown_lines.filter(status__in=common.BILLING).aggregate(
            initial_fare=Coalesce(Sum("base_fare"), ZERO, output_field=_DEC),
            total_fare=Coalesce(Sum("total_amount"), ZERO, output_field=_DEC))
        paid = dict.fromkeys(common.PAY_KEYS, ZERO)
        for row in (Payment.objects.filter(order__in=ids).values("mode__name", "kind")
                    .annotate(total=Sum("amount")).order_by()):
            sign = -1 if row["kind"] == PaymentKind.REFUND else 1
            paid[common.bucket(row["mode__name"])] += sign * (row["total"] or ZERO)
        return {
            "initial": initial, "collected": agg["collected"], "deposit": agg["balance"] + advance,
            "rounding": agg["rounding"], "tax": agg["tax"], "net": agg["net"], "credit": credit,
            "actual": agg["net"] - credit, **common.pay_cells(paid),
            "supervisor": agg["supervisor"], "card_discount": agg["card_discount"],
            "bill": agg["net"] - agg["tax"], **fares,
            "_vehicles": shown_lines.aggregate(n=Count("pk"))["n"],
        }
