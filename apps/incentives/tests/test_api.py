"""Incentives for the device apps (apps/incentives/api.py): the manager's
stations, employees and runs; the employee's own year and month, approved
amounts only. The operator app gets none."""

import datetime
from decimal import Decimal

import pytest

from apps.crew.models import CashierType, DutyRoster, DutyRosterDayType
from apps.crew.tests.test_attendance import (  # noqa: F401
    fresh_throttle,
    manager,
    operator,
    staff_app,
    world,
)
from apps.fare.tests.test_api import call
from apps.incentives import services
from apps.incentives.models import AppliesTo, Scheme
from apps.targets import services as targets
from apps.targets.tests.test_api import DUBAI, at, boss, clock, data, sale, target  # noqa: F401

D = Decimal
NOV = datetime.date(2026, 11, 1)
DEC_2 = datetime.datetime(2026, 12, 2, 10, 0, tzinfo=DUBAI)

BRANCHES, EMPLOYEES, RUN = ("/api/v1/manager/incentives/branches", "/api/v1/manager/incentives/employees",
                            "/api/v1/manager/incentives/run")
YEAR, MONTH = "/api/v1/employee/incentives/year", "/api/v1/employee/incentives/month"


def roster(employee, day, branch=None, day_type=DutyRosterDayType.WORKING):
    working = day_type == DutyRosterDayType.WORKING
    start = datetime.datetime(2026, 11, day, 9, 0, tzinfo=DUBAI)
    return DutyRoster.objects.create(
        employee=employee, date=datetime.date(2026, 11, day), day_type=day_type,
        role="cashier" if working else "", cashier_type=CashierType.PRIMARY if working else "",
        branch=branch if working else None,
        shift1_start=start if working else None, shift1_end=start + datetime.timedelta(hours=8) if working else None)


def monthly_sales(world):  # noqa: F811
    """The company's monthly sales profile -- the one create_targets() fills,
    as every scheme had after the move to profiles (design/11 §8)."""
    first, last = targets.month_bounds(NOV.year, NOV.month)
    return targets.default_profile(world["company"], targets.TargetKind.SALES, first, last, None)


@pytest.fixture
def month(world, boss, sale, clock):  # noqa: F811
    """November: Anil works AUH01 on the 5th (target met: 5,000 of 4,000) and
    AUH02 on the 6th (missed: 1,000 of 4,000), off on the 7th. AUH01's run is
    calculated (a draft); December 2 now."""
    scheme = Scheme.objects.create(company=world["company"], profile=monthly_sales(world), name="Standard incentive", effective_from=NOV,
                                   is_active=True)
    scheme.components.create(position=1, name="Primary cashier", applies_to=AppliesTo.PRIMARY, formula="basis * 1%")
    target(world, boss, world["auh1"], "4000")
    target(world, boss, world["auh2"], "4000")
    sale(at(11, 3), "5000")
    sale(at(11, 4), "1000", branch=world["auh2"])
    anil = world["staff"]
    roster(anil, 5, world["auh1"])
    roster(anil, 6, world["auh2"])
    roster(anil, 7, day_type=DutyRosterDayType.WEEK_OFF)
    clock(DEC_2)
    run = services.calculate(world["auh1"], NOV, boss, now=DEC_2)
    return {"run": run, "anil": anil}


# -- Manager ------------------------------------------------------------------------------------


def test_the_manager_sees_each_stations_month(client, world, manager, month):  # noqa: F811
    got = data(call(client, BRANCHES, {"year": 2026, "month": 11}, token=manager))
    rows = {r["code"]: r for r in got["branches"]}
    assert rows["AUH01"]["met"] is True and rows["AUH01"]["run"]["status"] == "draft"
    assert D(rows["AUH01"]["run"]["total"]) == D(50)                  # 5,000 / 1 working day x 1%
    assert rows["AUH02"]["met"] is False and rows["AUH02"]["run"] is None
    assert got["totals"]["stations"] == 2 and got["totals"]["met"] == 1
    assert D(got["totals"]["draft_total"]) == 50 and D(got["totals"]["approved_total"]) == 0


