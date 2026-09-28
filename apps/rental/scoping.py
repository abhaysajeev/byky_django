"""Which customers a user may see."""

from apps.rental.models import Customer
from core.scoping import scoped_to


def customers_for(user):
    return scoped_to(Customer.objects.all(), user)
