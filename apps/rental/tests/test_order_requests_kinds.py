"""Complimentary and reprint requests (apps/rental/requests.py): a complimentary
ride settles a running order at net 0 with any advance refunded; a reprint
lets the tablet print a settled bill once more. The shared rules -- ask,
withdraw, status, decide -- are proven for discounts in test_order_requests."""

from decimal import Decimal

import pytest

from apps.discount.models import ClaimStatus
from apps.discount.tests.conftest import make_claim
from apps.rental import api
from apps.rental.models import (
    DiscountSource,
    Invoice,
    Order,
    OrderRequest,
    OrderRequestStatus,
    Payment,
)
from apps.rental.tests import test_order_requests as rq
from core.ids import uuid7

call, shape, booking = rq.call, rq.shape, rq.booking
world, token, fresh_throttle, shop = rq.world, rq.token, rq.fresh_throttle, rq.shop
booked, manager, card = rq.booked, rq.manager, rq.card
ask, asked, decide, return_all, refund, bill = rq.ask, rq.asked, rq.decide, rq.return_all, rq.refund, rq.bill
ASK, WITHDRAW, STATUS, LIST, APPROVE, REJECT, REVOKE, SETTLE = (
    rq.ASK, rq.WITHDRAW, rq.STATUS, rq.LIST, rq.APPROVE, rq.REJECT, rq.REVOKE, rq.SETTLE)
USED = "/api/v1/operator/orders/requests/used"


def free_bill(booked, shop, request_sync_id, **overrides):
    """The whole 110.00 off: net 0, no VAT; the AED 100 advance handed back."""
    fields = {"tax_amount": "0.000", "net_amount": "0.000", "payments": refund(shop, "100.000"),
              "complimentary": {"request_sync_id": request_sync_id}, **overrides}
    return bill(booked, shop, **fields)


def approved_free(client, token, manager, booked):
    data = asked(client, token, booked, kind="complimentary")
    assert decide(client, manager, APPROVE, data["request_sync_id"])["code"] == "ok"
    return data["request_sync_id"]


@pytest.fixture
def settled(client, token, shop, booked):
    return_all(client, token, booked)
    assert call(client, SETTLE, bill(booked, shop), token=token).json()["code"] == "ok"
    return booked


def used(client, token, request_sync_id, **overrides):
    data = {"sync_id": str(uuid7()), "request_sync_id": request_sync_id, "used_at": "2026-10-02 18:03:10"}
    data.update(overrides)
    return call(client, USED, data, token=token).json()


# -- Complimentary ------------------------------------------------------------------


def test_a_complimentary_is_asked_and_approved_without_a_value(client, token, manager, booked):
    data = asked(client, token, booked, kind="complimentary", reason="Hotel manager's guest")

    body = decide(client, manager, APPROVE, data["request_sync_id"], note="OK")

    assert (body["code"], body["data"]["kind"], body["data"]["status"]) == ("ok", "complimentary", "approved")
    assert (body["data"]["discount_type"], body["data"]["discount_value"]) == (None, None)


@pytest.mark.parametrize(("first", "second", "code"), [
    ("discount", "complimentary", "discount_requested"),
    ("complimentary", "discount", "complimentary_requested"),
    ("complimentary", "complimentary", "complimentary_request_pending"),
])
def test_a_discount_and_a_complimentary_never_share_an_order(client, token, booked, first, second, code):
    asked(client, token, booked, kind=first)

    assert call(client, ASK, ask(booked, kind=second), token=token).json()["code"] == code


def test_a_card_discount_and_a_complimentary_exclude_each_other(client, token, shop, booked, card):
    claim = make_claim(card, shop["ahmed"], status=ClaimStatus.PENDING, order=Order.objects.get(pk=booked["sync_id"]))
    assert call(client, ASK, ask(booked, kind="complimentary"), token=token).json()["code"] == "card_discount_requested"

    claim.delete()
    asked(client, token, booked, kind="complimentary")
    body = call(client, "/api/v1/operator/card-discounts/approval", {
        "sync_id": str(uuid7()), "card_discount_id": card.pk, "full_number": shop["ahmed"].mobile_full,
        "order_id": booked["sync_id"], "bill_amount": "100.000", "discount_percent": "10.00",
        "discount_amount": "10.000", "net_amount": "90.000", "requested_at": "2026-10-02 16:31:00",
    }, token=token).json()
    assert body["code"] == "complimentary_requested"