def test_the_manager_sees_employees_and_a_run(client, world, manager, month):  # noqa: F811
    [anil] = data(call(client, EMPLOYEES, {"year": 2026, "month": 11}, token=manager))["employees"]
    assert anil["employee_code"] == "EMP001" and D(anil["total"]) == 50 and D(anil["draft"]) == 50
    assert [s["status"] for s in anil["stations"]] == ["draft"]

    run = data(call(client, RUN, {"year": 2026, "month": 11, "branch_id": world["auh1"].pk}, token=manager))
    assert run["work_days"] == 1 and D(run["basis"]) == 5000 and run["scheme"] == "Standard incentive"
    assert run["days"][0]["date"] == "2026-11-05" and run["employees"][0]["roles"] == {"primary": 1}
    missing = call(client, RUN, {"year": 2026, "month": 11, "branch_id": world["auh2"].pk}, token=manager)
    assert (missing.status_code, missing.json()["code"]) == (404, "no_run")


# -- Employee -----------------------------------------------------------------------------------


def test_a_draft_pays_nothing_yet(client, world, staff_app, month):  # noqa: F811
    got = data(call(client, MONTH, {"year": 2026, "month": 11}, token=staff_app))
    assert got["status"] == "pending_approval" and D(got["amount"]) == 0
    year = data(call(client, YEAR, {"year": 2026}, token=staff_app))
    assert D(year["total_earned"]) == 0 and year["months"][0]["status"] == "pending_approval"


def test_my_month_once_approved(client, world, staff_app, boss, month):  # noqa: F811
    services.approve(month["run"], boss)
    got = data(call(client, MONTH, {"year": 2026, "month": 11}, token=staff_app))
    assert (got["status"], D(got["amount"]), got["days_worked"], got["branches"]) == ("approved", D(50), 2, 2)
    assert D(got["amount_to_achieve"]) == 3000                         # AUH02's shortfall

    summary = {s["name"]: s for s in got["branch_summary"]}
    assert [s["name"] for s in got["branch_summary"]] == sorted(summary, key=str.lower)
    auh1, auh2 = summary[world["auh1"].name], summary[world["auh2"].name]
    assert (auh1["target_achieved"], D(auh1["amount"]), auh1["days"]) == (True, D(50), 1)
    assert (auh2["target_achieved"], D(auh2["pending"]), D(auh2["amount"])) == (False, D(3000), D(0))

    days = {d["date"]: d for d in got["calendar"]}
    assert len(got["calendar"]) == 30
    fifth, sixth, seventh = days["2026-11-05"], days["2026-11-06"], days["2026-11-07"]
    assert (fifth["branch"]["name"], fifth["role"], D(fifth["amount"])) == (world["auh1"].name, "primary", D(50))
    assert fifth["target"] is not None and fifth["achieved"] is not None and fifth["to_target"] is not None
    assert (sixth["branch"]["name"], D(sixth["amount"])) == (world["auh2"].name, D(0))
    assert (seventh["day_type"], seventh["branch"], seventh["amount"]) == ("week_off", None, None)
    assert days["2026-11-20"]["day_type"] == "not_set"


def test_my_year(client, world, staff_app, boss, month):  # noqa: F811
    services.approve(month["run"], boss)
    got = data(call(client, YEAR, {}, token=staff_app))
    assert (got["year"], D(got["total_earned"]), got["shifts_logged"]) == (2026, D(50), 2)
    [nov] = got["months"]
    assert (nov["label"], nov["days_worked"], nov["branches"], nov["status"]) == ("November 2026", 2, 2, "approved")
    assert nov["target_achieved"] is False                             # AUH02 missed its target


def test_a_month_in_progress(client, world, staff_app, clock):  # noqa: F811
    roster(world["staff"], 5, world["auh1"])
    got = data(call(client, MONTH, {"year": 2026, "month": 11}, token=staff_app))
    assert got["status"] == "in_progress" and got["branch_summary"][0]["target"] is None


# -- Gates --------------------------------------------------------------------------------------


def test_the_operator_app_gets_no_incentives(client, world, operator, clock):  # noqa: F811
    for url in (YEAR, MONTH, BRANCHES, EMPLOYEES):
        reply = call(client, url.replace("/v1/employee/", "/v1/operator/").replace("/v1/manager/", "/v1/operator/"),
                     {}, token=operator)
        assert (reply.status_code, reply.json()["code"]) == (403, "wrong_channel"), url


def test_each_app_only_its_own_calls(client, world, manager, staff_app, clock):  # noqa: F811
    assert call(client, YEAR.replace("/v1/employee/", "/v1/manager/"), {}, token=manager).json()["code"] == (
        "wrong_channel")
    assert call(client, BRANCHES.replace("/v1/manager/", "/v1/employee/"), {}, token=staff_app).json()["code"] == (
        "wrong_channel")


