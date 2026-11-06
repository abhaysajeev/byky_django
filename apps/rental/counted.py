"""Which money counts -- one rule for every report, dashboard, list total, export
and app API.

Owner, 6 Nov 2026: **a cancelled order's money counts nowhere.** That covers
both kinds of cancel:
  - an order cancelled while running (requests.cancel_order), and
  - a settled order whose invoice a system administrator cancelled
    (requests.cancel_settled): its invoice is kept and marked void, its
    payments are kept, but the money was handed back outside the system.

Every read of Payment, Invoice, InvoiceItem, OrderItem money or CreditNote
that adds money up goes through these helpers (a test checks:
apps/rental/tests/test_money_reads.py). Screens that show one order for audit
-- its detail page, its invoice -- read it directly and label it void instead.

`path` is the way from the queryset's model to the order: "order" for
Payment, Invoice, CreditNote, OrderItem and a card-discount claim,
"invoice__order" for InvoiceItem, "" for Order itself.
"""

from django.db.models import Q

from apps.rental.models import OrderStatus

CANCELLED = OrderStatus.CANCELLED


def _field(path):
    return f"{path}__status" if path else "status"


def counted(qs, path="order"):
    """`qs` without the cancelled orders' rows."""
    return qs.exclude(**{_field(path): CANCELLED})


def counted_q(path="order"):
    """The same rule as a Q, for filter= in an aggregate."""
    return ~Q(**{_field(path): CANCELLED})


def counts(order):
    """True when this order's money counts."""
    return order.status != CANCELLED
