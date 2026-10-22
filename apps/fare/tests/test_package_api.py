"""POST /api/v1/operator/packages -- the station's packages for a working day
(apps/fare/package_device.py) -- and the check an order line naming a package
must pass."""

import datetime
from decimal import Decimal

import pytest

from apps.company.models import Location
from apps.fare.models import (
    FreeOrPackagePrice,
    PackageFreeItem,
    PackageFreeItemTimeSlab,
    PackageItem,
    PackageLevel,
    PromotionFor,
    PromotionType,
    SlabDay,
)
from apps.fare.tests import test_api as base
from apps.fare.tests.conftest import D, make_package
from apps.fare.tests.test_api import call
from apps.rental.tests import test_order_api as orders
from core.enums import ApprovalStatus

world, token, fresh_throttle = base.world, base.token, base.fresh_throttle

URL = "/api/v1/operator/packages"
APPROVED = {"approval_status": ApprovalStatus.APPROVED}


def packages(client, token, **request_data):
    body = call(client, URL, {"date": "2026-09-30", **request_data}, token=token).json()
    assert body["code"] == "ok", body
    return body["data"]


def codes(data):
    return [p["code"] for p in data["packages"]]


def item(package, vehicle_type, minutes=30, value="22.500"):
    return PackageItem.objects.create(package=package, vehicle_type=vehicle_type, package_minutes=minutes,
                                      value=None if value is None else Decimal(value))


def test_only_packages_covering_this_station_that_day(client, world, token):
    elsewhere = Location.objects.create(country=world["adc1"].location.country, state=world["adc1"].location.state,
                                        short_code="FAR", name="Far away")
    make_package(world, code="CO", **APPROVED)
    make_package(world, code="HERE", level=PackageLevel.BRANCH, branch=world["adc1"], **APPROVED)
    make_package(world, code="THERE", level=PackageLevel.BRANCH, branch=world["adc2"], **APPROVED)
    make_package(world, code="LOC", level=PackageLevel.LOCATION, location=world["adc1"].location, **APPROVED)
    make_package(world, code="FARLOC", level=PackageLevel.LOCATION, location=elsewhere, **APPROVED)
    make_package(world, code="PAUSED", is_active=False, **APPROVED)
    make_package(world, code="PENDING")                                     # not approved
    make_package(world, code="OLD", valid_from=D(2026, 1, 1), valid_to=D(2026, 9, 29), **APPROVED)
    make_package(world, code="TOMORROW", valid_from=D(2026, 10, 1), valid_to=D(2026, 12, 31), **APPROVED)
    make_package(world, code="LATER", valid_from=D(2026, 10, 2), valid_to=D(2026, 12, 31), **APPROVED)
    make_package(world, code="THEIRS", company=world["other"], **APPROVED)

    data = packages(client, token)

    # Most specific first: this station's, its location's, then the company's -- by code.
    assert codes(data) == ["HERE", "LOC", "CO", "TOMORROW"]
    assert data["date"] == "2026-09-30"


def test_a_package_comes_whole(client, world, token):
    package = make_package(world, code="ADD-ECAR", lower_value=Decimal("1"), upper_value=Decimal("3"),
                           free_item_selectable=True, free_item_selectable_note="One per bike",
                           free_or_package_price=FreeOrPackagePrice.PACKAGE_PRICE, time_slab_applicable=True,
                           **APPROVED)
    first = item(package, world["monaco"], 45, value=None)
    PackageFreeItem.objects.create(package=package, vehicle_type=world["berg"], package_minutes=15,
                                   value=Decimal("20"))
    slab = PackageFreeItemTimeSlab.objects.create(
        package=package, day=SlabDay.MONDAY, from_time=datetime.time(10), to_time=datetime.time(13),
        vehicle_type=world["berg"], package_minutes=15, value=Decimal("20"))

    [row] = packages(client, token)["packages"]

    assert row == {
        "package_id": package.pk, "version": row["version"], "code": "ADD-ECAR", "name": "Promo",
        "level": "company", "valid_from": "2026-01-01", "valid_to": "2026-12-31",
        "qualifies": {"promotion_for": "quantity", "lower": "1", "upper": "3"},
        "promotion_type": "quantity",
        "items": [{"item_id": first.pk, "vehicle_type_id": world["monaco"].pk, "package_minutes": 45,
                   "value": None}],
        "free_items": {"mode": "package_price", "items": [
            {"free_item_id": package.free_items.get().pk, "vehicle_type_id": world["berg"].pk,
             "package_minutes": 15, "value": "20.000"}]},
        "time_slabs": [{"slab_id": slab.pk, "on_date": None, "weekday": 0, "start": "10:00", "end": "13:00",
                        "vehicle_type_id": world["berg"].pk, "package_minutes": 15, "value": "20.000"}],
        "free_item_note": "One per bike",
    }


