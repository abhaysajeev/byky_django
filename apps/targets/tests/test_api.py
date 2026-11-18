"""Targets for the device apps (apps/targets/api.py): the operator's own
station with today's fixed daily target, and the manager's month by state and
station. The employee app gets none."""

import datetime
import uuid
import zoneinfo
from decimal import Decimal

import pytest

from apps.company.models import Branch
from apps.crew.tests.test_attendance import (  # noqa: F401
    PASSWORD,
    login,
    manager,
    operator,
    staff_app,
    world,
)
from apps.devices.models import Device
from apps.fare.tests.test_api import call
from apps.rental.models import Order, OrderStatus
from apps.rental.tests.conftest import make_customer
from apps.targets import api, services
from apps.targets.models import TargetKind
from core.ids import uuid7
from core.models import User

DUBAI = zoneinfo.ZoneInfo("Asia/Dubai")
D = Decimal

OPERATOR, STATES, BRANCHES, BRANCH = ("/api/v1/operator/targets", "/api/v1/manager/targets/states",
                                      "/api/v1/manager/targets/branches", "/api/v1/manager/targets/branch")


def at(month, day, hour=10):
    return datetime.datetime(2026, month, day, hour, 0, tzinfo=DUBAI)


@pytest.fixture
def clock(monkeypatch):
    """Set the APIs' clock: clock(at(11, 10))."""
    from apps.incentives import api as incentives_api

    def set_now(moment):
        monkeypatch.setattr(api, "_now", lambda: moment)
        monkeypatch.setattr(incentives_api, "_now", lambda: moment)
        return moment

    set_now(at(11, 10))
    return set_now


@pytest.fixture
def sale(world):  # noqa: F811
    device = Device.objects.get(installation_id="till-1")
    customers = {}

    def make(when, net, branch=None, customer="A"):
        branch = branch or world["auh1"]
        if customer not in customers:
            customers[customer] = make_customer(world, customer_code=f"CU-{customer}",
                                                mobile_no=f"50{len(customers):07d}", first_name=customer)
        return Order.objects.create(
            id=uuid7(), company=branch.company, branch=branch, device=device, customer=customers[customer],
            order_no=f"T-{uuid.uuid4().hex[:8]}", booked_at=when, start_time=when, status=OrderStatus.COMPLETED,
            net_amount=D(net))

    return make


@pytest.fixture
def boss(world):  # noqa: F811
    return User.objects.get(username__iexact="MGR001")


def target(world, boss, branch, amount, kind=TargetKind.SALES, month=11, company=None):  # noqa: F811
    saved, refused = services.create_targets(company or world["company"], [branch], 2026, month, D(amount), "",
                                             boss, now=at(month, 1), kind=kind)
    assert saved and not refused
    return saved[0]


def data(response):
    body = response.json()
    assert response.status_code == 200, body
    return body["data"]


# -- Operator ----------------------------------------------------------------------------------


def test_the_operator_gets_its_own_stations_month(client, world, operator, boss, sale, clock):  # noqa: F811
    target(world, boss, world["auh1"], "3000")
    target(world, boss, world["auh1"], "10", kind=TargetKind.CUSTOMERS)
    target(world, boss, world["auh2"], "9999")                       # another station: never shown
    sale(at(11, 3), "450", customer="A")
    sale(at(11, 4), "300", customer="B")

    got = data(call(client, OPERATOR, {}, token=operator))
    assert got["branch"]["code"] == "AUH01" and got["period"]["label"] == "November 2026"
    assert got["business_date"] == "2026-11-10"
    sales = got["sales"]
    assert (D(sales["target"]), D(sales["achieved"]), D(sales["pending"])) == (D(3000), D(750), D(2250))
    assert sales["percent"] == "25.0" and sales["days_left"] == 21
    assert got["customers"] == {"target": 10, "achieved": 2, "pending": 8, "percent": "20.0",
                                "status": got["customers"]["status"]}


