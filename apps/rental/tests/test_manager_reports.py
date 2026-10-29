"""The manager app's masters and reports (apps/rental/manager_api.py,
apps/rental/reports.py): states and stations, collections per state and per
station (settled orders by settle day), and the orders booked in a range with
a status summary -- narrowed by state, station, vehicle type and vehicle --
and the vehicle type and vehicle masters."""

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.company.models import Branch, BranchType, Location, State
from apps.fleet.models import Category, Vehicle, VehicleType
from apps.rental import credit_notes, reports
from apps.rental.models import Invoice, Order, OrderItem, OrderItemStatus, OrderStatus
from apps.rental.tests import test_order_requests as rq
from apps.rental.tests.test_order_return import RETURN, back
from core.enums import ApprovalStatus
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
TYPES = "/api/v1/manager/vehicle-types"
VEHICLES = "/api/v1/manager/vehicles/list"
APPROVED = ApprovalStatus.APPROVED
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


@pytest.mark.parametrize("url", [STATES, BRANCHES, TYPES, VEHICLES, BY_STATE, BY_BRANCH, ORDERS])
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
        "orders": 0, "net_amount": "0.000", "credit_notes": "0.000",
        "total_card_payment": "0.000", "total_cash_payment": "0.000"}


def test_every_station_with_card_and_cash_when_no_state_is_sent(client, world, manager, settled, dubai):
    """Owner, 29 Oct 2026: state_id optional; card and cash payments per station
    and in the total. The settled order took 100.000 cash at booking and
    10.000 by card at settle."""
    data = get(client, manager, BY_BRANCH, **DAY)

    assert data["state_id"] is None
    names = [b["name"] for b in data["branches"]]
    assert set(names) == {"Abu Dhabi Corniche 1", "Abu Dhabi Corniche 2", "Creek Park Gate 1"}
    [sold] = [b for b in data["branches"] if b["orders"]]
    assert (sold["net_amount"], sold["card_payment"], sold["cash_payment"]) == ("110.000", "10.000", "100.000")
    assert data["total"] == {"orders": 1, "net_amount": "110.000", "credit_notes": "0.000",
                             "total_card_payment": "10.000", "total_cash_payment": "100.000"}
    sample = reports_sample("_BY_BRANCH_SAMPLE")
    assert shape({**data, "state_id": 0}) == shape({**sample, "state_id": 0})


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
    for path in ("states", "branches", "vehicle-types", "vehicles/list", "reports/collections/states",
                 "reports/collections/branches", "reports/orders"):
        assert f"/api/v1/{{app}}/{path}" in schema, path


# -- Order filters: state, station, vehicle type, vehicle ----------------------------------

ECHOED = ("state_id", "branch_id", "branch_name", "vehicle_type_id", "vehicle_id")


def report(client, manager, **filters):
    return get(client, manager, ORDERS, **DAY, **filters)


def numbers(data):
    return sorted(o["order_no"] for o in data["orders"])


@pytest.fixture
def spread(client, world, token, shop, dubai):
    """Three orders on 2 Oct: MO 41 + DC 02 (DC 02 back) and MO 42 at Corniche 1
    (Abu Dhabi), and DC 02 again, moved to Creek Park Gate 1 (Dubai). DC 02 is
    a Berg; the rest Monacos."""
    Vehicle.objects.filter(pk=shop["dc02"].pk).update(vehicle_type=world["berg"])
    both = book(client, world, shop, token, vehicles=("mo41", "dc02"), number=231)
    alone = book(client, world, shop, token, vehicles=("mo42",), number=232)
    call(client, RETURN, back(both, 1), token=token)
    creek = book(client, world, shop, token, vehicles=("dc02",), number=233)
    Order.objects.filter(order_no=creek["order_no"]).update(branch=dubai["open"])
    return {"both": both["order_no"], "alone": alone["order_no"], "creek": creek["order_no"]}


def every(spread):
    return sorted([spread["both"], spread["alone"], spread["creek"]])