def test_the_incentive_endpoints_are_in_the_api_docs(client, db):
    schema = client.get("/api/schema/").content.decode()
    for path in ("incentives/branches", "incentives/employees", "incentives/run", "incentives/year",
                 "incentives/month"):
        assert path in schema


# -- Target Profiles (design/11 §7) --------------------------------------------------------------


def _special(world, boss, payout):  # noqa: F811
    """A one-day special on 5 Nov at AUH01 paying a flat 10."""
    from apps.company.models import CompanyConfig
    from apps.incentives.models import Payout
    from apps.targets.models import PeriodType, TargetProfile, TargetProfileBranch

    CompanyConfig.objects.get_or_create(company=world["company"], defaults={"financial_year_start": 1})
    fifth = datetime.date(2026, 11, 5)
    profile = TargetProfile.objects.create(company=world["company"], name="5 Nov special", metric="sales",
                                           period_type=PeriodType.DAILY, starts_on=fifth, ends_on=fifth,
                                           is_active=True)
    TargetProfileBranch.objects.create(profile=profile, branch=world["auh1"], amount=D("100"))
    targets.sync_profile(profile, boss, now=at(11, 1))
    scheme = Scheme.objects.create(company=world["company"], profile=profile, name="Special", effective_from=fifth,
                                   is_active=True, payout=getattr(Payout, payout))
    scheme.components.create(position=1, name="Flat", applies_to=AppliesTo.EVERYONE, formula="10")
    return profile


def test_runs_of_two_profiles_and_their_payout_months(client, world, manager, staff_app, boss, sale, month):  # noqa: F811
    sale(at(11, 5), "300")
    special = _special(world, boss, "FINANCIAL_YEAR_END")             # FY = calendar year: paid in December
    run = services.calculate(world["auh1"], datetime.date(2026, 11, 5), boss, now=DEC_2, profile=special)
    assert run.payout_month == datetime.date(2026, 12, 1)
    services.approve(run, boss)
    monthly = services.calculate(world["auh1"], NOV, boss, now=DEC_2)          # again, with the 5th's sale
    services.approve(monthly, boss)

    by_id = data(call(client, RUN, {"run_id": run.pk}, token=manager))
    assert (by_id["profile"]["name"], by_id["payout_month"], by_id["period"]["label"]) == (
        "5 Nov special", "2026-12", "5 Nov 2026")
    old = data(call(client, RUN, {"year": 2026, "month": 11, "branch_id": world["auh1"].pk}, token=manager))
    assert old["run_id"] == monthly.pk and old["period"]["month"] == 11          # the old call: the monthly run

    nov = data(call(client, EMPLOYEES, {"year": 2026, "month": 11}, token=manager))["employees"]
    dec = data(call(client, EMPLOYEES, {"year": 2026, "month": 12}, token=manager))["employees"]
    assert [s["profile"]["name"] for s in nov[0]["stations"]] == ["Monthly sales"]
    assert [(s["profile"]["name"], D(s["amount"])) for s in dec[0]["stations"]] == [("5 Nov special", D(10))]

    year = data(call(client, YEAR, {"year": 2026}, token=staff_app))
    months = {m["month"]: m for m in year["months"]}
    assert D(months[12]["amount"]) == 10 and months[12]["profiles"][0]["name"] == "5 Nov special"
    assert D(year["total_earned"]) == D(months[11]["amount"]) + 10

    december = data(call(client, MONTH, {"year": 2026, "month": 12}, token=staff_app))
    assert [(p["profile"], D(p["amount"])) for p in december["payouts"]] == [("5 Nov special", D(10))]
    assert D(december["paid_in_month"]) == 10
    november = data(call(client, MONTH, {"year": 2026, "month": 11}, token=staff_app))
    fifth = next(d for d in november["calendar"] if d["date"] == "2026-11-05")
    assert {p["name"] for p in fifth["profiles"]} == {"Monthly sales", "5 Nov special"}

    branches = data(call(client, BRANCHES, {"profile_id": special.pk, "date": "2026-11-05"}, token=manager))
    assert branches["profile"]["name"] == "5 Nov special" and branches["branches"][0]["run"]["run_id"] == run.pk
