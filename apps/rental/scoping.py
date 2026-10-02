"""Which customers and orders a user may see."""

from apps.rental.models import Customer, Order
from core.scoping import scoped_to


def customers_for(user):
    return scoped_to(Customer.objects.all(), user)


def orders_for(user):
    return scoped_to(Order.objects.all(), user)
