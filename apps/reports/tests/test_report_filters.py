"""Rent Summary's and Consolidated's added filters (client, 22 Nov 2026):
payment mode, discount type, order status, vehicles, customer and order, and
on Rent Summary the cashier and the tablet -- plus Clear's business-day
defaults. On the settled 231 of test_money_reports (100.000 cash + 10.000
card), with 232 running and 233 cancelled beside it for Consolidated."""

import pytest

from apps.company.models import PaymentMode
from apps.fleet.models import Vehicle
from apps.rental.models import DiscountSource, Invoice
from apps.reports.tests import test_money_reports as money
from apps.reports.tests.helpers import data, web
from core.models import User

world, token, fresh_throttle, shop = money.world, money.token, money.fresh_throttle, money.shop
settled, dubai, book, mixed = money.settled, money.dubai, money.book, money.mixed
_ = web


def orders(d):
    return sum(r["cells"]["orders"] for r in d["rows"] if r["kind"] == "data")


# -- Rent Summary ----------------------------------------------------------------------


def test_rent_summary_by_payment_mode(web, world, settled):
    card = PaymentMode.objects.get(company=world["company"], name="Card")
    cheque = PaymentMode.objects.create(company=world["company"], name="Cheque")
    assert orders(data(web, "rent-summary", payment_mode=card.pk)) == 1
    assert orders(data(web, "rent-summary", payment_mode=cheque.pk)) == 0


@pytest.mark.parametrize(("source", "pick", "found"), [
    ("", "none", 1), ("", "card", 0), (DiscountSource.CARD, "card", 1), (DiscountSource.CARD, "none", 0),
    (DiscountSource.CARD_MANAGER, "card_manager", 1), (DiscountSource.COMPLIMENTARY, "complimentary", 1),
    (DiscountSource.MANAGER, "card_manager", 0),
])
def test_rent_summary_by_discount_type(web, settled, source, pick, found):
    Invoice.objects.update(discount_source=source)
    assert orders(data(web, "rent-summary", discount=pick)) == found


def test_rent_summary_by_cashier_device_customer_and_order(web, settled):
    operator = User.objects.get(username="opr001")
    assert orders(data(web, "rent-summary", cashier=operator.pk)) == 1
    assert orders(data(web, "rent-summary", cashier=User.objects.get(username="rep.user").pk)) == 0
    assert orders(data(web, "rent-summary", device="no such tablet")) == 0
    assert orders(data(web, "rent-summary", customer="ahmed", phone="501234567", order_no="231")) == 1
    assert orders(data(web, "rent-summary", customer="nobody")) == 0


def test_an_unknown_discount_type_is_all(web, settled):
    assert orders(data(web, "rent-summary", discount="bogus")) == 1


# -- Consolidated --------------------------------------------------------------------


@pytest.mark.parametrize(("status", "found"), [("received", 1), ("running", 1), ("cancelled", 1),
                                               ("partially_received", 0)])
def test_consolidated_by_order_status(web, mixed, status, found):
    assert orders(data(web, "consolidated", status=status)) == found


def test_consolidated_by_payment_mode_discount_and_customer(web, world, mixed):
    card = PaymentMode.objects.get(company=world["company"], name="Card")
    assert orders(data(web, "consolidated", payment_mode=card.pk)) == 1
    Invoice.objects.update(discount_source=DiscountSource.CARD)
    assert orders(data(web, "consolidated", discount="card")) == 1
    assert orders(data(web, "consolidated", order_no="232")) == 1


def test_consolidated_by_vehicle_type(web, world, shop, mixed):
    Vehicle.objects.filter(pk=shop["dc02"].pk).update(vehicle_type=world["berg"])
    assert orders(data(web, "consolidated", vehicle_type=world["berg"].pk)) == 1      # only 231 had DC 02
    assert orders(data(web, "consolidated", category=world["berg"].category_id)) >= 1


def test_the_new_filters_are_in_the_bar(web, settled):
    page = web.get("/reports/consolidated/").content.decode()
    for name in ("payment_mode", "discount", "status", "vehicle_type", "order_no"):
        assert f'name="{name}"' in page, name
    assert "Card + manager discount" in page


def test_clear_goes_back_to_the_business_day_not_the_opened_range(web, settled):
    page = web.get("/reports/rent-summary/", {"from": "2026-10-02T00:00", "to": "2026-10-02T23:59"}).content.decode()
    assert 'value="2026-10-02T00:00"' in page
    assert 'data-default="2026-10-02T00:00"' not in page
