"""Cancelled Invoices -- the settled bills a system administrator cancelled
(requests.cancel_settled, owner 6 Nov 2026).

Their money counts nowhere else (apps/rental/counted.py), so this is the one
place it is shown: what each bill came to, what had been collected and how,
who cancelled it, when and why. It explains a cash count that holds more than
that day's figures show -- the money was handed back to the customer outside
the system. By cancel time; the settle time is shown too.

The only report that reads a cancelled order's payments, on purpose.
"""

from collections import defaultdict

from django.db.models import Sum

from apps.rental.models import Payment, PaymentKind
from apps.rental.scoping import invoices_for
from apps.reports import common
from apps.reports.base import (
    COUNT,
    DATE,
    DATETIME,
    GRAND,
    MONEY,
    ZERO,
    Column,
    Kpi,
    Report,
    Row,
    local,
    paged,
    total_of,
)

PAY = {"cash": "Collected By Cash (AED)", "card": "Collected By Card (AED)", "tap": "Collected By Tap & Pay (AED)",
       "upi": "Collected By UPI (AED)", "other": "Collected By Other (AED)"}


def _collected(order_ids):
    """{order_id: {bucket: amount}} -- every payment as recorded, less refunds.
    Deliberately not counted(): these ARE the cancelled orders."""
    out = defaultdict(lambda: dict.fromkeys(common.PAY_KEYS, ZERO))
    rows = (Payment.objects.filter(order_id__in=list(order_ids))
            .values("order_id", "mode__name", "kind").annotate(total=Sum("amount")).order_by())
    for row in rows:
        sign = -1 if row["kind"] == PaymentKind.REFUND else 1
        out[row["order_id"]][common.bucket(row["mode__name"])] += sign * (row["total"] or ZERO)
    return out


class CancelledInvoices(Report):
    slug = "cancelled-invoices"
    title = "Cancelled Invoices"
    sheet_title = "Cancelled Invoices Report"
    page_code = "reports.cancelled_invoices"
    filters = ("state", "branch")
    unit = ("invoice", "invoices")
    empty = "No invoice was cancelled in this range."
    description = "Settled bills cancelled by a system administrator, with what had been collected."

    def build(self, ctx, page=None, per_page=None):
        f = ctx.f
        invoices = common.place(invoices_for(ctx.user).filter(voided_at__gte=f.since, voided_at__lt=f.until), f)
        invoices = list(invoices.select_related("order", "voided_by", "branch").order_by("voided_at", "pk"))
        paid = _collected(i.order_id for i in invoices)
        with_other = any(paid[i.order_id]["other"] for i in invoices)
        columns = [
            Column("cancelled", "Cancelled On", DATETIME, width=18),
            Column("invoice_no", "Invoice No", width=20),
            Column("branch", "Branch", width=22),
            Column("customer", "Customer Name", width=20),
            Column("phone", "Phone No", width=16),
            Column("settled", "Settled On", DATE, width=12),
            Column("by", "Cancelled By", width=22),
            Column("reason", "Reason", width=36),
            Column("net", "Net Bill Amount (AED)", MONEY),
            Column("collected", "Collected Amount (AED)", MONEY),
            *common.pay_columns(PAY, with_other),
        ]
        rows = []
        for invoice in invoices:
            order = invoice.order
            rows.append(Row({
                "cancelled": local(invoice.voided_at, ctx.zone), "invoice_no": invoice.invoice_no,
                "branch": invoice.branch.name, "customer": invoice.customer_name or order.customer_name,
                "phone": invoice.customer_mobile or order.customer_mobile,
                "settled": local(invoice.issued_at, ctx.zone).date(), "by": common.person(invoice.voided_by),
                "reason": invoice.void_reason, "net": invoice.net_amount, "collected": order.paid_amount,
                **common.pay_cells(paid[invoice.order_id]),
            }))
        total = total_of(rows, columns)
        shown = rows + ([Row(total, GRAND, label="TOTAL")] if rows else [])
        return paged(shown, page, per_page, columns=columns, total=total, count=len(rows),
                     kpis=[Kpi("Cancelled invoices", len(rows), COUNT), Kpi("Net bills", total.get("net", ZERO)),
                           Kpi("Collected, handed back", total.get("collected", ZERO))])
