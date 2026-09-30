"""Card Discount Approval and Redemption History -- claims are written by the
device APIs (next pass); here they are made directly."""

import csv
import datetime
import io
import uuid

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.discount.models import ClaimStatus
from apps.discount.tests.conftest import make_claim, make_discount, make_order, post
from apps.portal.models import RolePermission


@pytest.fixture
def pending(world):
    discount = make_discount(world["our"]["grade"], requires_approval=True)
    return make_claim(discount, world["our"]["customer"], card_number="4455",
                      branch=world["our"]["branch"], order=make_order(world["our"]["customer"], order_no="ORD-1001"), bill_amount="120.00")


def test_the_queue_shows_pending_requests_by_tab(client_in, world, pending):
    body = client_in.get("/discount/approval/list/").content.decode()
    assert "Ours Anil" in body and "4455" in body
    assert "No approved requests" in client_in.get("/discount/approval/list/?tab=approved").content.decode()


def test_the_detail_shows_the_request_and_the_customers_history(client_in, world, pending):
    earlier = make_claim(pending.card_discount, pending.customer, status=ClaimStatus.REDEEMED,
                         when=timezone.now() - datetime.timedelta(days=2), order=make_order(pending.customer, order_no="ORD-0900"))
    body = client_in.get(f"/discount/approval/{pending.pk}/").content.decode()
    assert "ORD-1001" in body and "ORD-0900" in body            # this request and the earlier one
    assert "No photo" in body
    assert "used 1" in body                                     # one redemption of this discount
    assert f"/discount/approval/{earlier.pk}/" in body


@pytest.mark.parametrize("action, status", [("approve", ClaimStatus.APPROVED), ("reject", ClaimStatus.REJECTED)])
def test_a_pending_request_is_decided_once(client_in, world, pending, action, status):
    body = post(client_in, f"/discount/approval/{pending.pk}/{action}/", {"remarks": "  checked card  "}).json()
    assert body["ok"] is True
    pending.refresh_from_db()
    assert pending.status == status and pending.remarks == "checked card"
    assert pending.decided_by.username == "sara.k" and pending.decided_at is not None

    again = post(client_in, f"/discount/approval/{pending.pk}/approve/", {})
    assert again.status_code == 400
    assert again.json()["errors"][0]["message"] == "This request is already decided."


def test_deciding_needs_the_approve_permission(client_in, world, pending):
    RolePermission.objects.filter(role=client_in.role, page__code="discount.approval").update(can_approve=False)
    assert post(client_in, f"/discount/approval/{pending.pk}/approve/", {}).status_code == 403
    assert "data-decision" not in client_in.get(f"/discount/approval/{pending.pk}/").content.decode()


def test_another_companys_request_is_not_found(client_in, world):
    discount = make_discount(world["their"]["grade"], requires_approval=True)
    theirs = make_claim(discount, world["their"]["customer"])
    assert client_in.get(f"/discount/approval/{theirs.pk}/").status_code == 404
    assert post(client_in, f"/discount/approval/{theirs.pk}/approve/", {}).status_code == 404


def test_the_database_keeps_a_claims_status_consistent(world):
    discount = make_discount(world["our"]["grade"], requires_approval=True)
    with pytest.raises(IntegrityError), transaction.atomic():
        make_claim(discount, world["our"]["customer"], id=uuid.uuid4(), status=ClaimStatus.APPROVED,
                   decided_at=None)
    auto = make_discount(world["our"]["grade"], start=datetime.date(2027, 1, 1), end=datetime.date(2027, 1, 31))
    with pytest.raises(IntegrityError), transaction.atomic():
        make_claim(auto, world["our"]["customer"], status=ClaimStatus.PENDING)   # automatic never waits


def test_redemption_history_lists_redeemed_claims_and_filters(client_in, world, pending):
    auto = make_discount(world["our"]["grade"], start=datetime.date(2027, 1, 1), end=datetime.date(2027, 1, 31))
    make_claim(auto, world["our"]["customer"], status=ClaimStatus.REDEEMED, order=make_order(world["our"]["customer"], order_no="ORD-2002"),
               branch=world["our"]["branch"])
    body = client_in.get("/discount/redemption/list/").content.decode()
    assert "ORD-2002" in body and "ORD-1001" not in body          # the pending one is not a redemption
    assert "ORD-2002" not in client_in.get("/discount/redemption/list/?q=nobody").content.decode()


def test_redemption_export_matches_the_filters(client_in, world):
    auto = make_discount(world["our"]["grade"])
    make_claim(auto, world["our"]["customer"], status=ClaimStatus.REDEEMED, order=make_order(world["our"]["customer"], order_no="ORD-3003"))
    response = client_in.get("/discount/redemption/export/")
    rows = list(csv.reader(io.StringIO(b"".join(response.streaming_content).decode("utf-8-sig"))))
    assert rows[0][:3] == ["Redeemed", "Customer", "Phone"]
    assert rows[1][8] == "ORD-3003"

    RolePermission.objects.filter(role=client_in.role, page__code="discount.redemption").update(can_print=False)
    assert client_in.get("/discount/redemption/export/").status_code == 403


# -- Order link, device figures, photo, cancelled ------------------------------------


def test_the_detail_shows_the_order_and_the_devices_figures(client_in, world, pending):
    pending.discount_amount, pending.net_amount = "18.00", "102.00"
    pending.save(update_fields=["discount_amount", "net_amount"])
    body = client_in.get(f"/discount/approval/{pending.pk}/").content.decode()
    assert "ORD-1001" in body and "Order Start" in body
    assert "120.00" in body and "18.00" in body and "102.00" in body


@pytest.mark.parametrize("photo, expected, absent", [
    ("https://photos.example/cards/4455.jpg", '<img src="https://photos.example/cards/4455.jpg"', "Open photo"),
    ("ftp://files.example/cards/4455.jpg", "Open photo", 'alt="Card photo"'),
    ("", "No photo", "Open photo"),
])
def test_the_card_photo(client_in, world, pending, photo, expected, absent):
    pending.card_photo = photo
    pending.save(update_fields=["card_photo"])
    body = client_in.get(f"/discount/approval/{pending.pk}/").content.decode()
    assert expected in body and absent not in body


def test_cancel_pending_for_order_cancels_only_the_pending_one(world, pending):
    from apps.discount import services

    assert services.cancel_pending_for_order(pending.order) == 1
    pending.refresh_from_db()
    assert pending.status == ClaimStatus.CANCELLED and pending.decided_at is not None
    assert services.cancel_pending_for_order(pending.order) == 0


def test_cancelled_requests_have_their_tab(client_in, world, pending):
    from apps.discount import services

    services.cancel_pending_for_order(pending.order)
    body = client_in.get("/discount/approval/list/?tab=cancelled").content.decode()
    assert "ORD-1001" in body and "Cancelled" in body


def test_the_database_allows_one_pending_request_per_order(world, pending):
    with pytest.raises(IntegrityError), transaction.atomic():
        make_claim(pending.card_discount, pending.customer, order=pending.order)