def test_no_filter_is_every_station_each_row_with_its_own(client, manager, spread):
    data = report(client, manager)

    assert numbers(data) == every(spread)
    assert (data["summary"]["all"], data["total_orders"]) == (3, 3)
    assert {o["order_no"]: o["station_name"] for o in data["orders"]}[spread["creek"]] == "Creek Park Gate 1"
    assert {key: data[key] for key in ECHOED} == dict.fromkeys(ECHOED)


def test_a_state_is_its_stations_orders(client, world, manager, spread, dubai):
    in_dubai = report(client, manager, state_id=dubai["state"].pk)
    in_abu_dhabi = report(client, manager, state_id=world["adc1"].location.state_id)

    assert (numbers(in_dubai), in_dubai["summary"]["all"], in_dubai["state_id"]) == (
        [spread["creek"]], 1, dubai["state"].pk)
    assert numbers(in_abu_dhabi) == sorted([spread["both"], spread["alone"]])


def test_a_station_outside_the_state_finds_nothing(client, world, manager, spread, dubai):
    data = report(client, manager, state_id=dubai["state"].pk, branch_id=world["adc1"].pk)

    assert (data["orders"], data["summary"]["all"], data["total_orders"]) == ([], 0, 0)
    assert (data["branch_id"], data["branch_name"]) == (world["adc1"].pk, "Abu Dhabi Corniche 1")


def test_a_vehicle_type_counts_each_order_once(client, world, token, manager, shop, spread):
    Vehicle.objects.filter(pk=shop["mo42"].pk).update(vehicle_type=world["berg"])
    # MO 42 and DC 02 back, free to go out again (Creek's order is not the tablet's station to return).
    OrderItem.objects.filter(order__order_no__in=[spread["alone"], spread["creek"]]).update(
        status=OrderItemStatus.RETURNED)
    two_bergs = book(client, world, shop, token, vehicles=("dc02", "mo42"), number=234)

    bergs = report(client, manager, vehicle_type_id=world["berg"].pk)
    monacos = report(client, manager, vehicle_type_id=world["monaco"].pk, branch_id=world["adc1"].pk)

    assert numbers(bergs) == sorted([*every(spread), two_bergs["order_no"]])
    assert (bergs["summary"]["all"], bergs["total_orders"]) == (4, 4)
    assert numbers(monacos) == [spread["both"]]
    lines = {v["vehicle_name"]: (v["vehicle_id"], v["vehicle_type_id"])
             for o in bergs["orders"] for v in o["vehicles"]}
    assert lines["DC 02"] == (shop["dc02"].pk, world["berg"].pk)


def test_a_vehicle_finds_its_orders_and_a_vehicle_of_another_type_nothing(client, world, manager, shop, spread):
    dc02 = report(client, manager, vehicle_id=shop["dc02"].pk)
    mismatched = report(client, manager, vehicle_id=shop["dc02"].pk, vehicle_type_id=world["monaco"].pk)

    assert numbers(dc02) == sorted([spread["both"], spread["creek"]])
    assert (dc02["vehicle_id"], mismatched["orders"], mismatched["summary"]["all"]) == (shop["dc02"].pk, [], 0)


def test_a_replaced_vehicle_still_finds_its_order(client, manager, shop, spread):
    OrderItem.objects.filter(order__order_no=spread["alone"]).update(status=OrderItemStatus.REPLACED)

    assert numbers(report(client, manager, vehicle_id=shop["mo42"].pk)) == [spread["alone"]]


def test_the_summary_follows_the_filters_and_the_status_narrows_only_the_list(client, manager, shop, spread):
    Order.objects.filter(order_no=spread["creek"]).update(status=OrderStatus.CANCELLED)

    data = report(client, manager, vehicle_id=shop["dc02"].pk, order_status="cancelled")

    assert (data["summary"]["all"], data["summary"]["partially_received"], data["summary"]["cancelled"]) == (
        2, 1, 1)
    assert (numbers(data), data["total_orders"]) == ([spread["creek"]], 1)


def test_another_companys_vehicle_finds_nothing(client, world, manager, shop, spread):
    theirs = Vehicle.objects.create(company=world["other"], vehicle_code="T1", vehicle_name="T1",
                                    vehicle_type=world["their_type"], uom=shop["mo41"].uom, approval_status=APPROVED)

    data = report(client, manager, vehicle_id=theirs.pk, vehicle_type_id=world["their_type"].pk)

    assert (data["orders"], data["summary"]["all"]) == ([], 0)


