"""Which fleet rows a user may see."""

from apps.fleet.models import UOM, Brand, Category, VehicleType
from core.scoping import scoped_to


def brands_for(user):
    return scoped_to(Brand.objects.all(), user)


def categories_for(user):
    return scoped_to(Category.objects.all(), user)


def vehicle_types_for(user):
    return scoped_to(VehicleType.objects.all(), user)


def uoms_for(user):
    return scoped_to(UOM.objects.all(), user)
