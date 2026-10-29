"""Credit Note Details -- the client's sheet "Credit Note Details Report" (the
sample's title said "Card Discount" by mistake).

**Approved** credit notes, by approval time -- a credit note has no separate
issue date; it is issued when approved. Item Description is the credited
bill's vehicles. Actual Collection is the bill's net less the credit note.
"""

from django.db.models import Exists, OuterRef, Prefetch

from apps.rental.models import CreditNoteStatus, OrderItem
from apps.rental.scoping import credit_notes_for
from apps.reports import common
from apps.reports.base import (
    COUNT,
    DATE,
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


class CreditNoteDetails(Report):
    slug = "credit-note-details"
    title = "Credit Note Details"
    sheet_title = "Credit Note Details Report"
    page_code = "reports.credit_note_details"
    filters = ("state", "branch", "cashier", "customer", "phone", "credit_note_no", "order_no", "vehicle_no")
    unit = ("credit note", "credit notes")
    empty = "No credit note was approved in this range."
    description = "Approved credit notes against their bills, and what was collected after them."

    def build(self, ctx, page=None, per_page=None):
        f = ctx.f
        notes = credit_notes_for(ctx.user).filter(status=CreditNoteStatus.APPROVED, decided_at__gte=f.since,
                                                  decided_at__lt=f.until)
        notes = common.customer(common.cashier(common.place(notes, f), f), f)
        if f.get("credit_note_no"):
            notes = notes.filter(credit_note_no__icontains=f.get("credit_note_no"))
        if f.get("vehicle_no"):
            notes = notes.filter(Exists(OrderItem.objects.filter(common.vehicle_q(f), order=OuterRef("order"))))
        notes = (notes.select_related("invoice", "order")
                 .prefetch_related(Prefetch("order__items", queryset=OrderItem.objects.select_related("vehicle")))
                 .order_by("decided_at", "pk"))

        columns = [
            Column("date", "Date", DATE, width=12),
            Column("credit_note_no", "Credit Note No", width=22),
            Column("invoice_no", "Invoice No", width=20),
            Column("customer", "Customer Name", width=20),
            Column("phone", "Phone No", width=16),
            Column("items", "Item Description", width=30),
            Column("taxable", "Invoice Taxable Amount (AED)", MONEY),
            Column("tax", "Invoice Tax Amount (AED)", MONEY),
            Column("net", "Invoice Net Amount (AED)", MONEY),
            Column("credit", "Credit Note Amount (AED)", MONEY),
            Column("actual", "Actual Collection Amount (AED)", MONEY),
        ]
        rows = []
        for note in notes:
            invoice = note.invoice
            rows.append(Row({
                "date": local(note.decided_at, ctx.zone).date(), "credit_note_no": note.credit_note_no,
                "invoice_no": invoice.invoice_no, "customer": note.order.customer_name,
                "phone": note.order.customer_mobile, "items": common.vehicles_of(note.order),
                "taxable": invoice.net_amount - invoice.tax_amount, "tax": invoice.tax_amount,
                "net": invoice.net_amount, "credit": note.net_amount, "actual": invoice.net_amount - note.net_amount,
            }))
        total = total_of(rows, columns)
        shown = rows + ([Row(total, GRAND, label="TOTAL")] if rows else [])
        return paged(shown, page, per_page, columns=columns, total=total, count=len(rows),
                     kpis=[Kpi("Credit notes", len(rows), COUNT), Kpi("Credit note amount", total.get("credit", ZERO)),
                           Kpi("Invoice net", total.get("net", ZERO)),
                           Kpi("Actual collection", total.get("actual", ZERO))])
