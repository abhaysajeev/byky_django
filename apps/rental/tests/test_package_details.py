"""The package the tablet applied to a vehicle line (owner, 10 Nov 2026):
`package_details`, stored as sent, returned and shown, never summed. The line's
base_fare is already after the package; fare_details is the fare before it."""

import pytest
from django.urls import reverse

from apps.rental.models import Invoice, OrderItem
from apps.rental.tests import test_order_api as orders
from apps.rental.tests import test_order_change as changes
from apps.rental.tests.test_order_change import ADD, REPLACE, add, line_of, replace
from apps.rental.tests.test_order_return import RETURN, back
from apps.rental.tests.test_order_settle import SETTLE, bill
from apps.rental.views import is_free_item, package_label
from apps.reports.defs.rent_details import _package
from apps.sync import services as sync_services
from apps.sync.tests import test_parking as parking

call, booking = orders.call, orders.booking
world, token, fresh_throttle, shop = orders.world, orders.token, orders.fresh_throttle, orders.shop
office = parking.office
mo50 = changes.mo50

COUPLE = {"package_id": 14, "code": "SCOOT-2", "name": "Couple Scooter Ride", "promotion_type": "percentage",
          "amount_off": "7.500", "is_free_item": False}
FREE = {"package_id": 14, "name": "Couple Scooter Ride", "amount_off": "50.000", "is_free_item": True}


def with_packages(world, shop, *details):
    data = booking(world, shop)
    for line, package in zip(data["items"], details, strict=False):
        if package is not None:
            line["package_details"] = package
    return data


def stored(sync_id):
    return OrderItem.objects.get(pk=sync_id).package_details


def test_a_booking_keeps_each_lines_package_and_returns_it(client, world, token, shop):
    data = with_packages(world, shop, COUPLE)

    body = call(client, orders.BOOK, data, token=token).json()

    assert body["code"] == "ok", body
    first, second = data["items"]
    assert stored(first["sync_id"]) == COUPLE
    assert stored(second["sync_id"]) is None
    assert line_of(body["data"], first["sync_id"])["package_details"] == COUPLE
    assert line_of(body["data"], second["sync_id"])["package_details"] is None


def test_any_keys_are_kept_as_sent(client, world, token, shop):
    extra = {**COUPLE, "row_id": 31, "note": "anything the app needs", "nested": {"a": [1, 2]}}

    call(client, orders.BOOK, with_packages(world, shop, extra), token=token)

    assert OrderItem.objects.get(package_details__isnull=False).package_details == extra


def test_without_it_a_booking_is_as_before(client, world, token, shop):
    data = booking(world, shop)

    assert call(client, orders.BOOK, data, token=token).json()["code"] == "ok"
    assert not OrderItem.objects.filter(package_details__isnull=False).exists()


def test_it_must_be_an_object(client, world, token, shop):
    data = with_packages(world, shop, ["not", "an", "object"])

    body = call(client, orders.BOOK, data, token=token).json()

    assert body["code"] == "invalid_request"


def test_add_and_replace_carry_it_too(client, world, token, shop, mo50):
    booked = booking(world, shop)
    assert call(client, orders.BOOK, booked, token=token).json()["code"] == "ok"
    added = add(booked, mo50, shop)
    added["item"]["package_details"] = FREE
    swap = replace(booked, shop["mo42"], shop)
    swap["new_item"]["package_details"] = COUPLE

    assert call(client, ADD, added, token=token).json()["code"] == "ok"
    assert call(client, REPLACE, swap, token=token).json()["code"] == "ok"

    assert stored(added["item"]["sync_id"]) == FREE
    assert stored(swap["new_item"]["sync_id"]) == COUPLE


def test_settle_is_unchanged_and_the_invoice_shows_each_lines_package(client, world, token, shop):
    booked = with_packages(world, shop, COUPLE)
    assert call(client, orders.BOOK, booked, token=token).json()["code"] == "ok"
    for index in (0, 1):
        assert call(client, RETURN, back(booked, index=index), token=token).json()["code"] == "ok"

    body = call(client, SETTLE, bill(booked, shop, subtotal="100.00", tax_amount="4.76", net_amount="100.00",
                                     payments=[]), token=token).json()

    assert body["code"] == "ok", body
    invoice = Invoice.objects.get()
    detail = call(client, "/api/v1/operator/invoices/detail", {"invoice_no": invoice.invoice_no}, token=token).json()
    packages = [line["package_details"] for line in detail["data"]["items"]]
    assert sorted(packages, key=lambda p: p is None) == [COUPLE, None]


def test_a_parked_booking_keeps_it_when_replayed(client, world, token, shop, office):
    data = with_packages(world, shop, COUPLE)
    data["items"][1]["vehicle_id"] = 999999                       # refused, so it parks

    body = parking.send(client, orders.BOOK, data, token=token, park=True).json()
    assert body["code"] == "parked", body

    fixed = {**data, "items": [data["items"][0], {**data["items"][1], "vehicle_id": shop["dc02"].pk}]}
    issue = parking.every_issue().get()
    outcome = sync_services.replay(parking.every_issue(), issue.pk, user=office, request_data=fixed)

    assert outcome["stopped_at"] is None
    assert stored(data["items"][0]["sync_id"]) == COUPLE


# -- what the screens and Rent Details show ------------------------------------------------

@pytest.mark.parametrize("details,label,free", [
    (COUPLE, "Couple Scooter Ride · AED 7.500 off", False),
    (FREE, "Couple Scooter Ride · AED 50.000 off", True),
    ({"name": "Weekend", "amount_off": "0.000"}, "Weekend · AED 0.000 off", False),
    ({"amount_off": "5.000"}, "Package · AED 5.000 off", False),
    ({"name": "Weekend"}, "Weekend", False),
    (None, "", False),
    ({}, "", False),
    ("text", "", False),
])
def test_the_label_reads_only_what_was_sent(details, label, free):
    assert (package_label(details), is_free_item(details)) == (label, free)


def test_rent_details_marks_a_free_vehicle():
    assert _package(FREE) == "Couple Scooter Ride · AED 50.000 off (free)"
    assert _package(None) == ""


def test_the_order_and_invoice_screens_show_it(client_in, world, token, shop):
    booked = with_packages(world, shop, COUPLE, FREE)
    assert call(client_in, orders.BOOK, booked, token=token).json()["code"] == "ok"

    page = client_in.get(reverse("rental-order-detail", args=[booked["sync_id"]])).content.decode()
    assert "Package: Couple Scooter Ride · AED 7.500 off" in page and ">Free</span>" in page

    for index in (0, 1):
        call(client_in, RETURN, back(booked, index=index), token=token)
    call(client_in, SETTLE, bill(booked, shop, subtotal="100.00", tax_amount="4.76", net_amount="100.00",
                                 payments=[]), token=token)
    page = client_in.get(reverse("rental-invoice-detail", args=[Invoice.objects.get().pk])).content.decode()
    assert "Package: Couple Scooter Ride · AED 7.500 off" in page and ">Free</span>" in page
