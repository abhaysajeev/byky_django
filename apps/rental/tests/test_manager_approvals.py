"""The manager app decides credit-note requests and card-discount claims the way
the web does -- the web's own services, with channel `manager` -- and counts
everything waiting for its badges."""

from decimal import Decimal

import pytest

from apps.discount.models import CardDiscountClaim, ClaimStatus
from apps.discount.tests.conftest import make_claim
from apps.rental import credit_notes
from apps.rental.models import CreditNote, CreditNoteStatus, DecisionChannel, Order
from apps.rental.tests import test_credit_notes as notes
from apps.rental.tests import test_order_requests as requests_tests
from apps.rental.tests.test_order_api import call

world, token, fresh_throttle, shop = notes.world, notes.token, notes.fresh_throttle, notes.shop
booked, returned, settled, web = notes.booked, notes.returned, notes.settled, notes.web
manager, card = requests_tests.manager, requests_tests.card

CN_LIST = "/api/v1/manager/credit-notes/list"
CN_APPROVE = "/api/v1/manager/credit-notes/approve"
CN_REJECT = "/api/v1/manager/credit-notes/reject"
CLAIM_LIST = "/api/v1/manager/card-discounts/claims/list"
CLAIM_APPROVE = "/api/v1/manager/card-discounts/claims/approve"
CLAIM_REJECT = "/api/v1/manager/card-discounts/claims/reject"
CLAIM_REVOKE = "/api/v1/manager/card-discounts/claims/revoke"
COUNTS = "/api/v1/manager/requests/pending-counts"


@pytest.fixture
def asked(client, token, settled):
    """A tablet's credit-note request on the settled order (net 110.00)."""
    body = call(client, notes.REQUEST, notes.ask(settled), token=token).json()
    assert body["code"] == "ok", body
    return body["data"]["credit_note_id"]


@pytest.fixture
def claim(card, shop, settled):
    return make_claim(card, shop["ahmed"], order=settled, branch=settled.branch, card_number="FAM-1")


# -- Credit notes ----------------------------------------------------------------------

def test_the_manager_lists_credit_note_requests_with_the_order(client, manager, asked, settled):
    body = call(client, CN_LIST, {"pending": True}, token=manager).json()

    assert (body["code"], body["data"]["total"]) == ("ok", 1)
    row = body["data"]["credit_notes"][0]
    assert (row["credit_note_id"], row["status"], row["order"]["order_no"]) == (
        asked, "pending", settled.order_no)
    assert (row["invoice"]["net_amount"], row["reason"], row["station"]) == (
        "110.000", "Chain broke after 10 minutes", settled.branch.name)


def test_the_manager_issues_one_with_an_amount_as_the_web_does(client, manager, asked):
    body = call(client, CN_APPROVE, {"credit_note_id": asked, "net_amount": "42.00", "remarks": "Half"},
                token=manager).json()

    assert body["code"] == "ok", body
    assert (body["data"]["status"], body["data"]["net_amount"], body["data"]["decided_channel"]) == (
        "approved", "42.000", "manager")
    note = CreditNote.objects.get(pk=asked)
    assert (note.decided_by.username, note.decided_channel, note.decision_remark) == (
        "mgr001", DecisionChannel.MANAGER, "Half")
    assert note.credit_note_no.endswith("CN") and note.tax_amount + note.taxable_amount == Decimal("42.000")


@pytest.mark.parametrize("amount", ["0", "110.01"])
def test_the_amount_rule_is_the_webs(client, manager, asked, amount):
    response = call(client, CN_APPROVE, {"credit_note_id": asked, "net_amount": amount}, token=manager)

    assert (response.status_code, response.json()["code"]) == (400, "invalid_amount")
    assert CreditNote.objects.get(pk=asked).status == CreditNoteStatus.PENDING


def test_the_manager_rejects_and_the_web_then_cannot_decide(client, manager, asked, world):
    body = call(client, CN_REJECT, {"credit_note_id": asked, "remarks": "No fault"}, token=manager).json()

    assert (body["data"]["status"], body["data"]["decision_remark"]) == ("rejected", "No fault")
    with pytest.raises(credit_notes.CreditNoteRefused, match="already rejected"):
        credit_notes.approve(None, asked, [world["company"].pk], "10")


def test_one_decided_on_the_web_is_refused_in_the_app(client, manager, asked, web):
    notes.post(web, f"/rental/credit-note/{asked}/reject/", {"remarks": "web said no"})

    response = call(client, CN_APPROVE, {"credit_note_id": asked, "net_amount": "10"}, token=manager)

    assert (response.status_code, response.json()["code"]) == (409, "credit_note_decided")
    assert CreditNote.objects.get(pk=asked).decided_channel == DecisionChannel.WEB


def test_an_unknown_credit_note_is_404(client, manager, asked):
    response = call(client, CN_REJECT, {"credit_note_id": "01a1013d-fa97-7179-93ae-3ae25842f1ce"}, token=manager)

    assert (response.status_code, response.json()["code"]) == (404, "unknown_credit_note")