# -- Vehicle masters -------------------------------------------------------------------


@pytest.fixture
def fleet(world, shop):
    """Every type approved; DC 02 a Berg, MO 42 still waiting for approval,
    MO 99 retired, and another company's T1."""
    VehicleType.objects.update(approval_status=APPROVED)
    Vehicle.objects.filter(pk=shop["dc02"].pk).update(vehicle_type=world["berg"])
    Vehicle.objects.filter(pk=shop["mo42"].pk).update(approval_status=ApprovalStatus.PENDING)
    Vehicle.objects.filter(pk=shop["away"].pk).update(is_active=False)
    Vehicle.objects.create(company=world["other"], vehicle_code="T1", vehicle_name="T1",
                           vehicle_type=world["their_type"], uom=shop["mo41"].uom, approval_status=APPROVED)
    return shop


def test_vehicle_types_are_the_companys_active_approved_types(client, world, manager, fleet):
    VehicleType.objects.filter(pk=world["berg"].pk).update(is_active=False)
    karts = Category.objects.create(company=world["company"], category_code="KRT", category_name="Karts")
    drift = VehicleType.objects.create(company=world["company"], category=karts, brand=world["monaco"].brand,
                                       vehicle_type_name="Drift", approval_status=APPROVED)
    VehicleType.objects.create(company=world["company"], category=karts, brand=world["monaco"].brand,
                               vehicle_type_name="Pending Kart")

    rows = get(client, manager, TYPES)["vehicle_types"]
    in_karts = get(client, manager, TYPES, category_id=karts.pk)["vehicle_types"]

    assert [r["name"] for r in rows] == ["Drift", "Monaco"]
    assert in_karts == [{"vehicle_type_id": drift.pk, "name": "Drift", "category_id": karts.pk,
                         "category": "Karts"}]
    assert shape(rows[0]) == shape(reports_sample("_VEHICLE_TYPES_SAMPLE")["vehicle_types"][0])
    assert get(client, manager, TYPES, category_id=999999)["vehicle_types"] == []


def test_vehicles_are_the_companys_active_approved_vehicles(client, world, manager, fleet):
    data = get(client, manager, VEHICLES)
    bergs = get(client, manager, VEHICLES, vehicle_type_id=world["berg"].pk)["vehicles"]

    assert [v["vehicle_name"] for v in data["vehicles"]] == ["DC 02", "MO 41"]
    assert (data["page"], data["pages"], data["total_vehicles"]) == (1, 1, 2)
    assert bergs == [{"vehicle_id": fleet["dc02"].pk, "vehicle_name": "DC 02",
                      "identifier": Vehicle.objects.get(pk=fleet["dc02"].pk).identifier,
                      "vehicle_type_id": world["berg"].pk, "vehicle_type": "Berg"}]
    assert shape(data) == shape(reports_sample("_VEHICLES_SAMPLE")) | {"vehicles": shape(data)["vehicles"]}
    assert get(client, manager, VEHICLES, vehicle_type_id=999999)["vehicles"] == []


@pytest.mark.parametrize("term", ["mo 4", "MO 41", "{identifier}"])
def test_vehicles_are_searched_by_name_code_and_identifier(client, manager, fleet, term):
    identifier = Vehicle.objects.get(pk=fleet["mo41"].pk).identifier

    rows = get(client, manager, VEHICLES, search=f" {term.format(identifier=identifier.lower())} ")["vehicles"]

    assert [v["vehicle_name"] for v in rows] == ["MO 41"]


def test_vehicles_are_paged(client, manager, fleet, monkeypatch):
    monkeypatch.setattr(reports, "PAGE_SIZE", 1)

    second = get(client, manager, VEHICLES, page=2)

    assert ([v["vehicle_name"] for v in second["vehicles"]], second["pages"], second["total_vehicles"]) == (
        ["MO 41"], 2, 2)


def reports_sample(name):
    from apps.rental import manager_api
    return getattr(manager_api, name)
