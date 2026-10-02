"""Which fares/packages a user may see."""

from apps.fare.models import Fare, Package
from core.scoping import scoped_to


def fares_for(user):
    return scoped_to(Fare.objects.all(), user)


def packages_for(user):
    return scoped_to(Package.objects.all(), user)
