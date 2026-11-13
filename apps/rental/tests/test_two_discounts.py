"""A card and a manager discount on one bill; a complimentary replaces both
(owner, 13 Nov 2026).

Both discounts are worked out on the full subtotal and added up, never past it
(the net is then 0); each is checked against its own approval and kept as sent.
A complimentary may be asked for over either, and once approved it revokes the
approved ones and closes the pending ones, with no push."""

import datetime
from decimal import Decimal
from importlib import import_module

import pytest
from django.apps import apps as django_apps
from django.utils import timezone

from apps.discount.models import CardDiscountClaim, ClaimStatus
from apps.discount.tests.conftest import make_claim
from apps.rental import requests as order_requests
from apps.rental import station_sync
from apps.rental.models import (
    DiscountSource,
    Invoice,
    Order,
    OrderRequest,
    OrderRequestKind,
    OrderRequestStatus,
)
from apps.rental.tests import test_order_requests as rq
from apps.rental.views import discount_lines
from core.ids import uuid7

call = rq.call
world, token, fresh_throttle, shop = rq.world, rq.token, rq.fresh_throttle, rq.shop
booked, manager, card = rq.booked, rq.manager, rq.card
ask, asked, decide, approved, return_all, refund, bill = (
    rq.ask, rq.asked, rq.decide, rq.approved, rq.return_all, rq.refund, rq.bill)
ASK, APPROVE, REVOKE, SETTLE = rq.ASK, rq.APPROVE, rq.REVOKE, rq.SETTLE
CARD_ASK = "/api/v1/operator/card-discounts/approval"
CLAIM_APPROVE = "/api/v1/manager/card-discounts/claims/approve"


def order_of(booked):
    return Order.objects.get(pk=booked["sync_id"])


def card_claim(card, shop, booked, status=ClaimStatus.APPROVED):
    return make_claim(card, shop["ahmed"], status=status, order=order_of(booked), when=timezone.now())


def ask_card(client, token, shop, booked, card):
    return call(client, CARD_ASK, {
        "sync_id": str(uuid7()), "card_discount_id": card.pk, "full_number": shop["ahmed"].mobile_full,
        "order_id": booked["sync_id"], "bill_amount": "110.000", "discount_percent": "10.00",
        "discount_amount": "11.000", "net_amount": "99.000", "requested_at": "2026-10-02 16:31:00",
    }, token=token).json()


def free_approved(client, token, manager, booked):
    data = asked(client, token, booked, kind="complimentary")
    body = decide(client, manager, APPROVE, data["request_sync_id"])
    assert body["code"] == "ok", body
    return data["request_sync_id"]


def both(claim, manager_id, card_amount="11.000", manager_amount="11.000", manager_pct="10.00"):
    manager_block = {"request_sync_id": manager_id, "discount_amount": manager_amount}
    if manager_pct:
        manager_block["discount_percentage"] = manager_pct
    return {"card_discount": {"request_sync_id": str(claim.pk), "discount_percentage": "10.00",
                              "discount_amount": card_amount},
            "manager_discount": manager_block}


# -- Asking ----------------------------------------------------------------------------


def test_a_discount_may_be_asked_for_under_a_pending_complimentary(client, token, shop, booked, card):
    asked(client, token, booked, kind="complimentary")

    assert call(client, ASK, ask(booked), token=token).json()["code"] == "ok"
    assert ask_card(client, token, shop, booked, card)["code"] == "ok"


def test_an_approved_complimentary_refuses_new_discounts_until_revoked(client, token, manager, shop, booked, card):
    free_id = free_approved(client, token, manager, booked)

    assert call(client, ASK, ask(booked), token=token).json()["code"] == "complimentary_approved"
    assert ask_card(client, token, shop, booked, card)["code"] == "complimentary_approved"

    assert decide(client, manager, REVOKE, free_id, note="Changed mind")["code"] == "ok"
    assert call(client, ASK, ask(booked), token=token).json()["code"] == "ok"
    assert ask_card(client, token, shop, booked, card)["code"] == "ok"


def test_a_complimentary_may_be_asked_for_over_approved_discounts(client, token, manager, shop, booked, card):
    approved(client, token, manager, booked)
    card_claim(card, shop, booked)

    assert call(client, ASK, ask(booked, kind="complimentary"), token=token).json()["code"] == "ok"


def test_a_second_card_claim_waits_for_the_first_to_be_decided(client, token, shop, booked, card):
    card_claim(card, shop, booked, status=ClaimStatus.PENDING)
    assert ask_card(client, token, shop, booked, card)["code"] == "request_pending"

    CardDiscountClaim.objects.filter(order=order_of(booked)).update(
        status=ClaimStatus.APPROVED, decided_at=timezone.now())
    assert ask_card(client, token, shop, booked, card)["code"] == "ok"             # e.g. another card


