"""invoices/recent (the station's last 5 invoices), the customer lookup's
invoices, and customers/blocked -- through HTTP as the till app sees them."""

import pytest
from django.test import Client
from django.utils import timezone

from apps.portal.models import Role
from apps.portal.services import grant_all
from apps.rental import api
from apps.rental.models import Customer, Invoice
from apps.rental.tests import test_order_api as orders
from apps.rental.tests import test_order_settle as settling
from apps.rental.tests.conftest import make_customer
from apps.rental.tests.test_order_return import RETURN, back
from core.enums import Channel
from core.models import User

call, booking, shape, sign_in_tablet = orders.call, orders.booking, orders.shape, orders.sign_in_tablet
world, token, fresh_throttle, shop = orders.world, orders.token, orders.fresh_throttle, orders.shop

RECENT = "/api/v1/operator/invoices/recent"
LOOKUP = "/api/v1/operator/customers/lookup"
BLOCKED = "/api/v1/operator/customers/blocked"


def rent_and_settle(client, world, shop, token, number, settled_at, customer=None):
    """Book MO 41 and DC 02, bring both back, settle at `settled_at`."""
    booked = booking(world, shop, number=number, **({"customer_id": customer.pk} if customer else {}))
    assert call(client, orders.BOOK, booked, token=token).json()["code"] == "ok"
    late = back(booked, 0, returned_at="2026-10-02 17:12:00", run_minutes=72, overtime_amount="10.00",
                total_amount="60.00")
    for request_data in (late, back(booked, 1)):
        assert call(client, RETURN, request_data, token=token).json()["code"] == "ok"
    body = settling.settle(client, token, settling.bill(booked, shop, settled_at=settled_at))
    assert body["code"] == "ok", body
    return booked


def lookup(client, token, customer):
    return call(client, LOOKUP, {"mobile_country_code": customer.mobile_country_code,
                                 "mobile_no": customer.mobile_no, "full_number": customer.mobile_full},
                token=token).json()


# -- invoices/recent -------------------------------------------------------------------

def test_the_five_newest_invoices_by_issue_time(client, world, token, shop):
    numbers = [rent_and_settle(client, world, shop, token, 200 + n, f"2026-10-0{n + 2} 18:00:00")["order_no"]
               for n in range(6)]
    # Settled last, but on the earliest day: not among the newest.
    early = rent_and_settle(client, world, shop, token, 300, "2026-10-01 09:00:00")["order_no"]

    body = call(client, RECENT, token=token).json()

    assert body["code"] == "ok", body
    assert [i["invoice_no"] for i in body["data"]["invoices"]] == list(reversed(numbers[1:]))
    assert early not in {i["invoice_no"] for i in body["data"]["invoices"]}


def test_each_invoice_is_whole_with_its_vehicles_and_amounts(client, world, token, shop):
    booked = rent_and_settle(client, world, shop, token, 231, "2026-10-02 17:13:30")

    [invoice] = call(client, RECENT, token=token).json()["data"]["invoices"]

    assert (invoice["invoice_no"], invoice["order_id"], invoice["issued_at"]) == (
        booked["order_no"], booked["sync_id"], "2026-10-02 17:13:30")
    lines = sorted((i["vehicle"], i["run_minutes"], i["overtime_amount"], i["total_amount"])
                   for i in invoice["items"])
    assert lines == [("DC 02", 60, "0.000", "50.000"), ("MO 41", 72, "10.000", "60.000")]
    assert (invoice["subtotal"], invoice["tax_amount"], invoice["net_amount"]) == ("110.000", "5.240", "110.000")
    assert len(invoice["payments"]) == 3
    assert shape(invoice) == shape(api._INVOICE_SAMPLE)


def test_any_tablet_at_the_station_sees_them_and_other_stations_do_not_count(client, world, token, shop):
    kept = rent_and_settle(client, world, shop, token, 231, "2026-10-02 17:00:00")
    moved = rent_and_settle(client, world, shop, token, 232, "2026-10-02 18:00:00")
    Invoice.objects.filter(order_id=moved["sync_id"]).update(branch=world["adc2"])
    other = sign_in_tablet(client, world, code="OPR002", installation_id="till-2", branch=world["adc1"])

    invoices = call(client, RECENT, token=other).json()["data"]["invoices"]

    assert [i["invoice_no"] for i in invoices] == [kept["order_no"]]


def test_a_station_with_no_invoices_gets_an_empty_list(client, token, shop):
    assert call(client, RECENT, token=token).json()["data"] == {"invoices": []}


def test_the_old_recent_orders_call_is_gone(client, token):
    assert call(client, "/api/v1/operator/orders/recent", token=token).status_code == 404


# -- customers/lookup: the customer's invoices ------------------------------------------

