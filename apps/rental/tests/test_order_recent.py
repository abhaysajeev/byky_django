"""POST /api/v1/operator/orders/recent: the station's newest orders, any
status and any tablet, newest by booking time, each in the detail shape."""

import pytest

from apps.fleet.models import Vehicle
from apps.rental.models import Order, OrderStatus
from apps.rental.tests import test_order_api as orders
from core.enums import ApprovalStatus
from core.ids import uuid7

call, booking, sign_in_tablet = orders.call, orders.booking, orders.sign_in_tablet
world, token, fresh_throttle, shop = orders.world, orders.token, orders.fresh_throttle, orders.shop

RECENT = "/api/v1/operator/orders/recent"


def book_one(client, world, shop, token, number, booked_at):
    """A one-vehicle booking on a bike of its own, booked at `booked_at`."""
    bike = Vehicle.objects.create(company=world["company"], vehicle_code=f"BK {number}", vehicle_name=f"BK {number}",
                                  vehicle_type=world["monaco"], uom=shop["mo41"].uom, current_branch=world["adc1"],
                                  approval_status=ApprovalStatus.APPROVED)
    data = booking(world, shop, number=number, booked_at=booked_at, payments=[])
    data["items"] = [{**data["items"][0], "sync_id": str(uuid7()), "vehicle_id": bike.pk}]
    assert call(client, orders.BOOK, data, token=token).json()["code"] == "ok"
    return data["sync_id"]


def recent(client, token):
    body = call(client, RECENT, token=token).json()
    assert body["code"] == "ok", body
    return body["data"]["orders"]


def test_the_five_newest_by_booking_time_in_any_status(client, world, token, shop):
    ids = [book_one(client, world, shop, token, 200 + n, f"2026-10-02 1{n}:00:00") for n in range(6)]
    # Uploaded last, but booked earliest: not among the newest.
    book_one(client, world, shop, token, 300, "2026-10-02 08:00:00")
    Order.objects.filter(pk=ids[5]).update(status=OrderStatus.CANCELLED)

    rows = recent(client, token)

    assert [row["sync_id"] for row in rows] == list(reversed(ids[1:]))
    assert rows[0]["status"] == OrderStatus.CANCELLED


def test_each_order_is_whole_in_the_detail_shape(client, world, token, shop):
    request_data = booking(world, shop)
    call(client, orders.BOOK, request_data, token=token)

    [row] = recent(client, token)
    detail = call(client, orders.DETAIL, {"sync_id": request_data["sync_id"]}, token=token).json()["data"]

    assert row == detail
    assert len(row["items"]) == 2 and len(row["payments"]) == 2


def test_any_tablet_at_the_station_sees_them_and_other_stations_do_not_count(client, world, token, shop):
    first = book_one(client, world, shop, token, 201, "2026-10-02 10:00:00")
    moved = book_one(client, world, shop, token, 202, "2026-10-02 11:00:00")
    Order.objects.filter(pk=moved).update(branch=world["adc2"])
    other = sign_in_tablet(client, world, code="OPR002", installation_id="till-2", branch=world["adc1"])

    assert [row["sync_id"] for row in recent(client, other)] == [first]


def test_a_station_with_no_orders_gets_an_empty_list(client, token):
    assert recent(client, token) == []


@pytest.mark.parametrize("app", ["manager", "employee"])
def test_only_the_operator_app(client, token, app):
    response = call(client, RECENT.replace("operator", app), token=token)

    assert (response.status_code, response.json()["code"]) == (403, "wrong_channel")
