"""The manager app's masters and reports (apps/rental/manager_api.py,
apps/rental/reports.py): states and stations, collections per state and per
station (settled orders by settle day), and one station's orders booked in a
range with a status summary."""

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.company.models import Branch, BranchType, Location, State
from apps.rental import credit_notes, reports
from apps.rental.models import Invoice, Order, OrderStatus
from apps.rental.tests import test_order_requests as rq
from apps.rental.tests.test_order_return import RETURN, back
from core.ids import uuid7
from core.models import User

call, shape, booking = rq.call, rq.shape, rq.booking
world, token, fresh_throttle, shop, manager = rq.world, rq.token, rq.fresh_throttle, rq.shop, rq.manager
bill, return_all, SETTLE = rq.bill, rq.return_all, rq.SETTLE

STATES = "/api/v1/manager/states"
BRANCHES = "/api/v1/manager/branches"
BY_STATE = "/api/v1/manager/reports/collections/states"
BY_BRANCH = "/api/v1/manager/reports/collections/branches"
ORDERS = "/api/v1/manager/reports/orders"
DAY = {"from_date": "2026-10-02", "to_date": "2026-10-02"}


@pytest.fixture
def dubai(world):
    """A second state: one active station and one closed."""
    adc1 = world["adc1"]
    state = State.objects.create(country=adc1.location.country, short_code="DXB", name="Dubai")
    location = Location.objects.create(country=adc1.location.country, state=state, short_code="CRK", name="Creek")

    def station(code, name, active=True):
        return Branch.objects.create(company=world["company"], location=location, short_code=code, name=name,
                                     branch_type=BranchType.STATION, is_active=active)

    return {"state": state, "open": station("CPG1", "Creek Park Gate 1"),
            "closed": station("CPG9", "Creek Park Old", active=False)}


def book(client, world, shop, token, vehicles=("mo41", "dc02"), number=231, **overrides):
    data = booking(world, shop, number=number, **overrides)
    data["items"] = [{**data["items"][0], "sync_id": str(uuid7()), "vehicle_id": shop[code].pk}
                     for code in vehicles]
    data["payments"] = [{**data["payments"][0], "sync_id": str(uuid7()), "amount": "100.00"}]
    assert call(client, rq.orders.BOOK, data, token=token).json()["code"] == "ok"
    return data


@pytest.fixture
def settled(client, world, token, shop):
    """Booked and settled on 2 Oct: lines 110.000, net 110.000."""
    data = book(client, world, shop, token)
    return_all(client, token, data)
    assert call(client, SETTLE, bill(data, shop), token=token).json()["code"] == "ok"
    return data


def get(client, manager, url, **request_data):
    body = call(client, url, request_data, token=manager).json()
    assert body["code"] == "ok", body
    return body["data"]


# -- Masters ------------------------------------------------------------------------


def test_states_are_those_with_active_stations(client, world, manager, dubai):
    rows = get(client, manager, STATES)["states"]

    assert [(r["name"], r["branch_count"]) for r in rows] == [("Abu Dhabi", 2), ("Dubai", 1)]
    assert shape(rows[0]) == shape(reports_sample("_STATES_SAMPLE")["states"][0])


def test_branches_are_the_active_stations_optionally_of_one_state(client, world, manager, dubai):
    everything = get(client, manager, BRANCHES)["branches"]
    in_dubai = get(client, manager, BRANCHES, state_id=dubai["state"].pk)["branches"]

    assert [b["name"] for b in everything] == ["Abu Dhabi Corniche 1", "Abu Dhabi Corniche 2", "Creek Park Gate 1"]
    assert [(b["branch_id"], b["state"]) for b in in_dubai] == [(dubai["open"].pk, "Dubai")]


def test_another_companys_stations_are_not_listed(client, world, manager):
    names = [b["name"] for b in get(client, manager, BRANCHES)["branches"]]

    assert "Elsewhere" not in names and len(names) == 2


