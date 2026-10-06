"""Credit notes: the tablet asks (and may cancel) on a settled order, the web
approves with the amount or rejects, or issues one directly on an invoice; the
tablet reads where they stand by order id. Nothing changes the order, its
invoice or its payments."""

from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction
from django.test import Client

from apps.portal.models import Role, RolePermission
from apps.portal.services import grant_all
from apps.rental import credit_notes
from apps.rental.models import CreditNote, Invoice, Order, OrderEvent, Payment
from apps.rental.tests import test_order_api as orders
from apps.rental.tests import test_order_settle as settling
from core.enums import Channel
from core.ids import uuid7
from core.models import User

call, booking = orders.call, orders.booking
world, token, fresh_throttle, shop = orders.world, orders.token, orders.fresh_throttle, orders.shop
booked, returned = settling.booked, settling.returned

REQUEST = "/api/v1/operator/orders/credit-notes"
CANCEL = "/api/v1/operator/orders/credit-notes/cancel"
STATUS = "/api/v1/operator/orders/credit-notes/status"
PASSWORD = "Byky#2026"


@pytest.fixture
def settled(client, token, shop, returned):
    """The design example settled: net 110.00, VAT 5% included, invoiced."""
    assert settling.settle(client, token, settling.bill(returned, shop))["code"] == "ok"
    return Order.objects.get(pk=returned["sync_id"])


@pytest.fixture
def web(world):
    role = Role.objects.create(company=world["company"], name="Back office")
    grant_all(role)
    User.objects.create_user("sara.k", PASSWORD, display_name="Sara K", company=world["company"], role=role,
                             allowed_channels=[Channel.WEB])
    browser = Client()
    browser.post("/login/", {"username": "sara.k", "password": PASSWORD})
    browser.role = role
    return browser


def ask(order, **fields):
    return {"sync_id": str(uuid7()), "order_id": str(order.pk), "requested_at": "2026-10-06 10:00:00",
            "reason": "Chain broke after 10 minutes", **fields}


def post(browser, url, data=None):
    return browser.post(url, data or {}, content_type="application/json")


# -- Tablet: request ------------------------------------------------------------------


def test_a_tablet_asks_and_its_sync_id_is_the_credit_note(client, token, settled):
    request_data = ask(settled)
    body = call(client, REQUEST, request_data, token=token).json()

    assert body["code"] == "ok", body
    note = CreditNote.objects.get()
    assert str(note.pk) == request_data["sync_id"] == body["data"]["credit_note_id"]
    assert (note.status, note.source, note.invoice, note.reason) == (
        "pending", "device", settled.invoice, "Chain broke after 10 minutes")
    assert body["data"]["net_amount"] is None and body["data"]["order_no"] == settled.order_no
    assert OrderEvent.objects.get(pk=request_data["sync_id"]).action == "credit_note_request"


def test_a_resent_request_is_a_duplicate_and_a_changed_one_a_conflict(client, token, settled):
    request_data = ask(settled)
    call(client, REQUEST, request_data, token=token)

    assert call(client, REQUEST, request_data, token=token).json()["code"] == "duplicate"
    assert call(client, REQUEST, {**request_data, "reason": "Other"}, token=token).json()["code"] == "sync_id_conflict"
    assert CreditNote.objects.count() == 1


def test_only_a_settled_order_can_ask(client, token, booked):
    order = Order.objects.get(pk=booked["sync_id"])
    assert call(client, REQUEST, ask(order), token=token).json()["code"] == "order_not_settled"


def test_an_unknown_order_is_not_synced_yet(client, token, settled):
    body = call(client, REQUEST, ask(settled, order_id=str(uuid7())), token=token).json()
    assert (body["code"], body["data"]["retry"]) == ("order_not_synced", True)


def test_a_waiting_request_blocks_another_but_a_rejected_one_does_not(client, token, settled, web):
    first = ask(settled)
    call(client, REQUEST, first, token=token)
    assert call(client, REQUEST, ask(settled), token=token).json()["code"] == "credit_note_pending"

    assert post(web, f"/rental/credit-note/{first['sync_id']}/reject/", {"remarks": "Not our fault"}).json()["ok"]
    assert call(client, REQUEST, ask(settled), token=token).json()["code"] == "ok"


