"""A vehicle's rent status follows parked calls (plan 3 Nov 2026):

- case A: a parked booking / add / replace takes its vehicle out -- other
  tablets see it on rent until its return arrives (even a parked one) or the
  issue is dismissed; replaying the call is not refused by itself;
- case B: a parked return brings the vehicle back at once -- free to book
  again -- while its line waits for the office's correction.

Live, 31 Oct 2026: vehicle 5127 sat in parked booking DUBPP100200000007 and
another tablet booked it again at 17:52."""

import pytest

from apps.rental.models import Order, OrderItem, OrderItemStatus
from apps.rental.tests import test_order_change as change
from apps.sync import services
from apps.sync.models import IssueStatus, SyncIssue
from apps.sync.tests import test_parking as parking

booking, send, back, office = parking.booking, parking.send, parking.back, parking.office
world, token, fresh_throttle, shop = parking.world, parking.token, parking.fresh_throttle, parking.shop
booked, mo50, new_line = change.booked, change.mo50, change.new_line
BOOK, RETURN, ADD, REPLACE, VEHICLES = parking.BOOK, parking.RETURN, change.ADD, change.REPLACE, change.VEHICLES


def vehicle_row(client, token, vehicle):
    data = send_get(client, token)
    return {v["vehicle_id"]: v for c in data["categories"] for t in c["vehicle_types"]
            for v in t["vehicles"]}[vehicle.pk]


def send_get(client, token):
    return change.call(client, VEHICLES, token=token).json()["data"]


def parked_booking(client, world, shop, token, number=250):
    """A booking refused (unknown customer) and parked: nothing on the server."""
    data = booking(world, shop, number=number, customer_id=999999)
    body = send(client, BOOK, data, token=token, park=True).json()
    assert body["code"] == "parked", body
    return data


def issues():
    return SyncIssue.objects.all()


# -- Case A: a parked call takes its vehicles out --------------------------------------------

def test_a_parked_bookings_vehicles_are_on_rent_and_cannot_be_booked_again(client, world, shop, token):
    parked = parked_booking(client, world, shop, token)

    row = vehicle_row(client, token, shop["mo41"])
    assert (row["on_rent"], row["can_rent"]) == (True, False)
    assert row["rental"]["order_no"] == parked["order_no"]
    again = send(client, BOOK, booking(world, shop, number=251), token=token).json()
    assert again["code"] == "vehicle_already_rented"
    assert not Order.objects.exists()


def test_a_parked_return_behind_the_parked_booking_frees_that_vehicle(client, world, shop, token):
    parked = parked_booking(client, world, shop, token)

    body = send(client, RETURN, back(parked, 0), token=token, park=True).json()

    assert body["code"] == "parked" and body["data"]["waiting_on"] is not None
    assert vehicle_row(client, token, shop["mo41"])["can_rent"] is True      # its line came back
    assert vehicle_row(client, token, shop["dc02"])["can_rent"] is False     # the other is still out


def test_dismissing_the_parked_booking_frees_its_vehicles(client, world, shop, token, office):
    parked_booking(client, world, shop, token)
    head = issues().get()

    services.dismiss(issues(), head.pk, user=office, reason="Customer left")

    assert vehicle_row(client, token, shop["mo41"])["can_rent"] is True


def test_replaying_the_parked_booking_is_not_refused_by_itself(client, world, shop, token, office):
    parked = parked_booking(client, world, shop, token)
    head = issues().get()

    outcome = services.replay(issues(), head.pk, user=office,
                              request_data={**parked, "customer_id": shop["ahmed"].pk})

    assert outcome["stopped_at"] is None, outcome
    assert Order.objects.get(pk=parked["sync_id"]).items.count() == 2
    assert vehicle_row(client, token, shop["mo41"])["can_rent"] is False     # out on the real line now


def test_a_parked_added_vehicle_is_on_rent(client, token, shop, booked, mo50):
    data = change.add(booked, mo50, shop, item=new_line(mo50, shop, fare_id=999999))

    assert send(client, ADD, data, token=token, park=True).json()["code"] == "parked"

    assert vehicle_row(client, token, mo50)["can_rent"] is False


