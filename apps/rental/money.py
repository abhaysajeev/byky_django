"""An order's running money, read the same way wherever it is shown -- the
manager app's request rows (apps/rental/requests.py) and Live Monitoring
(apps/rental/live.py). Owner, 28 Oct 2026; legacy FareAmount / DepositAmount
(GetOrderWithID_ForRequestApproval, DMSGetAllStatusTrackingDetailsForPartialView_New).

Needs the order's items (with vehicle and vehicle type) and payments loaded.
"""

from decimal import Decimal

from apps.rental.models import OrderItemStatus, OrderStatus, PaymentKind

ZERO = Decimal("0")
BILLABLE = (OrderItemStatus.ACTIVE, OrderItemStatus.RETURNED)


def billable(order):
    return [item for item in order.items.all() if item.status in BILLABLE]


def fare(order):
    """The fare: once settled, the bill's subtotal; before, the running total --
    each returned vehicle's total, plus the base fare of each still out."""
    if order.status == OrderStatus.COMPLETED and order.subtotal is not None:
        return order.subtotal
    return sum((item.total_amount if item.total_amount is not None else item.base_fare
                for item in billable(order)), ZERO)


def deposit(order):
    """The advance taken at booking (and any later advance)."""
    return sum((p.amount for p in order.payments.all() if p.kind == PaymentKind.ADVANCE), ZERO)
