"""Which Brand rows a user may see."""

from apps.fleet.models import Brand
from core.scoping import scoped_to


def brands_for(user):
    return scoped_to(Brand.objects.all(), user)
