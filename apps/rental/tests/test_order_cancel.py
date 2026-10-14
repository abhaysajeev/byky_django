"""Cancelling a running order (SRS 8.4, owner 14 Oct 2026): the operator asks
with a reason, a manager approves (app or web), and the order and its vehicles
still out are cancelled at once. Or the back office cancels it straight on the
web. Never a settled order."""

import json

import pytest
from django.test import Client

from apps.discount.models import ClaimStatus
from apps.discount.tests.conftest import make_claim
from apps.portal.models import Role, RolePermission
from apps.portal.services import grant_all
from apps.rental.models import (
    DecisionChannel,
    Order,
    OrderItem,
    OrderItemStatus,
    OrderRequest,
    OrderRequestKind,
    OrderRequestStatus,
    OrderStatus,
    Payment,
)
from apps.rental.tests import test_order_api as orders
from apps.rental.tests import test_order_requests as rq
from apps.rental.tests import test_order_settle as settles
from apps.rental.tests.test_order_return import RETURN, back
from core.enums import Channel
from core.ids import uuid7
from core.models import User

call, booking = orders.call, orders.booking
world, token, fresh_throttle, shop = orders.world, orders.token, orders.fresh_throttle, orders.shop
booked, manager, card = rq.booked, rq.manager, rq.card
ASK, WITHDRAW, LIST = rq.ASK, rq.WITHDRAW, "/api/v1/operator/orders/requests/list"
APPROVE, REJECT, REVOKE = rq.APPROVE, rq.REJECT, rq.REVOKE
COUNTS = "/api/v1/manager/requests/pending-counts"
PAYMENTS = "/api/v1/operator/orders/payments"


def ask_cancel(client, token, booked, **overrides):
    data = {"sync_id": str(uuid7()), "order_id": booked["sync_id"], "kind": "cancel",
            "reason": "Booked MO 41 by mistake", "requested_at": "2026-10-02 16:05:00", **overrides}
    return call(client, ASK, data, token=token).json()


def asked_cancel(client, token, booked):
    body = ask_cancel(client, token, booked)
    assert body["code"] == "ok", body
    return body["data"]["request_sync_id"]


def order_of(booked):
    return Order.objects.get(pk=booked["sync_id"])


# -- Asking ---------------------------------------------------------------------------

def test_a_cancel_needs_a_reason(client, token, booked):
    body = ask_cancel(client, token, booked, reason="  ")

    assert (body["code"], body["data"]["errors"]) == ("invalid_request", {"reason": "is required for a cancel"})


def test_only_one_live_cancel_and_it_can_be_withdrawn(client, token, booked):
    first = asked_cancel(client, token, booked)
    assert ask_cancel(client, token, booked)["code"] == "cancel_request_pending"

    withdrawn = call(client, WITHDRAW, {"sync_id": str(uuid7()), "request_sync_id": first,
                                        "withdrawn_at": "2026-10-02 16:06:00"}, token=token).json()

    assert withdrawn["data"]["status"] == "withdrawn"
    assert ask_cancel(client, token, booked)["code"] == "ok"


def test_a_settled_order_cannot_be_cancelled(client, token, shop, booked):
    rq.return_all(client, token, booked)
    assert settles.settle(client, token, settles.bill(booked, shop))["code"] == "ok"

    assert ask_cancel(client, token, booked)["code"] == "order_closed"


def test_a_cancel_sits_beside_a_discount_request(client, token, booked):
    rq.asked(client, token, booked)                          # a discount waiting

    assert ask_cancel(client, token, booked)["code"] == "ok"


# -- While it waits -------------------------------------------------------------------

def test_returns_go_on_but_settle_waits_for_the_decision(client, token, shop, manager, booked):
    request_id = asked_cancel(client, token, booked)
    rq.return_all(client, token, booked)                     # returns still allowed

    refused = settles.settle(client, token, settles.bill(booked, shop))
    assert refused["code"] == "cancel_requested"

    rq.decide(client, manager, REJECT, request_id, note="Ride it out")
    assert settles.settle(client, token, settles.bill(booked, shop))["code"] == "ok"


# -- Approved --------------------------------------------------------------------------

def test_approval_cancels_the_order_and_frees_its_vehicles(client, world, token, shop, manager, booked, card):
    late = back(booked, 1)                                   # DC 02 already back: its line stays returned
    assert call(client, RETURN, late, token=token).json()["code"] == "ok"
    discount = rq.asked(client, token, booked)["request_sync_id"]
    claim = make_claim(card, shop["ahmed"], order=order_of(booked), branch=order_of(booked).branch)
    request_id = asked_cancel(client, token, booked)

    body = rq.decide(client, manager, APPROVE, request_id, note="Operator mistake")

    assert (body["code"], body["data"]["status"], body["data"]["kind"]) == ("ok", "approved", "cancel")
    order = order_of(booked)
    assert order.status == OrderStatus.CANCELLED and order.cancelled_at is not None
    lines = {str(i.pk): i.status for i in OrderItem.objects.filter(order=order)}
    assert lines[booked["items"][0]["sync_id"]] == OrderItemStatus.CANCELLED
    assert lines[booked["items"][1]["sync_id"]] == OrderItemStatus.RETURNED
    assert OrderRequest.objects.get(pk=discount).status == OrderRequestStatus.CLOSED
    claim.refresh_from_db()
    assert claim.status == ClaimStatus.CANCELLED
    # MO 41 can be rented again straight away.
    again = booking(world, shop, number=240)
    again["items"] = again["items"][:1]
    assert call(client, orders.BOOK, again, token=token).json()["code"] == "ok"
    # The tablet sees it: the order, and the request.
    status = call(client, rq.STATUS, {"order_ids": [booked["sync_id"]]}, token=token).json()["data"]["requests"]
    assert {r["kind"]: r["status"] for r in status}["cancel"] == "approved"


