"""The manager app's requests/list shows every kind waiting for a manager in
one list -- order requests, credit-note requests, card discounts that need
approval -- sorted and paged together. Each is still decided with its own call."""

import datetime

import pytest

from apps.discount.tests.conftest import make_claim
from apps.rental import api
from apps.rental import requests as order_requests
from apps.rental.models import CreditNote
from apps.rental.tests import test_manager_approvals as approvals
from apps.rental.tests.test_order_api import call
from core.ids import uuid7

world, token, fresh_throttle, shop = approvals.world, approvals.token, approvals.fresh_throttle, approvals.shop
booked, returned, settled = approvals.booked, approvals.returned, approvals.settled
manager, card = approvals.manager, approvals.card

LIST = "/api/v1/manager/requests/list"
DUBAI = datetime.timezone(datetime.timedelta(hours=4))


def listed(client, manager, **request_data):
    body = call(client, LIST, request_data, token=manager).json()
    assert body["code"] == "ok", body
    return body["data"]


@pytest.fixture
def three(client, token, settled, card, shop):
    """On the settled order: a reprint (09:00), a credit note (10:00), a card
    discount sent for approval (11:00)."""
    reprint = call(client, "/api/v1/operator/orders/requests", {
        "sync_id": str(uuid7()), "order_id": str(settled.pk), "kind": "reprint",
        "requested_at": "2026-10-06 09:00:00"}, token=token).json()["data"]["request_sync_id"]
    note = call(client, "/api/v1/operator/orders/credit-notes", {
        "sync_id": str(uuid7()), "order_id": str(settled.pk), "requested_at": "2026-10-06 10:00:00",
        "reason": "Chain broke"}, token=token).json()["data"]["credit_note_id"]
    claim = make_claim(card, shop["ahmed"], order=settled, branch=settled.branch, card_number="FAM-1",
                       card_photo="https://photos.test/fam-1.jpg",
                       when=datetime.datetime(2026, 10, 6, 11, 0, tzinfo=DUBAI))
    return {"reprint": reprint, "note": note, "claim": str(claim.pk)}


def test_every_kind_in_one_list_newest_first(client, manager, three, settled):
    data = listed(client, manager)

    assert [(r["kind"], r["request_sync_id"]) for r in data["requests"]] == [
        ("card_discount", three["claim"]), ("credit_note", three["note"]), ("reprint", three["reprint"])]
    assert data["total"] == 3
    claim_row, note_row, reprint_row = data["requests"]
    assert set(reprint_row["details"]) == {"invoice_no", "invoice_net_amount"}
    assert note_row["details"]["invoice_no"] == settled.order_no and note_row["details"]["net_amount"] is None
    assert claim_row["details"]["card_photo"] == "https://photos.test/fam-1.jpg"
    for row in data["requests"]:
        assert row["station"] == settled.branch.name and row["order"]["order_no"] == settled.order_no
    assert set(note_row) == set(reprint_row) == set(claim_row) == set(api._MANAGER_CREDIT_NOTE_ROW)


def test_kind_pending_and_station_filter_every_kind(client, manager, three, settled, world):
    assert [r["kind"] for r in listed(client, manager, kind="credit_note")["requests"]] == ["credit_note"]
    assert [r["kind"] for r in listed(client, manager, kind="card_discount")["requests"]] == ["card_discount"]
    assert [r["kind"] for r in listed(client, manager, kind="reprint")["requests"]] == ["reprint"]
    assert len(listed(client, manager, pending=True)["requests"]) == 3
    assert listed(client, manager, branch_id=world["adc2"].pk)["requests"] == []
    assert len(listed(client, manager, from_date="2026-10-06", to_date="2026-10-06")["requests"]) == 3


def test_decided_with_its_own_call_then_shown_decided(client, manager, three):
    approved = call(client, "/api/v1/manager/credit-notes/approve",
                    {"credit_note_id": three["note"], "net_amount": "20"}, token=manager).json()
    assert approved["code"] == "ok", approved

    row = listed(client, manager, kind="credit_note")["requests"][0]
    assert (row["status"], row["decided_channel"], row["details"]["net_amount"]) == ("approved", "manager", "20.000")
    assert [r["kind"] for r in listed(client, manager, pending=True)["requests"]] == ["card_discount", "reprint"]


def test_another_companys_are_never_listed(client, manager, three, world):
    CreditNote.objects.update(company=world["other"])

    assert "credit_note" not in {r["kind"] for r in listed(client, manager)["requests"]}


def test_paging_runs_across_kinds(client, manager, three, monkeypatch):
    monkeypatch.setattr(order_requests, "PAGE_SIZE", 2)

    first, second = listed(client, manager), listed(client, manager, page=2)

    assert [r["kind"] for r in first["requests"]] == ["card_discount", "credit_note"]
    assert [r["kind"] for r in second["requests"]] == ["reprint"]
