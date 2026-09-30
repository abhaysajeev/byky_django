"""Which discount rows a user may see."""

from apps.discount.models import CardDiscount, CardDiscountClaim, CardGrade, CardType
from core.scoping import scoped_to


def card_types_for(user):
    return scoped_to(CardType.objects.all(), user)


def card_grades_for(user):
    return scoped_to(CardGrade.objects.all(), user)


def card_discounts_for(user):
    return scoped_to(CardDiscount.objects.all(), user)


def claims_for(user):
    return scoped_to(CardDiscountClaim.objects.all(), user)
