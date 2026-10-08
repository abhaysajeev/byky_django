"""Which customers, orders, invoices, credit notes and requests a user may see."""

from apps.rental.models import CreditNote, Customer, Invoice, Order, OrderRequest
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