def test_an_issued_credit_note_blocks_another_request(client, token, settled, web):
    first = ask(settled)
    call(client, REQUEST, first, token=token)
    post(web, f"/rental/credit-note/{first['sync_id']}/approve/", {"net_amount": "21.00"})

    assert call(client, REQUEST, ask(settled), token=token).json()["code"] == "credit_note_issued"


# -- Tablet: cancel --------------------------------------------------------------------


def test_a_tablet_cancels_its_waiting_request_and_may_ask_again(client, token, settled):
    first = ask(settled)
    call(client, REQUEST, first, token=token)
    cancel = {"sync_id": str(uuid7()), "credit_note_id": first["sync_id"], "cancelled_at": "2026-10-06 10:05:00"}

    body = call(client, CANCEL, cancel, token=token).json()

    assert (body["code"], body["data"]["status"]) == ("ok", "cancelled")
    assert call(client, CANCEL, cancel, token=token).json()["code"] == "duplicate"
    assert call(client, REQUEST, ask(settled), token=token).json()["code"] == "ok"


def test_a_decided_request_cannot_be_cancelled(client, token, settled, web):
    first = ask(settled)
    call(client, REQUEST, first, token=token)
    post(web, f"/rental/credit-note/{first['sync_id']}/approve/", {"net_amount": "21.00"})
    cancel = {"sync_id": str(uuid7()), "credit_note_id": first["sync_id"], "cancelled_at": "2026-10-06 10:05:00"}

    assert call(client, CANCEL, cancel, token=token).json()["code"] == "credit_note_closed"


# -- Tablet: status ----------------------------------------------------------------------


def test_status_by_order_shows_tablet_and_web_credit_notes(client, token, settled, web):
    request_data = ask(settled)
    call(client, REQUEST, request_data, token=token)
    post(web, f"/rental/credit-note/{request_data['sync_id']}/approve/", {"net_amount": "21.00", "remarks": "Half"})

    body = call(client, STATUS, {"order_ids": [str(settled.pk), str(uuid7())]}, token=token).json()

    assert body["code"] == "ok", body
    [note] = body["data"]["credit_notes"]
    assert (note["status"], note["source"], note["credit_note_no"]) == ("approved", "device", f"{settled.order_no}CN")
    assert (note["net_amount"], note["tax_amount"], note["taxable_amount"]) == ("21.00", "1.00", "20.00")
    assert note["decision_remark"] == "Half"


def test_status_sees_a_credit_note_issued_on_the_web(client, token, settled, web):
    response = post(web, f"/rental/credit-note/issue/{settled.invoice.pk}/", {"net_amount": "10.50", "reason": "Call"})
    assert response.json()["ok"], response.json()

    [note] = call(client, STATUS, {"order_ids": [str(settled.pk)]}, token=token).json()["data"]["credit_notes"]
    assert (note["source"], note["status"], note["net_amount"], note["tax_amount"]) == ("web", "approved", "10.50", "0.50")


def test_status_needs_order_ids(client, token):
    body = call(client, STATUS, {"order_ids": []}, token=token).json()
    assert body["code"] == "invalid_request"


# -- Web: approve, reject, issue --------------------------------------------------------


def test_approving_issues_it_with_the_vat_inside_and_leaves_the_money_alone(client, token, settled, web):
    request_data = ask(settled)
    call(client, REQUEST, request_data, token=token)
    payments_before = list(Payment.objects.filter(order=settled).values_list("pk", "amount"))

    body = post(web, f"/rental/credit-note/{request_data['sync_id']}/approve/", {"net_amount": "21"}).json()

    assert body["ok"], body
    note = CreditNote.objects.get()
    assert (note.status, note.credit_note_no, note.net_amount, note.tax_amount, note.taxable_amount) == (
        "approved", f"{settled.order_no}CN", Decimal("21.00"), Decimal("1.00"), Decimal("20.00"))
    assert note.decided_by.username == "sara.k"
    settled.refresh_from_db()
    assert (settled.net_amount, settled.status) == (Decimal("110.00"), "completed")
    assert Invoice.objects.get(order=settled).net_amount == Decimal("110.00")
    assert list(Payment.objects.filter(order=settled).values_list("pk", "amount")) == payments_before


