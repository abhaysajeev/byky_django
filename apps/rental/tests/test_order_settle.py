"""POST /api/v1/operator/orders/settle through HTTP: the bill closed, the card
discount redeemed, the order completed and the invoice issued
(order_lifecycle_design.md 3.4) -- and every way a settle is refused."""

import copy
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.discount.models import CardDiscountClaim, CardGrade, CardType, ClaimStatus
from apps.discount.tests.conftest import make_claim, make_discount
from apps.fare.tests.test_api import shape
from apps.rental import api
from apps.rental.models import (
    Invoice,
    Order,
    OrderEvent,
    OrderItem,
    OrderItemStatus,
    OrderStatus,
    Payment,
    PaymentStatus,
)
from apps.rental.tests import test_order_api as orders
from apps.rental.tests.test_order_return import RETURN, back
from core.ids import uuid7

call, booking = orders.call, orders.booking
world, token, fresh_throttle, shop = orders.world, orders.token, orders.fresh_throttle, orders.shop

SETTLE = "/api/v1/operator/orders/settle"


@pytest.fixture
def grade(world):
    card_type = CardType.objects.create(company=world["company"], code="FAM", name="Family Card")
    return CardGrade.objects.create(company=world["company"], card_type=card_type, code="GOLD", name="Gold")


@pytest.fixture
def booked(client, world, token, shop):
    """3.1: MO 41 and DC 02 out for an hour, AED 100 down (60 cash, 40 card)."""
    request_data = booking(world, shop)
    assert call(client, orders.BOOK, request_data, token=token).json()["code"] == "ok"
    return request_data


@pytest.fixture
def returned(client, token, booked):
    """3.3: DC 02 back on time (50.00), MO 41 twelve minutes late (60.00)."""
    late = back(booked, 0, returned_at="2026-10-02 17:12:00", run_minutes=72, overtime_amount="10.00",
                total_amount="60.00")
    for request_data in (late, back(booked, 1)):
        assert call(client, RETURN, request_data, token=token).json()["code"] == "ok"
    return booked


def bill(booked, shop, **overrides):
    """3.4 without a discount: lines 110.00, VAT included (5.24 of it), net
    110.00 -- 100.00 paid down, the 10.00 still owed by card."""
    data = {
        "sync_id": str(uuid7()), "order_id": booked["sync_id"], "settled_at": "2026-10-02 17:13:30",
        "subtotal": "110.00", "tax_percentage": "5.00", "tax_amount": "5.24", "rounding_adjustment": "0.00",
        "net_amount": "110.00",
        "payments": [{"sync_id": str(uuid7()), "kind": "settlement", "payment_mode_id": shop["card"].pk,
                      "amount": "10.00", "reference_no": "4421", "paid_at": "2026-10-02 17:13:30"}],
    }
    data.update(overrides)
    return data


def settle(client, token, request_data):
    return call(client, SETTLE, request_data, token=token).json()


def test_the_design_example_settles_redeems_and_invoices(client, world, token, shop, grade, returned):
    """3.4: lines 110.00 (a replaced line not billed), 5% approved card
    discount 5.50, net 104.50 with VAT 4.98 included; 100.00 paid down, 4.50
    by card now."""
    order = Order.objects.get(pk=returned["sync_id"])
    replaced = OrderItem.objects.create(
        id=uuid7(), order=order, vehicle=shop["mo42"], status=OrderItemStatus.REPLACED, package_minutes=60,
        start_time=order.start_time, expected_end_time=order.start_time, end_time=order.start_time, base_fare=50,
    )
    approval = make_discount(grade, requires_approval=True)
    approved = make_claim(approval, shop["ahmed"], status=ClaimStatus.APPROVED, order=order)
    other = make_claim(approval, shop["ahmed"], status=ClaimStatus.PENDING, order=order)
    request_data = bill(
        returned, shop, tax_amount="4.980", net_amount="104.500",
        discount={"claim_id": str(approved.pk), "discount_percentage": "5.00", "discount_amount": "5.500"},
    )
    request_data["payments"][0]["amount"] = "4.500"

    body = settle(client, token, request_data)

    assert body["code"] == "ok", body
    data = body["data"]
    assert (data["status"], data["payment_status"], data["completed_at"]) == (
        "completed", "paid", "2026-10-02 17:13:30")
    assert (data["subtotal"], data["tax_amount"], data["rounding_adjustment"], data["net_amount"]) == (
        "110.000", "4.980", "0.000", "104.500")
    assert data["discount"] == {"claim_id": str(approved.pk), "discount_percentage": "5.00",
                                "discount_amount": "5.500"}
    assert (data["paid_amount"], data["balance_due"]) == ("104.500", "0.000")
    assert data["invoice"] == {"invoice_no": returned["order_no"], "issued_at": "2026-10-02 17:13:30",
                               "net_amount": "104.500"}

    approved.refresh_from_db()
    assert (approved.status, approved.bill_amount, approved.discount_amount, approved.net_amount) == (
        ClaimStatus.REDEEMED, Decimal("110.000"), Decimal("5.500"), Decimal("104.500"))
    assert approved.redeemed_at is not None
    other.refresh_from_db()
    assert (other.status, other.decided_at is not None) == (ClaimStatus.CANCELLED, True)

    invoice = Invoice.objects.get()
    assert (invoice.invoice_no, invoice.net_amount, invoice.discount_amount) == (
        returned["order_no"], Decimal("104.500"), Decimal("5.500"))
    assert (invoice.branch_name, invoice.customer_name) == ("Abu Dhabi Corniche 1", "Ahmed Al Mansoori")
    assert sorted((i.vehicle_name, i.run_minutes, i.total_amount) for i in invoice.items.all()) == [
        ("DC 02", 60, Decimal("50.000")), ("MO 41", 72, Decimal("60.000"))]
    assert not invoice.items.filter(order_item=replaced).exists()
    assert [(p["mode"], p["kind"], p["amount"]) for p in invoice.payments] == [
        ("Cash", "advance", "60.000"), ("Card", "advance", "40.000"), ("Card", "settlement", "4.500")]
    assert OrderEvent.objects.get(pk=request_data["sync_id"]).action == "settle"


