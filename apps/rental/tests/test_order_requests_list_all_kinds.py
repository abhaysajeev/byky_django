"""orders/requests/list shows every kind the station sent for a decision in
one list: order requests, credit-note requests, and card discounts that went
for approval -- sorted and paged together."""

import datetime

import pytest

from apps.discount.models import ClaimStatus
from apps.discount.tests.conftest import make_claim
from apps.rental import api, credit_notes
from apps.rental import requests as order_requests
from apps.rental.models import CreditNote
from apps.rental.tests import test_credit_notes as notes
from apps.rental.tests import test_order_requests as requests_tests
from apps.rental.tests.test_order_api import call
from core.ids import uuid7

world, token, fresh_throttle, shop = notes.world, notes.token, notes.fresh_throttle, notes.shop
booked, returned, settled = notes.booked, notes.returned, notes.settled
card = requests_tests.card

LIST = "/api/v1/operator/orders/requests/list"
DUBAI = datetime.timezone(datetime.timedelta(hours=4))


def at(hh, mm=0):
    return datetime.datetime(2026, 10, 6, hh, mm, tzinfo=DUBAI)


def listed(client, token, **request_data):
    body = call(client, LIST, request_data, token=token).json()
    assert body["code"] == "ok", body
    return body["data"]


@pytest.fixture
def three(client, token, settled, card, shop):
    """On the settled order: a reprint (09:00), a credit note (10:00), and a
    card discount sent for approval (11:00)."""
    reprint = call(client, "/api/v1/operator/orders/requests", {
        "sync_id": str(uuid7()), "order_id": str(settled.pk), "kind": "reprint",
        "requested_at": "2026-10-06 09:00:00"}, token=token).json()["data"]
    note = call(client, notes.REQUEST, notes.ask(settled, requested_at="2026-10-06 10:00:00"),
                token=token).json()["data"]
    claim = make_claim(card, shop["ahmed"], order=settled, branch=settled.branch, when=at(11), card_number="FAM-1",
                       rms_installation_id="till-1")
    return {"reprint": reprint["request_sync_id"], "note": note["credit_note_id"], "claim": str(claim.pk)}


def test_every_kind_is_in_one_list_newest_first(client, token, three):
    data = listed(client, token)

    assert [(r["kind"], r["request_sync_id"]) for r in data["requests"]] == [
        ("card_discount", three["claim"]), ("credit_note", three["note"]), ("reprint", three["reprint"])]
    assert data["total"] == 3
    claim_row, note_row, reprint_row = data["requests"]
    assert reprint_row["details"] is None
    assert note_row["details"] == {"credit_note_no": None, "net_amount": None}
    assert (claim_row["details"]["card_type"], claim_row["details"]["card_number"]) == ("Family Card", "FAM-1")
    assert note_row["reason"] == "Chain broke after 10 minutes"
    assert set(note_row) == set(reprint_row) == set(claim_row) == set(api._LIST_CREDIT_NOTE_SAMPLE)


def test_kind_and_pending_narrow_across_kinds(client, token, three, world):
    credit_notes.approve(None, three["note"], [world["company"].pk], "20")

    assert [r["kind"] for r in listed(client, token, kind="credit_note")["requests"]] == ["credit_note"]
    assert [r["kind"] for r in listed(client, token, kind="card_discount")["requests"]] == ["card_discount"]
    assert [r["kind"] for r in listed(client, token, kind="reprint")["requests"]] == ["reprint"]
    waiting = listed(client, token, pending=True)["requests"]
    assert sorted(r["kind"] for r in waiting) == ["card_discount", "reprint"]
    issued = listed(client, token, kind="credit_note")["requests"][0]
    assert (issued["status"], issued["details"]["net_amount"]) == ("approved", "20.000")


def test_the_dates_filter_every_kind(client, token, three):
    rows = listed(client, token, from_date="2026-10-06", to_date="2026-10-06")["requests"]

    assert len(rows) == 3
    assert listed(client, token, from_date="2026-10-07")["requests"] == []


def test_web_issued_notes_and_claims_without_approval_are_not_listed(client, token, settled, card, shop, world):
    from apps.discount.models import CardGrade
    from apps.discount.tests.conftest import make_discount

    # Another grade: one card discount per grade over a period (card_discount_no_overlap).
    silver = CardGrade.objects.create(company=world["company"], card_type=card.card_grade.card_type,
                                      code="SIL", name="Silver")
    automatic = make_discount(silver, requires_approval=False)
    make_claim(automatic, shop["ahmed"], order=settled, branch=settled.branch, status=ClaimStatus.REDEEMED)
    credit_notes.issue_directly(None, settled.invoice.pk, [world["company"].pk], "5", "web")

    assert listed(client, token)["requests"] == []
    assert CreditNote.objects.count() == 1


def test_another_stations_are_not_listed(client, token, three, world):
    CreditNote.objects.update(branch=world["adc2"])

    assert "credit_note" not in {r["kind"] for r in listed(client, token)["requests"]}


def test_paging_runs_across_kinds(client, token, three, monkeypatch):
    monkeypatch.setattr(order_requests, "PAGE_SIZE", 2)

    first, second = listed(client, token), listed(client, token, page=2)

    assert [r["kind"] for r in first["requests"]] == ["card_discount", "credit_note"]
    assert [r["kind"] for r in second["requests"]] == ["reprint"]
    assert (first["pages"], first["total"]) == (2, 3)


def test_an_unknown_kind_is_refused(client, token):
    response = call(client, LIST, {"kind": "refund"}, token=token)

    assert (response.status_code, response.json()["code"]) == (400, "invalid_request")


def test_mine_shows_only_what_this_tablet_sent(client, world, token, three):
    from apps.devices.models import Device
    from apps.rental.models import OrderRequest
    from apps.rental.tests.test_order_api import sign_in_tablet

    other = sign_in_tablet(client, world, code="OPR002", installation_id="till-2", branch=world["adc1"])
    OrderRequest.objects.filter(pk=three["reprint"]).update(device=Device.objects.get(installation_id="till-2"))

    mine = [r["kind"] for r in listed(client, token, mine=True)["requests"]]
    theirs = [r["kind"] for r in listed(client, other, mine=True)["requests"]]
    station = [r["kind"] for r in listed(client, other)["requests"]]

    assert mine == ["card_discount", "credit_note"]
    assert theirs == ["reprint"]
    assert station == ["card_discount", "credit_note", "reprint"]
    assert listed(client, token, mine=True, kind="reprint")["requests"] == []