# -- A complimentary approved replaces the discounts -----------------------------------


@pytest.fixture
def pushes(monkeypatch):
    """Every push the approvals send: (subject id, action)."""
    sent = []
    monkeypatch.setattr(order_requests.notifications, "notify", lambda subject, action, **kw: sent.append(
        (str(subject.pk), str(action))))
    monkeypatch.setattr(order_requests.notifications, "notify_all", lambda subjects, action, **kw: sent.extend(
        (str(s.pk), str(action)) for s in subjects))
    monkeypatch.setattr(station_sync, "request_changed", lambda subject, action, **kw: sent.append(
        (str(subject.pk), "sync:" + str(action))))
    return sent


def test_an_approved_complimentary_revokes_the_approved_discounts(client, token, manager, shop, booked, card,
                                                                   pushes):
    manager_id = approved(client, token, manager, booked)
    claim = card_claim(card, shop, booked)
    pending = card_claim(card, shop, booked, status=ClaimStatus.PENDING)
    free = asked(client, token, booked, kind="complimentary")
    pushes.clear()

    assert decide(client, manager, APPROVE, free["request_sync_id"])["code"] == "ok"

    discount = OrderRequest.objects.get(pk=manager_id)
    assert (discount.status, discount.revoke_note, discount.revoked_by.username) == (
        OrderRequestStatus.REVOKED, "Replaced by complimentary", "mgr001")
    claim.refresh_from_db()
    pending.refresh_from_db()
    assert (claim.status, claim.revoke_note, claim.revoked_by.username) == (
        ClaimStatus.REVOKED, "Replaced by complimentary", "mgr001")
    assert (pending.status, pending.remarks) == (ClaimStatus.CANCELLED, "Replaced by complimentary")
    # Only the complimentary's own approval is pushed, never the replaced ones.
    assert {subject for subject, _ in pushes} == {free["request_sync_id"]}


def test_an_approved_complimentary_closes_a_pending_manager_discount(client, token, manager, booked):
    waiting = asked(client, token, booked)
    free_approved(client, token, manager, booked)

    discount = OrderRequest.objects.get(pk=waiting["request_sync_id"])
    assert (discount.status, discount.closed_at is not None) == (OrderRequestStatus.CLOSED, True)


def test_a_discount_cannot_be_approved_once_the_order_is_complimentary(client, token, manager, shop, booked, card):
    waiting = asked(client, token, booked)
    claim = card_claim(card, shop, booked, status=ClaimStatus.PENDING)
    free = asked(client, token, booked, kind="complimentary")
    # As if the complimentary were approved while these two waited (a race the
    # approval's own replacement normally closes first).
    OrderRequest.objects.filter(pk=free["request_sync_id"]).update(
        status=OrderRequestStatus.APPROVED, decided_at=timezone.now())

    body = decide(client, manager, APPROVE, waiting["request_sync_id"], discount_type="percent", discount_value="5")
    assert body["code"] == "complimentary_approved"
    body = call(client, CLAIM_APPROVE, {"claim_id": str(claim.pk)}, token=manager).json()
    assert body["code"] == "complimentary_approved"


# -- Settle ----------------------------------------------------------------------------


def test_a_card_and_a_manager_discount_go_on_one_bill(client, token, manager, shop, booked, card):
    manager_id = approved(client, token, manager, booked)
    claim = card_claim(card, shop, booked)
    return_all(client, token, booked)

    body = call(client, SETTLE, bill(booked, shop, tax_amount="4.190", net_amount="88.000",
                                     payments=refund(shop, "12.000"), **both(claim, manager_id)), token=token).json()

    assert body["code"] == "ok", body
    data = body["data"]
    assert data["discount"] == {"source": "card_manager", "request_sync_id": None, "discount_percentage": None,
                                "discount_amount": "22.000"}
    assert data["card_discount"] == {"request_sync_id": str(claim.pk), "discount_percentage": "10.00",
                                     "discount_amount": "11.000"}
    assert data["manager_discount"] == {"request_sync_id": manager_id, "discount_percentage": "10.00",
                                        "discount_amount": "11.000"}
    assert data["net_amount"] == "88.000"
    invoice = Invoice.objects.get(order_id=booked["sync_id"])
    assert (invoice.discount_source, invoice.discount_amount, invoice.card_discount_amount,
            invoice.manager_discount_amount) == (
        DiscountSource.CARD_MANAGER, Decimal("22.000"), Decimal("11.000"), Decimal("11.000"))
    assert OrderRequest.objects.get(pk=manager_id).applied_amount == Decimal("11.000")     # its own, not 22
    claim.refresh_from_db()
    assert claim.status == ClaimStatus.REDEEMED
    assert [line["label"] for line in discount_lines(invoice)] == ["Card Discount", "Manager Discount"]


