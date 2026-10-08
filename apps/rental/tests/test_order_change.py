"""POST /api/v1/operator/orders/add, /replace and /remove through HTTP: the
vehicles on an active order changed (order_lifecycle_design.md 3.2) -- and
that only returned vehicles are ever billed."""

import copy
from decimal import Decimal

import pytest

from apps.fleet.models import Vehicle
from apps.rental.models import (
    Invoice,
    Order,
    OrderEvent,
    OrderItem,
    OrderItemStatus,
    OrderStatus,
    Payment,
)
from apps.rental.tests import test_order_api as orders
from apps.rental.tests.test_order_return import RETURN, back
from apps.rental.tests.test_order_settle import SETTLE, bill
from core.enums import ApprovalStatus
from core.ids import uuid7

call, booking, sign_in_tablet = orders.call, orders.booking, orders.sign_in_tablet
world, token, fresh_throttle, shop = orders.world, orders.token, orders.fresh_throttle, orders.shop

ADD, REPLACE, REMOVE = "/api/v1/operator/orders/add", "/api/v1/operator/orders/replace", "/api/v1/operator/orders/remove"
VEHICLES = "/api/v1/operator/vehicles"


@pytest.fixture
def booked(client, world, token, shop):
    """3.1: MO 41 and DC 02 out for an hour, AED 100 down."""
    request_data = booking(world, shop)
    assert call(client, orders.BOOK, request_data, token=token).json()["code"] == "ok"
    return request_data


@pytest.fixture
def mo50(world, shop):
    """One more bike at the station, free."""
    like = shop["mo41"]
    return Vehicle.objects.create(company=world["company"], vehicle_code="MO 50", vehicle_name="MO 50",
                                  vehicle_type=like.vehicle_type, uom=like.uom, current_branch=world["adc1"],
                                  approval_status=ApprovalStatus.APPROVED)


def new_line(vehicle, shop, start="2026-10-02 16:30:00", **overrides):
    line = {"sync_id": str(uuid7()), "vehicle_id": vehicle.pk, "fare_id": shop["fare"].pk, "package_minutes": 30,
            "start_time": start, "expected_end_time": "2026-10-02 17:00:00", "base_fare": "30.00"}
    line.update(overrides)
    return line


def add(booked, vehicle, shop, **overrides):
    data = {"sync_id": str(uuid7()), "order_id": booked["sync_id"], "added_at": "2026-10-02 16:30:00",
            "item": new_line(vehicle, shop)}
    data.update(overrides)
    return data


def replace(booked, vehicle, shop, index=0, **overrides):
    data = {"sync_id": str(uuid7()), "order_id": booked["sync_id"], "old_item_id": booked["items"][index]["sync_id"],
            "replaced_at": "2026-10-02 16:20:00", "reason": "Chain broken",
            "new_item": new_line(vehicle, shop, start="2026-10-02 16:20:00", package_minutes=60, base_fare="50.00")}
    data.update(overrides)
    return data


def remove(booked, item_id, **overrides):
    data = {"sync_id": str(uuid7()), "order_id": booked["sync_id"], "item_id": item_id,
            "removed_at": "2026-10-02 16:05:00", "reason": "Booked by mistake"}
    data.update(overrides)
    return data


def line_of(data, sync_id):
    return next(item for item in data["items"] if item["sync_id"] == sync_id)


def can_rent(client, token, vehicle):
    data = call(client, VEHICLES, token=token).json()["data"]
    return {v["vehicle_id"]: v["can_rent"] for c in data["categories"] for t in c["vehicle_types"]
            for v in t["vehicles"]}[vehicle.pk]


# -- Add ---------------------------------------------------------------------------------


def test_a_vehicle_joins_the_order_with_its_advance(client, token, shop, booked, mo50):
    request_data = add(booked, mo50, shop, payments=[
        {"sync_id": str(uuid7()), "kind": "advance", "payment_mode_id": shop["cash"].pk, "amount": "30.000",
         "paid_at": "2026-10-02 16:30:00"}])

    body = call(client, ADD, request_data, token=token).json()

    assert body["code"] == "ok", body
    line = line_of(body["data"], request_data["item"]["sync_id"])
    assert (line["status"], line["vehicle"]["name"], line["base_fare"], line["total_amount"]) == (
        "active", "MO 50", "30.000", None)
    assert (body["data"]["items_out"], body["data"]["paid_amount"]) == (3, "130.000")
    assert Payment.objects.filter(order_id=booked["sync_id"]).count() == 3
    event = OrderEvent.objects.get(pk=request_data["sync_id"])
    assert (event.action, str(event.new_item_id)) == ("add", request_data["item"]["sync_id"])
    assert can_rent(client, token, mo50) is False