def test_a_parked_replacements_new_vehicle_is_on_rent(client, token, shop, booked, mo50):
    data = change.replace(booked, mo50, shop, new_item=new_line(mo50, shop, fare_id=999999))

    assert send(client, REPLACE, data, token=token, park=True).json()["code"] == "parked"

    assert vehicle_row(client, token, mo50)["can_rent"] is False


# -- Case B: a parked return brings the vehicle back ---------------------------------------

def test_a_parked_return_frees_the_vehicle_while_the_line_waits(client, world, shop, token, booked):
    wrong = back(booked, 0, total_amount="55.00", returned_at="2026-10-02 16:45:00")

    assert send(client, RETURN, wrong, token=token, park=True).json()["code"] == "parked"

    line = OrderItem.objects.get(pk=booked["items"][0]["sync_id"])
    assert line.status == OrderItemStatus.ACTIVE and line.return_pending_at is not None
    assert vehicle_row(client, token, shop["mo41"])["can_rent"] is True
    mo41 = shop["mo41"]
    again = booking(world, shop, number=252, items=[new_line(mo41, shop)], payments=[])
    assert send(client, BOOK, again, token=token).json()["code"] == "ok"            # booked again


def test_replaying_the_return_later_closes_the_old_line_beside_the_new_rental(client, world, shop, token,
                                                                             booked, office):
    wrong = back(booked, 0, total_amount="55.00")
    send(client, RETURN, wrong, token=token, park=True)
    again = booking(world, shop, number=253, items=[new_line(shop["mo41"], shop)], payments=[])
    assert send(client, BOOK, again, token=token).json()["code"] == "ok"
    head = issues().get()

    outcome = services.replay(issues(), head.pk, user=office, request_data={**wrong, "total_amount": "50.00"})

    assert outcome["stopped_at"] is None, outcome
    old = OrderItem.objects.get(pk=booked["items"][0]["sync_id"])
    assert (old.status, old.return_pending_at, old.total_amount) == (OrderItemStatus.RETURNED, None, 50)
    assert vehicle_row(client, token, shop["mo41"])["can_rent"] is False     # out on the new rental


def test_dismissing_the_parked_return_leaves_the_vehicle_free(client, token, shop, booked, office):
    send(client, RETURN, back(booked, 0, total_amount="55.00"), token=token, park=True)

    services.dismiss(issues(), issues().get().pk, user=office, reason="Checked by hand")

    line = OrderItem.objects.get(pk=booked["items"][0]["sync_id"])
    assert line.status == OrderItemStatus.ACTIVE and line.return_pending_at is not None
    assert vehicle_row(client, token, shop["mo41"])["can_rent"] is True


def test_a_replayed_booking_whose_return_still_fails_keeps_that_vehicle_free(client, world, shop, token,
                                                                             office):
    parked = parked_booking(client, world, shop, token)
    send(client, RETURN, back(parked, 0, total_amount="55.00"), token=token, park=True)
    head = issues().order_by("id").first()

    outcome = services.replay(issues(), head.pk, user=office,
                              request_data={**parked, "customer_id": shop["ahmed"].pk})

    assert outcome["stopped_at"] is not None                                 # the return is still wrong
    assert issues().filter(status=IssueStatus.OPEN).count() == 1
    line = OrderItem.objects.get(pk=parked["items"][0]["sync_id"])
    assert line.return_pending_at is not None
    assert vehicle_row(client, token, shop["mo41"])["can_rent"] is True


@pytest.mark.parametrize("index", [0, 1])
def test_a_normal_return_still_frees_the_vehicle_and_leaves_no_mark(client, token, shop, booked, index):
    assert send(client, RETURN, back(booked, index), token=token).json()["code"] == "ok"

    line = OrderItem.objects.get(pk=booked["items"][index]["sync_id"])
    assert (line.status, line.return_pending_at) == (OrderItemStatus.RETURNED, None)
