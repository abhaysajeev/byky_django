"""Cart holds (apps/rental/holds.py, cart_api.py): a vehicle in one tablet's
cart at a multi-device station is held 10 minutes; no other tablet there can
cart or book it. Owner, 18 Nov 2026."""

import datetime

import pytest
from django.utils import timezone

from apps.devices.models import Device, DeviceMapping
from apps.notifications import sender as sender_module
from apps.portal.session_models import AppSession
from apps.rental import holds
from apps.rental.models import HoldRelease, OrderItem, VehicleHold
from apps.rental.tests import test_order_api as orders
from apps.rental.tests import test_order_change as changes
from apps.sync import services as sync_services
from apps.sync.models import SyncIssue
from apps.sync.tests.test_parking import office, send  # noqa: F401 -- office is a fixture
from core.ids import uuid7

call, booking, sign_in_tablet, receipt = orders.call, orders.booking, orders.sign_in_tablet, orders.receipt
world, token, fresh_throttle, shop = orders.world, orders.token, orders.fresh_throttle, orders.shop

ADD, REMOVE, CLEAR, STATUS = ("/api/v1/operator/cart/add", "/api/v1/operator/cart/remove",
                              "/api/v1/operator/cart/clear", "/api/v1/operator/cart/status")
VEHICLES = "/api/v1/operator/vehicles"


@pytest.fixture
def other(client, world):
    """Mona's till-2 at Corniche 1 -- which makes Corniche 1 multi-device -- letter B."""
    access = sign_in_tablet(client, world, code="OPR002", installation_id="till-2", branch=world["adc1"])
    DeviceMapping.objects.filter(device__installation_id="till-2").update(transaction_letter="B")
    return access


def cart(client, token, vehicle, sync_id=None):
    return call(client, ADD, {"sync_id": sync_id or str(uuid7()), "vehicle_id": vehicle.pk,
                              "added_at": "2026-11-18 14:05:10"}, token=token)


def open_holds():
    return VehicleHold.objects.filter(released_at__isnull=True)


# -- add -----------------------------------------------------------------------------------


def test_a_vehicle_goes_in_the_cart_for_ten_minutes(client, world, token, shop, other):
    before = timezone.now()
    reply = cart(client, token, shop["mo41"])
    body = reply.json()
    assert (reply.status_code, body["code"]) == (200, "ok")
    hold = body["data"]["hold"]
    assert (hold["vehicle_id"], hold["vehicle_name"]) == (shop["mo41"].pk, "MO 41")
    assert body["data"]["cart"] == [hold]
    row = VehicleHold.objects.get()
    assert row.expires_at - row.added_at == holds.TTL and row.added_at >= before
    assert row.device_added_at is not None and row.device.installation_id == "till-1"


def test_a_resend_or_the_same_vehicle_again_is_a_duplicate(client, world, token, shop, other):
    first = cart(client, token, shop["mo41"]).json()["data"]["hold"]
    again = cart(client, token, shop["mo41"], sync_id=first["sync_id"]).json()
    same_vehicle = cart(client, token, shop["mo41"]).json()
    assert again["code"] == same_vehicle["code"] == "duplicate"
    assert again["data"]["hold"]["sync_id"] == same_vehicle["data"]["hold"]["sync_id"] == first["sync_id"]
    assert VehicleHold.objects.count() == 1


def test_a_sync_id_of_another_vehicle_is_a_conflict(client, world, token, shop, other):
    first = cart(client, token, shop["mo41"]).json()["data"]["hold"]
    reply = cart(client, token, shop["dc02"], sync_id=first["sync_id"])
    assert (reply.status_code, reply.json()["code"]) == (409, "sync_id_conflict")


def test_another_tablet_cannot_cart_a_held_vehicle(client, world, token, shop, other):
    cart(client, token, shop["mo41"])
    reply = cart(client, other, shop["mo41"])
    body = reply.json()
    assert (reply.status_code, body["code"]) == (409, "vehicle_in_other_cart")
    assert body["data"]["vehicle_id"] == shop["mo41"].pk
    assert body["data"]["held_by"]["username"] == "opr001"
    assert set(body["data"]) == {"vehicle_id", "held_by", "added_at", "expires_at"}
    # ...and the other way round shows till-2's letter.
    cart(client, other, shop["dc02"])
    seen = cart(client, token, shop["dc02"]).json()["data"]["held_by"]
    assert (seen["device_letter"], seen["username"]) == ("B", "opr002")


