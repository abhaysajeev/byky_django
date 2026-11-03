"""orders/requests/list rows carry `order_details` (owner, 3 Nov 2026): the
whole order, exactly as orders/detail returns it without next_order_number,
right after `order_no`, on every kind -- loaded for the page at once."""

from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.rental import api
from apps.rental.tests import test_order_requests_list_all_kinds as kinds
from apps.rental.tests.test_manager_reports import book
from apps.rental.tests.test_order_api import call
from core.timezones import zone_for

world, token, fresh_throttle, shop = kinds.world, kinds.token, kinds.fresh_throttle, kinds.shop
booked, returned, settled, card, three = kinds.booked, kinds.returned, kinds.settled, kinds.card, kinds.three
listed, LIST = kinds.listed, kinds.LIST
DETAIL = "/api/v1/operator/orders/detail"


def detail(client, token, order_id):
    data = call(client, DETAIL, {"sync_id": order_id}, token=token).json()["data"]
    data.pop("next_order_number")
    return data


def test_every_kind_carries_the_whole_order_right_after_order_no(client, token, three, settled):
    rows = listed(client, token)["requests"]

    assert {r["kind"] for r in rows} == {"card_discount", "credit_note", "reprint"}
    whole = detail(client, token, str(settled.pk))
    for row in rows:
        keys = list(row)
        assert keys[keys.index("order_no") + 1] == "order_details", row["kind"]
        assert row["order_details"] == whole, row["kind"]


def test_a_settled_order_has_its_bill_and_invoice(client, token, three, settled):
    details = listed(client, token)["requests"][0]["order_details"]

    assert details["status"] == "completed"
    assert details["invoice"] is not None and details["net_amount"] is not None
    assert details["close_by"] is not None
    assert "next_order_number" not in details


def test_nothing_else_in_the_row_changes(client, token, three):
    row = listed(client, token, kind="reprint")["requests"][0]
    rest = [k for k in row if k != "order_details"]

    assert rest == ["request_sync_id", "order_id", "order_no", "kind", "status", "reason", "requested_at",
                    "discount_type", "discount_value", "decided_by", "decided_channel", "decided_at",
                    "decision_note", "revoked_by", "revoked_channel", "revoked_at", "revoke_note",
                    "applied_amount", "applied_at", "used_at", "customer_name", "details"]


def test_more_orders_cost_no_more_queries(client, world, token, shop, settled):
    second = book(client, world, shop, token, vehicles=("mo41",), number=240)
    tz = zone_for(settled.company)
    with CaptureQueriesContext(connection) as one:
        api.order_details_for([str(settled.pk)], tz)
    with CaptureQueriesContext(connection) as two:
        found = api.order_details_for([str(settled.pk), second["sync_id"]], tz)

    assert len(found) == 2
    assert len(two) == len(one)


def test_the_api_example_matches_the_reply(client, token, three):
    row = listed(client, token, kind="reprint")["requests"][0]

    assert set(row["order_details"]) == set(api._ORDER_DETAILS_SAMPLE)