def test_a_discount_package_and_an_amount_band(client, world, token):
    package = make_package(world, code="AMT", promotion_for=PromotionFor.AMOUNT, lower_value=Decimal("200"),
                           upper_value=Decimal("500"), promotion_type=PromotionType.PERCENTAGE, **APPROVED)
    item(package, world["monaco"], value="10")

    [row] = packages(client, token)["packages"]

    assert row["qualifies"] == {"promotion_for": "amount", "lower": "200.000", "upper": "500.000"}
    assert (row["items"][0]["value"], row["free_items"], row["time_slabs"]) == ("10.000", None, [])


def test_packages_by_vehicle_type_and_the_filter(client, world, token):
    scooter = make_package(world, code="A", **APPROVED)
    item(scooter, world["monaco"])
    both = make_package(world, code="B", **APPROVED)
    item(both, world["monaco"])
    item(both, world["berg"])

    data = packages(client, token)
    narrowed = packages(client, token, vehicle_type_id=world["berg"].pk)

    assert data["by_vehicle_type"] == {str(world["monaco"].pk): [scooter.pk, both.pk],
                                       str(world["berg"].pk): [both.pk]}
    assert codes(narrowed) == ["B"]


def test_sent_even_when_no_fare_prices_its_items(client, world, token):
    package = make_package(world, code="NOFARE", **APPROVED)          # no fare exists for Berg here
    item(package, world["berg"], 90)

    assert codes(packages(client, token)) == ["NOFARE"]


def test_another_companys_vehicle_type_is_refused(client, world, token):
    response = call(client, URL, {"date": "2026-09-30", "vehicle_type_id": world["their_type"].pk}, token=token)

    assert (response.status_code, response.json()["code"]) == (400, "unknown_vehicle_type")


def test_date_is_required_and_other_apps_refused(client, world, token):
    missing = call(client, URL, {}, token=token)
    other_app = call(client, "/api/v1/manager/packages", {"date": "2026-09-30"}, token=token)

    assert (missing.status_code, missing.json()["code"]) == (400, "invalid_request")
    assert (other_app.status_code, other_app.json()["code"]) == (403, "wrong_channel")


def test_it_is_in_the_api_docs(client, db):
    schema = client.get("/api/schema/").content.decode()

    assert "/api/v1/{app}/packages" in schema and "by_vehicle_type" in schema


# -- Orders naming a package ----------------------------------------------------------------

# The order tests use the same world and tablet (the rental tests build on these).
shop = orders.shop


def book_with(client, token, world, shop, package):
    data = orders.booking(world, shop)
    for line in data["items"]:
        line["offer_id"] = package.pk
    return call(client, orders.BOOK, data, token=token)


def test_an_order_may_use_a_live_package(client, world, token, shop):
    package = make_package(world, **APPROVED)

    body = book_with(client, token, world, shop, package).json()

    assert body["code"] == "ok", body


@pytest.mark.parametrize("fields, words", [
    ({"is_active": False}, "not active"),
    ({"level": PackageLevel.BRANCH, "branch": "adc2"}, "does not apply at this station"),
    ({"valid_to": D(2026, 10, 1)}, "not in force on 02 Oct 2026"),         # the order starts 2 Oct
])
def test_an_order_cannot_use_a_package_it_may_not(client, world, token, shop, fields, words):
    if fields.get("branch") == "adc2":
        fields = {**fields, "branch": world["adc2"]}
    package = make_package(world, **{**APPROVED, **fields})

    response = book_with(client, token, world, shop, package)

    assert (response.status_code, response.json()["code"]) == (409, "package_not_available")
    assert words in response.json()["message"]
