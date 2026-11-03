"""Money settings on the apps (CR1, 3 Nov 2026): replies use the company's
decimals, too-precise amounts are refused, the settle round-off is checked,
and the tablet is told the setting."""

from decimal import Decimal

import pytest

from apps.company.models import CompanyMoneySetting
from apps.devices import services as devices_services
from apps.rental.tests import test_order_requests as rq
from apps.rental.tests import test_order_settle as settles
from apps.rental.tests.test_order_api import booking

call = rq.call
world, token, fresh_throttle, shop, manager = rq.world, rq.token, rq.fresh_throttle, rq.shop, rq.manager
BOOK, SETTLE, bill, return_all = rq.orders.BOOK, settles.SETTLE, settles.bill, rq.return_all


@pytest.fixture
def two_decimals(world):
    company = world["company"]
    return CompanyMoneySetting.objects.create(company=company, country=company.country, decimal_places=2,
                                              round_off_mode="upward", round_off_step=Decimal("0.25"))


def test_without_a_setting_replies_keep_three_decimals(client, world, token, shop):
    body = call(client, BOOK, booking(world, shop), token=token).json()
    assert body["data"]["amount_received"] == "100.000"


def test_replies_use_the_companys_decimals(client, world, token, shop, two_decimals):
    body = call(client, BOOK, booking(world, shop), token=token).json()
    assert body["code"] == "ok"
    assert (body["data"]["amount_received"], body["data"]["items"][0]["base_fare"]) == ("100.00", "50.00")


def test_an_amount_with_too_many_decimals_is_refused(client, world, token, shop, two_decimals):
    data = booking(world, shop)
    data["items"][0]["base_fare"] = "50.005"
    body = call(client, BOOK, data, token=token).json()
    assert body["code"] == "invalid_request"
    assert "must have at most 2 decimal places" in str(body["data"])


def test_trailing_zeros_are_not_too_many(client, world, token, shop, two_decimals):
    data = booking(world, shop)
    data["items"][0]["base_fare"] = "50.000"
    assert call(client, BOOK, data, token=token).json()["code"] == "ok"


def test_the_settle_round_off_follows_the_setting(client, world, token, shop, two_decimals):
    booked = booking(world, shop)
    assert call(client, BOOK, booked, token=token).json()["code"] == "ok"
    return_all(client, token, booked)
    wrong = bill(booked, shop, net_amount="110.10", rounding_adjustment="0.10")
    body = call(client, SETTLE, wrong, token=token).json()
    assert body["code"] == "rounding_mismatch"
    assert call(client, SETTLE, bill(booked, shop), token=token).json()["code"] == "ok"


def test_without_a_setting_the_round_off_is_not_checked(client, world, token, shop):
    booked = booking(world, shop)
    call(client, BOOK, booked, token=token)
    return_all(client, token, booked)
    # 110.10 is no multiple of 0.25, and no money settings are saved: taken as sent.
    data = bill(booked, shop, net_amount="110.10", rounding_adjustment="0.10")
    data["payments"][0]["amount"] = "10.10"
    assert call(client, SETTLE, data, token=token).json()["code"] == "ok"


def test_the_tablet_is_told_the_setting(world, shop, two_decimals):
    reply = devices_services.operator_settings(shop["tablet"], world["adc1"])
    assert reply["money"] == {"decimal_places": 2, "round_off_mode": "upward", "round_off_step": "0.25"}
    assert (reply["device_settings"]["round_off_mode"], reply["device_settings"]["round_off_step"]) == (
        "upward", "0.25")