def test_a_vehicle_joins_with_no_money_taken(client, token, shop, booked, mo50):
    body = call(client, ADD, add(booked, mo50, shop), token=token).json()

    assert (body["code"], body["data"]["paid_amount"], body["data"]["items_out"]) == ("ok", "100.000", 3)


@pytest.mark.parametrize(("vehicle", "code"), [
    ("mo41", "vehicle_already_rented"),
    ("away", "vehicle_not_at_station"),
], ids=["already-out", "other-station"])
def test_an_added_vehicle_is_checked_as_at_booking(client, token, shop, booked, vehicle, code):
    assert call(client, ADD, add(booked, shop[vehicle], shop), token=token).json()["code"] == code


def test_an_unknown_vehicle_or_a_used_line_id_is_refused(client, token, shop, booked, mo50):
    unknown = add(booked, mo50, shop)
    unknown["item"]["vehicle_id"] = 999999
    used = add(booked, mo50, shop)
    used["item"]["sync_id"] = booked["items"][0]["sync_id"]

    assert call(client, ADD, unknown, token=token).json()["code"] == "unknown_vehicle"
    assert call(client, ADD, used, token=token).json()["code"] == "item_id_used"


def test_an_added_payment_id_already_used_is_refused_and_nothing_added(client, token, shop, booked, mo50):
    request_data = add(booked, mo50, shop, payments=[
        {**booked["payments"][0], "paid_at": "2026-10-02 16:30:00"}])

    assert call(client, ADD, request_data, token=token).json()["code"] == "payment_id_used"
    assert OrderItem.objects.filter(order_id=booked["sync_id"]).count() == 2


def test_nothing_is_added_to_a_closed_order_or_one_not_yet_synced(client, token, shop, booked, mo50):
    assert call(client, ADD, add(booked, mo50, shop, order_id=str(uuid7())), token=token).json()["code"] == \
        "order_not_synced"
    Order.objects.filter(pk=booked["sync_id"]).update(status=OrderStatus.CANCELLED)
    assert call(client, ADD, add(booked, mo50, shop), token=token).json()["code"] == "order_closed"


# -- Replace -----------------------------------------------------------------------------


def test_a_broken_vehicle_is_swapped(client, token, shop, booked):
    """3.2: MO 41 breaks at 16:20 and MO 42 takes over."""
    request_data = replace(booked, shop["mo42"], shop)

    body = call(client, REPLACE, request_data, token=token).json()

    assert body["code"] == "ok", body
    old = line_of(body["data"], booked["items"][0]["sync_id"])
    new = line_of(body["data"], request_data["new_item"]["sync_id"])
    assert (old["status"], old["end_time"], old["reason"], old["total_amount"]) == (
        "replaced", "2026-10-02 16:20:00", "Chain broken", None)
    assert (new["status"], new["vehicle"]["name"], new["replaced_item_id"], new["base_fare"]) == (
        "active", "MO 42", old["sync_id"], "50.000")
    assert body["data"]["items_out"] == 2
    assert can_rent(client, token, shop["mo41"]) is True
    assert can_rent(client, token, shop["mo42"]) is False
    event = OrderEvent.objects.get(pk=request_data["sync_id"])
    assert (str(event.item_id), str(event.new_item_id), event.detail["reason"]) == (
        old["sync_id"], new["sync_id"], "Chain broken")


@pytest.mark.parametrize(("change", "code"), [
    (lambda data, shop: data["new_item"].update(vehicle_id=shop["mo41"].pk), "vehicle_repeated"),
    (lambda data, shop: data["new_item"].update(vehicle_id=shop["dc02"].pk), "vehicle_already_rented"),
    (lambda data, shop: data.update(replaced_at="2026-10-02 15:00:00"), "invalid_time"),
    (lambda data, shop: data.update(old_item_id=str(uuid7())), "unknown_item"),
], ids=["same-vehicle", "new-one-already-out", "before-start", "unknown-line"])
def test_a_wrong_swap_is_refused_and_nothing_changes(client, token, shop, booked, change, code):
    request_data = replace(booked, shop["mo42"], shop)
    change(request_data, shop)

    assert call(client, REPLACE, request_data, token=token).json()["code"] == code
    assert OrderItem.objects.get(pk=booked["items"][0]["sync_id"]).status == OrderItemStatus.ACTIVE
    assert OrderItem.objects.filter(order_id=booked["sync_id"]).count() == 2


def test_a_returned_vehicle_cannot_be_swapped(client, token, shop, booked):
    call(client, RETURN, back(booked), token=token)

    assert call(client, REPLACE, replace(booked, shop["mo42"], shop), token=token).json()["code"] == "item_not_active"


@pytest.mark.parametrize("reason", ["", "   "])
def test_a_swap_needs_a_reason(client, token, shop, booked, reason):
    body = call(client, REPLACE, replace(booked, shop["mo42"], shop, reason=reason), token=token).json()

    assert body["code"] == "invalid_request" and "reason" in body["data"]["errors"]


