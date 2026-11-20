"""Calculating a station's month (design/09-incentives.md sections 2, 3, 6):
the worked example, the October staffing shapes, the gates, approval and the
per-employee totals across stations."""

import datetime
import zoneinfo
from decimal import Decimal

import pytest

from apps.company.models import Branch, BranchType
from apps.crew.models import (
    CashierType,
    Designation,
    DutyRoster,
    DutyRosterDayType,
    Employee,
    RosterCategory,
)
from apps.incentives import services
from apps.incentives.models import AppliesTo, Line, RunStatus, Scheme
from apps.incentives.services import IncentiveError
from apps.targets import services as targets
from apps.targets.tests.test_targets import local, sale, staff, two_decimals  # noqa: F401

pytestmark = pytest.mark.django_db
DUBAI = zoneinfo.ZoneInfo("Asia/Dubai")
AFTER = datetime.datetime(2026, 12, 2, 10, 0, tzinfo=DUBAI)       # November has ended
DURING = datetime.datetime(2026, 11, 20, 10, 0, tzinfo=DUBAI)
NOV = datetime.date(2026, 11, 1)
D = Decimal


def monthly_sales(world):
    """The company's monthly sales profile -- the one create_targets() fills,
    as every scheme had after the move to profiles (design/11 §8)."""
    first, last = targets.month_bounds(NOV.year, NOV.month)
    return targets.default_profile(world["company"], targets.TargetKind.SALES, first, last, None)


@pytest.fixture
def people(world):
    company = world["company"]
    cash = Designation.objects.create(company=company, code="CASH", title="Cashier",
                                      roster_category=RosterCategory.CASHIER)
    lab = Designation.objects.create(company=company, code="LAB", title="Labour", roster_category=RosterCategory.LABOUR)

    def person(code, name, designation):
        return Employee.objects.create(company=company, employee_code=code, first_name=name, designation=designation)

    return {"anas": person("BYKY001", "Anas", cash), "riyas": person("BYKY002", "Riyas", cash),
            "jaib": person("BYKY003", "Jaib", lab), "sabeer": person("BYKY004", "Sabeer", lab),
            "vibin": person("BYKY005", "Vibin", lab)}


def roster(employee, day, branch, role, cashier_type="", day_type=DutyRosterDayType.WORKING):
    working = day_type == DutyRosterDayType.WORKING
    start = datetime.datetime(2026, 11, day, 9, 0, tzinfo=DUBAI)
    return DutyRoster.objects.create(
        employee=employee, date=datetime.date(2026, 11, day), day_type=day_type,
        role=role if working else "", cashier_type=cashier_type if working else "",
        branch=branch if working else None,
        shift1_start=start if working else None, shift1_end=start + datetime.timedelta(hours=8) if working else None)


@pytest.fixture
def scheme(world):
    s = Scheme.objects.create(company=world["company"], profile=monthly_sales(world), name="Standard incentive", effective_from=NOV,
                              is_active=True)
    s.components.create(position=1, name="Primary cashier", applies_to=AppliesTo.PRIMARY, formula="basis * 1%")
    s.components.create(position=2, name="Tier 2 share", applies_to=AppliesTo.TIER2,
                        formula="basis * 1% / max(tier2, 2)")
    return s


@pytest.fixture
def example(world, staff, sale, people, scheme):  # noqa: F811
    """The design's worked example: target 4,000, achieved 5,000 over 4
    working days -> basis 1,250."""
    adc1 = world["adc1"]
    targets.create_targets(world["company"], [adc1], 2026, 11, D("4000"), "", staff, now=local(1))
    sale(local(3), "5000")
    p = people
    roster(p["anas"], 5, adc1, "cashier", CashierType.PRIMARY)          # primary + 1 labour
    roster(p["jaib"], 5, adc1, "labour")
    roster(p["anas"], 6, adc1, "cashier", CashierType.PRIMARY)          # primary + secondary + 2 labour
    roster(p["riyas"], 6, adc1, "cashier", CashierType.SECONDARY)
    roster(p["jaib"], 6, adc1, "labour")
    roster(p["sabeer"], 6, adc1, "labour")
    roster(p["anas"], 7, adc1, "cashier", CashierType.PRIMARY)          # primary alone
    roster(p["sabeer"], 8, adc1, "labour")                               # labour, no cashier
    roster(p["vibin"], 8, adc1, "labour", day_type=DutyRosterDayType.WEEK_OFF)   # off: unpaid
    return p


def amounts(run):
    return sorted((ln.date.day, ln.employee.first_name, ln.component, ln.amount) for ln in run.lines.all())


def test_the_worked_example(world, staff, example):  # noqa: F811
    run = services.calculate(world["adc1"], NOV, staff, now=AFTER)
    assert (run.achieved, run.work_days, run.basis) == (D("5000"), 4, D("1250.000"))
    assert amounts(run) == [
        (5, "Anas", "Primary cashier", D("12.50")), (5, "Jaib", "Tier 2 share", D("6.25")),
        (6, "Anas", "Primary cashier", D("12.50")), (6, "Jaib", "Tier 2 share", D("4.17")),
        (6, "Riyas", "Tier 2 share", D("4.17")), (6, "Sabeer", "Tier 2 share", D("4.17")),
        (7, "Anas", "Primary cashier", D("12.50")),
        (8, "Sabeer", "Tier 2 share", D("6.25")),
    ]
    assert run.total == D("62.51") and run.status == RunStatus.DRAFT
    line = Line.objects.get(date=datetime.date(2026, 11, 6), employee__first_name="Riyas")
    assert (line.role, line.inputs["tier2"], line.inputs["basis"]) == ("secondary", "3", "1250")