def test_the_lookup_counts_and_lists_the_customers_invoices(client, world, token, shop):
    numbers = [rent_and_settle(client, world, shop, token, 200 + n, f"2026-10-0{n + 2} 18:00:00")["order_no"]
               for n in range(6)]
    someone_else = make_customer(world, first_name="Mona", mobile_no="509999999", customer_code="CU002")
    rent_and_settle(client, world, shop, token, 400, "2026-10-09 18:00:00", customer=someone_else)

    data = lookup(client, token, shop["ahmed"])["data"]

    assert data["invoice_count"] == 6
    assert [i["invoice_no"] for i in data["invoices"]] == list(reversed(numbers[1:]))
    assert data["invoices"][0]["items"] and data["invoices"][0]["net_amount"] == "110.000"


def test_a_customer_with_no_invoices(client, token, shop):
    data = lookup(client, token, shop["ahmed"])["data"]

    assert (data["invoice_count"], data["invoices"]) == (0, [])
    assert (data["blocked_at"], data["blocked_by"]) == (None, None)


# -- Blocking: stamped on the web, listed for the tablet ---------------------------------

@pytest.fixture
def web(world):
    role = Role.objects.create(company=world["company"], name="Back office")
    grant_all(role)
    User.objects.create_user("sara.k", orders.fare_api.PASSWORD, display_name="Sara K", company=world["company"],
                             role=role, allowed_channels=[Channel.WEB])
    browser = Client()
    browser.post("/login/", {"username": "sara.k", "password": orders.fare_api.PASSWORD})
    return browser


def save_on_web(web, customer, **fields):
    data = {
        "pk": customer.pk, "company": customer.company_id, "first_name": customer.first_name,
        "last_name": customer.last_name, "mobile_country_code": customer.mobile_country_code,
        "mobile_no": customer.mobile_no, "is_active": "on", **fields,
    }
    data = {k: v for k, v in data.items() if v is not None}
    response = web.post("/rental/customer/save/", data)
    assert response.json()["ok"] is True, response.json()


def test_blocking_on_the_web_is_stamped_and_unblocking_clears_it(web, shop):
    ahmed = shop["ahmed"]
    save_on_web(web, ahmed, is_blocked="on", block_reason="Damaged a kart")
    ahmed.refresh_from_db()
    stamped = ahmed.blocked_at
    assert (ahmed.is_blocked, ahmed.blocked_by.username, ahmed.block_reason) == (True, "sara.k", "Damaged a kart")
    assert stamped is not None

    save_on_web(web, ahmed, is_blocked="on", block_reason="Damaged a kart twice")    # still blocked
    ahmed.refresh_from_db()
    assert ahmed.blocked_at == stamped

    save_on_web(web, ahmed)                                                         # unblocked
    ahmed.refresh_from_db()
    assert (ahmed.is_blocked, ahmed.blocked_at, ahmed.blocked_by) == (False, None, None)


def test_the_tablet_lists_blocked_customers_newest_block_first(client, world, token, shop, web):
    mona = make_customer(world, first_name="Mona", mobile_no="509999999", customer_code="CU002")
    old = make_customer(world, first_name="Zaid", mobile_no="508888888", customer_code="CU003")
    Customer.objects.filter(pk=old.pk).update(is_blocked=True, block_reason="From the old system")
    save_on_web(web, shop["ahmed"], is_blocked="on", block_reason="Damaged a kart")
    save_on_web(web, mona, is_blocked="on", block_reason="Abusive")
    Customer.objects.filter(pk=mona.pk).update(blocked_at=timezone.now() + timezone.timedelta(minutes=1))

    body = call(client, BLOCKED, token=token).json()

    assert body["code"] == "ok", body
    rows = body["data"]["customers"]
    assert [r["first_name"] for r in rows] == ["Mona", "Ahmed", "Zaid"]
    assert (rows[1]["block_reason"], rows[1]["blocked_by"]) == ("Damaged a kart", "Sara K")
    assert rows[1]["blocked_at"] is not None and rows[2]["blocked_at"] is None
    assert (body["data"]["page"], body["data"]["pages"], body["data"]["total"]) == (1, 1, 3)
    assert set(rows[1]) == set(api._CUSTOMER_BLOCKED_SAMPLE)


def test_another_companys_blocked_customers_are_not_listed(client, world, token, shop):
    Customer.objects.filter(pk=shop["ahmed"].pk).update(is_blocked=True, company=world["other"])

    assert call(client, BLOCKED, token=token).json()["data"]["customers"] == []


@pytest.mark.parametrize("url", [RECENT, BLOCKED])
def test_only_the_operator_app(client, token, url):
    response = call(client, url.replace("operator", "manager"), token=token)

    assert (response.status_code, response.json()["code"]) == (403, "wrong_channel")
