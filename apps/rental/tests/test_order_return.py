"""POST /api/v1/operator/orders/return through HTTP: one vehicle back
(order_lifecycle_design.md 3.3) -- on time and late, resent, refused, and
from another tablet at the station."""

import copy

import pytest

from apps.rental.models import Order, OrderEvent, OrderItem, OrderItemStatus, OrderStatus
from apps.rental.tests import test_order_api as orders
from core.ids import uuid7

call, booking, sign_in_tablet = orders.call, orders.booking, orders.sign_in_tablet
world, token, fresh_throttle, shop = orders.world, orders.token, orders.fresh_throttle, orders.shop

RETURN = "/api/v1/operator/orders/return"
VEHICLES = "/api/v1/operator/vehicles"


@pytest.fixture
def booked(client, world, token, shop):
    """The design's 3.1 booking, on the server: MO 41 and DC 02 out for an hour."""
    request_data = booking(world, shop)
    assert call(client, orders.BOOK, request_data, token=token).json()["code"] == "ok"
    return request_data


def back(booked, index=0, **overrides):
    """A return of the booking's `index`-th vehicle, on time."""
    data = {
        "sync_id": str(uuid7()), "order_id": booked["sync_id"], "item_id": booked["items"][index]["sync_id"],
        "returned_at": "2026-10-02 17:00:00", "run_minutes": 60, "overtime_amount": "0.00", "total_amount": "50.00",
    }
    data.update(overrides)
    return data


def test_a_vehicle_back_on_time_closes_its_line(client, token, booked):
    body = call(client, RETURN, back(booked), token=token).json()

    assert body["code"] == "ok", body
    line = next(item for item in body["data"]["items"] if item["sync_id"] == booked["items"][0]["sync_id"])
    assert (line["status"], line["end_time"], line["run_minutes"]) == ("returned", "2026-10-02 17:00:00", 60)
    assert (line["base_fare"], line["overtime_amount"], line["total_amount"]) == ("50.00", "0.00", "50.00")
    assert body["data"]["items_out"] == 1
    # Still no bill: that comes at settle.
    assert (body["data"]["status"], body["data"]["net_amount"]) == ("active", None)


def test_a_late_vehicle_carries_its_overtime(client, token, booked):
    late = back(booked, 1, returned_at="2026-10-02 17:12:00", run_minutes=72, overtime_amount="10.00",
                total_amount="60.00")

    data = call(client, RETURN, late, token=token).json()["data"]

    line = OrderItem.objects.get(pk=booked["items"][1]["sync_id"])
    assert (line.run_minutes, line.overtime_amount, line.total_amount) == (72, 10, 60)
    event = OrderEvent.objects.get(pk=late["sync_id"])
    assert (event.action, str(event.item_id)) == ("return", booked["items"][1]["sync_id"])
    assert data["items_out"] == 1


def test_a_returned_vehicle_can_be_rented_again(client, token, booked, shop):
    def can_rent():
        data = call(client, VEHICLES, token=token).json()["data"]
        return {v["vehicle_id"]: v["can_rent"] for c in data["categories"] for t in c["vehicle_types"]
                for v in t["vehicles"]}[shop["mo41"].pk]

    assert can_rent() is False
    call(client, RETURN, back(booked), token=token)
    assert can_rent() is True


def test_a_resent_return_is_a_duplicate(client, token, booked):
    request_data = back(booked)
    first = call(client, RETURN, request_data, token=token).json()

    again = call(client, RETURN, request_data, token=token).json()

    assert (again["code"], again["data"]) == ("duplicate", first["data"])
    assert OrderEvent.objects.filter(action="return").count() == 1


def test_the_same_call_id_with_another_body_is_a_conflict(client, token, booked):
    request_data = back(booked)
    call(client, RETURN, request_data, token=token)
    changed = copy.deepcopy(request_data)
    changed["run_minutes"] = 61

    assert call(client, RETURN, changed, token=token).json()["code"] == "sync_id_conflict"


def test_a_return_before_its_booking_is_retried_later(client, token, booked):
    body = call(client, RETURN, back(booked, order_id=str(uuid7())), token=token).json()

    assert (body["code"], body["data"]) == ("order_not_synced", {"retry": True})


@pytest.mark.parametrize(("change", "code"), [
    (lambda data: data.update(item_id=str(uuid7())), "unknown_item"),
    (lambda data: data.update(returned_at="2026-10-02 15:59:00"), "invalid_return_time"),
    (lambda data: data.update(total_amount="55.00"), "amount_mismatch"),
], ids=["unknown-item", "before-start", "total-not-base-plus-overtime"])
def test_a_wrong_return_is_refused_and_nothing_changes(client, token, booked, change, code):
    request_data = back(booked)
    change(request_data)

    body = call(client, RETURN, request_data, token=token).json()

    assert (body["code"], body["data"]) == (code, {"retry": False})
    assert OrderItem.objects.filter(status=OrderItemStatus.ACTIVE).count() == 2


def test_a_vehicle_cannot_come_back_twice(client, token, booked):
    call(client, RETURN, back(booked), token=token)

    assert call(client, RETURN, back(booked), token=token).json()["code"] == "item_not_active"


def test_a_closed_order_takes_no_returns(client, token, booked):
    Order.objects.filter(pk=booked["sync_id"]).update(status=OrderStatus.CANCELLED)

    assert call(client, RETURN, back(booked), token=token).json()["code"] == "order_closed"


def test_another_tablet_at_the_station_can_take_it_back(client, world, booked):
    other = sign_in_tablet(client, world, code="OPR002", installation_id="till-2", branch=world["adc1"])

    assert call(client, RETURN, back(booked), token=other).json()["code"] == "ok"


def test_a_tablet_at_another_station_cannot(client, world, booked):
    elsewhere = sign_in_tablet(client, world, code="OPR003", installation_id="till-3", branch=world["adc2"])

    assert call(client, RETURN, back(booked), token=elsewhere).json()["code"] == "order_not_synced"


def test_the_return_endpoint_is_in_the_api_docs(client, db):
    assert "/api/v1/{app}/orders/return" in client.get("/api/schema/").content.decode()
