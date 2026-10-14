"""Two reprints (owner, 14 Oct 2026): `order_reprint` -- the order receipt
printed at booking, on a running or settled order -- beside `reprint`, the
invoice of a settled order. Same call and fields, approve / reject, printed
once then `used`. And invoices/detail, for printing the invoice again."""

import pytest

from apps.rental.models import OrderRequest, OrderRequestStatus, OrderStatus
from apps.rental.tests import test_order_api as orders
from apps.rental.tests import test_order_cancel as cancels
from apps.rental.tests import test_order_requests as rq
from apps.rental.tests import test_order_settle as settles
from core.ids import uuid7

call, booking = orders.call, orders.booking
world, token, fresh_throttle, shop = orders.world, orders.token, orders.fresh_throttle, orders.shop
booked, manager = rq.booked, rq.manager
ASK, APPROVE, REJECT, REVOKE = rq.ASK, rq.APPROVE, rq.REJECT, rq.REVOKE
USED = "/api/v1/operator/orders/requests/used"
DETAIL = "/api/v1/operator/invoices/detail"
OPERATOR_LIST, MANAGER_LIST = "/api/v1/operator/orders/requests/list", "/api/v1/manager/requests/list"
COUNTS = "/api/v1/manager/requests/pending-counts"


def ask(client, token, booked, kind):
    return call(client, ASK, {"sync_id": str(uuid7()), "order_id": booked["sync_id"], "kind": kind,
                              "requested_at": "2026-10-02 16:10:00"}, token=token).json()


def used(client, token, request_id):
    return call(client, USED, {"sync_id": str(uuid7()), "request_sync_id": request_id,
                               "used_at": "2026-10-02 16:20:00"}, token=token).json()


def settle(client, token, shop, booked):
    rq.return_all(client, token, booked)
    assert settles.settle(client, token, settles.bill(booked, shop))["code"] == "ok"


# -- order_reprint ---------------------------------------------------------------------

def test_an_order_receipt_reprint_on_a_running_order_printed_once(client, token, manager, booked):
    body = ask(client, token, booked, "order_reprint")
    assert (body["code"], body["data"]["kind"], body["data"]["status"]) == ("ok", "order_reprint", "pending")
    request_id = body["data"]["request_sync_id"]

    assert used(client, token, request_id)["code"] == "request_not_approved"
    assert rq.decide(client, manager, APPROVE, request_id)["data"]["status"] == "approved"
    assert used(client, token, request_id)["data"]["status"] == "used"
    assert used(client, token, request_id)["code"] == "request_used"
    assert rq.decide(client, manager, REVOKE, request_id)["code"] == "request_not_revocable"
    # Spent: another needs a new request.
    assert ask(client, token, booked, "order_reprint")["code"] == "ok"


def test_an_order_receipt_reprint_after_the_order_settled(client, token, shop, manager, booked):
    settle(client, token, shop, booked)

    request_id = ask(client, token, booked, "order_reprint")["data"]["request_sync_id"]

    assert rq.decide(client, manager, APPROVE, request_id)["data"]["status"] == "approved"


def test_not_on_a_cancelled_order(client, token, manager, booked):
    rq.decide(client, manager, APPROVE, cancels.asked_cancel(client, token, booked))

    assert ask(client, token, booked, "order_reprint")["code"] == "order_closed"


def test_one_live_per_kind_and_both_kinds_at_once(client, token, shop, booked):
    settle(client, token, shop, booked)
    assert ask(client, token, booked, "order_reprint")["code"] == "ok"
    assert ask(client, token, booked, "order_reprint")["code"] == "order_reprint_request_pending"

    assert ask(client, token, booked, "reprint")["code"] == "ok"              # the invoice, beside it


def test_the_invoice_reprint_is_unchanged(client, token, booked):
    assert ask(client, token, booked, "reprint")["code"] == "order_not_settled"


def test_a_rejected_order_reprint_cannot_be_printed(client, token, manager, booked):
    request_id = ask(client, token, booked, "order_reprint")["data"]["request_sync_id"]
    rq.decide(client, manager, REJECT, request_id, note="Not needed")

    assert used(client, token, request_id)["code"] == "request_not_approved"
    assert OrderRequest.objects.get(pk=request_id).status == OrderRequestStatus.REJECTED


# -- Lists ----------------------------------------------------------------------------

def test_an_invoice_reprint_row_carries_its_invoice(client, token, shop, manager, booked):
    settle(client, token, shop, booked)
    invoice_reprint = ask(client, token, booked, "reprint")["data"]["request_sync_id"]
    receipt_reprint = ask(client, token, booked, "order_reprint")["data"]["request_sync_id"]

    for url, who in ((OPERATOR_LIST, token), (MANAGER_LIST, manager)):
        rows = {r["request_sync_id"]: r for r in call(client, url, {}, token=who).json()["data"]["requests"]}
        assert rows[invoice_reprint]["details"] == {"invoice_no": booked["order_no"],
                                                     "invoice_net_amount": "110.000"}, url
        assert rows[receipt_reprint]["details"] is None, url

    counts = call(client, COUNTS, token=manager).json()["data"]
    assert (counts["reprint"], counts["order_reprint"]) == (1, 1)


# -- invoices/detail --------------------------------------------------------------------

def test_one_invoice_by_its_order_or_its_number(client, token, shop, booked):
    settle(client, token, shop, booked)

    by_order = call(client, DETAIL, {"order_id": booked["sync_id"]}, token=token).json()
    by_number = call(client, DETAIL, {"invoice_no": booked["order_no"]}, token=token).json()

    assert by_order["code"] == "ok", by_order
    assert by_order["data"] == by_number["data"]
    assert (by_order["data"]["invoice_no"], by_order["data"]["net_amount"]) == (booked["order_no"], "110.000")
    assert len(by_order["data"]["items"]) == 2


def test_no_invoice_until_settled_and_not_another_stations(client, world, token, shop, booked):
    running = call(client, DETAIL, {"order_id": booked["sync_id"]}, token=token)
    assert (running.status_code, running.json()["code"]) == (404, "unknown_invoice")

    settle(client, token, shop, booked)
    from apps.rental.models import Invoice
    Invoice.objects.update(branch=world["adc2"])
    elsewhere = call(client, DETAIL, {"order_id": booked["sync_id"]}, token=token)
    assert (elsewhere.status_code, elsewhere.json()["code"]) == (404, "unknown_invoice")


@pytest.mark.parametrize("request_data", [{}, {"order_id": "01a1013d-fa97-7179-93ae-3ae25842f1ce",
                                               "invoice_no": "X1"}])
def test_exactly_one_of_order_id_or_invoice_no(client, token, request_data):
    response = call(client, DETAIL, request_data, token=token)

    assert (response.status_code, response.json()["code"]) == (400, "invalid_request")


def test_order_status_untouched_by_a_reprint(client, token, manager, booked):
    request_id = ask(client, token, booked, "order_reprint")["data"]["request_sync_id"]
    rq.decide(client, manager, APPROVE, request_id)
    used(client, token, request_id)

    from apps.rental.models import Order
    assert Order.objects.get(pk=booked["sync_id"]).status == OrderStatus.ACTIVE
