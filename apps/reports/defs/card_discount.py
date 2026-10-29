"""Card Discount Details -- the client's sheet "Card Discount Details Report".

Settled orders whose bill took a discount card (the claim applied to the
order), by settle time: the card, the order, its vehicles and the bill.
Bill Amount is the net less VAT. **Amount Received is the money the customer
actually paid** -- the sample's column showed the fare before the discount,
which is Bill + Tax + Card Discount here (owner, 29 Oct 2026).
"""

from django.db.models import Exists, OuterRef, Prefetch

from apps.rental.models import DiscountSource, OrderItem, OrderStatus
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
    Row,
    local,
    paged,
    total_of,
)


class CardDiscountDetails(Report):
    slug = "card-discount-details"
    title = "Card Discount Details"
    sheet_title = "Card Discount Details Report"
    page_code = "reports.card_discount_details"
    filters = ("state", "branch", "cashier", "customer", "phone", "order_no", "vehicle_no", "card_type",
               "card_grade")
    unit = ("order", "orders")
    empty = "No bill took a discount card in this range."
    description = "Settled bills that used a discount card: the card, the bill and what was paid."

    def build(self, ctx, page=None, per_page=None):
        f = ctx.f
        orders = orders_for(ctx.user).filter(status=OrderStatus.COMPLETED,
                                             invoice__discount_source=DiscountSource.CARD,
                                             invoice__issued_at__gte=f.since, invoice__issued_at__lt=f.until)
        orders = common.customer(common.cashier(common.place(orders, f), f, order=""), f, order="")
        if f.get("vehicle_no"):
            orders = orders.filter(Exists(OrderItem.objects.filter(common.vehicle_q(f), order=OuterRef("pk"))))
        if f.get("card_type"):
            orders = orders.filter(discount_claim__card_type_id=f.get("card_type"))
        if f.get("card_grade"):
            orders = orders.filter(discount_claim__card_grade_id=f.get("card_grade"))
        orders = (orders.select_related("invoice", "branch__location__state", "discount_claim__card_type",
                                        "discount_claim__card_grade")
                  .prefetch_related(Prefetch("items", queryset=OrderItem.objects.select_related("vehicle")))
                  .order_by("invoice__issued_at", "pk"))

        columns = [
            Column("state", "State", width=14),
            Column("branch", "Branch", width=22),
            Column("card_type", "Card Type", width=16),
            Column("card_grade", "Card Grade", width=24),
            Column("order_no", "Order No", width=20),
            Column("booked", "Order Date", DATETIME, width=21),
            Column("vehicles", "Vehicle No", width=16),
            Column("customer", "Customer Name", width=20),
            Column("phone", "Phone No", width=16),
            Column("start", "Start Date And Time", DATETIME, width=21),
            Column("exit", "Exit Date Time", DATETIME, width=21),
            # Owner, 29 Oct 2026: the card discount first, left of the bill.
            Column("discount", "Card Discount (AED)", MONEY),
            Column("bill", "Bill Amount (AED)", MONEY),
            Column("tax_pct", "Tax Percentage", PERCENT, width=10),
            Column("tax", "Tax Amount (AED)", MONEY),
            Column("rounding", "Rounded Diff", MONEY),
            Column("received", "Amount Received (AED)", MONEY),
        ]
        rows = []
        for order in orders:
            invoice, claim = order.invoice, order.discount_claim
            ends = [i.end_time for i in order.items.all() if i.status in common.BILLING and i.end_time]
            rows.append(Row({
                "state": order.branch.location.state.name, "branch": order.branch.name,
                "card_type": claim.card_type.name if claim else None,
                "card_grade": claim.card_grade.name if claim else None,
                "order_no": order.order_no, "booked": local(order.booked_at, ctx.zone),
                "vehicles": common.vehicles_of(order), "customer": order.customer_name,
                "phone": order.customer_mobile, "start": local(order.start_time, ctx.zone),
                "exit": local(max(ends), ctx.zone) if ends else None,
                "bill": invoice.net_amount - invoice.tax_amount, "tax_pct": invoice.tax_percentage,
                "tax": invoice.tax_amount, "discount": invoice.discount_amount,
                "rounding": invoice.rounding_adjustment, "received": order.paid_amount,
            }))
        total = total_of(rows, columns)
        shown = rows + ([Row(total, GRAND, label="TOTAL")] if rows else [])
        return paged(shown, page, per_page, columns=columns, total=total, count=len(rows),
                     kpis=[Kpi("Orders", len(rows), COUNT), Kpi("Card discount", total.get("discount", ZERO)),
                           Kpi("Bill amount", total.get("bill", ZERO)),
                           Kpi("Amount received", total.get("received", ZERO))])
