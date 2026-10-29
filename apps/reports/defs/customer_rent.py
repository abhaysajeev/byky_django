"""Customer Wise Rent Details -- the client's sheet "Customer Wise Rent Details".

One row per customer x day x type: "Invoice" -- their bills settled that day
(by settle time); "Credit Note" -- the credit notes approved on their bills
that day. Bill Amount is net less VAT (a credit note's taxable amount), with
its VAT and the bills' discount. No TOTAL row: bills and credit notes would
be added together (the sample has none either).

Filters: state, branch, cashier, customer name and phone -- not credit note,
order or vehicle number (owner, 29 Oct 2026): this report is per customer.
"""

from collections import defaultdict

from django.db.models import Count

from apps.rental.models import CreditNoteStatus
from apps.rental.scoping import credit_notes_for, invoices_for
from apps.reports import common
from apps.reports.base import COUNT, DATE, MONEY, ZERO, Column, Kpi, Report, Row, local, paged

INVOICE, CREDIT = "Invoice", "Credit Note"


class CustomerWiseRent(Report):
    slug = "customer-wise-rent"
    title = "Customer Wise Rent Details"
    sheet_title = "Customer Wise Rent Details Report"
    page_code = "reports.customer_wise_rent"
    filters = ("state", "branch", "cashier", "customer", "phone")
    unit = ("row", "rows")
    empty = "No customer was billed or credited in this range."
    description = "Each customer's bills and credit notes per day."

    def _narrow(self, qs, f):
        return common.customer(common.cashier(common.place(qs, f), f), f)

    def build(self, ctx, page=None, per_page=None):
        f = ctx.f
        groups = defaultdict(lambda: {"count": 0, "vehicles": 0, "bill": ZERO, "tax": ZERO, "discount": ZERO})
        customers = {}
        invoices = self._narrow(invoices_for(ctx.user).filter(issued_at__gte=f.since, issued_at__lt=f.until), f)
        for inv in invoices.select_related("order__customer").annotate(n_items=Count("items")):
            customer = inv.order.customer
            customers[customer.pk] = customer
            g = groups[(customer.pk, local(inv.issued_at, ctx.zone).date(), INVOICE)]
            g["count"] += 1
            g["vehicles"] += inv.n_items
            g["bill"] += inv.net_amount - inv.tax_amount
            g["tax"] += inv.tax_amount
            g["discount"] += inv.discount_amount
        notes = self._narrow(credit_notes_for(ctx.user).filter(
            status=CreditNoteStatus.APPROVED, decided_at__gte=f.since, decided_at__lt=f.until), f)
        for note in notes.select_related("order__customer").annotate(n_items=Count("invoice__items")):
            customer = note.order.customer
            customers[customer.pk] = customer
            g = groups[(customer.pk, local(note.decided_at, ctx.zone).date(), CREDIT)]
            g["count"] += 1
            g["vehicles"] += note.n_items
            g["bill"] += note.taxable_amount or ZERO
            g["tax"] += note.tax_amount or ZERO

        columns = [
            Column("type", "Type", width=12),
            Column("customer", "Customer Name", width=22),
            Column("mobile", "Mobile No", width=16),
            Column("email", "Email ID", width=24),
            Column("status", "Status", width=10),
            Column("date", "Order Date", DATE, width=12),
            Column("count", "No Of Orders/Sales Return", COUNT, summed=False),
            Column("vehicles", "No Of Vehicles", COUNT, summed=False),
            # Owner, 29 Oct 2026: the discount before the bill amount.
            Column("discount", "Total Discount (AED)", MONEY, summed=False),
            Column("bill", "Total Bill Amount", MONEY, summed=False),
            Column("tax", "Total Tax", MONEY, summed=False),
        ]
        rows = []
        order = sorted(groups, key=lambda k: (customers[k[0]].full_name.lower(), k[1], k[2] != INVOICE))
        for key in order:
            customer, g = customers[key[0]], groups[key]
            rows.append(Row({
                "type": key[2], "customer": customer.full_name, "mobile": customer.mobile_full,
                "email": customer.email or None, "status": "Blocked" if customer.is_blocked else "Active",
                "date": key[1], **g,
            }))
        bills = [r for r in rows if r.cells["type"] == INVOICE]
        return paged(rows, page, per_page, columns=columns, total=None, count=len(rows),
                     kpis=[Kpi("Customers", len(customers), COUNT),
                           Kpi("Bills", sum(r.cells["count"] for r in bills), COUNT),
                           Kpi("Credit notes", sum(r.cells["count"] for r in rows if r.cells["type"] == CREDIT), COUNT),
                           Kpi("Bill amount", sum((r.cells["bill"] for r in bills), ZERO))])