def test_an_expired_hold_frees_the_vehicle(client, world, token, shop, other):
    cart(client, token, shop["mo41"])
    VehicleHold.objects.update(expires_at=timezone.now() - datetime.timedelta(seconds=1))
    assert cart(client, other, shop["mo41"]).json()["code"] == "ok"
    old = VehicleHold.objects.get(device__installation_id="till-1")
    assert old.release_reason == HoldRelease.EXPIRED and old.released_at is not None


def test_any_cart_call_restarts_the_ten_minutes(client, world, token, shop, other):
    cart(client, token, shop["mo41"])
    soon = timezone.now() + datetime.timedelta(minutes=1)
    VehicleHold.objects.update(expires_at=soon)
    call(client, STATUS, {}, token=token)
    assert VehicleHold.objects.get().expires_at > soon + datetime.timedelta(minutes=8)


@pytest.mark.parametrize("how", ["logout", "force", "expired_session"])
def test_signing_out_frees_the_tablets_holds(client, world, token, shop, other, how):
    from apps.portal.auth import close_sessions_for_device
    from apps.portal.session_models import LogoutReason

    cart(client, token, shop["mo41"])
    if how == "logout":
        assert call(client, "/api/v1/operator/auth/logout", {}, token=token).status_code == 200
    elif how == "force":
        close_sessions_for_device(Device.objects.get(installation_id="till-1"), LogoutReason.FORCED)
    else:
        AppSession.objects.filter(device__installation_id="till-1").update(
            refresh_expires_at=timezone.now() - datetime.timedelta(seconds=1))
    assert cart(client, other, shop["mo41"]).json()["code"] == "ok"
    assert VehicleHold.objects.get(device__installation_id="till-1").release_reason == HoldRelease.LOGOUT


def test_a_single_device_station_has_no_cart(client, world, token, shop):
    for url, data in ((ADD, {"sync_id": str(uuid7()), "vehicle_id": shop["mo41"].pk}), (REMOVE, {"vehicle_id": 1}),
                      (CLEAR, {}), (STATUS, {})):
        reply = call(client, url, data, token=token)
        assert (reply.status_code, reply.json()["code"]) == (409, "single_device_branch"), url
    assert not VehicleHold.objects.exists()


def test_a_vehicle_that_cannot_be_rented_cannot_be_carted(client, world, token, shop, other):
    assert call(client, orders.BOOK, booking(world, shop), token=token).json()["code"] == "ok"   # MO 41, DC 02 out
    cases = [(shop["mo41"], 409, "vehicle_already_rented"), (shop["away"], 400, "vehicle_not_at_station")]
    for vehicle, status, code in cases:
        reply = cart(client, other, vehicle)
        assert (reply.status_code, reply.json()["code"]) == (status, code)
    unknown = call(client, ADD, {"sync_id": str(uuid7()), "vehicle_id": 999999}, token=other)
    assert (unknown.status_code, unknown.json()["code"]) == (404, "unknown_vehicle")
    assert not VehicleHold.objects.exists()


def test_the_request_is_checked(client, world, token, shop, other):
    reply = call(client, ADD, {"sync_id": "nope", "vehicle_id": shop["mo41"].pk}, token=token)
    assert (reply.status_code, reply.json()["code"]) == (400, "invalid_request")
    both = call(client, REMOVE, {"sync_id": str(uuid7()), "vehicle_id": 1}, token=token)
    assert (both.status_code, both.json()["code"]) == (400, "invalid_request")