def test_both_approved_means_both_on_the_bill(client, token, manager, shop, booked, card):
    manager_id = approved(client, token, manager, booked)
    claim = card_claim(card, shop, booked)
    return_all(client, token, booked)

    only_manager = bill(booked, shop, tax_amount="4.714", net_amount="99.000", payments=refund(shop, "1.000"),
                        manager_discount=both(claim, manager_id)["manager_discount"])
    assert call(client, SETTLE, only_manager, token=token).json()["code"] == "approved_card_discount_not_applied"

    only_card = bill(booked, shop, tax_amount="4.714", net_amount="99.000", payments=refund(shop, "1.000"),
                     card_discount=both(claim, manager_id)["card_discount"])
    assert call(client, SETTLE, only_card, token=token).json()["code"] == "approved_discount_not_applied"


def test_discounts_above_the_subtotal_make_the_bill_net_zero(client, token, manager, shop, booked, card):
    manager_id = approved(client, token, manager, booked, "amount", "100")
    claim = card_claim(card, shop, booked)
    return_all(client, token, booked)
    discounts = both(claim, manager_id, manager_amount="100.000", manager_pct=None)    # 11 + 100 > 110

    with_vat = bill(booked, shop, tax_amount="0.476", net_amount="0.000", payments=refund(shop, "100.000"),
                    **discounts)
    assert call(client, SETTLE, with_vat, token=token).json()["code"] == "amount_mismatch"

    body = call(client, SETTLE, bill(booked, shop, tax_amount="0.000", net_amount="0.000",
                                     payments=refund(shop, "100.000"), **discounts), token=token).json()

    assert body["code"] == "ok", body
    order = order_of(booked)
    assert (order.net_amount, order.discount_amount, order.card_discount_amount, order.manager_discount_amount) == (
        Decimal("0.000"), Decimal("110.000"), Decimal("11.000"), Decimal("100.000"))


def test_a_single_discount_settles_as_before(client, token, manager, shop, booked):
    manager_id = approved(client, token, manager, booked)
    return_all(client, token, booked)

    body = call(client, SETTLE, bill(booked, shop, tax_amount="4.714", net_amount="99.000",
                                     payments=refund(shop, "1.000"),
                                     manager_discount={"request_sync_id": manager_id, "discount_percentage": "10.00",
                                                       "discount_amount": "11.000"}), token=token).json()

    assert body["code"] == "ok", body
    data = body["data"]
    assert (data["discount"]["source"], data["discount"]["discount_percentage"], data["card_discount"]) == (
        "manager", "10.00", None)
    invoice = Invoice.objects.get(order_id=booked["sync_id"])
    assert (invoice.discount_source, invoice.manager_discount_amount, invoice.card_discount_amount) == (
        DiscountSource.MANAGER, Decimal("11.000"), None)


def test_the_manager_order_block_shows_both(client, token, manager, shop, booked, card):
    manager_id = approved(client, token, manager, booked)
    claim = card_claim(card, shop, booked)
    return_all(client, token, booked)
    assert call(client, SETTLE, bill(booked, shop, tax_amount="4.190", net_amount="88.000",
                                     payments=refund(shop, "12.000"), **both(claim, manager_id)),
                token=token).json()["code"] == "ok"
    order = Order.objects.select_related("invoice").get(pk=booked["sync_id"])

    block = order_requests._order_block(order, datetime.UTC, timezone.now())

    assert (block["discount_amount"], block["card_discount_amount"], block["manager_discount_amount"]) == (
        "22.000", "11.000", "11.000")


# -- Migration -------------------------------------------------------------------------


def test_the_migration_copies_a_single_discount_into_its_pair(client, token, manager, shop, booked):
    manager_id = approved(client, token, manager, booked)
    return_all(client, token, booked)
    assert call(client, SETTLE, bill(booked, shop, tax_amount="4.714", net_amount="99.000",
                                     payments=refund(shop, "1.000"),
                                     manager_discount={"request_sync_id": manager_id, "discount_percentage": "10.00",
                                                       "discount_amount": "11.000"}),
                token=token).json()["code"] == "ok"
    # As settled before the change: the pair empty.
    Invoice.objects.update(manager_discount_amount=None, manager_discount_percentage=None)
    Order.objects.update(manager_discount_amount=None, manager_discount_percentage=None)

    import_module("apps.rental.migrations.0028_two_discounts").fill(django_apps, None)

    invoice, order = Invoice.objects.get(), order_of(booked)
    assert (invoice.manager_discount_amount, invoice.manager_discount_percentage) == (
        Decimal("11.000"), Decimal("10.00"))
    assert (order.manager_discount_amount, invoice.card_discount_amount) == (Decimal("11.000"), None)
    assert OrderRequestKind.DISCOUNT == "discount"