# -- Card-discount claims --------------------------------------------------------------

def test_the_manager_lists_claims_with_the_card(client, manager, claim):
    row = call(client, CLAIM_LIST, {"pending": True}, token=manager).json()["data"]["claims"][0]

    assert (row["claim_id"], row["status"], row["card_type"], row["card_grade"], row["card_number"]) == (
        str(claim.pk), "pending", "Family Card", "Gold", "FAM-1")


def test_the_manager_approves_and_rejects_claims(client, manager, card, shop, claim):
    approved = call(client, CLAIM_APPROVE, {"claim_id": str(claim.pk), "remarks": "Card seen"}, token=manager).json()
    again = call(client, CLAIM_REJECT, {"claim_id": str(claim.pk)}, token=manager)

    assert (approved["code"], approved["data"]["status"], approved["data"]["decided_channel"]) == (
        "ok", "approved", "manager")
    claim.refresh_from_db()
    assert (claim.status, claim.decided_by.username, claim.remarks) == (ClaimStatus.APPROVED, "mgr001", "Card seen")
    assert (again.status_code, again.json()["code"]) == (409, "claim_decided")


@pytest.fixture
def running_claim(card, shop):
    """An approved claim on an order still running -- the one a manager may revoke."""
    return make_claim(card, shop["ahmed"], status=ClaimStatus.APPROVED, card_number="FAM-2")


def test_the_manager_revokes_an_approval_with_a_reason(client, manager, running_claim):
    no_note = call(client, CLAIM_REVOKE, {"claim_id": str(running_claim.pk)}, token=manager)
    body = call(client, CLAIM_REVOKE, {"claim_id": str(running_claim.pk), "note": "Card was expired"},
                token=manager).json()

    assert (no_note.status_code, no_note.json()["data"]["errors"]) == (400, {"note": "is required"})
    assert body["code"] == "ok", body
    assert {k: body["data"][k] for k in ("status", "revoked_by", "revoked_channel", "revoke_note")} == {
        "status": "revoked", "revoked_by": "Meera", "revoked_channel": "manager", "revoke_note": "Card was expired"}
    assert body["data"]["revoked_at"] is not None
    running_claim.refresh_from_db()
    assert (running_claim.status, running_claim.revoked_by.username) == (ClaimStatus.REVOKED, "mgr001")
    listed = call(client, CLAIM_LIST, {"status": "revoked"}, token=manager).json()["data"]["claims"]
    assert [row["claim_id"] for row in listed] == [str(running_claim.pk)]


def test_only_an_approved_claim_on_a_running_order_is_revoked(client, manager, claim, running_claim):
    pending = call(client, CLAIM_REVOKE, {"claim_id": str(claim.pk), "note": "x"}, token=manager)
    Order.objects.filter(pk=running_claim.order_id).update(status="completed")
    billed = call(client, CLAIM_REVOKE, {"claim_id": str(running_claim.pk), "note": "x"}, token=manager)

    assert (pending.status_code, pending.json()["code"]) == (409, "claim_not_approved")
    assert (billed.status_code, billed.json()["code"]) == (409, "order_closed")


def test_an_unknown_or_other_company_claim_is_404(client, manager, claim, world):
    CardDiscountClaim.objects.filter(pk=claim.pk).update(company=world["other"])

    response = call(client, CLAIM_APPROVE, {"claim_id": str(claim.pk)}, token=manager)

    assert (response.status_code, response.json()["code"]) == (404, "unknown_claim")


# -- Badges, scope -----------------------------------------------------------------------

def test_pending_counts_cover_every_kind(client, token, manager, asked, claim, settled):
    body = call(client, COUNTS, token=manager).json()

    assert body["data"] == {"discount": 0, "complimentary": 0, "reprint": 0, "order_reprint": 0, "cancel": 0, "credit_note": 1,
                            "card_discount": 1, "leave": 0, "service": 0, "total": 2}
    other_station = call(client, COUNTS, {"branch_id": settled.branch_id + 1000}, token=manager).json()
    assert other_station["data"]["total"] == 0


def test_another_companys_rows_are_never_listed(client, manager, asked, world):
    CreditNote.objects.update(company=world["other"])

    assert call(client, CN_LIST, {}, token=manager).json()["data"]["credit_notes"] == []


@pytest.mark.parametrize("url", [CN_LIST, CN_APPROVE, CLAIM_LIST, CLAIM_APPROVE, CLAIM_REVOKE, COUNTS])
def test_an_operator_token_is_refused(client, token, url):
    response = call(client, url, {}, token=token)

    assert (response.status_code, response.json()["code"]) == (403, "wrong_channel")


def test_web_decisions_record_the_web_channel(client, asked, web, claim):
    notes.post(web, f"/rental/credit-note/{asked}/approve/", {"net_amount": "5"})

    assert CreditNote.objects.get(pk=asked).decided_channel == DecisionChannel.WEB
    assert Order.objects.exists()