@pytest.mark.parametrize("amount", ["0", "-5", "110.01", "abc"])
def test_the_amount_must_be_above_zero_and_not_above_the_order(client, token, settled, web, amount):
    request_data = ask(settled)
    call(client, REQUEST, request_data, token=token)

    response = post(web, f"/rental/credit-note/{request_data['sync_id']}/approve/", {"net_amount": amount})

    assert response.status_code == 400
    assert CreditNote.objects.get().status == "pending"


def test_the_whole_order_can_be_credited(client, token, settled, web):
    request_data = ask(settled)
    call(client, REQUEST, request_data, token=token)
    assert post(web, f"/rental/credit-note/{request_data['sync_id']}/approve/", {"net_amount": "110.00"}).json()["ok"]


def test_direct_issue_is_blocked_while_a_request_waits(client, token, settled, web):
    call(client, REQUEST, ask(settled), token=token)

    response = post(web, f"/rental/credit-note/issue/{settled.invoice.pk}/", {"net_amount": "5.00"})

    assert response.status_code == 400
    assert "approve or reject it instead" in response.json()["errors"][0]["message"]


@pytest.mark.parametrize(("url", "flag"), [
    ("/rental/credit-note/{note}/approve/", "can_approve"),
    ("/rental/credit-note/issue/{invoice}/", "can_create"),
])
def test_web_actions_need_their_permission(client, token, settled, web, url, flag):
    request_data = ask(settled)
    call(client, REQUEST, request_data, token=token)
    RolePermission.objects.filter(role=web.role, page__code="rental.credit_note").update(**{flag: False})

    response = post(web, url.format(note=request_data["sync_id"], invoice=settled.invoice.pk), {"net_amount": "5"})

    assert response.status_code == 403


def test_vat_split_adds_back_to_net():
    assert credit_notes.vat_split(Decimal("21.00"), Decimal("5")) == (Decimal("1.00"), Decimal("20.00"))
    assert credit_notes.vat_split(Decimal("10.01"), Decimal("5")) == (Decimal("0.48"), Decimal("9.53"))
    assert credit_notes.vat_split(Decimal("10.00"), Decimal("0")) == (Decimal("0.00"), Decimal("10.00"))


# -- Web screens --------------------------------------------------------------------------


def test_the_list_puts_waiting_requests_first_and_the_detail_shows_both_links(client, token, settled, web):
    request_data = ask(settled)
    call(client, REQUEST, request_data, token=token)

    listing = web.get("/rental/credit-note/list/").content.decode()
    assert "Request" in listing and settled.invoice.invoice_no in listing and "Pending" in listing

    page = web.get(f"/rental/credit-note/{request_data['sync_id']}/").content.decode()
    assert f'href="/rental/invoice/{settled.invoice.pk}/"' in page
    assert f'href="/rental/order/{settled.pk}/"' in page
    assert 'data-scr-modal-open="credit-note-approve"' in page


def test_order_and_invoice_pages_link_to_the_credit_note(client, token, settled, web):
    request_data = ask(settled)
    call(client, REQUEST, request_data, token=token)
    post(web, f"/rental/credit-note/{request_data['sync_id']}/approve/", {"net_amount": "21.00"})
    link = f'class="scr-link-blue" href="/rental/credit-note/{request_data["sync_id"]}/"'

    assert link in web.get(f"/rental/order/{settled.pk}/").content.decode()
    invoice_page = web.get(f"/rental/invoice/{settled.invoice.pk}/").content.decode()
    assert link in invoice_page and "Issue credit note" not in invoice_page
    receipt = web.get(f"/rental/invoice/{settled.invoice.pk}/receipt/").content.decode()
    assert f"{settled.order_no}CN" in receipt and "AED 21.00" in receipt


def test_an_issued_credit_note_prints(client, token, settled, web):
    post(web, f"/rental/credit-note/issue/{settled.invoice.pk}/", {"net_amount": "21.00"})
    note = CreditNote.objects.get()

    receipt = web.get(f"/rental/credit-note/{note.pk}/receipt/").content.decode()

    assert "TAX CREDIT NOTE" in receipt and "AED 21.00" in receipt and settled.invoice.invoice_no in receipt


def test_one_live_credit_note_per_order_is_enforced_by_the_database(client, token, settled):
    call(client, REQUEST, ask(settled), token=token)
    with pytest.raises(IntegrityError), transaction.atomic():
        CreditNote.objects.create(id=uuid7(), company=settled.company, branch=settled.branch, order=settled,
                                  invoice=settled.invoice, source="device", status="pending")
