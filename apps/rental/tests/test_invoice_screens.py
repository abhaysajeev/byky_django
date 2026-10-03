"""The Invoices screen: the list, the print preview's receipt (laid out as the
station's printed tax invoice) and the detail page."""

import datetime
from decimal import Decimal

import pytest

from apps.devices import services as devices_services
from apps.devices.models import BillKind
from apps.fare.tests.conftest import make_fare
from apps.rental.models import Invoice, InvoiceItem, OrderItemStatus, Payment, PaymentKind
from apps.rental.tests import test_order_screens as screens
from apps.rental.tests.conftest import make_customer
from apps.rental.tests.test_views import revoke
from core.ids import uuid7
from core.models import User

# The Orders screen's seed helpers, reused here.
at, make_order, settle, shop = screens.at, screens.make_order, screens.settle, screens.shop

LIST = "/rental/invoice/list/"


def receipt_no(shop, world, number):
    counter = devices_services.counter_for(shop["tablet"], world["adc1"], BillKind.ORDER)
    return devices_services.format_bill_number(counter, number)


def issue(world, shop, number=231, *, fare=True, day=0, customer=None):
    """A settled order and its invoice: MO 41 back after 72 minutes (50.00 +
    10.00 overtime), a 10% card discount -- net 54.00, VAT 5% included (2.57)."""
    order_no = receipt_no(shop, world, number)
    order = make_order(world, shop, order_no, lines=("returned",), day=day)
    if customer is not None:
        order.customer = customer
        order.save(update_fields=["customer"])
    line = order.items.get(status=OrderItemStatus.RETURNED)
    if fare:
        line.fare = make_fare({**world, "monaco": line.vehicle.vehicle_type}, package_minutes=60,
                              concurrent_fare=Decimal("10.00"), concurrent_interval_minutes=15)
        line.save(update_fields=["fare"])
    settle(order, net="54.00", day=day, world=world)
    order.refresh_from_db()
    Payment.objects.create(id=uuid7(), order=order, kind=PaymentKind.ADVANCE, mode=shop["cash"],
                           amount=Decimal("54.00"), paid_at=at(day, world=world))
    invoice = Invoice.objects.create(
        id=uuid7(), order=order, company=world["company"], branch=world["adc1"], device=shop["tablet"],
        issued_by=User.objects.get(username="sara.k"), invoice_no=order_no, issued_at=at(day, 17, 15, world=world),
        company_name="BYKY", company_trn="100297867200003", branch_name="Abu Dhabi Corniche 1",
        customer_name=order.customer_name, customer_mobile=order.customer_mobile,
        subtotal=Decimal("60.00"), discount_percentage=Decimal("10.00"), discount_amount=Decimal("6.00"),
        tax_percentage=Decimal("5.00"), tax_amount=Decimal("2.57"), rounding_adjustment=Decimal("0.00"),
        net_amount=Decimal("54.00"),
        payments=[{"mode": "Cash", "kind": "advance", "amount": "54.00", "reference_no": ""}],
    )
    InvoiceItem.objects.create(
        id=uuid7(), invoice=invoice, order_item=line, vehicle_identifier=line.vehicle.identifier,
        vehicle_name="MO 41", vehicle_type="Monaco", package_minutes=60, run_minutes=72,
        start_time=line.start_time, end_time=line.end_time, base_fare=Decimal("50.00"),
        overtime_amount=Decimal("10.00"), total_amount=Decimal("60.00"),
    )
    return invoice


@pytest.fixture
def invoice(client_in, world, shop):
    return issue(world, shop)


def rows(client_in, **params):
    return client_in.get(LIST, params).context["rows"]


# -- List ------------------------------------------------------------------------------


def test_the_list_shows_this_companys_invoices(client_in, world, shop, invoice):
    response = client_in.get(LIST)

    [row] = response.context["rows"]
    assert (row["invoice_no"], row["customer"], row["station"], row["net_amount"], row["tax_amount"]) == (
        invoice.invoice_no, "Ahmed Al Mansoori", "Abu Dhabi Corniche 1", Decimal("54.00"), Decimal("2.57"))
    html = response.content.decode()
    assert f'data-invoice-receipt="/rental/invoice/{invoice.pk}/receipt/"' in html
    assert f'href="/rental/invoice/{invoice.pk}/"' in html          # the row menu's View
    assert "AED 54.00" in html and "VAT incl. AED 2.57" in html


