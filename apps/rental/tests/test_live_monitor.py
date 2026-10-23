"""Rental -> Live Monitoring: the screen and its JSON, the push after every
order change, and the websocket that carries it."""

import datetime
import json
from decimal import Decimal
from unittest import mock

import pytest
from asgiref.sync import async_to_sync
from asgiref.testing import ApplicationCommunicator
from channels.layers import get_channel_layer
from django.utils import timezone

from apps.portal.page_registry import PAGES
from apps.portal.page_sync import sync
from apps.rental import live, requests, services
from apps.rental.models import Order, OrderAction, OrderEvent, OrderStatus, Payment, PaymentKind
from apps.rental.tests import test_order_screens as screens
from apps.rental.tests.conftest import make_customer
from apps.rental.tests.test_views import revoke
from core.ids import uuid7

SCREEN, ROWS = "/rental/live/", "/rental/live/rows/"
# The Orders screen's tablet, customer and bikes, reused as a fixture here.
shop, at, make_order, settle = screens.shop, screens.at, screens.make_order, screens.settle


def rows(client_in, **params):
    response = client_in.get(ROWS, params)
    assert response.status_code == 200, response.content
    return response.json()


def ids(payload):
    return [r["order_no"] for r in payload["rows"]]


# -- The screen and its JSON -------------------------------------------------------------


def test_the_page_is_registered_under_rental():
    assert ("rental.live", "rental", "Live Monitoring", "rental-live", ["read"], 0, False) in PAGES


def test_the_screen_needs_live_monitoring_read(client_in, world, shop):
    assert client_in.get(SCREEN).status_code == 200
    revoke(client_in, "read", page="rental.live")
    assert client_in.get(SCREEN).status_code == 403
    assert client_in.get(ROWS).status_code == 403


def test_signed_out_json_is_401(client, world):
    assert client.get(ROWS).status_code == 401


def test_only_this_companys_orders_today_and_still_running_ones(client_in, world, shop):
    make_order(world, shop, "TODAY-1")
    # A bike runs on one order at a time: the others get the second bike, or none out.
    make_order(world, shop, "YESTERDAY-RUNNING", day=-1, lines=("returned", "active"))
    settle(make_order(world, shop, "YESTERDAY-DONE", day=-1, lines=("returned",)), day=-1, world=world)
    Order.objects.create(
        id=uuid7(), company=world["other"], branch=world["theirs"], device=shop["their_tablet"],
        customer=make_customer({"company": world["other"]}, customer_code="CU9", mobile_no="509999999"),
        order_no="THEIRS-1", booked_at=at(world=world), start_time=at(world=world),
    )
    payload = rows(client_in)
    assert sorted(ids(payload)) == ["TODAY-1", "YESTERDAY-RUNNING"]
    assert payload["live"] is True
    # Another day is a look back: what was booked then, not live.
    past = rows(client_in, day=at(-1, world=world).date().isoformat())
    assert sorted(ids(past)) == ["YESTERDAY-DONE", "YESTERDAY-RUNNING"] and past["live"] is False


def test_filters_tab_status_station_emirate_and_vehicle_search(client_in, world, shop):
    make_order(world, shop, "RENT-1")
    make_order(world, shop, "DIRECT-1", is_direct_bill=True, lines=("returned", "active"))
    settle(make_order(world, shop, "DONE-1", lines=("returned",)), world=world)
    assert sorted(ids(rows(client_in))) == ["DONE-1", "RENT-1"]
    assert ids(rows(client_in, tab="direct")) == ["DIRECT-1"]
    assert ids(rows(client_in, status="active")) == ["RENT-1"]
    assert rows(client_in)["counts"] == {"rent": 2, "direct": 1}
    assert ids(rows(client_in, q="MO 41", status="active")) == ["RENT-1"]
    state = world["adc1"].location.state_id
    assert len(rows(client_in, state=state)["rows"]) == 2
    assert rows(client_in, state=state + 999)["rows"] == []
    assert rows(client_in, branch=world["adc1"].pk + 999)["rows"] == []


