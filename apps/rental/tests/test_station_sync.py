"""Station sync (apps/rental/station_sync.py): an order change on one tablet is
pushed to the station's other operator tablets -- a visible notification with
the order's id -- and orders/changes catches up what was missed."""

import datetime

import pytest
from django.utils import timezone

from apps.notifications import sender as sender_module
from apps.notifications.sender import PushError
from apps.portal.session_models import AppSession
from apps.rental import services, station_sync
from apps.rental.models import Order, OrderStatus
from apps.rental.tests import test_order_api as orders
from apps.rental.tests import test_order_change as changes
from apps.rental.tests import test_order_requests as requests_tests
from apps.rental.tests.test_order_return import RETURN, back
from core.enums import Channel

call, booking, sign_in_tablet = orders.call, orders.booking, orders.sign_in_tablet
world, token, fresh_throttle, shop = orders.world, orders.token, orders.fresh_throttle, orders.shop
manager = requests_tests.manager

CHANGES = "/api/v1/operator/orders/changes"


class FakeSender:
    def __init__(self):
        self.sent, self.fail = [], None

    def send(self, push, token):
        if self.fail:
            raise self.fail
        self.sent.append((token, push.title, push.body, dict(push.data), push.android_channel, push.tag,
                          push.collapse_key))
        return "projects/x/messages/1"


@pytest.fixture
def fcm(monkeypatch):
    fake = FakeSender()
    monkeypatch.setattr(sender_module, "get_sender", lambda: fake)
    return fake


@pytest.fixture
def station(client, world, token, manager):
    """Till-1 (the acting tablet) and Mona's till-2 at Corniche 1, Omar's till-3
    at Corniche 2, Meera's manager phone -- every one with a push token."""
    mona = sign_in_tablet(client, world, code="OPR002", installation_id="till-2", branch=world["adc1"])
    omar = sign_in_tablet(client, world, code="OPR003", installation_id="till-3", branch=world["adc2"])
    for session in AppSession.objects.filter(logged_out_at__isnull=True).select_related("device"):
        session.push_token = f"tok-{session.device.installation_id}"
        session.save(update_fields=["push_token"])
    return {"mona": mona, "omar": omar}


def tokens(fcm):
    return sorted({sent[0] for sent in fcm.sent})


def booked(client, world, token, shop, django_capture_on_commit_callbacks):
    data = booking(world, shop)
    with django_capture_on_commit_callbacks(execute=True):
        assert call(client, orders.BOOK, data, token=token).json()["code"] == "ok"
    return data


def test_a_booking_reaches_the_other_tablet_at_the_station_only(client, world, token, shop, station, fcm,
                                                                 django_capture_on_commit_callbacks):
    data = booked(client, world, token, shop, django_capture_on_commit_callbacks)
    assert tokens(fcm) == ["tok-till-2"]               # not till-1 (acting), till-3, nor the manager
    token_, title, body, payload, channel, tag, collapse = fcm.sent[0]
    order = Order.objects.get(pk=data["sync_id"])
    assert title == f"Order {order.order_no} booked"
    assert body == "MO 41, DC 02 · Abu Dhabi Corniche 1"
    assert payload == {"type": "order_changed", "order_id": str(order.pk), "order_no": order.order_no,
                       "action": "book", "branch_id": str(world["adc1"].pk), "at": payload["at"]}
    assert (channel, tag, collapse) == ("station_activity", f"order:{order.pk}", str(order.pk))


def test_each_order_change_is_pushed(client, world, token, shop, station, fcm,
                                     django_capture_on_commit_callbacks):
    data = booked(client, world, token, shop, django_capture_on_commit_callbacks)
    steps = [
        (changes.ADD, changes.add(data, shop["mo42"], shop), "vehicle added", "MO 42"),
        (RETURN, back(data, 0), "vehicle returned", "MO 41"),
    ]
    for url, request_data, verb, vehicle in steps:
        fcm.sent.clear()
        with django_capture_on_commit_callbacks(execute=True):
            assert call(client, url, request_data, token=token).json()["code"] == "ok"
        assert [(s[0], s[1].endswith(verb), s[2].startswith(vehicle)) for s in fcm.sent] == [
            ("tok-till-2", True, True)]


def test_a_resend_and_a_request_push_nothing(client, world, token, shop, station, fcm,
                                             django_capture_on_commit_callbacks):
    data = booked(client, world, token, shop, django_capture_on_commit_callbacks)
    fcm.sent.clear()
    with django_capture_on_commit_callbacks(execute=True):
        assert call(client, orders.BOOK, data, token=token).json()["code"] == "duplicate"
        asked = requests_tests.ask(data, kind="discount")
        assert call(client, requests_tests.ASK, asked, token=token).json()["code"] == "ok"
    assert fcm.sent == []