def test_the_cart_is_for_the_operator_app_only(client, world, token, shop, other):
    reply = client.post("/api/v1/manager/cart/status", {"credentials": {}, "request_data": {}},
                        content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {token}")
    assert reply.json()["code"] == "wrong_channel"


# -- remove, clear, status -----------------------------------------------------------------


def test_remove_by_vehicle_or_sync_id_is_safe_to_resend(client, world, token, shop, other):
    first = cart(client, token, shop["mo41"]).json()["data"]["hold"]
    cart(client, token, shop["dc02"])
    gone = call(client, REMOVE, {"vehicle_id": shop["dc02"].pk}, token=token).json()
    assert (gone["data"]["removed"], [h["vehicle_id"] for h in gone["data"]["cart"]]) == (True, [shop["mo41"].pk])
    assert call(client, REMOVE, {"vehicle_id": shop["dc02"].pk}, token=token).json()["data"]["removed"] is False
    assert call(client, REMOVE, {"sync_id": first["sync_id"]}, token=token).json()["data"] == {
        "removed": True, "cart": []}
    assert set(VehicleHold.objects.values_list("release_reason", flat=True)) == {HoldRelease.REMOVED}


def test_another_tablets_hold_cannot_be_removed(client, world, token, shop, other):
    cart(client, token, shop["mo41"])
    assert call(client, REMOVE, {"vehicle_id": shop["mo41"].pk}, token=other).json()["data"]["removed"] is False
    assert open_holds().count() == 1


def test_clear_empties_only_this_tablets_cart(client, world, token, shop, other):
    cart(client, token, shop["mo41"])
    cart(client, token, shop["dc02"])
    cart(client, other, shop["mo42"])
    assert call(client, CLEAR, {}, token=token).json()["data"] == {"removed": 2, "cart": []}
    assert list(open_holds().values_list("vehicle_id", flat=True)) == [shop["mo42"].pk]


def test_status_shows_my_cart_and_the_stations_other_holds(client, world, token, shop, other):
    cart(client, token, shop["mo41"])
    cart(client, other, shop["mo42"])
    data = call(client, STATUS, {}, token=token).json()["data"]
    assert data["ttl_minutes"] == 10 and data["server_time"]
    assert [h["vehicle_id"] for h in data["my_cart"]] == [shop["mo41"].pk]
    [theirs] = data["station_holds"]
    assert (theirs["vehicle_id"], theirs["held_by"]["device_letter"], theirs["held_by"]["username"]) == (
        shop["mo42"].pk, "B", "opr002")


# -- booking -------------------------------------------------------------------------------


def till2_booking(world, shop, vehicle, number=501):
    data = booking(world, shop, number=number)
    data["order_no"] = receipt(Device.objects.get(installation_id="till-2"), world["adc1"], number)
    data["items"] = [{**data["items"][0], "vehicle_id": vehicle.pk}]
    return data


def test_booking_a_vehicle_in_another_tablets_cart_is_refused(client, world, token, shop, other):
    cart(client, other, shop["dc02"])
    reply = call(client, orders.BOOK, booking(world, shop), token=token)   # MO 41 and DC 02
    body = reply.json()
    assert (reply.status_code, body["code"]) == (409, "vehicle_in_other_cart")
    assert body["data"]["retry"] is False and body["data"]["vehicle_id"] == shop["dc02"].pk
    assert body["data"]["held_by"]["device_letter"] == "B"
    assert not OrderItem.objects.exists()


def test_booking_releases_this_tablets_own_holds(client, world, token, shop, other):
    cart(client, other, shop["mo42"])
    cart(client, other, shop["dc02"])
    assert call(client, orders.BOOK, till2_booking(world, shop, shop["mo42"]), token=other).json()["code"] == "ok"
    reasons = dict(VehicleHold.objects.values_list("vehicle_id", "release_reason"))
    assert reasons == {shop["mo42"].pk: HoldRelease.BOOKED, shop["dc02"].pk: ""}     # DC 02 still in the cart


def test_add_and_replace_are_refused_for_another_tablets_hold(client, world, token, shop, other):
    booked = booking(world, shop)
    assert call(client, orders.BOOK, booked, token=token).json()["code"] == "ok"
    cart(client, other, shop["mo42"])
    for url, data in ((changes.ADD, changes.add(booked, shop["mo42"], shop)),
                      (changes.REPLACE, changes.replace(booked, shop["mo42"], shop))):
        reply = call(client, url, data, token=token)
        assert (reply.status_code, reply.json()["code"]) == (409, "vehicle_in_other_cart"), url
    # This tablet's own hold is no obstacle -- and is released by the add.
    VehicleHold.objects.all().delete()
    cart(client, token, shop["mo42"])
    assert call(client, changes.ADD, changes.add(booked, shop["mo42"], shop), token=token).json()["code"] == "ok"
    assert VehicleHold.objects.get().release_reason == HoldRelease.BOOKED


def test_a_parked_booking_replayed_is_never_refused_for_a_hold(client, world, token, shop, other, office):  # noqa: F811
    cart(client, other, shop["dc02"])
    parked = send(client, orders.BOOK, booking(world, shop), token=token, park=True).json()
    assert parked["code"] == "parked" and parked["data"]["error"]["code"] == "vehicle_in_other_cart"

    outcome = sync_services.replay(SyncIssue.objects.all(), parked["data"]["issue_id"], user=office)
    assert outcome["stopped_at"] is None
    assert OrderItem.objects.count() == 2
    assert VehicleHold.objects.get().release_reason == HoldRelease.BOOKED


# -- vehicles list, push, sweep ------------------------------------------------------------


def vehicle_row(client, token, vehicle):
    data = call(client, VEHICLES, {}, token=token).json()["data"]
    rows = [v for c in data["categories"] for t in c["vehicle_types"] for v in t["vehicles"]]
    return next(v for v in rows if v["vehicle_id"] == vehicle.pk)


def test_the_vehicles_list_shows_the_hold(client, world, token, shop, other):
    assert vehicle_row(client, token, shop["mo41"])["cart_hold"] is None
    cart(client, token, shop["mo41"])
    mine, theirs = vehicle_row(client, token, shop["mo41"]), vehicle_row(client, other, shop["mo41"])
    assert (mine["cart_hold"]["mine"], mine["can_rent"]) == (True, True)
    assert (theirs["cart_hold"]["mine"], theirs["can_rent"]) == (False, False)
    assert theirs["cart_hold"]["username"] == "opr001"


class FakeSender:
    def __init__(self):
        self.sent = []

    def send(self, push, token):
        self.sent.append((token, push))
        return "projects/x/messages/1"


def test_the_other_tablets_get_a_silent_push(client, world, token, shop, other, monkeypatch,
                                            django_capture_on_commit_callbacks):
    fake = FakeSender()
    monkeypatch.setattr(sender_module, "get_sender", lambda: fake)
    AppSession.objects.filter(logged_out_at__isnull=True).update(push_token="tok")
    AppSession.objects.filter(device__installation_id="till-2").update(push_token="tok-till-2")

    with django_capture_on_commit_callbacks(execute=True):
        cart(client, token, shop["mo41"])
    with django_capture_on_commit_callbacks(execute=True):
        call(client, REMOVE, {"vehicle_id": shop["mo41"].pk}, token=token)

    assert [t for t, _ in fake.sent] == ["tok-till-2", "tok-till-2"]          # never the acting tablet
    pushes = [p for _, p in fake.sent]
    assert all(p.silent for p in pushes)
    assert [(p.data["type"], p.data["action"], p.data["vehicle_id"]) for p in pushes] == [
        ("cart_changed", "added", str(shop["mo41"].pk)), ("cart_changed", "removed", str(shop["mo41"].pk))]


def test_a_silent_push_carries_no_notification():
    from types import SimpleNamespace

    pytest.importorskip("firebase_admin")
    from apps.notifications.sender import FcmSender

    push = SimpleNamespace(title="", body="", data={"type": "cart_changed"}, tag="t", collapse_key="c",
                           android_channel="station_activity", ttl_seconds=600, silent=True)
    message = FcmSender.message(object.__new__(FcmSender), push, "tok")
    assert message.notification is None and message.android.notification is None
    assert message.data == {"type": "cart_changed"}


def test_the_sweep_releases_expired_and_signed_out_holds(client, world, token, shop, other):
    cart(client, token, shop["mo41"])
    cart(client, other, shop["mo42"])
    cart(client, other, shop["dc02"])
    VehicleHold.objects.filter(vehicle=shop["mo42"]).update(expires_at=timezone.now() - datetime.timedelta(seconds=1))
    AppSession.objects.filter(device__installation_id="till-1").update(logged_out_at=timezone.now())

    assert holds.release_stale() == {HoldRelease.LOGOUT: 1, HoldRelease.EXPIRED: 1}
    assert list(open_holds().values_list("vehicle_id", flat=True)) == [shop["dc02"].pk]
    assert holds.release_stale() == {}


def test_the_cart_endpoints_are_in_the_api_docs(client, db):
    schema = client.get("/api/schema/").content.decode()
    for path in ("cart/add", "cart/remove", "cart/clear", "cart/status"):
        assert path in schema
