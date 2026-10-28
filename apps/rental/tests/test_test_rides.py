"""Test rides (apps/rental/test_rides.py): a station's tablet takes one vehicle
out free for the station's test minutes; the vehicle cannot be rented
meanwhile; the ride ends, is cancelled, or is rented -- the booking names it
(owner, 28 Oct 2026)."""

import datetime

import pytest
from django.utils import timezone

from apps.devices import services as devices_services
from apps.devices.models import BillKind, DeviceSettings
from apps.fleet.tests.test_image_shares import web_client
from apps.fleet.tests.test_service_requests import vehicle_row
from apps.portal.models import RolePermission
from apps.rental.models import Order, TestRide, TestRideStatus
from apps.rental.tests import test_order_api as orders
from apps.rental.tests import test_order_change as changes
from apps.rental.tests import test_order_requests as requests_tests
from apps.rental.tests import test_station_sync as sync_tests
from core.ids import uuid7

call, booking = orders.call, orders.booking
world, token, fresh_throttle, shop = orders.world, orders.token, orders.fresh_throttle, orders.shop
manager = requests_tests.manager
fcm, station = sync_tests.fcm, sync_tests.station

START = "/api/v1/operator/test-rides"
END = "/api/v1/operator/test-rides/end"
CANCEL = "/api/v1/operator/test-rides/cancel"
LIST = "/api/v1/operator/test-rides/list"


@pytest.fixture
def allowed(world):
    """Corniche 1 allows test rides: customers 5 minutes, staff 10."""
    world["adc1"].allows_test_ride = True
    world["adc1"].save()
    DeviceSettings.objects.filter(branch=world["adc1"]).update(customer_test_minutes=5, staff_test_minutes=10)
    return world["adc1"]


def number_for(world, shop, number):
    counter = devices_services.counter_for(shop["tablet"], world["adc1"], BillKind.TEST_RIDE)
    return devices_services.format_bill_number(counter, number)


def ride(world, shop, vehicle="mo41", number=6, **extra):
    data = {"sync_id": str(uuid7()), "test_no": number_for(world, shop, number), "vehicle_id": shop[vehicle].pk,
            "mode": "customer", "customer_name": "Ahmed Al Mansoori", "customer_mobile": "971501234567",
            "started_at": "2026-10-28 11:10:00"}
    data.update(extra)
    return data


def started(client, token, data):
    body = call(client, START, data, token=token).json()
    assert body["code"] == "ok", body
    return body["data"]


# -- Start --------------------------------------------------------------------------------


def test_a_customer_test_ride_and_a_resend(client, world, token, shop, allowed):
    data = ride(world, shop)
    row = started(client, token, data)
    assert (row["test_ride_id"], row["mode"], row["status"], row["vehicle_name"]) == (
        data["sync_id"], "customer", "on_test", "MO 41")
    assert (row["allowed_minutes"], row["started_at"], row["expected_end_at"]) == (
        5, "2026-10-28 11:10:00", "2026-10-28 11:15:00")
    assert (row["customer_name"], row["customer_mobile"]) == ("Ahmed Al Mansoori", "971501234567")
    assert row["next_test_number"] == 7                                    # the test counter was raised
    assert call(client, START, data, token=token).json()["code"] == "duplicate"
    assert TestRide.objects.count() == 1


def test_a_staff_test_ride_takes_the_staff_minutes_and_no_customer(client, world, token, shop, allowed):
    row = started(client, token, ride(world, shop, mode="staff", customer_name="", customer_mobile=""))
    assert (row["mode"], row["allowed_minutes"], row["expected_end_at"], row["customer_name"]) == (
        "staff", 10, "2026-10-28 11:20:00", "")


def test_a_customer_test_ride_names_the_customer(client, world, token, shop, allowed):
    body = call(client, START, ride(world, shop, customer_mobile=""), token=token).json()
    assert body["code"] == "invalid_request" and "customer_mobile" in body["data"]["errors"]


def test_only_where_test_rides_are_allowed(client, world, token, shop):
    assert call(client, START, ride(world, shop), token=token).json()["code"] == "test_rides_not_allowed"


@pytest.mark.parametrize("change,code", [
    (lambda w, s: {"vehicle_id": s["away"].pk}, "vehicle_not_at_station"),
    (lambda w, s: {"vehicle_id": 999999}, "unknown_vehicle"),
])
def test_a_vehicle_not_here_is_refused(client, world, token, shop, allowed, change, code):
    assert call(client, START, ride(world, shop, **change(world, shop)), token=token).json()["code"] == code


