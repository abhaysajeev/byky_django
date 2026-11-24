"""log.order_nos on the copy sent to the log server (owner, 24 Nov 2026): the
orders a device call was about, so the log viewer's "Order no" filter finds
every call of one order -- also the ones that name it only by its id."""

import json

import pytest

from apps.monitoring import shipper
from apps.monitoring.tests.test_shipper import fresh_shipper, inline, log_server  # noqa: F401
from apps.rental.models import Order
from apps.rental.tests import test_order_api as orders
from apps.rental.tests.test_order_return import RETURN, back

call, booking = orders.call, orders.booking
world, token, fresh_throttle, shop = orders.world, orders.token, orders.fresh_throttle, orders.shop

ORDER_ID = "01a1c3e4-6b25-7e61-8d41-797c1541107b"


def fields(request=None, response=None, url_name="", **flags):
    return {"request_body": json.dumps(request) if isinstance(request, dict) else (request or ""),
            "response_body": json.dumps(response) if isinstance(response, dict) else (response or ""),
            "url_name": url_name, "request_truncated": flags.get("request_truncated", False),
            "response_truncated": flags.get("response_truncated", False)}


def sent_for(log_server, path):  # noqa: F811
    return [d["log"] for batch in log_server for d in batch["logs"] if d["log"]["path"] == path]


# -- Finding the orders a call names -------------------------------------------------------------


def test_order_numbers_anywhere_in_the_reply():
    numbers, ids = shipper.order_refs(fields(
        response={"data": {"orders": [{"order_no": "AUH1"}, {"order_no": "AUH2", "items": []}]}}))
    assert (numbers, ids) == ({"AUH1", "AUH2"}, set())


def test_an_order_named_only_by_its_id():
    numbers, ids = shipper.order_refs(fields(request={"request_data": {"order_id": ORDER_ID.upper()}}))
    assert (numbers, ids) == (set(), {ORDER_ID})


def test_the_request_sync_id_is_the_order_only_on_create_and_detail():
    body = {"request_data": {"sync_id": ORDER_ID}}
    assert shipper.order_refs(fields(request=body, url_name="api-app-order-create"))[1] == {ORDER_ID}
    assert shipper.order_refs(fields(request=body, url_name="api-app-order-detail"))[1] == {ORDER_ID}
    # A return's own sync_id is the event's, not the order's.
    assert shipper.order_refs(fields(request=body, url_name="api-app-order-return"))[1] == set()


def test_a_body_cut_short_is_searched_as_text():
    cut = '{"data": {"orders": [{"order_no": "AUH1", "x": 1}, {"order_id": "' + ORDER_ID + '", "order_no": "AU'
    numbers, ids = shipper.order_refs(fields(response=cut, response_truncated=True))
    assert (numbers, ids) == ({"AUH1"}, {ORDER_ID})


def test_a_call_about_no_order_names_none():
    assert shipper.order_refs(fields(request={"request_data": {"phone": "0501234567"}},
                                     response={"code": "ok", "data": {"customer_id": 4}})) == (set(), set())


def test_at_most_fifty_and_sorted():
    ref = ({f"N{n:03d}" for n in range(80)}, set())
    got = shipper.order_nos(ref, {})
    assert len(got) == shipper.MAX_ORDER_NOS and got == sorted(got) and got[0] == "N000"


def test_ids_become_numbers_and_unknown_ids_are_left_out():
    assert shipper.order_nos(({"A"}, {ORDER_ID, "00000000-0000-7000-8000-000000000000"}), {ORDER_ID: "B"}) == ["A", "B"]


def test_a_failed_lookup_still_ships_the_direct_numbers(monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("database gone")
    monkeypatch.setattr(Order.objects, "filter", broken)
    refs = [({"A"}, {ORDER_ID})]
    assert shipper._order_numbers(refs) == {}
    assert shipper.order_nos(refs[0], {}) == ["A"]


# -- End to end: the copies of one order's calls ------------------------------------------------


def test_every_call_of_an_order_carries_its_number(client, world, token, shop, log_server, inline):  # noqa: F811
    request_data = booking(world, shop)
    assert call(client, orders.BOOK, request_data, token=token).json()["code"] == "ok"
    order = Order.objects.get(pk=request_data["sync_id"])
    assert call(client, RETURN, back(request_data), token=token).json()["code"] == "ok"

    [booked] = sent_for(log_server, orders.BOOK)
    [returned] = sent_for(log_server, RETURN)
    assert booked["order_nos"] == [order.order_no]
    assert returned["order_nos"] == [order.order_no]       # the return sent only order_id


def test_a_call_about_no_order_ships_an_empty_list(client, world, token, log_server, inline):  # noqa: F811
    [doc] = [d for d in _login_docs(client, log_server)]
    assert doc["order_nos"] == []


def _login_docs(client, log_server):  # noqa: F811
    from apps.monitoring.tests import test_monitoring as monitoring

    monitoring.call(client, "/api/v1/operator/vehicles", {}, token=None)
    return [d["log"] for batch in log_server for d in batch["logs"]]


@pytest.fixture(autouse=True)
def _shipper_state(fresh_shipper):  # noqa: F811
    yield