@pytest.mark.parametrize("url", [STATES, BRANCHES, BY_STATE, BY_BRANCH, ORDERS])
def test_an_operator_token_is_refused(client, token, url):
    assert call(client, url, {}, token=token).json()["code"] == "wrong_channel"


# -- Collections ----------------------------------------------------------------------


def test_collections_per_state_count_settled_orders_on_their_settle_day(client, world, token, manager, shop,
                                                                       settled, dubai):
    book(client, world, shop, token, vehicles=("mo42",), number=232)            # running: not a collection

    data = get(client, manager, BY_STATE, **DAY)

    assert [(s["name"], s["orders"], s["net_amount"], s["credit_notes"]) for s in data["states"]] == [
        ("Abu Dhabi", 1, "110.000", "0.000"), ("Dubai", 0, "0.000", "0.000")]
    assert data["total"] == {"orders": 1, "net_amount": "110.000", "credit_notes": "0.000"}
    assert shape(data) == shape(reports_sample("_BY_STATE_SAMPLE"))
    empty = get(client, manager, BY_STATE, from_date="2026-10-03", to_date="2026-10-09")
    assert empty["total"]["orders"] == 0


def test_credit_notes_are_shown_beside_the_net_not_taken_off(client, world, manager, settled):
    invoice = Invoice.objects.get()
    credit_notes.issue_directly(User.objects.get(username="opr001"), invoice.pk, [world["company"].pk], "20")

    data = get(client, manager, BY_STATE, **DAY)

    assert (data["total"]["net_amount"], data["total"]["credit_notes"]) == ("110.000", "20.000")


def test_collections_per_station_of_one_state(client, world, manager, settled, dubai):
    Invoice.objects.update(branch=dubai["closed"])         # a closed station that sold: still counted

    data = get(client, manager, BY_BRANCH, state_id=dubai["state"].pk, **DAY)

    assert [(b["name"], b["orders"], b["net_amount"]) for b in data["branches"]] == [
        ("Creek Park Old", 1, "110.000"), ("Creek Park Gate 1", 0, "0.000")]
    assert data["total"]["net_amount"] == "110.000"
    assert get(client, manager, BY_BRANCH, state_id=world["adc1"].location.state_id, **DAY)["total"] == {
        "orders": 0, "net_amount": "0.000", "credit_notes": "0.000"}


@pytest.mark.parametrize(("dates", "field"), [
    ({"from_date": "2026-10-05", "to_date": "2026-10-01"}, "to_date"),
    ({"to_date": "2026-10-01"}, "from_date"),
    ({"from_date": "2026-10-01", "to_date": "01-10-2026"}, "to_date"),
], ids=["to-before-from", "missing", "wrong-format"])
def test_the_dates_are_checked(client, manager, dates, field):
    body = call(client, BY_STATE, dates, token=manager).json()

    assert body["code"] == "invalid_request" and field in body["data"]["errors"]


# -- Order details ------------------------------------------------------------------------


def statuses(client, manager, world, **extra):
    data = get(client, manager, ORDERS, branch_id=world["adc1"].pk, **DAY, **extra)
    return data, [o["order_status"] for o in data["orders"]]


def test_an_order_moves_through_the_statuses(client, world, token, manager, shop):
    data = book(client, world, shop, token)
    assert statuses(client, manager, world)[1] == ["running"]

    call(client, RETURN, back(data, 1), token=token)
    assert statuses(client, manager, world)[1] == ["partially_received"]

    call(client, RETURN, back(data, 0), token=token)
    assert statuses(client, manager, world)[1] == ["awaiting_settlement"]

    call(client, SETTLE, bill(data, shop, subtotal="100.000", tax_amount="4.762", net_amount="100.000",
                              payments=[]), token=token)
    report, found = statuses(client, manager, world)
    assert found == ["fully_received"]
    [order] = report["orders"]
    assert (order["amount"], order["invoice_no"], order["end_time"], order["duration_minutes"]) == (
        "100.000", data["order_no"], "2026-10-02 17:00:00", 60)

    Order.objects.update(status=OrderStatus.CANCELLED)
    assert statuses(client, manager, world)[1] == ["cancelled"]