def test_a_row_carries_the_legacy_fields(client_in, world, shop, user):
    order = make_order(world, shop, "ADC-1", lines=("active", "replaced"), created_by=user)
    Payment.objects.create(id=uuid7(), order=order, kind=PaymentKind.ADVANCE, mode=shop["cash"],
                           amount=Decimal("30"), paid_at=timezone.now())
    Order.objects.filter(pk=order.pk).update(amount_received=Decimal("30"))
    [row] = rows(client_in)["rows"]
    assert row["station"] == "Abu Dhabi Corniche 1" and row["emirate"] == "Abu Dhabi"
    assert row["opened_by"] == "Sara K" and row["closed_by"] == "—"
    assert row["vehicles"] == [{"no": "MO 41", "state": "out"}, {"no": "DC 02", "state": "replaced"}]
    assert (row["out"], row["status"], row["status_label"], row["collected"]) == (1, "active", "Running", "30.00")
    assert row["url"] == f"/rental/order/{order.pk}/"         # a row opens the order's own page


def test_a_settled_row_names_who_closed_it_and_the_bill(client_in, world, shop, user):
    order = make_order(world, shop, "ADC-2", lines=("returned",))
    settle(order, world=world)
    OrderEvent.objects.create(id=uuid7(), order=order, action=OrderAction.SETTLE, request_hash="x", response={},
                              happened_at=timezone.now(), user=user)
    [row] = rows(client_in)["rows"]
    assert (row["status"], row["closed_by"], row["net"], row["tax"], row["last"]) == (
        "completed", "Sara K", "63.00", "3.00", "Settled")


def test_ids_returns_just_those_and_drops_what_no_longer_matches(client_in, world, shop):
    a, b = make_order(world, shop, "A-1"), make_order(world, shop, "B-1", lines=("returned", "active"))
    assert ids(rows(client_in, ids=f"{a.pk},not-a-uuid")) == ["A-1"]
    settle(b, world=world)
    assert rows(client_in, ids=str(b.pk), status="active")["rows"] == []


def test_another_companys_order_never_comes_back_by_id(client_in, world, shop):
    theirs = Order.objects.create(
        id=uuid7(), company=world["other"], branch=world["theirs"], device=shop["their_tablet"],
        customer=make_customer({"company": world["other"]}, customer_code="CU9", mobile_no="509999999"),
        order_no="THEIRS-1", booked_at=at(world=world), start_time=at(world=world),
    )
    assert rows(client_in, ids=str(theirs.pk))["rows"] == []


def test_kpis_count_the_day(client_in, world, shop):
    order = make_order(world, shop, "K-1", lines=("active", "active"))
    Payment.objects.create(id=uuid7(), order=order, kind=PaymentKind.ADVANCE, mode=shop["cash"],
                           amount=Decimal("40"), paid_at=timezone.now())
    cancelled = make_order(world, shop, "K-2", lines=())
    Order.objects.filter(pk=cancelled.pk).update(status=OrderStatus.CANCELLED, cancelled_at=timezone.now())
    assert rows(client_in)["kpis"] == {"vehicles": 2, "running": 1, "today": 2, "cancelled": 1, "collected": "40.00"}


# -- The push -----------------------------------------------------------------------------


@pytest.fixture
def pushed():
    with mock.patch.object(live, "publish") as publish:
        yield publish


def _apply(order, user):
    def apply():
        return order, {"action": OrderAction.RETURN, "happened_at": timezone.now(), "user": user}, {"ok": True}
    return apply


def test_a_tablet_change_publishes_once_and_a_resend_not_at_all(world, shop, user, pushed,
                                                               django_capture_on_commit_callbacks):
    order = make_order(world, shop, "P-1")
    event = uuid7()
    services.run_once(event_id=event, company=world["company"], request_data={"a": 1}, apply=_apply(order, user))
    pushed.assert_called_once_with(live.company_group(world["company"].pk),
                                   {"type": "order.changed", "order": str(order.pk)})
    pushed.reset_mock()
    services.run_once(event_id=event, company=world["company"], request_data={"a": 1}, apply=_apply(order, user))
    pushed.assert_not_called()


def test_a_web_cancel_publishes(world, shop, user, pushed):
    order = make_order(world, shop, "P-2")
    requests.cancel_directly(user, order.pk, [world["company"].pk], "Wrong bike")
    pushed.assert_called_once_with(live.company_group(world["company"].pk),
                                   {"type": "order.changed", "order": str(order.pk)})


