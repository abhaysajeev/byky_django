"""Which customers, orders, invoices, credit notes, requests and suspense entries a user may see."""

from apps.company.scoping import branches_for
from apps.rental.models import (
    CreditNote,
    Customer,
    Invoice,
    Order,
    OrderRequest,
    SuspenseEntry,
    TestRide,
)
from core.scoping import scoped_to


def customers_for(user):
    return scoped_to(Customer.objects.all(), user)


def orders_for(user):
    return scoped_to(Order.objects.all(), user)


def invoices_for(user):
    return scoped_to(Invoice.objects.all(), user)


def credit_notes_for(user):
    return scoped_to(CreditNote.objects.all(), user)


def requests_for(user):
    return scoped_to(OrderRequest.objects.all(), user)


def test_rides_for(user):
    return scoped_to(TestRide.objects.all(), user)


def suspense_for(user):
    """The user's companies, then only the branches they may see."""
    return scoped_to(SuspenseEntry.objects.all(), user).filter(branch__in=branches_for(user))
