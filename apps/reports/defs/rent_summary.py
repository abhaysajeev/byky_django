"""Rent Summary -- the client's sheet "Rent Summary Report".

Settled orders by **settle time** (the invoice's issue), the basis agreed for
collections (apps/rental/reports.py): one row per day x station, in one block
per state with its TOTAL, then the GRAND TOTAL. Credit notes are the approved
ones on those bills; Actual Collection is the net less them. Payments are each
bill's advance and settlement less refunds, by mode.

The sheet's "Referance Number (AED)" is left out: a payment's reference has
no meaning summed per station (Rent Details lists them per order).
"""

from collections import defaultdict

from apps.rental.scoping import invoices_for
from apps.reports import common
from apps.reports.base import (
    COUNT,
    DATE,
    GRAND,
    MONEY,
    TOTAL,
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


class RentSummary(Report):
    slug = "rent-summary"
    title = "Rent Summary"
    sheet_title = "Rent Summary Report"
    page_code = "reports.rent_summary"
    filters = ("state", "branch")
    unit = ("branch-day", "branch-days")
    empty = "No orders were settled in this range."
    description = "Settled orders per day and branch, by state, with how they were paid."

    def build(self, ctx, page=None, per_page=None):
        f = ctx.f
        invoices = common.place(invoices_for(ctx.user).filter(issued_at__gte=f.since, issued_at__lt=f.until), f)
        invoices = list(invoices.select_related("branch__location__state")
                        .only("id", "order_id", "issued_at", "net_amount", "branch__name",
                              "branch__location__state__name"))
        order_ids = [i.order_id for i in invoices]
        paid = common.payments_by_order(order_ids)
        credited = common.credit_notes_by_order(order_ids)

        groups = defaultdict(lambda: {"orders": 0, "net": ZERO, "credit": ZERO,
                                      **{k: ZERO for k in common.PAY_KEYS}})
        for invoice in invoices:
            branch = invoice.branch
            key = (branch.location.state.name, invoice.issued_at.astimezone(ctx.zone).date(), branch.name)
            g = groups[key]
            g["orders"] += 1
            g["net"] += invoice.net_amount
            g["credit"] += credited.get(invoice.order_id) or ZERO
            for k, v in paid[invoice.order_id].items():
                g[k] += v

        with_other = any(g["other"] for g in groups.values())
        columns = [
            Column("date", "Date", DATE, width=12),
            Column("state", "State", width=16),
            Column("branch", "Branch", width=26),
            Column("orders", "Total No Of Order", COUNT),
            Column("net", "Net Bill Amount (AED)", MONEY),
            Column("credit", "Credit Note Amount (AED)", MONEY),
            Column("actual", "Actual Collection Amount (AED)", MONEY),
            *common.pay_columns(PAY, with_other),
        ]

        rows, data = [], []
        for state in sorted({k[0] for k in groups}):
            block = []
            for key in sorted(k for k in groups if k[0] == state):
                g = groups[key]
                block.append(Row({"date": key[1], "state": state, "branch": key[2], "orders": g["orders"],
                                  "net": g["net"], "credit": g["credit"], "actual": g["net"] - g["credit"],
                                  **common.pay_cells(g)}))
            rows += block
            rows.append(Row(total_of(block, columns), TOTAL, label="TOTAL"))
            data += block

        grand = total_of(data, columns)
        if rows:
            rows.append(Row(grand, GRAND, label="GRAND TOTAL"))
        return paged(
            rows, page, per_page, columns=columns, total=grand, count=len(data), blocks=True,
            kpis=[Kpi("Orders", grand.get("orders", 0), COUNT), Kpi("Net bill", grand.get("net", ZERO)),
                  Kpi("Credit notes", grand.get("credit", ZERO)),
                  Kpi("Actual collection", grand.get("actual", ZERO))],
        )
