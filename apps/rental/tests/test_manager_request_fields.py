"""The manager app's request rows carry the order's money (owner, 28 Oct 2026):
fare, deposit, received, refunded, tax % -- and, only once settled, the bill's
tax and net amount and who closed it."""

from apps.rental.models import Order
from apps.rental.tests import test_credit_notes as notes_tests
from apps.rental.tests import test_order_api as orders
from apps.rental.tests import test_order_requests as requests_tests
from apps.rental.tests import test_order_settle as settling
from core.ids import uuid7

call = orders.call
world, token, fresh_throttle, shop = orders.world, orders.token, orders.fresh_throttle, orders.shop
booked, returned, settled = settling.booked, settling.returned, notes_tests.settled
manager = requests_tests.manager

LIST = "/api/v1/manager/requests/list"


def rows(client, manager, **filters):
    return call(client, LIST, filters, token=manager).json()["data"]["requests"]


def test_a_discount_request_on_a_running_order(client, world, token, shop, booked, manager):
    Order.objects.filter(pk=booked["sync_id"]).update(transaction_no="ADC1-3")
    requests_tests.asked(client, token, booked)
    [row] = rows(client, manager, kind="discount")
    order = row["order"]
    tax = shop["mo41"].vehicle_type.tax_percentage
    assert (order["transaction_no"], order["fare_amount"], order["deposit_amount"]) == ("ADC1-3", "100.000", "100.000")
    assert (order["amount_received"], order["amount_refunded"]) == ("100.000", "0.000")
    assert order["tax_percentage"] == (str(tax) if tax is not None else None)
    assert (order["tax_amount"], order["bill_amount"], order["closed_by"], order["closed_at"]) == (
        None, None, None, None)                                                   # no bill before settle
    assert {v["base_fare"] for v in order["vehicles"]} == {"50.000"}
    assert {v["total_amount"] for v in order["vehicles"]} == {None}


def test_an_invoice_reprint_on_a_settled_order(client, world, token, shop, settled, manager):
    Order.objects.filter(pk=settled.pk).update(transaction_no="ADC1-4")
    body = call(client, requests_tests.ASK, {"sync_id": str(uuid7()), "order_id": str(settled.pk),
                                             "kind": "reprint", "reason": "Lost", "requested_at":
                                             "2026-10-02 18:00:00"}, token=token).json()
    assert body["code"] == "ok", body
    [row] = rows(client, manager, kind="reprint")
    order, invoice = row["order"], settled.invoice
    assert (order["transaction_no"], order["bill_amount"], order["tax_amount"]) == (
        "ADC1-4", str(invoice.net_amount), str(invoice.tax_amount))
    assert (order["fare_amount"], order["tax_percentage"]) == (str(settled.subtotal), str(invoice.tax_percentage))
    assert order["closed_by"] == (invoice.issued_by.display_name or invoice.issued_by.username)
    assert order["closed_at"] == "2026-10-02 17:13:30"
    assert row["details"]["invoice_net_amount"] == str(invoice.net_amount)               # kept


def test_a_credit_note_request_row_carries_the_same_order_money(client, world, token, settled, manager):
    assert call(client, notes_tests.REQUEST, notes_tests.ask(settled), token=token).json()["code"] == "ok"
    [row] = rows(client, manager, kind="credit_note")
    assert row["order"]["bill_amount"] == str(settled.invoice.net_amount)
    assert row["order"]["closed_by"] is not None
    assert row["details"]["net_amount"] is None                     # its own amount: once approved