def test_todays_target_is_fixed_all_day_and_recalculated_next_day(client, world, operator, boss, sale, clock):  # noqa: F811
    target(world, boss, world["auh1"], "3000")
    sale(at(11, 3), "450")
    morning = data(call(client, OPERATOR, {}, token=operator))["sales"]["today"]
    assert D(morning["target"]) == (D(2550) / 21).quantize(D("0.001"))       # (3000 - 450) / 21 days left
    assert D(morning["achieved"]) == 0 and morning["met"] is False

    sale(at(11, 10, 12), "500")                                        # a busy afternoon...
    clock(at(11, 10, 18))
    evening = data(call(client, OPERATOR, {}, token=operator))["sales"]["today"]
    assert evening["target"] == morning["target"]                      # ...does not move today's target
    assert D(evening["achieved"]) == 500 and evening["met"] is True

    clock(at(11, 11))
    tomorrow = data(call(client, OPERATOR, {}, token=operator))["sales"]["today"]
    assert tomorrow["date"] == "2026-11-11"
    assert D(tomorrow["target"]) == (D(2050) / 20).quantize(D("0.001"))      # (3000 - 950) / 20


def test_no_target_is_null(client, world, operator, clock):  # noqa: F811
    got = data(call(client, OPERATOR, {}, token=operator))
    assert (got["sales"], got["customers"]) == (None, None)


def test_the_employee_app_gets_no_targets(client, world, staff_app, clock):  # noqa: F811
    for url in (OPERATOR.replace("operator", "employee"), STATES.replace("manager", "employee")):
        reply = call(client, url, {}, token=staff_app)
        assert (reply.status_code, reply.json()["code"]) == (403, "wrong_channel"), url


# -- Manager -----------------------------------------------------------------------------------


def test_the_manager_sees_states_then_branches(client, world, manager, boss, sale, clock):  # noqa: F811
    target(world, boss, world["auh1"], "3000")
    target(world, boss, world["auh2"], "1000")
    sale(at(11, 3), "3600")                                            # AUH01 well above its target
    sale(at(11, 3), "400", branch=world["auh2"])
    other = Branch.objects.get(short_code="OTH01")
    target(world, boss, other, "5000", company=world["other"])         # another company: never counted

    states = data(call(client, STATES, {"year": 2026, "month": 11}, token=manager))
    total = states["total"]
    assert (D(total["target"]), D(total["achieved"]), total["branch_count"]) == (D(4000), D(4000), 2)
    assert D(total["pending"]) == 600                                  # AUH02's shortfall, not hidden by AUH01
    [state] = states["states"]
    assert state["code"] == "AUH" and D(state["pending"]) == 600

    branches = data(call(client, BRANCHES, {"year": 2026, "month": 11, "state_id": state["state_id"]},
                         token=manager))
    assert [(b["code"], D(b["pending"])) for b in branches["branches"]] == [("AUH01", D(0)), ("AUH02", D(600))]
    assert branches["state"]["name"] == state["name"]


def test_the_manager_switches_to_customers_and_defaults_to_this_month(client, world, manager, boss, sale, clock):  # noqa: F811
    target(world, boss, world["auh1"], "5", kind=TargetKind.CUSTOMERS)
    sale(at(11, 2), "10", customer="A")
    sale(at(11, 3), "10", customer="A")
    sale(at(11, 4), "10", customer="B")
    got = data(call(client, STATES, {"kind": "customers"}, token=manager))
    assert got["period"]["month"] == 11 and got["kind"] == "customers"
    assert (got["total"]["target"], got["total"]["achieved"], got["total"]["pending"]) == (5, 2, 3)


def test_one_stations_month_with_its_days(client, world, manager, boss, sale, clock):  # noqa: F811
    target(world, boss, world["auh1"], "3000")
    sale(at(11, 3), "450")
    got = data(call(client, BRANCH, {"year": 2026, "month": 11, "branch_id": world["auh1"].pk}, token=manager))
    assert len(got["days"]) == 30 and got["days"][2]["achieved"] is not None
    assert got["days"][15]["achieved"] is None                         # not reached yet
    assert got["today"]["date"] == "2026-11-10"
    missing = call(client, BRANCH, {"year": 2026, "month": 11, "branch_id": world["auh2"].pk}, token=manager)
    assert (missing.status_code, missing.json()["code"]) == (404, "no_target")


def test_the_month_is_checked(client, world, manager, clock):  # noqa: F811
    reply = call(client, STATES, {"year": 2026}, token=manager)
    assert (reply.status_code, reply.json()["code"]) == (400, "invalid_request")
    reply = call(client, STATES, {"year": 2026, "month": 13}, token=manager)
    assert reply.status_code == 400


def test_the_target_endpoints_are_in_the_api_docs(client, db):
    schema = client.get("/api/schema/").content.decode()
    for path in ("/targets", "targets/states", "targets/branches", "targets/branch"):
        assert path in schema