def test_the_summary_counts_the_whole_range_and_the_filter_narrows_the_list(client, world, token, manager, shop):
    first = book(client, world, shop, token, vehicles=("mo41",), number=231)
    book(client, world, shop, token, vehicles=("dc02",), number=232)
    call(client, RETURN, back(first, 0), token=token)

    report, found = statuses(client, manager, world, order_status="running")

    assert report["summary"] == {"all": 2, "running": 1, "partially_received": 0, "awaiting_settlement": 1,
                                 "fully_received": 0, "cancelled": 0, "replaced": 0}
    assert found == ["running"] and report["total_orders"] == 1


def test_a_running_order_shows_its_lines_payments_and_money_so_far(client, world, token, manager, shop):
    data = book(client, world, shop, token)
    call(client, RETURN, back(data, 1), token=token)

    report = get(client, manager, ORDERS, branch_id=world["adc1"].pk, **DAY)

    [order] = report["orders"]
    assert (order["order_no"], order["customer_name"], order["station_name"]) == (
        data["order_no"], "Ahmed Al Mansoori", "Abu Dhabi Corniche 1")
    assert (order["amount"], order["advance_paid"], order["end_time"], order["invoice_no"]) == (
        "100.000", "100.000", "2026-10-02 17:00:00", None)
    assert sorted((v["vehicle_name"], v["vehicle_status"]) for v in order["vehicles"]) == [
        ("DC 02", "returned"), ("MO 41", "running")]
    assert [(p["mode"], p["kind"], p["amount"]) for p in order["payments"]] == [("Cash", "advance", "100.000")]
    assert shape(report) == shape(reports_sample("_ORDERS_SAMPLE")) | {"orders": shape(report)["orders"]}


def test_orders_are_chosen_by_booking_day(client, world, token, manager, shop):
    book(client, world, shop, token)

    later = get(client, manager, ORDERS, branch_id=world["adc1"].pk, from_date="2026-10-03",
                to_date="2026-10-03")

    assert (later["total_orders"], later["summary"]["all"]) == (0, 0)


def test_a_station_of_another_company_is_unknown(client, world, manager):
    elsewhere = Branch.objects.create(company=world["other"], location=world["adc1"].location, short_code="OTH1",
                                      name="Elsewhere", branch_type=BranchType.STATION)

    body = call(client, ORDERS, {"branch_id": elsewhere.pk, **DAY}, token=manager).json()

    assert body["code"] == "unknown_branch"


def test_the_list_is_paged_and_a_page_costs_the_same(client, world, token, manager, shop, monkeypatch):
    for number, vehicle in enumerate(("mo41", "dc02", "mo42"), start=231):
        book(client, world, shop, token, vehicles=(vehicle,), number=number)
    monkeypatch.setattr(reports, "PAGE_SIZE", 1)

    def page(n):
        with CaptureQueriesContext(connection) as queries:
            data = get(client, manager, ORDERS, branch_id=world["adc1"].pk, page=n, **DAY)
        return data, len(queries)

    first, cost_one = page(1)
    monkeypatch.setattr(reports, "PAGE_SIZE", 3)
    everything, cost_three = page(1)

    assert (first["pages"], len(first["orders"]), first["total_orders"]) == (3, 1, 3)
    assert len(everything["orders"]) == 3
    assert cost_three == cost_one          # lines, payments, credit notes: one query each per page


def test_the_report_endpoints_are_in_the_api_docs(client, db):
    schema = client.get("/api/schema/").content.decode()
    for path in ("states", "branches", "reports/collections/states", "reports/collections/branches",
                 "reports/orders"):
        assert f"/api/v1/{{app}}/{path}" in schema, path


def reports_sample(name):
    from apps.rental import manager_api
    return getattr(manager_api, name)
