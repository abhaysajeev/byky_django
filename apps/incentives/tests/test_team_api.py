"""Team Incentive in the employee app (owner, 24 Nov 2026): the manager's
three incentive calls on /api/v1/employee/, by the designation's Team Scope,
approved runs only, each person's amount, money only from the caller's
stations, and a profile picker. No new calculation: the approved lines."""

import datetime
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.company.models import AuthorityType, BranchApprovalAuthority
from apps.crew.models import Designation, Employee, TeamScope
from apps.crew.tests import test_attendance as base
from apps.crew.tests.test_attendance import (  # noqa: F401
    fresh_throttle,
    manager,
    operator,
    staff_app,
    world,
)
from apps.devices.models import Device, DeviceStatus
from apps.fare.tests.test_api import call
from apps.incentives import services
from apps.incentives.tests.test_api import DEC_2, NOV, _special, month, roster  # noqa: F401
from apps.targets.tests.test_api import at, boss, clock, data, sale, target  # noqa: F401
from core.enums import Channel
from core.models import User

BRANCHES, EMPLOYEES, RUN = ("/api/v1/employee/incentives/branches", "/api/v1/employee/incentives/employees",
                            "/api/v1/employee/incentives/run")
NOV_Q = {"year": 2026, "month": 11}


def signed_in(client, world, code, scope, stations=()):  # noqa: F811
    designation = Designation.objects.create(company=world["company"], code=f"D{code}", title=f"Title {code}",
                                             attendance_method="self", team_scope=scope)
    employee = Employee.objects.create(company=world["company"], employee_code=code, first_name=code,
                                            last_name="K", designation=designation)
    User.objects.create_user(code, base.PASSWORD, display_name=code, company=world["company"],
                             role=world["role"], employee=employee, allowed_channels=[Channel.EMPLOYEE])
    Device.objects.create(company=world["company"], installation_id=f"phone-{code}", platform="android",
                          channel=Channel.EMPLOYEE, status=DeviceStatus.APPROVED, approved_at=timezone.now())
    for branch in stations:
        BranchApprovalAuthority.objects.create(branch=branch, employee=employee,
                                               authority_type=AuthorityType.BRANCH_SUPERVISOR)
    return base.login(client, "employee", code, f"phone-{code}")


@pytest.fixture
def supervisor(client, world):  # noqa: F811
    """Branch Supervisor of AUH01 only."""
    return signed_in(client, world, "SUP001", TeamScope.BRANCHES, [world["auh1"]])


@pytest.fixture
def area(client, world):  # noqa: F811
    """A Team Scope of the whole company."""
    return signed_in(client, world, "PRO001", TeamScope.COMPANY)


@pytest.fixture
def both_approved(world, boss, sale, month):  # noqa: F811
    """Anil's AUH01 and AUH02 runs, both approved (AUH02 topped up to its target)."""
    sale(at(11, 6), "3000", branch=world["auh2"])
    services.approve(month["run"], boss)
    other = services.calculate(world["auh2"], NOV, boss, now=DEC_2)
    services.approve(other, boss)
    return {"auh1": month["run"], "auh2": other}


def test_a_draft_run_is_not_shown(client, world, supervisor, month):  # noqa: F811
    branches = data(call(client, BRANCHES, NOV_Q, token=supervisor))
    [row] = branches["branches"]
    assert (row["code"], row["run"]) == ("AUH01", None)                # calculated, but a draft
    assert data(call(client, EMPLOYEES, NOV_Q, token=supervisor))["employees"] == []
    reply = call(client, RUN, {"run_id": month["run"].pk}, token=supervisor)
    assert (reply.status_code, reply.json()["code"]) == (404, "no_run")


