"""Which fares/offers a user may see."""

from apps.fare.models import Fare, Offer
from core.scoping import scoped_to


def fares_for(user):
    return scoped_to(Fare.objects.all(), user)


def offers_for(user):
    return scoped_to(Offer.objects.all(), user)