def test_an_automatic_discount_is_recorded_and_an_overpayment_refunded(client, world, token, shop, grade,
                                                                       returned):
    """Lines 110.00 less 20% automatic 22.00 = 88.00, no VAT; 100.00 was paid
    down, so 12.00 goes back."""
    automatic = make_discount(grade, percent="20")
    request_data = bill(
        returned, shop, tax_percentage="0.00", tax_amount="0.000", net_amount="88.000",
        discount={"card_discount_id": automatic.pk, "card_number": "FAM-0091", "discount_percentage": "20.00",
                  "discount_amount": "22.000"},
        payments=[{"sync_id": str(uuid7()), "kind": "refund", "payment_mode_id": shop["cash"].pk,
                   "amount": "12.000", "reference_no": "R-17", "reference_date": "2026-10-02",
                   "paid_at": "2026-10-02 17:13:30"}],
    )

    data = settle(client, token, request_data)["data"]

    claim = CardDiscountClaim.objects.get(card_discount=automatic)
    assert (claim.status, claim.requires_approval, claim.card_number, claim.discount_amount) == (
        ClaimStatus.REDEEMED, False, "FAM-0091", Decimal("22.000"))
    assert (str(claim.order_id), claim.redeemed_at is not None) == (returned["sync_id"], True)
    assert (data["discount"]["claim_id"], data["amount_refunded"], data["paid_amount"]) == (
        str(claim.pk), "12.000", "88.000")
    # The Swagger example is exactly what a tablet gets.
    assert shape(data) == shape(api._SETTLED_SAMPLE)


def test_a_direct_rental_is_returned_then_paid_at_settle(client, world, token, shop):
    request_data = booking(world, shop, payments=[], is_direct_bill=True)
    call(client, orders.BOOK, request_data, token=token)
    for index in (0, 1):
        call(client, RETURN, back(request_data, index), token=token)

    body = settle(client, token, bill(request_data, shop, subtotal="100.000", tax_amount="4.760", net_amount="100.000",
                                      payments=[{"sync_id": str(uuid7()), "kind": "settlement",
                                                 "payment_mode_id": shop["cash"].pk, "amount": "100.000",
                                                 "paid_at": "2026-10-02 17:01:00"}]))

    assert (body["code"], body["data"]["payment_status"], body["data"]["paid_amount"]) == ("ok", "paid", "100.000")


def test_a_zero_bill_settles(client, world, token, shop, booked):
    """Every vehicle came off the order: nothing billed, the advance handed back."""
    OrderItem.objects.filter(order_id=booked["sync_id"]).update(status=OrderItemStatus.REMOVED)

    body = settle(client, token, bill(
        booked, shop, subtotal="0.000", tax_amount="0.000", net_amount="0.000",
        payments=[{"sync_id": str(uuid7()), "kind": "refund", "payment_mode_id": shop["cash"].pk,
                   "amount": "100.000", "paid_at": "2026-10-02 17:13:30"}],
    ))

    assert (body["code"], body["data"]["net_amount"], body["data"]["invoice"]["net_amount"]) == ("ok", "0.000", "0.000")
    assert Invoice.objects.get().items.count() == 0


def test_a_resent_settle_is_a_duplicate_and_a_changed_one_a_conflict(client, token, shop, returned):
    request_data = bill(returned, shop)
    first = settle(client, token, request_data)

    assert settle(client, token, request_data) == {**first, "code": "duplicate", "message": "Already recorded."}
    changed = copy.deepcopy(request_data)
    changed["settled_at"] = "2026-10-02 17:20:00"
    assert settle(client, token, changed)["code"] == "sync_id_conflict"
    assert (Invoice.objects.count(), Payment.objects.filter(kind="settlement").count()) == (1, 1)


