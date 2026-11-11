"""A parked order call is pushed to the station's other tablets (owner, 11 Nov
2026): multi-device stations only, the parking tablet left out, with what the
other tablets need because a parked booking is not an order on the server yet."""

import pytest

from apps.company.models import Branch
from apps.portal.session_models import AppSession
from apps.rental import tasks
from apps.rental.tests import test_order_api as orders
from apps.rental.tests import test_order_change as changes
from apps.rental.tests import test_station_sync as stations
from apps.rental.tests.test_order_return import RETURN, back
from apps.rental.tests.test_order_settle import SETTLE, bill
from apps.sync.models import SyncIssue
from apps.sync.tests.test_parking import send

booking = orders.booking
world, token, fresh_throttle, shop = orders.world, orders.token, orders.fresh_throttle, orders.shop
manager, fcm, station = stations.manager, stations.fcm, stations.station

ANY = object()


@pytest.fixture
def park(client, token, django_capture_on_commit_callbacks):
    def go(url, data):
        with django_capture_on_commit_callbacks(execute=True):
            return send(client, url, data, token=token, park=True).json()
    return go


@pytest.fixture
def booked(client, world, token, shop, station, fcm, django_capture_on_commit_callbacks):
    data = booking(world, shop)
    with django_capture_on_commit_callbacks(execute=True):
        assert send(client, orders.BOOK, data, token=token).json()["code"] == "ok"
    fcm.sent.clear()
    return data


def refused_booking(world, shop):
    """Parks: its second vehicle does not exist."""
    data = booking(world, shop)
    data["items"][1]["vehicle_id"] = 999999
    return data


def test_a_parked_booking_reaches_the_other_tablet_with_its_details(world, shop, station, fcm, park):
    data = refused_booking(world, shop)

    assert park(orders.BOOK, data)["code"] == "parked"

    issue = SyncIssue.objects.get()
    assert stations.tokens(fcm) == ["tok-till-2"]          # not the parking till, another station or a manager
    _, title, body, payload, channel, tag, collapse = fcm.sent[0]
    assert title == f"Order {data['order_no']}: booking parked"
    assert body == "MO 41 · Abu Dhabi Corniche 1"
    assert payload == {"type": "order_parked", "action": "booking", "issue_id": str(issue.pk),
                       "order_id": data["sync_id"], "order_no": data["order_no"],
                       "vehicle_ids": f"{shop['mo41'].pk},999999", "branch_id": str(world["adc1"].pk),
                       "at": payload["at"]}
    assert (channel, tag, collapse) == ("station_activity", f"parked:{issue.pk}", f"parked:{issue.pk}")


def test_each_kind_of_order_call_is_pushed_with_its_vehicle(shop, booked, fcm, park):
    taken = shop["dc02"].pk                                   # already out, so these are refused
    add = changes.add(booked, shop["dc02"], shop)
    swap = changes.replace(booked, shop["dc02"], shop)

    for url, data, action, vehicle in (
        (changes.ADD, add, "add", str(taken)),
        (changes.REPLACE, swap, "replace", str(taken)),
    ):
        fcm.sent.clear()
        # the second is a follower behind the first: it is still a parked order call
        assert park(url, data)["code"] == "parked"
        payload = fcm.sent[0][3]
        assert (payload["action"], payload["vehicle_ids"], payload["order_no"]) == (action, vehicle, booked["order_no"])


def test_a_parked_return_names_the_line_vehicle(shop, booked, fcm, park):
    wrong = back(booked, 0, total_amount="1.00")

    assert park(RETURN, wrong)["code"] == "parked"

    assert fcm.sent[0][3]["vehicle_ids"] == str(shop["mo41"].pk) and fcm.sent[0][3]["action"] == "return"
    assert fcm.sent[0][1] == f"Order {booked['order_no']}: vehicle return parked"


def test_a_parked_settle_has_no_vehicles(shop, booked, fcm, park):
    assert park(SETTLE, bill(booked, shop))["code"] == "parked"           # vehicles still out

    assert (fcm.sent[0][3]["action"], fcm.sent[0][3]["vehicle_ids"]) == ("settle", "")


def test_a_follower_is_pushed_and_finds_its_vehicle_on_the_parked_booking(world, shop, station, fcm, park):
    data = refused_booking(world, shop)
    park(orders.BOOK, data)
    fcm.sent.clear()

    assert park(RETURN, back(data, 0))["code"] == "parked"

    payload = fcm.sent[0][3]
    assert (payload["action"], payload["vehicle_ids"], payload["order_no"]) == (
        "return", str(shop["mo41"].pk), data["order_no"])


def test_a_single_device_station_is_not_told(world, shop, station, fcm, park):
    Branch.objects.filter(pk=world["adc1"].pk).update(is_multi_device=False)

    assert park(orders.BOOK, refused_booking(world, shop))["code"] == "parked"

    assert fcm.sent == []


def test_a_signed_out_tablet_or_one_with_no_token_is_not_told(world, shop, station, fcm, park):
    AppSession.objects.filter(device__installation_id="till-2").update(push_token="")

    park(orders.BOOK, refused_booking(world, shop))

    assert fcm.sent == []


def test_a_resend_of_a_parked_call_is_not_pushed_again(world, shop, station, fcm, park):
    data = refused_booking(world, shop)
    park(orders.BOOK, data)
    fcm.sent.clear()

    assert park(orders.BOOK, data)["code"] == "parked"

    assert fcm.sent == []


def test_a_call_that_is_not_about_an_order_is_not_pushed(station, fcm, park):
    customer = {"sync_id": "01923e1c-0a11-7b22-8c33-d4e5f6a7b8c0", "first_name": "Mariam", "last_name": "Saeed",
                "mobile_country_code": "+971", "mobile_no": "509876543", "full_number": "971500000000"}

    assert park("/api/v1/operator/customers/create", customer)["code"] == "parked"

    assert fcm.sent == []


def test_a_normal_call_only_sends_the_usual_push(client, world, token, shop, station, fcm,
                                                 django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        assert send(client, orders.BOOK, booking(world, shop), token=token, park=True).json()["code"] == "ok"

    assert [sent[3]["type"] for sent in fcm.sent] == ["order_changed"]


def test_a_push_that_cannot_be_queued_does_not_stop_the_parking(world, shop, station, fcm, park, monkeypatch):
    def broken(*args, **kwargs):
        raise ConnectionError("no broker")

    monkeypatch.setattr(tasks.station_parked_push, "delay", broken)

    assert park(orders.BOOK, refused_booking(world, shop))["code"] == "parked"
    assert SyncIssue.objects.count() == 1


def test_a_malformed_body_still_parks_and_pushes(world, shop, station, fcm, park):
    data = refused_booking(world, shop)
    data["items"] = "not a list"

    assert park(orders.BOOK, data)["code"] == "parked"

    assert fcm.sent[0][3]["vehicle_ids"] == ""
