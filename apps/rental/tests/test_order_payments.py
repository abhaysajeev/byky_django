"""POST /api/v1/operator/orders/payments through HTTP: an extra advance or a
refund, on its own -- one kind per call, allowed only while the order can
still take that money."""

import copy
from decimal import Decimal

import pytest

from apps.rental.models import Order, OrderEvent, OrderStatus, Payment
from apps.rental.tests import test_order_api as orders
from core.ids import uuid7

call, booking, sign_in_tablet = orders.call, orders.booking, orders.sign_in_tablet
world, token, fresh_throttle, shop = orders.world, orders.token, orders.fresh_throttle, orders.shop

PAYMENTS = "/api/v1/operator/orders/payments"


@pytest.fixture
def booked(client, world, token, shop):
    """3.1: two bikes out, AED 100 down (60 cash, 40 card)."""
    request_data = booking(world, shop)
    assert call(client, orders.BOOK, request_data, token=token).json()["code"] == "ok"
    return request_data


def entry(shop, mode="cash", amount="50.00", **fields):
    return {"sync_id": str(uuid7()), "payment_mode_id": shop[mode].pk, "amount": amount,
            "paid_at": "2026-10-02 16:40:00", **fields}


def money(booked, shop, kind="advance", entries=None, **overrides):
    data = {"sync_id": str(uuid7()), "order_id": booked["sync_id"], "kind": kind,
            "payments": entries if entries is not None else [entry(shop)]}
    data.update(overrides)
    return data


def set_status(booked, status):
    Order.objects.filter(pk=booked["sync_id"]).update(status=status)


def test_an_extra_advance_by_cash_and_card(client, token, shop, booked):
    request_data = money(booked, shop, entries=[
        entry(shop), entry(shop, "card", "20.00", reference_no="448901", reference_date="2026-10-02")])

    body = call(client, PAYMENTS, request_data, token=token).json()

    assert body["code"] == "ok", body
    assert (body["data"]["amount_received"], body["data"]["paid_amount"]) == ("170.00", "170.00")
    new = Payment.objects.filter(pk__in=[p["sync_id"] for p in request_data["payments"]]).order_by("amount")
    assert [(p.kind, p.mode.name, p.amount, p.reference_no) for p in new] == [
        ("advance", "Card", Decimal("20.00"), "448901"), ("advance", "Cash", Decimal("50.00"), "")]
    event = OrderEvent.objects.get(pk=request_data["sync_id"])
    assert (event.action, event.detail) == ("payment", {"kind": "advance", "total": "70.00", "count": 2})


def test_part_of_an_advance_handed_back(client, token, shop, booked):
    body = call(client, PAYMENTS, money(booked, shop, "refund", [entry(shop, amount="40.00")]),
                token=token).json()

    assert (body["code"], body["data"]["amount_refunded"], body["data"]["paid_amount"]) == ("ok", "40.00", "60.00")


def test_money_handed_back_after_a_cancel(client, token, shop, booked):
    set_status(booked, OrderStatus.CANCELLED)

    body = call(client, PAYMENTS, money(booked, shop, "refund", [entry(shop, amount="100.00")]),
                token=token).json()

    assert (body["code"], body["data"]["paid_amount"], body["data"]["payment_status"]) == ("ok", "0.00", None)


@pytest.mark.parametrize(("status", "kind"), [
    (OrderStatus.CANCELLED, "advance"),
    (OrderStatus.COMPLETED, "advance"),
    (OrderStatus.COMPLETED, "refund"),
], ids=["advance-after-cancel", "advance-after-settle", "refund-after-settle"])
def test_money_the_order_can_no_longer_take_is_refused(client, token, shop, booked, status, kind):
    set_status(booked, status)

    body = call(client, PAYMENTS, money(booked, shop, kind), token=token).json()

    assert (body["code"], body["data"]) == ("order_closed", {"retry": False})
    assert Payment.objects.count() == 2


def test_an_unknown_mode_or_a_used_payment_id_is_refused_and_nothing_written(client, token, shop, booked):
    unknown = money(booked, shop, entries=[entry(shop), {**entry(shop), "payment_mode_id": 999999}])
    used = money(booked, shop, entries=[entry(shop), {**entry(shop), "sync_id": booked["payments"][0]["sync_id"]}])

    assert call(client, PAYMENTS, unknown, token=token).json()["code"] == "unknown_payment_mode"
    assert call(client, PAYMENTS, used, token=token).json()["code"] == "payment_id_used"
    assert Payment.objects.count() == 2
    assert Order.objects.get(pk=booked["sync_id"]).amount_received == Decimal("100.00")


def test_a_payment_before_its_booking_is_retried_later(client, token, shop, booked):
    body = call(client, PAYMENTS, money(booked, shop, order_id=str(uuid7())), token=token).json()

    assert (body["code"], body["data"]) == ("order_not_synced", {"retry": True})


@pytest.mark.parametrize(("change", "field"), [
    (lambda data: data.update(kind="settlement"), "kind"),
    (lambda data: data.update(payments=[]), "payments"),
    (lambda data: data["payments"].append(dict(data["payments"][0])), "payments"),
    (lambda data: data["payments"][0].update(amount="0.00"), "payments"),
], ids=["settlement-kind", "no-entries", "repeated-id", "zero-amount"])
def test_the_request_is_checked(client, token, shop, booked, change, field):
    request_data = money(booked, shop)
    change(request_data)

    body = call(client, PAYMENTS, request_data, token=token).json()

    assert body["code"] == "invalid_request" and field in body["data"]["errors"]


def test_a_resent_payment_is_a_duplicate_and_a_changed_one_a_conflict(client, token, shop, booked):
    request_data = money(booked, shop)
    first = call(client, PAYMENTS, request_data, token=token).json()

    again = call(client, PAYMENTS, request_data, token=token).json()
    changed = copy.deepcopy(request_data)
    changed["payments"][0]["amount"] = "55.00"

    assert (again["code"], again["data"]) == ("duplicate", first["data"])
    assert call(client, PAYMENTS, changed, token=token).json()["code"] == "sync_id_conflict"
    assert Payment.objects.count() == 3


def test_another_tablet_at_the_station_can_take_the_money(client, world, shop, booked):
    other = sign_in_tablet(client, world, code="OPR002", installation_id="till-2", branch=world["adc1"])

    assert call(client, PAYMENTS, money(booked, shop), token=other).json()["code"] == "ok"


def test_the_payments_endpoint_is_in_the_api_docs(client, db):
    assert "/api/v1/{app}/orders/payments" in client.get("/api/schema/").content.decode()
