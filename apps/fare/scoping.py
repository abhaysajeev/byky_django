"""Which fares a user may see."""

from apps.fare.models import Fare
from core.scoping import scoped_to


def fares_for(user):
    return scoped_to(Fare.objects.all(), user)
