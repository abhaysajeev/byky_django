"""The customer's star rating: sent with orders/settle, stored on the invoice,
and kept on the customer as the latest -- a settle arriving late never
replaces a newer rating."""

import datetime
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.rental import api
from apps.rental.models import Customer, Invoice
from apps.rental.services import rate_customer
from apps.rental.tests import test_order_settle as settles

call = settles.call
world, token, fresh_throttle, shop = settles.world, settles.token, settles.fresh_throttle, settles.shop
booked, returned, bill, settle = settles.booked, settles.returned, settles.bill, settles.settle


def test_a_rating_is_stored_on_the_invoice_and_the_customer(client, world, token, shop, returned):
    body = settle(client, token, bill(returned, shop, star_rating="4.5"))

    assert body["code"] == "ok", body
    assert Invoice.objects.get().star_rating == Decimal("4.5")
    customer = Customer.objects.get(pk=shop["ahmed"].pk)
    assert customer.star_rating == Decimal("4.5")
    assert customer.star_rating_at == datetime.datetime(2026, 10, 2, 13, 13, 30, tzinfo=datetime.UTC)


@pytest.mark.parametrize("sent", [None, "absent"])
def test_no_rating_changes_nothing(client, world, token, shop, returned, sent):
    Customer.objects.filter(pk=shop["ahmed"].pk).update(star_rating=Decimal("3.0"), star_rating_at=timezone.now())
    request_data = bill(returned, shop)
    if sent is None:
        request_data["star_rating"] = None

    assert settle(client, token, request_data)["code"] == "ok"
    assert Invoice.objects.get().star_rating is None
    assert Customer.objects.get(pk=shop["ahmed"].pk).star_rating == Decimal("3.0")


@pytest.mark.parametrize("rating", ["0.5", "5.5", "4.25", "abc", "10"])
def test_a_rating_outside_one_to_five_is_refused(client, world, token, shop, returned, rating):
    body = settle(client, token, bill(returned, shop, star_rating=rating))

    assert body["code"] == "invalid_request", body
    assert "star_rating" in body["data"]["errors"]
    assert not Invoice.objects.exists()


@pytest.mark.parametrize("rating", ["1", "5", "2.5", 4])
def test_whole_and_half_ratings_are_accepted(client, world, token, shop, returned, rating):
    assert settle(client, token, bill(returned, shop, star_rating=rating))["code"] == "ok"
    assert Invoice.objects.get().star_rating == Decimal(str(rating))


def test_an_older_settle_arriving_late_keeps_the_newer_rating(world, shop):
    ahmed = shop["ahmed"]
    newer = datetime.datetime(2026, 10, 5, 10, tzinfo=datetime.UTC)
    rate_customer(ahmed.pk, Decimal("5.0"), newer)
    rate_customer(ahmed.pk, Decimal("2.0"), newer - datetime.timedelta(days=1))       # parked, synced late

    ahmed.refresh_from_db()
    assert (ahmed.star_rating, ahmed.star_rating_at) == (Decimal("5.0"), newer)

    rate_customer(ahmed.pk, Decimal("3.5"), newer + datetime.timedelta(hours=1))
    ahmed.refresh_from_db()
    assert ahmed.star_rating == Decimal("3.5")


def test_the_rating_appears_in_the_customer_and_invoice_replies(client, world, token, shop, returned):
    assert settle(client, token, bill(returned, shop, star_rating="4.5"))["code"] == "ok"

    customer = Customer.objects.get(pk=shop["ahmed"].pk)
    invoice = Invoice.objects.get()
    assert api._customer_json(customer)["star_rating"] == "4.5"
    assert api.invoice_json(invoice, datetime.UTC)["star_rating"] == "4.5"
    assert api._customer_json(Customer(company=world["company"], star_rating=None))["star_rating"] is None


def test_it_is_in_the_api_docs(client, db):
    schema = client.get("/api/schema/").content.decode()

    assert "star_rating" in schema