def test_a_vehicle_on_rent_or_on_test_and_a_used_number(client, world, token, shop, allowed):
    assert call(client, orders.BOOK, booking(world, shop), token=token).json()["code"] == "ok"   # MO 41, DC 02
    assert call(client, START, ride(world, shop), token=token).json()["code"] == "vehicle_already_rented"
    started(client, token, ride(world, shop, vehicle="mo42", number=7))
    assert call(client, START, ride(world, shop, vehicle="mo42", number=8), token=token).json()[
        "code"] == "vehicle_on_test_ride"
    TestRide.objects.update(status=TestRideStatus.ENDED, ended_at=timezone.now())
    assert call(client, START, ride(world, shop, vehicle="mo42", number=7), token=token).json()[
        "code"] == "test_no_used"


# -- On test: not rentable ----------------------------------------------------------------


def test_a_vehicle_on_test_cannot_be_rented_until_it_is_back(client, world, token, shop, allowed):
    row = started(client, token, ride(world, shop))
    listed = vehicle_row(client, token, shop["mo41"])
    assert (listed["on_test_ride"], listed["can_rent"], listed["test_ride"]["test_no"]) == (
        True, False, row["test_no"])
    assert listed["test_ride"]["expected_end_time"] == "2026-10-28 11:15:00"
    assert call(client, orders.BOOK, booking(world, shop), token=token).json()["code"] == "vehicle_on_test_ride"

    body = call(client, END, {"sync_id": str(uuid7()), "test_ride_id": row["test_ride_id"],
                              "ended_at": "2026-10-28 11:14:00"}, token=token).json()
    assert (body["code"], body["data"]["status"], body["data"]["ended_at"]) == ("ok", "ended", "2026-10-28 11:14:00")
    assert vehicle_row(client, token, shop["mo41"])["can_rent"] is True
    assert call(client, orders.BOOK, booking(world, shop), token=token).json()["code"] == "ok"


def test_add_and_replace_refuse_a_vehicle_on_test(client, world, token, shop, allowed):
    booked = booking(world, shop)
    assert call(client, orders.BOOK, booked, token=token).json()["code"] == "ok"
    started(client, token, ride(world, shop, vehicle="mo42"))
    assert call(client, changes.ADD, changes.add(booked, shop["mo42"], shop), token=token).json()[
        "code"] == "vehicle_on_test_ride"


# -- End and cancel -----------------------------------------------------------------------


def test_end_cancel_and_their_resends(client, world, token, shop, allowed):
    row = started(client, token, ride(world, shop))
    cancel = {"sync_id": str(uuid7()), "test_ride_id": row["test_ride_id"], "cancelled_at": "2026-10-28 11:11:00"}
    assert call(client, CANCEL, cancel, token=token).json()["code"] == "ok"
    assert call(client, CANCEL, {**cancel, "sync_id": str(uuid7())}, token=token).json()["code"] == "duplicate"
    end = {"sync_id": str(uuid7()), "test_ride_id": row["test_ride_id"], "ended_at": "2026-10-28 11:12:00"}
    assert call(client, END, end, token=token).json()["code"] == "test_ride_closed"
    assert call(client, END, {**end, "test_ride_id": str(uuid7())}, token=token).json()["code"] == \
        "unknown_test_ride"


# -- Rented after the test: the booking names it ------------------------------------------


def test_a_booking_that_names_the_test_ride_rents_it_and_links_them(client, world, token, shop, allowed):
    row = started(client, token, ride(world, shop))
    data = booking(world, shop, test_ride_id=row["test_ride_id"])
    body = call(client, orders.BOOK, data, token=token).json()
    assert body["code"] == "ok", body
    assert body["data"]["test_ride"] == {"test_ride_id": row["test_ride_id"], "test_no": row["test_no"]}
    test = TestRide.objects.get()
    assert (test.status, str(test.order_id), test.ended_at) == (
        TestRideStatus.RENTED, data["sync_id"], Order.objects.get().start_time)
    assert call(client, orders.BOOK, data, token=token).json()["code"] == "duplicate"      # resend: unchanged
    again = booking(world, shop, number=232, test_ride_id=row["test_ride_id"])
    again["items"] = [{**again["items"][0], "vehicle_id": shop["mo42"].pk}]
    assert call(client, orders.BOOK, again, token=token).json()["code"] == "test_ride_closed"


