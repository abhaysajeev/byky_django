"""The device team's order changes (owner, 29 Oct 2026): the fare terms each
line was priced on (`fare_details`), who opened and closed the order, and the
customer's rating remarks at settle -- in the API and on the web."""

from decimal import Decimal

import pytest

from apps.rental.models import Customer, Invoice, OrderItem
from apps.rental.tests import test_order_api as orders
from apps.rental.tests.test_order_change import (
    ADD,
    REPLACE,
    add,
    booked,
    line_of,
    mo50,
    new_line,
    replace,
)
from apps.rental.tests.test_order_requests import return_all
from apps.rental.tests.test_order_settle import SETTLE, bill
from apps.rental.views import fare_terms
from core.models import User

world, token, fresh_throttle, shop = orders.world, orders.token, orders.fresh_throttle, orders.shop
call, booking, fare_details = orders.call, orders.booking, orders.fare_details
_ = (booked, mo50)     # fixtures


# -- A. fare_details --------------------------------------------------------------------


def test_a_booked_line_keeps_the_fare_terms_it_was_priced_on(client, world, token, shop):
    request_data = booking(world, shop)
    request_data["items"][0]["fare_details"] = fare_details(shop, applied="special_price", special_price_id=31)

    data = call(client, orders.BOOK, request_data, token=token).json()["data"]

    first, second = data["items"]
    assert first["fare_details"]["special_price_id"] == 31 and first["fare_details"]["applied"] == "special_price"
    assert second["fare_details"] == fare_details(shop)
    stored = OrderItem.objects.get(pk=first["sync_id"]).fare_details
    assert stored == request_data["items"][0]["fare_details"]          # exactly as sent


def test_fare_details_are_optional_and_must_be_an_object(client, world, token, shop):
    without = booking(world, shop)
    for item in without["items"]:
        del item["fare_details"]
    data = call(client, orders.BOOK, without, token=token).json()["data"]
    assert [item["fare_details"] for item in data["items"]] == [None, None]

    wrong = booking(world, shop, number=232)
    wrong["items"][0]["fare_details"] = "special price"
    body = call(client, orders.BOOK, wrong, token=token).json()
    assert body["code"] == "invalid_request"
    assert "must be an object" in str(body["data"]["errors"])


def test_fare_details_ride_along_on_add(client, token, shop, booked, mo50):
    added = add(booked, mo50, shop, item=new_line(mo50, shop, fare_details=fare_details(shop, package_minutes=30)))

    data = call(client, ADD, added, token=token).json()["data"]

    assert line_of(data, added["item"]["sync_id"])["fare_details"]["package_minutes"] == 30


def test_fare_details_ride_along_on_replace(client, token, shop, booked, mo50):
    swap = replace(booked, mo50, shop)
    swap["new_item"]["fare_details"] = fare_details(shop, applied="season", season_id=4)

    body = call(client, REPLACE, swap, token=token).json()

    assert body["code"] == "ok", body
    new = line_of(body["data"], swap["new_item"]["sync_id"])
    assert (new["fare_details"]["applied"], new["fare_details"]["season_id"]) == ("season", 4)


# -- B. open_by / close_by -------------------------------------------------------------


def test_open_by_from_booking_close_by_from_settle(client, world, token, shop, booked):
    data = call(client, "/api/v1/operator/orders/detail", {"sync_id": booked["sync_id"]}, token=token).json()["data"]
    operator = User.objects.get(username="opr001")
    assert (data["open_by"], data["close_by"]) == (operator.display_name or operator.username, None)

    return_all(client, token, booked)
    settled = call(client, SETTLE, bill(booked, shop), token=token).json()["data"]
    assert settled["close_by"] == data["open_by"]


# -- C. rating remarks -------------------------------------------------------------------


def test_rating_remarks_are_the_orders_not_the_customers(client, world, token, shop, booked):
    return_all(client, token, booked)
    request_data = bill(booked, shop, star_rating="4.5", rating_remarks="Quick and friendly")

    data = call(client, SETTLE, request_data, token=token).json()["data"]

    assert (data["star_rating"], data["rating_remarks"]) == ("4.5", "Quick and friendly")
    invoice = Invoice.objects.get()
    assert (invoice.star_rating, invoice.rating_remarks) == (Decimal("4.5"), "Quick and friendly")
    customer = Customer.objects.get(pk=shop["ahmed"].pk)
    assert customer.star_rating == Decimal("4.5")                     # the star still rates the customer
    assert not hasattr(customer, "rating_remarks")                    # the remark stays with the order


def test_a_settle_without_a_remark_leaves_it_empty(client, world, token, shop, booked):
    return_all(client, token, booked)

    data = call(client, SETTLE, bill(booked, shop), token=token).json()["data"]

    assert (data["star_rating"], data["rating_remarks"]) == (None, None)
    assert Invoice.objects.get().rating_remarks == ""


# -- Web --------------------------------------------------------------------------------


@pytest.mark.parametrize(("details", "text"), [
    (None, ""),
    ({}, ""),
    ({"applied": "special_price", "base_fare": "50.00", "grace_minutes": 5, "concurrent_interval_minutes": 15,
      "concurrent_fare": "15.00", "concurrent_grace_minutes": 3},
     "Special price · AED 50.00 · grace 5 min · AED 15.00 per extra 15 min (grace 3)"),
    ({"applied": "base", "base_fare": "30.00"}, "Base fare · AED 30.00"),
])
def test_the_web_writes_the_fare_terms_in_one_line(details, text):
    assert fare_terms(details) == text
