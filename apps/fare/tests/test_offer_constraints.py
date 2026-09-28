"""The database's own guard on the offer tables: dates ordered, the value
band ordered, the scope FK matching the chosen level, and code uniqueness.

No overlap/crossing constraints here, unlike Fare -- see the note at the top
of the Offer models in apps/fare/models.py for why this pass doesn't need
one.
"""

from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction

from apps.fare.models import OfferFreeItem, OfferFreeItemTimeSlab, OfferItem, OfferLevel
from apps.fare.tests.conftest import D, make_offer


def refused(fn, constraint=None):
    with pytest.raises(IntegrityError) as caught, transaction.atomic():
        fn()
    if constraint:
        assert caught.value.__cause__.diag.constraint_name == constraint


# -- offer -------------------------------------------------------------------------


def test_offer_dates_and_value_band(world):
    refused(lambda: make_offer(world, valid_to=D(2025, 12, 31)), "offer_valid_dates")
    refused(lambda: make_offer(world, lower_value=Decimal("5"), upper_value=Decimal("1")), "offer_value_band")
    make_offer(world, lower_value=Decimal("3"), upper_value=Decimal("3"))          # equal band: fine


def test_offer_code_is_unique_per_company(world):
    make_offer(world, code="PROMO1")
    refused(lambda: make_offer(world, code="PROMO1"), "uniq_offer_code_per_company")
    make_offer(world, code="PROMO1", company=world["other"])                       # another company: fine


def test_offer_scope_must_match_its_level(world):
    refused(lambda: make_offer(world, level=OfferLevel.BRANCH), "offer_scope_matches_level")
    refused(lambda: make_offer(world, level=OfferLevel.COMPANY, branch=world["adc1"]),
            "offer_scope_matches_level")
    refused(lambda: make_offer(world, level=OfferLevel.LOCATION), "offer_scope_matches_level")
    make_offer(world, code="BR1", level=OfferLevel.BRANCH, branch=world["adc1"])
    make_offer(world, code="LOC1", level=OfferLevel.LOCATION, location=world["location"])


# -- offer_item ----------------------------------------------------------------------


def test_offer_item_package_and_value(world):
    offer = make_offer(world)
    refused(lambda: OfferItem.objects.create(offer=offer, vehicle_type=world["monaco"], package_minutes=0),
            "offer_item_package_range")
    refused(lambda: OfferItem.objects.create(offer=offer, vehicle_type=world["monaco"], package_minutes=1441),
            "offer_item_package_range")
    refused(lambda: OfferItem.objects.create(offer=offer, vehicle_type=world["monaco"], package_minutes=30,
                                             value=Decimal("-1")), "offer_item_value_not_negative")
    OfferItem.objects.create(offer=offer, vehicle_type=world["monaco"], package_minutes=30)   # value null: fine


def test_deleting_an_offer_takes_its_rows_with_it(world):
    offer = make_offer(world)
    OfferItem.objects.create(offer=offer, vehicle_type=world["monaco"], package_minutes=30)
    OfferFreeItem.objects.create(offer=offer, vehicle_type=world["monaco"], package_minutes=30, value=Decimal("1"))
    OfferFreeItemTimeSlab.objects.create(offer=offer, vehicle_type=world["monaco"], package_minutes=30,
                                         from_time="10:00", to_time="12:00", value=Decimal("1"))
    offer.delete()
    assert not OfferItem.objects.exists() and not OfferFreeItem.objects.exists() \
        and not OfferFreeItemTimeSlab.objects.exists()


# -- offer_free_item -------------------------------------------------------------------


def test_offer_free_item_package_and_value(world):
    offer = make_offer(world)
    refused(lambda: OfferFreeItem.objects.create(offer=offer, vehicle_type=world["monaco"], package_minutes=0,
                                                 value=Decimal("1")), "offer_free_item_package_range")
    refused(lambda: OfferFreeItem.objects.create(offer=offer, vehicle_type=world["monaco"], package_minutes=30,
                                                 value=Decimal("-1")), "offer_free_item_value_not_negative")


# -- offer_free_item_time_slab ----------------------------------------------------------


def test_offer_slab_time_and_date_mode(world):
    offer = make_offer(world)
    refused(lambda: OfferFreeItemTimeSlab.objects.create(
        offer=offer, vehicle_type=world["monaco"], package_minutes=30, from_time="12:00", to_time="12:00",
        value=Decimal("1")), "offer_slab_time_order")
    refused(lambda: OfferFreeItemTimeSlab.objects.create(
        offer=offer, vehicle_type=world["monaco"], package_minutes=30, from_time="10:00", to_time="12:00",
        value=Decimal("-1")), "offer_slab_value_not_negative")
    refused(lambda: OfferFreeItemTimeSlab.objects.create(
        offer=offer, vehicle_type=world["monaco"], package_minutes=30, from_time="10:00", to_time="12:00",
        value=Decimal("1"), date_mode="specific_date"), "offer_slab_date_matches_mode")
    refused(lambda: OfferFreeItemTimeSlab.objects.create(
        offer=offer, vehicle_type=world["monaco"], package_minutes=30, from_time="10:00", to_time="12:00",
        value=Decimal("1"), date_mode="all_dates", specific_date=D(2026, 4, 1)), "offer_slab_date_matches_mode")
    OfferFreeItemTimeSlab.objects.create(
        offer=offer, vehicle_type=world["monaco"], package_minutes=30, from_time="10:00", to_time="12:00",
        value=Decimal("1"), date_mode="specific_date", specific_date=D(2026, 4, 1))