def test_a_web_cancel_reaches_every_tablet_at_the_station(client, world, token, shop, station, fcm,
                                                          django_capture_on_commit_callbacks):
    from apps.rental import requests

    data = booked(client, world, token, shop, django_capture_on_commit_callbacks)
    fcm.sent.clear()
    user = AppSession.objects.filter(device__installation_id="till-1").first().user
    with django_capture_on_commit_callbacks(execute=True):
        requests.cancel_directly(user, data["sync_id"], [world["company"].pk], "Booked twice")
    assert tokens(fcm) == ["tok-till-1", "tok-till-2"]           # no acting tablet on the web
    assert fcm.sent[0][1].endswith("cancelled")


def test_a_signed_out_tablet_and_a_failing_send(client, world, token, shop, station, fcm,
                                                django_capture_on_commit_callbacks):
    AppSession.objects.filter(device__installation_id="till-2").update(push_token="")
    booked(client, world, token, shop, django_capture_on_commit_callbacks)
    assert fcm.sent == []
    AppSession.objects.filter(device__installation_id="till-2").update(push_token="tok-till-2")
    fcm.fail = PushError("unregistered")
    data = booking(world, shop, number=232)
    data["items"] = [{**data["items"][0], "vehicle_id": shop["mo42"].pk}]
    with django_capture_on_commit_callbacks(execute=True):
        assert call(client, orders.BOOK, data, token=token).json()["code"] == "ok"   # the order stands
    assert AppSession.objects.get(device__installation_id="till-2").push_token == ""  # the dead token is cleared


# -- orders/changes ----------------------------------------------------------------------


def test_first_call_is_running_orders_and_todays_changes(client, world, token, shop):
    data = booking(world, shop)
    assert call(client, orders.BOOK, data, token=token).json()["code"] == "ok"
    old = Order.objects.get(pk=data["sync_id"])
    long_ago = timezone.now() - datetime.timedelta(days=3)
    Order.objects.filter(pk=old.pk).update(created_on=long_ago, modified_on=long_ago)
    old.events.update(received_at=long_ago)
    settled = booking(world, shop, number=232)
    settled["items"] = [{**settled["items"][0], "vehicle_id": shop["mo42"].pk}]
    assert call(client, orders.BOOK, settled, token=token).json()["code"] == "ok"
    Order.objects.filter(pk=settled["sync_id"]).update(status=OrderStatus.COMPLETED)

    body = call(client, CHANGES, {}, token=token).json()
    assert body["code"] == "ok"
    ids = {o["sync_id"] for o in body["data"]["orders"]}
    assert ids == {str(old.pk), settled["sync_id"]}     # running from 3 days ago + today's
    assert body["data"]["synced_at"] and body["data"]["truncated"] is False


def test_since_returns_only_later_changes_and_never_more_than_a_day(world, shop, client, token):
    data = booking(world, shop)
    assert call(client, orders.BOOK, data, token=token).json()["code"] == "ok"
    branch, now = world["adc1"], timezone.now()
    later, _ = services.station_changes(branch, now + datetime.timedelta(minutes=5), now=now)
    assert later == []
    recent, _ = services.station_changes(branch, now - datetime.timedelta(seconds=10), now=now)
    assert [str(o.pk) for o in recent] == [data["sync_id"]]          # inside the 30 s overlap too
    Order.objects.filter(pk=data["sync_id"]).update(created_on=now - datetime.timedelta(hours=30),
                                                    modified_on=now - datetime.timedelta(hours=30))
    Order.objects.get(pk=data["sync_id"]).events.update(received_at=now - datetime.timedelta(hours=30))
    old_since, _ = services.station_changes(branch, now - datetime.timedelta(days=5), now=now)
    assert old_since == []                                           # capped at 24 h


def test_a_cancel_is_caught_up_and_other_stations_are_not(client, world, token, shop, station):
    data = booking(world, shop)
    assert call(client, orders.BOOK, data, token=token).json()["code"] == "ok"
    synced = call(client, CHANGES, {}, token=token).json()["data"]["synced_at"]
    long_ago = timezone.now() - datetime.timedelta(hours=1)
    order = Order.objects.get(pk=data["sync_id"])
    Order.objects.filter(pk=order.pk).update(created_on=long_ago, modified_on=long_ago)
    order.events.update(received_at=long_ago)
    assert call(client, CHANGES, {"since": synced}, token=token).json()["data"]["orders"] == []
    Order.objects.filter(pk=order.pk).update(status=OrderStatus.CANCELLED, modified_on=timezone.now())
    caught = call(client, CHANGES, {"since": synced}, token=token).json()["data"]["orders"]
    assert [o["sync_id"] for o in caught] == [data["sync_id"]]
    assert call(client, CHANGES, {}, token=station["omar"]).json()["data"]["orders"] == []   # Corniche 2


def test_changes_is_for_the_operator_app_only(client, world, token, manager):
    assert call(client, "/api/v1/manager/orders/changes", {}, token=manager).json()["code"] == "wrong_channel"
    assert call(client, CHANGES, {"since": "yesterday"}, token=token).json()["code"] == "invalid_request"


def test_verbs_cover_every_order_action():
    assert set(station_sync.VERBS) == {"book", "add", "replace", "remove", "return", "payment", "settle", "cancel"}
    assert Channel.OPERATOR == "operator"
