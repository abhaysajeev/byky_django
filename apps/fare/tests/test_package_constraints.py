"""The database's own guard on the package tables: dates ordered, the value
band ordered, the scope FK matching the chosen level, and code uniqueness.

No overlap/crossing constraints here, unlike Fare -- see the note at the top
of the Package models in apps/fare/models.py for why this pass doesn't need
one.
"""

from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction

from apps.fare.models import PackageFreeItem, PackageFreeItemTimeSlab, PackageItem, PackageLevel
from apps.fare.tests.conftest import D, make_package


def refused(fn, constraint=None):
    with pytest.raises(IntegrityError) as caught, transaction.atomic():
        fn()
    if constraint:
        assert caught.value.__cause__.diag.constraint_name == constraint


# -- package -------------------------------------------------------------------------


def test_package_dates_and_value_band(world):
    refused(lambda: make_package(world, valid_to=D(2025, 12, 31)), "package_valid_dates")
    refused(lambda: make_package(world, lower_value=Decimal("5"), upper_value=Decimal("1")), "package_value_band")
    make_package(world, lower_value=Decimal("3"), upper_value=Decimal("3"))          # equal band: fine


def test_package_code_is_unique_per_company(world):
    make_package(world, code="PROMO1")
    refused(lambda: make_package(world, code="PROMO1"), "uniq_package_code_per_company")
    make_package(world, code="PROMO1", company=world["other"])                       # another company: fine


def test_package_scope_must_match_its_level(world):
    refused(lambda: make_package(world, level=PackageLevel.BRANCH), "package_scope_matches_level")
    refused(lambda: make_package(world, level=PackageLevel.COMPANY, branch=world["adc1"]),
            "package_scope_matches_level")
    refused(lambda: make_package(world, level=PackageLevel.LOCATION), "package_scope_matches_level")
    make_package(world, code="BR1", level=PackageLevel.BRANCH, branch=world["adc1"])
    make_package(world, code="LOC1", level=PackageLevel.LOCATION, location=world["location"])


# -- package_item ----------------------------------------------------------------------


def test_package_item_package_and_value(world):
    package = make_package(world)
    refused(lambda: PackageItem.objects.create(package=package, vehicle_type=world["monaco"], package_minutes=0),
            "package_item_package_range")
    refused(lambda: PackageItem.objects.create(package=package, vehicle_type=world["monaco"], package_minutes=1441),
            "package_item_package_range")
    refused(lambda: PackageItem.objects.create(package=package, vehicle_type=world["monaco"], package_minutes=30,
                                             value=Decimal("-1")), "package_item_value_not_negative")
    PackageItem.objects.create(package=package, vehicle_type=world["monaco"], package_minutes=30)   # value null: fine


def test_deleting_a_package_takes_its_rows_with_it(world):
    package = make_package(world)
    PackageItem.objects.create(package=package, vehicle_type=world["monaco"], package_minutes=30)
    PackageFreeItem.objects.create(package=package, vehicle_type=world["monaco"], package_minutes=30, value=Decimal("1"))
    PackageFreeItemTimeSlab.objects.create(package=package, vehicle_type=world["monaco"], package_minutes=30,
                                         from_time="10:00", to_time="12:00", value=Decimal("1"))
    package.delete()
    assert not PackageItem.objects.exists() and not PackageFreeItem.objects.exists() \
        and not PackageFreeItemTimeSlab.objects.exists()


# -- package_free_item -------------------------------------------------------------------


def test_package_free_item_package_and_value(world):
    package = make_package(world)
    refused(lambda: PackageFreeItem.objects.create(package=package, vehicle_type=world["monaco"], package_minutes=0,
                                                 value=Decimal("1")), "package_free_item_package_range")
    refused(lambda: PackageFreeItem.objects.create(package=package, vehicle_type=world["monaco"], package_minutes=30,
                                                 value=Decimal("-1")), "package_free_item_value_not_negative")


# -- package_free_item_time_slab ----------------------------------------------------------


def test_package_slab_time_and_date_mode(world):
    package = make_package(world)
    refused(lambda: PackageFreeItemTimeSlab.objects.create(
        package=package, vehicle_type=world["monaco"], package_minutes=30, from_time="12:00", to_time="12:00",
        value=Decimal("1")), "package_slab_time_order")
    refused(lambda: PackageFreeItemTimeSlab.objects.create(
        package=package, vehicle_type=world["monaco"], package_minutes=30, from_time="10:00", to_time="12:00",
        value=Decimal("-1")), "package_slab_value_not_negative")
    refused(lambda: PackageFreeItemTimeSlab.objects.create(
        package=package, vehicle_type=world["monaco"], package_minutes=30, from_time="10:00", to_time="12:00",
        value=Decimal("1"), date_mode="specific_date"), "package_slab_date_matches_mode")
    refused(lambda: PackageFreeItemTimeSlab.objects.create(
        package=package, vehicle_type=world["monaco"], package_minutes=30, from_time="10:00", to_time="12:00",
        value=Decimal("1"), date_mode="all_dates", specific_date=D(2026, 4, 1)), "package_slab_date_matches_mode")
    PackageFreeItemTimeSlab.objects.create(
        package=package, vehicle_type=world["monaco"], package_minutes=30, from_time="10:00", to_time="12:00",
        value=Decimal("1"), date_mode="specific_date", specific_date=D(2026, 4, 1))