def test_search_and_filters_narrow_the_list(client_in, world, shop, invoice):
    fatima = make_customer(world, customer_code="CU2", first_name="Fatima", mobile_no="509999999")
    older = issue(world, shop, 232, day=-3, customer=fatima, fare=False)
    older.customer_name, older.customer_mobile = "Fatima Hassan", "971509999999"
    older.save()
    today = at(world=world).date().isoformat()

    assert [r["invoice_no"] for r in rows(client_in, q="000231")] == [invoice.invoice_no]
    assert [r["invoice_no"] for r in rows(client_in, q="509999")] == [older.invoice_no]
    assert [r["invoice_no"] for r in rows(client_in, **{"from": today, "to": today})] == [invoice.invoice_no]
    assert [r["invoice_no"] for r in rows(client_in, branch=world["adc1"].pk)] == [
        invoice.invoice_no, older.invoice_no]                       # newest first


def test_the_list_needs_read(client_in):
    revoke(client_in, "read", page="rental.invoice")

    assert client_in.get(LIST).status_code == 403


# -- Receipt -----------------------------------------------------------------------------


def test_the_receipt_reads_like_the_printed_tax_invoice(client_in, world, shop, invoice):
    shop["ahmed"].id_type, shop["ahmed"].id_no = "emirates_id", "784-1990-1234567-1"
    shop["ahmed"].save()

    response = client_in.get(f"/rental/invoice/{invoice.pk}/receipt/")

    assert response.status_code == 200
    html = response.content.decode()
    assert "<html" not in html                                      # a fragment, for the modal
    bill = response.context["bill"]
    assert (bill["trans_no"], bill["trn"], bill["invoice_no"]) == (231, "100297867200003", invoice.invoice_no)
    [line] = bill["lines"]
    assert (line["sno"], line["vehicle"], line["base_rate"], line["extra_rate"], line["duration"], line["amount"]) == (
        "01", "MO 41", "50.00/60 MINS", "10.00/15 MINS", "01:12", Decimal("60.00"))
    assert (bill["taxable_amount"], bill["balance"], bill["time"]) == (Decimal("51.43"), Decimal("0.00"), "01:12")
    for text in ("TAX INVOICE", "ABU DHABI CORNICHE 1", "TRN : 100297867200003", "VAT @5.0% incl.", "NET TOTAL",
                 "AED 54.00", "AED 6.00 (10.00%)", "AED 51.43", "Emirates ID", "784-1990-1234567-1",
                 "Bike Rental Details"):
        assert text in html, text


def test_a_line_without_a_fare_has_no_extra_rate(client_in, world, shop):
    invoice = issue(world, shop, fare=False)

    [line] = client_in.get(f"/rental/invoice/{invoice.pk}/receipt/").context["bill"]["lines"]

    assert line["extra_rate"] == "—"


def test_an_invoice_number_not_in_the_tablets_shape_has_no_trans_no(client_in, world, shop, invoice):
    Invoice.objects.filter(pk=invoice.pk).update(invoice_no="HANDWRITTEN-7")

    assert client_in.get(f"/rental/invoice/{invoice.pk}/receipt/").context["bill"]["trans_no"] == "—"


def test_the_receipt_needs_read(client_in, invoice):
    revoke(client_in, "read", page="rental.invoice")

    assert client_in.get(f"/rental/invoice/{invoice.pk}/receipt/").status_code == 403


# -- Detail ------------------------------------------------------------------------------


def test_the_detail_page_shows_the_invoice(client_in, invoice):
    response = client_in.get(f"/rental/invoice/{invoice.pk}/")

    assert response.status_code == 200
    html = response.content.decode()
    for text in ("100297867200003", "Abu Dhabi Corniche 1", "MO 41", "AED 60.00", "AED 54.00", "Cash",
                 "data-invoice-modal", f"/rental/order/{invoice.order_id}/"):
        assert text in html, text


@pytest.mark.parametrize("path", ["/rental/invoice/{pk}/", "/rental/invoice/{pk}/receipt/"])
def test_another_companys_invoice_is_not_found(client_in, world, shop, invoice, path):
    Invoice.objects.filter(pk=invoice.pk).update(company=world["other"])

    assert client_in.get(path.format(pk=invoice.pk)).status_code == 404


def test_the_dates_on_the_receipt_are_company_time(client_in, world, invoice):
    bill = client_in.get(f"/rental/invoice/{invoice.pk}/receipt/").context["bill"]

    assert bill["date"] == at(world=world).strftime("%d-%m-%Y")
    assert (bill["starting_time"], bill["closing_time"]) == ("04:00 PM", "05:15 PM")
    assert bill["issued_at"].utcoffset() == datetime.timedelta(hours=4)