def test_an_approved_cancel_is_final(client, token, manager, booked):
    request_id = asked_cancel(client, token, booked)
    rq.decide(client, manager, APPROVE, request_id)

    assert rq.decide(client, manager, REVOKE, request_id)["code"] == "request_not_revocable"
    assert ask_cancel(client, token, booked)["code"] == "order_closed"


def test_a_cancel_cannot_be_approved_once_the_order_settled(client, token, shop, manager, booked):
    request_id = asked_cancel(client, token, booked)
    OrderRequest.objects.filter(pk=request_id).update(status=OrderRequestStatus.REJECTED,
                                                      decided_at=order_of(booked).booked_at)
    rq.return_all(client, token, booked)
    settles.settle(client, token, settles.bill(booked, shop))
    OrderRequest.objects.filter(pk=request_id).update(status=OrderRequestStatus.PENDING, decided_at=None)

    assert rq.decide(client, manager, APPROVE, request_id)["code"] == "order_closed"


def test_a_refund_can_be_recorded_after_the_cancel(client, token, shop, manager, booked):
    rq.decide(client, manager, APPROVE, asked_cancel(client, token, booked))

    body = call(client, PAYMENTS, {"sync_id": str(uuid7()), "order_id": booked["sync_id"], "kind": "refund",
                                   "payments": rq.refund(shop, "100.00")}, token=token).json()

    assert body["code"] == "ok", body
    assert Payment.objects.filter(order=order_of(booked), kind="refund").count() == 1


def test_a_cancel_is_listed_and_counted(client, token, manager, booked):
    request_id = asked_cancel(client, token, booked)

    rows = call(client, LIST, {"kind": "cancel"}, token=token).json()["data"]["requests"]
    counts = call(client, COUNTS, token=manager).json()["data"]

    assert [r["request_sync_id"] for r in rows] == [request_id]
    assert (counts["cancel"], counts["total"]) == (1, 1)


# -- Web --------------------------------------------------------------------------------

@pytest.fixture
def web(world):
    role = Role.objects.create(company=world["company"], name="Back office")
    grant_all(role)
    User.objects.create_user("sara.k", orders.fare_api.PASSWORD, display_name="Sara K", company=world["company"],
                             role=role, allowed_channels=[Channel.WEB])
    browser = Client()
    browser.post("/login/", {"username": "sara.k", "password": orders.fare_api.PASSWORD})
    browser.role = role
    return browser


def cancel_on_web(web, booked, reason="Wrong vehicle booked"):
    return web.post(f"/rental/order/{booked['sync_id']}/cancel/", json.dumps({"reason": reason}),
                    content_type="application/json")


def test_the_web_cancels_a_running_order_directly(client, web, booked):
    page = web.get(f"/rental/order/{booked['sync_id']}/").content.decode()
    assert "data-order-cancel" in page and "Cancel order" in page

    response = cancel_on_web(web, booked)

    assert response.json() == {"ok": True, "message": "Order cancelled."}
    order = order_of(booked)
    assert order.status == OrderStatus.CANCELLED
    req = OrderRequest.objects.get(order=order, kind=OrderRequestKind.CANCEL)
    assert (req.status, req.decided_channel, req.device_id, req.reason) == (
        OrderRequestStatus.APPROVED, DecisionChannel.WEB, None, "Wrong vehicle booked")
    assert req.requested_by == req.decided_by
    page = web.get(f"/rental/order/{booked['sync_id']}/").content.decode()
    assert "Wrong vehicle booked" in page and "data-order-cancel" not in page


def test_the_web_cancel_needs_a_reason_a_running_order_and_the_permission(client, token, web, booked):
    assert cancel_on_web(web, booked, reason=" ").json()["ok"] is False

    asked_cancel(client, token, booked)
    waiting = cancel_on_web(web, booked).json()
    assert waiting["ok"] is False and "waiting" in waiting["errors"][0]["message"]

    RolePermission.objects.filter(role=web.role, page__code="rental.request").update(can_approve=False)
    assert cancel_on_web(web, booked).status_code == 403
    assert "data-order-cancel" not in web.get(f"/rental/order/{booked['sync_id']}/").content.decode()


def test_approving_a_cancel_on_the_requests_screen_says_so(client, token, web, booked):
    request_id = asked_cancel(client, token, booked)

    body = web.post(f"/rental/request/{request_id}/approve/", json.dumps({}),
                    content_type="application/json").json()

    assert body["ok"] is True and body["message"] == "Order cancelled."
    assert order_of(booked).status == OrderStatus.CANCELLED