# -- Remove ------------------------------------------------------------------------------


def test_a_vehicle_taken_off_stays_as_removed(client, token, shop, booked):
    request_data = remove(booked, booked["items"][1]["sync_id"])

    body = call(client, REMOVE, request_data, token=token).json()

    assert body["code"] == "ok", body
    line = line_of(body["data"], booked["items"][1]["sync_id"])
    assert (line["status"], line["end_time"], line["reason"]) == ("removed", "2026-10-02 16:05:00",
                                                                   "Booked by mistake")
    assert body["data"]["items_out"] == 1
    assert can_rent(client, token, shop["dc02"]) is True
    assert OrderEvent.objects.get(pk=request_data["sync_id"]).action == "remove"


@pytest.mark.parametrize(("overrides", "code"), [
    ({"removed_at": "2026-10-02 15:00:00"}, "invalid_time"),
    ({"reason": ""}, "invalid_request"),
], ids=["before-start", "no-reason"])
def test_a_wrong_removal_is_refused(client, token, booked, overrides, code):
    assert call(client, REMOVE, remove(booked, booked["items"][1]["sync_id"], **overrides),
                token=token).json()["code"] == code


def test_a_vehicle_cannot_be_removed_twice(client, token, booked):
    call(client, REMOVE, remove(booked, booked["items"][1]["sync_id"]), token=token)

    assert call(client, REMOVE, remove(booked, booked["items"][1]["sync_id"]), token=token).json()["code"] == \
        "item_not_active"


# -- Every change ------------------------------------------------------------------------


@pytest.mark.parametrize("which", ["add", "replace", "remove"])
def test_a_resent_change_is_a_duplicate_and_a_changed_one_a_conflict(client, token, shop, booked, mo50, which):
    url, request_data = {
        "add": (ADD, add(booked, mo50, shop)),
        "replace": (REPLACE, replace(booked, mo50, shop)),
        "remove": (REMOVE, remove(booked, booked["items"][1]["sync_id"])),
    }[which]
    first = call(client, url, request_data, token=token).json()

    again = call(client, url, request_data, token=token).json()
    changed = copy.deepcopy(request_data)
    changed["order_id"] = booked["sync_id"]
    changed["sync_id"] = request_data["sync_id"]
    changed[{"add": "added_at", "replace": "replaced_at", "remove": "removed_at"}[which]] = "2026-10-02 16:45:00"

    assert (again["code"], again["data"]) == ("duplicate", first["data"])
    assert call(client, url, changed, token=token).json()["code"] == "sync_id_conflict"


def test_another_tablet_at_the_station_can_change_the_order(client, world, shop, booked):
    other = sign_in_tablet(client, world, code="OPR002", installation_id="till-2", branch=world["adc1"])

    assert call(client, REPLACE, replace(booked, shop["mo42"], shop), token=other).json()["code"] == "ok"


def test_only_returned_vehicles_are_billed_and_invoiced(client, token, shop, booked, mo50):
    """Book MO 41 + DC 02; MO 41 swapped for MO 42; MO 50 added and then taken
    off; MO 42 and DC 02 returned. The bill is MO 42 + DC 02 only."""
    swap = replace(booked, shop["mo42"], shop)
    added = add(booked, mo50, shop)
    for url, request_data in ((REPLACE, swap), (ADD, added), (REMOVE, remove(booked, added["item"]["sync_id"],
                                                                             removed_at="2026-10-02 16:31:00"))):
        assert call(client, url, request_data, token=token).json()["code"] == "ok"
    for item_id in (swap["new_item"]["sync_id"], booked["items"][1]["sync_id"]):
        assert call(client, RETURN, back(booked, item_id=item_id), token=token).json()["code"] == "ok"

    # Lines 50.00 + 50.00, VAT included (4.76 of it); net 100.00 -- the 100.00 down covers it.
    body = call(client, SETTLE, bill(booked, shop, subtotal="100.00", tax_amount="4.76", net_amount="100.00",
                                     payments=[]), token=token).json()

    assert body["code"] == "ok", body
    invoice = Invoice.objects.get()
    assert sorted(invoice.items.values_list("vehicle_name", "total_amount")) == [
        ("DC 02", Decimal("50.00")), ("MO 42", Decimal("50.00"))]
    statuses = dict(OrderItem.objects.filter(order_id=booked["sync_id"]).values_list("vehicle__vehicle_name", "status"))
    assert statuses == {"MO 41": "replaced", "DC 02": "returned", "MO 42": "returned", "MO 50": "removed"}


def test_the_change_endpoints_are_in_the_api_docs(client, db):
    schema = client.get("/api/schema/").content.decode()

    assert all(f"/api/v1/{{app}}/orders/{name}" in schema for name in ("add", "replace", "remove"))