def test_the_customer_may_rent_another_vehicle_after_the_test(client, world, token, shop, allowed):
    row = started(client, token, ride(world, shop, vehicle="mo42"))                       # tested MO 42
    body = call(client, orders.BOOK, booking(world, shop, test_ride_id=row["test_ride_id"]), token=token).json()
    assert body["code"] == "ok", body                                                     # rents MO 41 + DC 02
    assert TestRide.objects.get().status == TestRideStatus.RENTED


def test_a_refused_booking_leaves_the_test_ride_on_test(client, world, token, shop, allowed):
    row = started(client, token, ride(world, shop))
    data = booking(world, shop, test_ride_id=row["test_ride_id"], customer_id=999999)
    assert call(client, orders.BOOK, data, token=token).json()["code"] == "unknown_customer"
    assert TestRide.objects.get().status == TestRideStatus.ON_TEST
    other = call(client, orders.BOOK, booking(world, shop, number=233, test_ride_id=str(uuid7())), token=token)
    assert other.json()["code"] == "unknown_test_ride"


# -- The station's other tablets ----------------------------------------------------------


def test_start_and_end_reach_the_other_tablet_at_the_station(client, world, token, shop, allowed, station, fcm,
                                                            django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        row = started(client, token, ride(world, shop))
    assert [(s[0], s[3]["type"], s[3]["action"]) for s in fcm.sent] == [("tok-till-2", "test_ride_changed", "start")]
    assert fcm.sent[0][1] == f"Test ride {row['test_no']} started"
    assert fcm.sent[0][4:] == ("station_activity", f"test_ride:{row['test_ride_id']}", f"test_ride:{row['test_ride_id']}")
    fcm.sent.clear()
    with django_capture_on_commit_callbacks(execute=True):
        call(client, END, {"sync_id": str(uuid7()), "test_ride_id": row["test_ride_id"],
                           "ended_at": "2026-10-28 11:14:00"}, token=station["mona"])
    assert sorted((s[0], s[3]["action"]) for s in fcm.sent) == [("tok-till-1", "end")]     # not Mona's own


def test_the_list_is_the_catch_up(client, world, token, shop, allowed, station):
    first = started(client, token, ride(world, shop))
    started(client, token, ride(world, shop, vehicle="mo42", number=7))
    call(client, END, {"sync_id": str(uuid7()), "test_ride_id": first["test_ride_id"],
                       "ended_at": "2026-10-28 11:14:00"}, token=token)
    open_ = call(client, LIST, {}, token=station["mona"]).json()["data"]        # another tablet, same station
    assert [r["vehicle_name"] for r in open_["test_rides"]] == ["MO 42"]
    every = call(client, LIST, {"open": False}, token=token).json()["data"]
    assert every["total"] == 2
    assert call(client, LIST, {"open": False, "status": "ended"}, token=token).json()["data"]["total"] == 1
    assert call(client, LIST, {}, token=station["omar"]).json()["data"]["total"] == 0        # Corniche 2


# -- Web ----------------------------------------------------------------------------------


def test_the_web_list_with_overdue_and_the_read_right(client, world, token, shop, allowed):
    started(client, token, ride(world, shop, started_at="2026-10-01 11:10:00"))       # long overdue
    started(client, token, ride(world, shop, vehicle="mo42", number=7, started_at=(
        timezone.localtime(timezone.now()) + datetime.timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")))
    browser = web_client(world)
    page = browser.get("/rental/test-ride/list/")
    assert page.status_code == 200
    assert sorted(r["status"] for r in page.context["rows"]) == ["on_test", "overdue"]
    assert len(browser.get("/rental/test-ride/list/?status=overdue").context["rows"]) == 1
    assert len(browser.get("/rental/test-ride/list/?q=MO 42").context["rows"]) == 1
    RolePermission.objects.filter(role=browser.role, page__code="rental.test_ride").update(can_read=False)
    assert browser.get("/rental/test-ride/list/").status_code == 403


# -- Device settings: Cashier -> Staff -----------------------------------------------------


def test_login_sends_the_staff_minutes_under_both_names(client, world, shop, allowed):
    body = call(client, "/api/v1/operator/auth/login", {
        "username": "OPR001", "password": orders.fare_api.PASSWORD, "installation_id": "till-1"}).json()
    settings = body["data"]["device_settings"]
    assert (settings["customer_test_minutes"], settings["staff_test_minutes"], settings["cashier_test_minutes"]) == (
        5, 10, 10)