def test_a_settled_order_cannot_be_settled_again(client, token, shop, returned):
    settle(client, token, bill(returned, shop))

    assert settle(client, token, bill(returned, shop))["code"] == "order_closed"


# -- Refusals --------------------------------------------------------------------------


def assert_nothing_settled(booked):
    order = Order.objects.get(pk=booked["sync_id"])
    assert (order.status, order.payment_status, order.net_amount) == (OrderStatus.ACTIVE, PaymentStatus.PENDING, None)
    assert (Invoice.objects.count(), Payment.objects.count()) == (0, 2)


def test_every_vehicle_must_be_back(client, token, shop, booked):
    call(client, RETURN, back(booked, 1), token=token)

    assert settle(client, token, bill(booked, shop))["code"] == "items_still_out"
    assert_nothing_settled(booked)


@pytest.mark.parametrize(("overrides", "names"), [
    ({"subtotal": "100.00", "net_amount": "100.00"}, "subtotal"),
    ({"net_amount": "111.00"}, "net_amount"),
    # VAT is included in the fares: a net with VAT added on top is wrong.
    ({"net_amount": "115.24"}, "VAT is included"),
], ids=["subtotal-not-the-lines", "net-does-not-add-up", "vat-added-on-top"])
def test_figures_that_do_not_agree_are_refused(client, token, shop, returned, overrides, names):
    body = settle(client, token, bill(returned, shop, **overrides))

    assert body["code"] == "amount_mismatch" and names in body["message"]
    assert_nothing_settled(returned)


def test_a_bill_not_paid_in_full_is_refused_and_nothing_written(client, token, shop, returned):
    request_data = bill(returned, shop)
    request_data["payments"][0]["amount"] = "5.00"

    body = settle(client, token, request_data)

    assert body["code"] == "balance_not_settled" and "105.00" in body["message"]
    assert_nothing_settled(returned)


@pytest.mark.parametrize(("status", "code"), [
    (ClaimStatus.PENDING, "card_claim_not_approved"),
    (ClaimStatus.REJECTED, "card_claim_not_approved"),
    (None, "unknown_card_claim"),
], ids=["pending", "rejected", "not-this-orders"])
def test_only_an_approved_request_on_this_order_can_be_redeemed(client, world, token, shop, grade, returned,
                                                                  status, code):
    approval = make_discount(grade, requires_approval=True)
    order = Order.objects.get(pk=returned["sync_id"]) if status else None
    claim = make_claim(approval, shop["ahmed"], status=status or ClaimStatus.APPROVED, order=order,
                       when=timezone.now())
    request_data = bill(returned, shop, net_amount="104.50",
                        discount={"claim_id": str(claim.pk), "discount_percentage": "5.00",
                                  "discount_amount": "5.50"})
    request_data["payments"][0]["amount"] = "4.50"

    assert settle(client, token, request_data)["code"] == code
    claim.refresh_from_db()
    assert claim.status == (status or ClaimStatus.APPROVED)
    assert_nothing_settled(returned)


def test_an_unknown_card_discount_is_refused(client, token, shop, returned):
    request_data = bill(returned, shop, net_amount="104.50",
                        discount={"card_discount_id": 999999, "discount_percentage": "5.00",
                                  "discount_amount": "5.50"})
    request_data["payments"][0]["amount"] = "4.50"

    assert settle(client, token, request_data)["code"] == "unknown_card_discount"


@pytest.mark.parametrize("discount", [
    {"discount_percentage": "10.00", "discount_amount": "11.00"},
    {"claim_id": str(uuid7()), "card_discount_id": 1, "discount_percentage": "10.00", "discount_amount": "11.00"},
    {"card_discount_id": 1, "discount_percentage": "0", "discount_amount": "0.00"},
], ids=["neither", "both", "zero-percent"])
def test_the_discount_names_one_source(client, token, shop, returned, discount):
    body = settle(client, token, bill(returned, shop, discount=discount))

    assert body["code"] == "invalid_request" and "discount" in body["data"]["errors"]


def test_a_payment_kind_other_than_settlement_or_refund_is_refused(client, token, shop, returned):
    request_data = bill(returned, shop)
    request_data["payments"][0]["kind"] = "advance"

    body = settle(client, token, request_data)

    assert body["code"] == "invalid_request" and "payments" in body["data"]["errors"]


def test_the_settle_endpoint_is_in_the_api_docs(client, db):
    schema = client.get("/api/schema/").content.decode()

    assert "/api/v1/{app}/orders/settle" in schema and "balance_not_settled" in schema