def test_a_complimentary_settles_at_net_zero_with_the_advance_refunded(client, token, manager, shop, booked):
    request_id = approved_free(client, token, manager, booked)
    return_all(client, token, booked)

    body = call(client, SETTLE, free_bill(booked, shop, request_id), token=token).json()

    assert body["code"] == "ok", body
    data = body["data"]
    assert data["discount"] == {"source": "complimentary", "request_sync_id": request_id,
                                "discount_percentage": "100.00", "discount_amount": "110.000"}
    assert (data["net_amount"], data["paid_amount"], data["amount_refunded"]) == ("0.000", "0.000", "100.000")
    # The advance and its refund both stay in the payment history.
    assert sorted((p.kind, p.amount) for p in Payment.objects.filter(order_id=booked["sync_id"])) == [
        ("advance", Decimal("40.000")), ("advance", Decimal("60.000")), ("refund", Decimal("100.000"))]
    req = OrderRequest.objects.get()
    assert (req.status, req.applied_amount) == (OrderRequestStatus.APPROVED, Decimal("110.000"))
    invoice = Invoice.objects.get()
    assert (invoice.discount_source, invoice.net_amount, invoice.discount_amount) == (
        DiscountSource.COMPLIMENTARY, Decimal("0.000"), Decimal("110.000"))


def test_an_approved_complimentary_must_settle_as_one(client, token, manager, shop, booked):
    request_id = approved_free(client, token, manager, booked)
    return_all(client, token, booked)

    body = call(client, SETTLE, bill(booked, shop), token=token).json()

    assert body["code"] == "approved_complimentary_not_applied"
    assert (body["data"]["request_sync_id"], body["data"]["kind"]) == (request_id, "complimentary")


def test_a_complimentary_bill_carries_no_vat_and_names_its_approval(client, token, manager, shop, booked):
    request_id = approved_free(client, token, manager, booked)
    return_all(client, token, booked)

    assert call(client, SETTLE, free_bill(booked, shop, request_id, tax_amount="5.240"),
                token=token).json()["code"] == "amount_mismatch"
    assert call(client, SETTLE, free_bill(booked, shop, str(uuid7())),
                token=token).json()["code"] == "unknown_complimentary_request"
    assert call(client, SETTLE, free_bill(booked, shop, request_id, net_amount="10.000"),
                token=token).json()["code"] == "amount_mismatch"


def test_an_unknown_or_revoked_complimentary_is_refused(client, token, manager, shop, booked):
    request_id = approved_free(client, token, manager, booked)
    decide(client, manager, REVOKE, request_id)
    return_all(client, token, booked)

    assert call(client, SETTLE, free_bill(booked, shop, request_id),
                token=token).json()["code"] == "complimentary_request_not_approved"
    assert call(client, SETTLE, free_bill(booked, shop, str(uuid7())),
                token=token).json()["code"] == "unknown_complimentary_request"


def test_two_blocks_are_refused(client, token, shop, booked):
    body = call(client, SETTLE, bill(
        booked, shop, complimentary={"request_sync_id": str(uuid7())},
        manager_discount={"request_sync_id": str(uuid7()), "discount_amount": "5.500"}), token=token).json()

    assert body["code"] == "invalid_request"


def test_the_complimentary_invoice_prints_marked(client_in, token, manager, shop, booked):
    request_id = approved_free(client_in, token, manager, booked)
    return_all(client_in, token, booked)
    call(client_in, SETTLE, free_bill(booked, shop, request_id), token=token)
    invoice = Invoice.objects.get()

    receipt = client_in.get(f"/rental/invoice/{invoice.pk}/receipt/").content.decode()
    detail = client_in.get(f"/rental/invoice/{invoice.pk}/").content.decode()

    assert "COMPLIMENTARY" in receipt and "Card Disc." not in receipt
    assert "AED 110.000" in receipt and "Complimentary" in detail


# -- Reprint --------------------------------------------------------------------------


def test_a_reprint_needs_a_settled_order(client, token, booked):
    body = call(client, ASK, ask(booked, kind="reprint"), token=token).json()

    assert (body["code"], body["message"]) == ("order_not_settled", "A reprint needs a settled order.")