def test_recalculating_replaces_the_lines(world, staff, example):  # noqa: F811
    services.calculate(world["adc1"], NOV, staff, now=AFTER)
    DutyRoster.objects.filter(date=datetime.date(2026, 11, 8)).delete()
    run = services.calculate(world["adc1"], NOV, staff, now=AFTER)
    assert run.work_days == 3 and run.basis == D("1666.667")
    assert Line.objects.filter(run=run, date=datetime.date(2026, 11, 8)).count() == 0


@pytest.mark.parametrize(("now", "message"), [(DURING, "is not over yet")])
def test_a_month_not_over_is_refused(world, staff, example, now, message):  # noqa: F811
    with pytest.raises(IncentiveError, match=message):
        services.calculate(world["adc1"], NOV, staff, now=now)


def test_a_target_not_met_is_refused(world, staff, sale, people, scheme):  # noqa: F811
    targets.create_targets(world["company"], [world["adc1"]], 2026, 11, D("9000"), "", staff, now=local(1))
    sale(local(3), "5000")
    with pytest.raises(IncentiveError, match="did not meet"):
        services.calculate(world["adc1"], NOV, staff, now=AFTER)
    info = services.eligibility(world["adc1"], NOV, now=AFTER)
    assert (info["can_calculate"], info["reason"]) == (False, "Target not met")


def test_no_target_and_no_scheme_are_refused(world, staff, people):  # noqa: F811
    with pytest.raises(IncentiveError, match="no sales target"):
        services.calculate(world["adc1"], NOV, staff, now=AFTER)
    targets.create_targets(world["company"], [world["adc1"]], 2026, 11, D("10"), "", staff, now=local(1))
    with pytest.raises(IncentiveError, match="No active scheme"):
        services.calculate(world["adc1"], NOV, staff, now=AFTER)


def test_an_approved_run_is_final(world, staff, example, scheme):  # noqa: F811
    run = services.calculate(world["adc1"], NOV, staff, now=AFTER)
    services.approve(run, staff, now=AFTER)
    assert run.is_approved and scheme.is_used
    with pytest.raises(IncentiveError, match="approved"):
        services.calculate(world["adc1"], NOV, staff, now=AFTER)
    with pytest.raises(IncentiveError, match="already approved"):
        services.approve(run, staff)


def test_an_employee_is_paid_station_by_station(world, staff, example, sale):  # noqa: F811
    zabeel = Branch.objects.create(company=world["company"], location=world["adc1"].location, short_code="ZAB",
                                   name="Zabeel Park 1", branch_type=BranchType.STATION)
    targets.create_targets(world["company"], [zabeel], 2026, 11, D("100"), "", staff, now=local(1))
    sale(local(4), "1000", branch=zabeel)
    roster(example["jaib"], 9, zabeel, "labour")                 # one day at Zabeel, alone
    services.calculate(world["adc1"], NOV, staff, now=AFTER)
    services.calculate(zabeel, NOV, staff, now=AFTER)
    totals = {r["employee"].first_name: r for r in services.employee_totals(world["company"],
                                                                           [world["adc1"], zabeel], NOV)}
    jaib = totals["Jaib"]
    # Creek: 6.25 + 4.17; Zabeel: basis 1,000 / 1 day, alone -> 0.5% = 5.00
    assert [(s["branch"].name, s["days"], s["amount"]) for s in jaib["stations"]] == [
        (world["adc1"].name, 2, D("10.42")), ("Zabeel Park 1", 1, D("5.00"))]
    assert (jaib["draft"], jaib["approved"], jaib["days"]) == (D("15.42"), D("0"), 3)


def test_a_station_specific_scheme_wins(world, staff, example, scheme):  # noqa: F811
    special = Scheme.objects.create(company=world["company"], profile=monthly_sales(world), name="Corniche", effective_from=NOV, is_active=True)
    special.branches.set([world["adc1"]])
    special.components.create(position=1, name="Flat", applies_to=AppliesTo.EVERYONE, formula="10")
    run = services.calculate(world["adc1"], NOV, staff, now=AFTER)
    assert run.scheme == special and {ln.amount for ln in run.lines.all()} == {D("10.00")}


def test_validate_names_every_problem():
    errors = services.validate("achieved >= target and role", [("rate", "1% / max(tier2, 2)"), ("basis", "1")],
                               [("Tier 2", "tier2", "basis * rate"), ("", "nobody", "bogus")])
    messages = " | ".join(f"{e['field']}: {e['message']}" for e in errors)
    assert "Pays when" in messages and "role" in messages
    assert "Variable basis: That name is already a variable." in messages
    assert "Choose who it applies to" in messages and "Unknown variable: bogus" in messages
    assert "Variable rate" not in messages and "Component Tier 2" not in messages


def test_try_it_shows_one_real_day(world, staff, example, scheme):  # noqa: F811
    view = services.try_scheme(scheme, world["adc1"], datetime.date(2026, 11, 6), now=AFTER)
    got = {p["employee"].first_name: p["amount"] for p in view["people"]}
    assert got == {"Anas": D("12.50"), "Riyas": D("4.17"), "Jaib": D("4.17"), "Sabeer": D("4.17")}
    assert view["pays_now"] is True and view["counts"]["tier2"] == D(3)