def test_a_supervisor_sees_their_stations_approved_money(client, world, supervisor, both_approved):  # noqa: F811
    branches = data(call(client, BRANCHES, NOV_Q, token=supervisor))
    assert [row["code"] for row in branches["branches"]] == ["AUH01"]     # not AUH02
    assert branches["branches"][0]["run"]["status"] == "approved"
    assert Decimal(branches["totals"]["draft_total"]) == 0

    [anil] = data(call(client, EMPLOYEES, NOV_Q, token=supervisor))["employees"]
    # Anil also earned at AUH02: only the AUH01 money is the supervisor's to see.
    assert [s["name"] for s in anil["stations"]] == ["Creek Park 1"]
    assert anil["total"] == anil["approved"] == anil["stations"][0]["amount"]
    assert Decimal(anil["draft"]) == 0

    run = data(call(client, RUN, {"run_id": both_approved["auh1"].pk}, token=supervisor))
    assert run["employees"][0]["name"] == "Anil K"
    outside = call(client, RUN, {"run_id": both_approved["auh2"].pk}, token=supervisor)
    assert (outside.status_code, outside.json()["code"]) == (404, "no_run")


def test_a_company_scope_sees_every_station(client, world, area, both_approved):  # noqa: F811
    branches = data(call(client, BRANCHES, NOV_Q, token=area))
    assert [row["code"] for row in branches["branches"]] == ["AUH01", "AUH02"]
    [anil] = data(call(client, EMPLOYEES, NOV_Q, token=area))["employees"]
    assert sorted(s["name"] for s in anil["stations"]) == ["Creek Park 1", "Creek Park 2"]


def test_own_scope_is_refused(client, world, staff_app, month):  # noqa: F811
    for url in (BRANCHES, EMPLOYEES, RUN):
        reply = call(client, url, {"run_id": month["run"].pk}, token=staff_app)
        assert (reply.status_code, reply.json()["code"]) == (403, "no_team_scope"), url


def test_no_stations_assigned_is_an_empty_team(client, world, month):  # noqa: F811
    token = signed_in(client, world, "SUP002", TeamScope.BRANCHES)
    body = call(client, BRANCHES, NOV_Q, token=token).json()
    assert (body["code"], body["message"], body["data"]["branches"]) == ("ok", "No stations are assigned to you.", [])


def test_the_profile_picker(client, world, area, boss, sale, both_approved):  # noqa: F811
    sale(at(11, 5), "300")
    special = _special(world, boss, "PERIOD_END_MONTH")
    run = services.calculate(world["auh1"], datetime.date(2026, 11, 5), boss, now=DEC_2, profile=special)
    services.approve(run, boss)

    everything = data(call(client, EMPLOYEES, NOV_Q, token=area))["employees"]
    only = data(call(client, EMPLOYEES, {**NOV_Q, "profile_id": special.pk}, token=area))["employees"]
    names = {s["profile"]["name"] for p in everything for s in p["stations"]}
    assert "5 Nov special" in names and len(names) == 2
    assert {s["profile"]["name"] for p in only for s in p["stations"]} == {"5 Nov special"}
    missing = call(client, EMPLOYEES, {**NOV_Q, "profile_id": 999999}, token=area)
    assert (missing.status_code, missing.json()["code"]) == (404, "no_profile")


def test_the_manager_reply_is_unchanged(client, world, manager, month):  # noqa: F811
    """Drafts included, as before."""
    [anil] = data(call(client, EMPLOYEES.replace("/employee/", "/manager/"), NOV_Q, token=manager))["employees"]
    assert Decimal(anil["draft"]) > 0 and Decimal(anil["approved"]) == 0
    [row] = [r for r in data(call(client, BRANCHES.replace("/employee/", "/manager/"), NOV_Q,
                                  token=manager))["branches"] if r["code"] == "AUH01"]
    assert row["run"]["status"] == "draft"


def test_the_operator_still_gets_none(client, world, operator, clock):  # noqa: F811
    for url in (BRANCHES, EMPLOYEES, RUN):
        reply = call(client, url.replace("/employee/", "/operator/"), {"run_id": 1}, token=operator)
        assert (reply.status_code, reply.json()["code"]) == (403, "wrong_channel"), url