def test_one_approval_is_one_reprint(client, token, manager, settled):
    data = asked(client, token, settled, kind="reprint", reason="Customer lost the receipt")
    request_id = data["request_sync_id"]

    assert used(client, token, request_id)["code"] == "request_not_approved"
    decide(client, manager, APPROVE, request_id)
    assert call(client, ASK, ask(settled, kind="reprint"), token=token).json()["code"] == "reprint_approved"

    body = used(client, token, request_id)

    assert (body["code"], body["data"]["status"], body["data"]["used_at"]) == ("ok", "used", "2026-10-02 18:03:10")
    assert shape(body["data"]) == shape(api._REPRINT_USED_SAMPLE)
    assert used(client, token, request_id)["code"] == "request_used"
    # Spent: another reprint is a new request.
    assert call(client, ASK, ask(settled, kind="reprint"), token=token).json()["code"] == "ok"


def test_a_resent_used_call_is_a_duplicate(client, token, manager, settled):
    request_id = asked(client, token, settled, kind="reprint")["request_sync_id"]
    decide(client, manager, APPROVE, request_id)
    request_data = {"sync_id": str(uuid7()), "request_sync_id": request_id, "used_at": "2026-10-02 18:03:10"}

    first = call(client, USED, request_data, token=token).json()
    again = call(client, USED, request_data, token=token).json()

    assert (again["code"], again["data"]) == ("duplicate", first["data"])


def test_a_reprint_is_not_revoked_but_may_be_rejected_or_withdrawn(client, token, manager, settled):
    request_id = asked(client, token, settled, kind="reprint")["request_sync_id"]
    withdrawn = call(client, WITHDRAW, {"sync_id": str(uuid7()), "request_sync_id": request_id,
                                        "withdrawn_at": "2026-10-02 18:00:00"}, token=token).json()
    assert withdrawn["data"]["status"] == "withdrawn"

    second = asked(client, token, settled, kind="reprint")["request_sync_id"]
    decide(client, manager, APPROVE, second)
    assert decide(client, manager, REVOKE, second)["code"] == "request_not_revocable"
    assert OrderRequest.objects.get(pk=second).status == OrderRequestStatus.APPROVED


def test_a_reprint_can_be_rejected(client, token, manager, settled):
    request_id = asked(client, token, settled, kind="reprint")["request_sync_id"]

    body = decide(client, manager, REJECT, request_id, note="Already printed twice")

    assert (body["code"], body["data"]["status"], body["data"]["decision_note"]) == (
        "ok", "rejected", "Already printed twice")


def test_used_is_only_for_reprints(client, token, manager, booked):
    request_id = asked(client, token, booked)["request_sync_id"]
    decide(client, manager, APPROVE, request_id, discount_type="percent", discount_value="5")

    assert used(client, token, request_id)["code"] == "unknown_request"


def test_the_manager_filters_by_kind(client, token, manager, settled):
    asked(client, token, settled, kind="reprint")

    reprints = call(client, LIST, {"kind": "reprint"}, token=manager).json()["data"]["requests"]
    discounts = call(client, LIST, {"kind": "discount"}, token=manager).json()["data"]["requests"]

    assert [r["kind"] for r in reprints] == ["reprint"] and discounts == []
    assert reprints[0]["order"]["net_amount"] == "110.000"


def test_the_web_approves_a_reprint_with_a_note(client_in, token, settled):
    request_id = asked(client_in, token, settled, kind="reprint")["request_sync_id"]
    page = client_in.get(f"/rental/request/{request_id}/").content.decode()
    assert "Approve reprint" in page and "Discount as" not in page

    response = client_in.post(f"/rental/request/{request_id}/approve/", {"note": "Fine"},
                              content_type="application/json")

    assert response.json()["ok"], response.json()
    assert OrderRequest.objects.get().status == OrderRequestStatus.APPROVED
    listed = client_in.get("/rental/request/list/", {"tab": "approved", "kind": "reprint"}).context["rows"]
    assert [r["rule"] for r in listed] == ["One print"]


def test_the_used_endpoint_is_in_the_api_docs(client, db):
    assert "/api/v1/{app}/orders/requests/used" in client.get("/api/schema/").content.decode()
