"""Which fleet rows a user may see."""

from apps.fleet.models import Brand, Category
from core.scoping import scoped_to


def brands_for(user):
    return scoped_to(Brand.objects.all(), user)


def categories_for(user):
    return scoped_to(Category.objects.all(), user)
