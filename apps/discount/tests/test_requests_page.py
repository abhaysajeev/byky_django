"""Card-discount requests in the web Requests page (owner, 3 Nov 2026): one
list with the operator requests; each row follows its own kind's permission;
the Card Discount Approval sidebar entry folds into Requests for anyone who can
open it. UI only -- the card actions are unchanged (test_approval.py)."""

import pytest
from django.utils import timezone

from apps.discount.models import ClaimStatus
from apps.discount.tests.conftest import make_claim, make_discount, make_order
from apps.portal.models import RolePermission
from apps.rental.models import OrderRequest, OrderRequestKind, OrderRequestStatus
from core.ids import uuid7

LIST = "/rental/request/list/"


@pytest.fixture
def card(world):
    discount = make_discount(world["our"]["grade"], requires_approval=True)
    return make_claim(discount, world["our"]["customer"], card_number="4455", branch=world["our"]["branch"],
                      order=make_order(world["our"]["customer"], order_no="ORD-CARD"), bill_amount="120.00")


@pytest.fixture
def operator_request(world):
    order = make_order(world["our"]["customer"], order_no="ORD-REQ")
    return OrderRequest.objects.create(
        id=uuid7(), company=world["ours"], order=order, branch=order.branch, kind=OrderRequestKind.DISCOUNT,
        status=OrderRequestStatus.PENDING, reason="Regular customer", requested_at=timezone.now(),
    )


def drop(client, page_code):
    RolePermission.objects.filter(role=client.role, page__code=page_code).delete()


def test_both_kinds_are_listed_together_with_tab_counts(client_in, card, operator_request):
    body = client_in.get(LIST).content.decode()

    assert "ORD-CARD" in body and "ORD-REQ" in body
    assert "Card discount" in body and f"/discount/approval/{card.pk}/" in body
    assert 'Pending <span class="scr-content-tab-count">2</span>' in body


def test_the_kind_filter_picks_one_kind(client_in, card, operator_request):
    cards_only = client_in.get(f"{LIST}?kind=card_discount").content.decode()
    discounts_only = client_in.get(f"{LIST}?kind=discount").content.decode()

    assert "ORD-CARD" in cards_only and "ORD-REQ" not in cards_only
    assert "ORD-REQ" in discounts_only and "ORD-CARD" not in discounts_only


def test_search_finds_a_card_request_by_phone(client_in, card, operator_request):
    body = client_in.get(f"{LIST}?q={card.mobile_full[-5:]}").content.decode()
    assert "ORD-CARD" in body


def test_a_used_card_discount_is_in_approved_with_a_used_badge(client_in, world):
    discount = make_discount(world["our"]["grade"], requires_approval=True)
    make_claim(discount, world["our"]["customer"], status=ClaimStatus.REDEEMED, branch=world["our"]["branch"],
               order=make_order(world["our"]["customer"], order_no="ORD-USED"), discount_amount="12.00")

    body = client_in.get(f"{LIST}?tab=approved").content.decode()

    assert "ORD-USED" in body and "Used" in body


def test_a_requests_only_user_sees_no_card_rows(client_in, card, operator_request):
    drop(client_in, "discount.approval")

    body = client_in.get(LIST).content.decode()

    assert "ORD-REQ" in body and "ORD-CARD" not in body
    assert 'value="card_discount"' not in body


def test_a_card_only_user_opens_requests_and_sees_only_card_rows(client_in, card, operator_request):
    drop(client_in, "rental.request")

    response = client_in.get(LIST)

    body = response.content.decode()
    assert response.status_code == 200
    assert "ORD-CARD" in body and "ORD-REQ" not in body


def test_a_user_with_neither_is_refused(client_in, card):
    drop(client_in, "rental.request")
    drop(client_in, "discount.approval")
    assert client_in.get(LIST).status_code == 403


def test_the_sidebar_folds_card_approval_into_requests(client_in, card):
    menu = client_in.get(LIST).content.decode()
    assert "Card Discount Approval" not in menu

    drop(client_in, "rental.request")                      # a card-only approver keeps the entry
    assert "Card Discount Approval" in client_in.get(f"/discount/approval/{card.pk}/").content.decode()


def test_the_card_detail_goes_back_to_requests(client_in, card):
    body = client_in.get(f"/discount/approval/{card.pk}/").content.decode()
    assert "/rental/request/list/?kind=card_discount&amp;tab=pending" in body
