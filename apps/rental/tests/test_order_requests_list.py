"""POST /api/v1/operator/orders/requests/list: one station's requests, for the
operator app's Requests screen -- every tablet's at that station, newest
first, optionally only those waiting or one kind."""

from apps.rental import api
from apps.rental.tests import test_order_requests as rq

call, shape, sign_in_tablet = rq.call, rq.shape, rq.sign_in_tablet
world, token, fresh_throttle, shop, booked, manager = (
    rq.world, rq.token, rq.fresh_throttle, rq.shop, rq.booked, rq.manager)
asked, decide, APPROVE = rq.asked, rq.decide, rq.APPROVE
LIST = "/api/v1/operator/orders/requests/list"


def listed(client, token, **request_data):
    body = call(client, LIST, request_data, token=token).json()
    assert body["code"] == "ok", body
    return body["data"]


def test_the_station_lists_its_requests_newest_first(client, token, manager, booked):
    first = asked(client, token, booked, requested_at="2026-10-02 16:30:00")
    decide(client, manager, APPROVE, first["request_sync_id"], discount_type="percent", discount_value="5")

    data = listed(client, token)

    assert [r["request_sync_id"] for r in data["requests"]] == [first["request_sync_id"]]
    row = data["requests"][0]
    assert (row["status"], row["customer_name"], row["order_no"]) == ("approved", "Ahmed Al Mansoori",
                                                                      booked["order_no"])
    assert (data["page"], data["pages"], data["total"]) == (1, 1, 1)
    assert shape(row) == shape({**api._REQUEST_APPROVED_SAMPLE, "customer_name": ""})


def test_pending_and_kind_narrow_the_list(client, token, manager, booked):
    approved = asked(client, token, booked)
    decide(client, manager, APPROVE, approved["request_sync_id"], discount_type="percent", discount_value="5")
    decide(client, manager, rq.REVOKE, approved["request_sync_id"])      # frees the slot for a new one
    waiting = asked(client, token, booked)

    pending = listed(client, token, pending=True)["requests"]
    reprints = listed(client, token, kind="reprint")["requests"]

    assert [r["request_sync_id"] for r in pending] == [waiting["request_sync_id"]]
    assert reprints == []


def test_another_station_sees_none_of_them(client, world, token, booked):
    asked(client, token, booked)
    other = sign_in_tablet(client, world, code="OPR002", installation_id="till-2", branch=world["adc2"])

    assert listed(client, other)["requests"] == []


def test_another_tablet_at_the_station_sees_them(client, world, token, booked):
    asked(client, token, booked)
    other = sign_in_tablet(client, world, code="OPR002", installation_id="till-2", branch=world["adc1"])

    assert len(listed(client, other)["requests"]) == 1


def test_only_the_operator_app(client, manager):
    assert call(client, "/api/v1/manager/orders/requests/list", {}, token=manager).json()["code"] == "wrong_channel"


def test_the_list_is_in_the_api_docs(client, db):
    assert "/api/v1/{app}/orders/requests/list" in client.get("/api/schema/").content.decode()