def test_it_sends_only_after_commit_and_never_breaks_the_save(world, shop, user,
                                                             django_capture_on_commit_callbacks):
    order = make_order(world, shop, "P-3")
    layer = get_channel_layer()
    with mock.patch.object(layer, "group_send", side_effect=ConnectionError("redis down")) as send:
        with django_capture_on_commit_callbacks(execute=False) as callbacks:
            services.run_once(event_id=uuid7(), company=world["company"], request_data={"b": 1},
                              apply=_apply(order, user))
        send.assert_not_called()                     # not before the commit
        for callback in callbacks:
            callback()                               # redis down: logged, not raised
        send.assert_called_once()
    assert OrderEvent.objects.filter(order=order).count() == 1


def test_a_failed_change_publishes_nothing(world, shop, user, pushed):
    order = make_order(world, shop, "P-4")

    def apply():
        raise RuntimeError("refused")

    with pytest.raises(RuntimeError):
        services.run_once(event_id=uuid7(), company=world["company"], request_data={"c": 1}, apply=apply)
    pushed.assert_not_called()
    assert order.status == OrderStatus.ACTIVE


# -- The websocket ------------------------------------------------------------------------


class Socket(ApplicationCommunicator):
    """A browser on /ws/live/, through the whole ASGI stack (origin check,
    session, user). channels.testing's WebsocketCommunicator would do, but
    importing that package needs daphne, which nothing else here uses."""

    def __init__(self, cookie):
        from config.asgi import application

        headers = [(b"host", b"localhost"), (b"origin", b"http://localhost")]
        if cookie:
            headers.append((b"cookie", cookie.encode()))
        super().__init__(application, {
            "type": "websocket", "path": "/ws/live/", "query_string": b"", "headers": headers,
            "subprotocols": [], "client": ["127.0.0.1", 50000], "server": ["localhost", 80],
        })

    async def connect(self):
        await self.send_input({"type": "websocket.connect"})
        reply = await self.receive_output(2)
        return reply["type"] == "websocket.accept", reply.get("code")

    async def receive_json_from(self, timeout=2):
        return json.loads((await self.receive_output(timeout))["text"])

    async def disconnect(self):
        await self.send_input({"type": "websocket.disconnect", "code": 1000})
        await self.wait(1)


def _connect(cookie):
    return Socket(cookie)


def _cookie(client):
    return "; ".join(f"{k}={v.value}" for k, v in client.cookies.items())


@pytest.fixture
def pages(db):
    """The screens the migrations register. A transactional test runs on
    emptied tables -- any earlier one (the migration tests) flushes them, pages
    included -- so put them back before client_in grants its role everything."""
    sync()


@pytest.mark.django_db(transaction=True)
def test_the_socket_carries_this_companys_changes_only(pages, client_in, world):
    async def scenario():
        communicator = _connect(_cookie(client_in))
        connected, _ = await communicator.connect()
        assert connected
        layer = get_channel_layer()
        await layer.group_send(live.company_group(world["other"].pk), {"type": "order.changed", "order": "theirs"})
        await layer.group_send(live.company_group(world["company"].pk), {"type": "order.changed", "order": "ours"})
        assert await communicator.receive_json_from(timeout=2) == {"type": "order.changed", "order": "ours"}
        assert await communicator.receive_nothing(timeout=0.2)
        await communicator.disconnect()

    async_to_sync(scenario)()


@pytest.mark.django_db(transaction=True)
def test_the_socket_refuses_strangers_and_users_without_the_screen(pages, client, client_in, world):
    async def refused(cookie):
        communicator = _connect(cookie)
        connected, code = await communicator.connect()
        await communicator.disconnect()
        return connected, code

    assert async_to_sync(refused)("") == (False, 4403)                 # signed out
    revoke(client_in, "read", page="rental.live")
    assert async_to_sync(refused)(_cookie(client_in)) == (False, 4403)


def test_due_is_the_earliest_running_vehicle(client_in, world, shop):
    order = make_order(world, shop, "D-1")
    [row] = rows(client_in)["rows"]
    due = datetime.datetime.fromisoformat(row["due"])
    assert due == order.items.get().expected_end_time
