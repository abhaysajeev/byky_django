"""What the Employee App's login response reads: apps/crew/services.py's
employee_profile / current_week_roster.

design/device-login.md section 4.
"""

import datetime

import pytest
from django.utils import timezone

from apps.company.models import Branch, BranchType, Company, Country, Location, State
from apps.crew import services
from apps.crew.models import Designation, DutyRoster, DutyRosterDayType, Employee, RosterCategory


@pytest.fixture
def world(db):
    country = Country.objects.create(short_code="AE", name="United Arab Emirates")
    state = State.objects.create(country=country, short_code="AUH", name="Abu Dhabi")
    company = Company.objects.create(
        short_code="BYKY", name="BYKY", country=country, state=state,
        phone_number="+9710000000", email="ops@byky.test",
    )
    location = Location.objects.create(
        country=country, state=state, short_code="CORN", name="Corniche"
    )
    branch = Branch.objects.create(
        company=company, location=location, short_code="AUH01",
        name="Creek Park 1", branch_type=BranchType.STATION,
    )
    cashier = Designation.objects.create(
        company=company, code="CASH", title="Cashier", roster_category=RosterCategory.CASHIER,
    )
    return {"company": company, "branch": branch, "cashier": cashier}


@pytest.fixture
def employee(world):
    return Employee.objects.create(
        company=world["company"], employee_code="BYKY001",
        first_name="Rashed", middle_name="", last_name="K",
        designation=world["cashier"], mobile="0500000000", email="rashed@byky.test",
    )


def test_employee_profile_reads_straight_off_the_record(employee):
    profile = services.employee_profile(employee)
    assert profile["employee_code"] == "BYKY001"
    assert profile["full_name"] == employee.full_name
    assert profile["designation"] == "Cashier"
    assert profile["role"] == "Cashier"
    assert profile["mobile"] == "0500000000"
    assert profile["email"] == "rashed@byky.test"
    assert profile["photo_url"] is None
    assert profile["branch"] is None


def test_employee_profile_role_is_none_for_a_non_roster_designation(world, employee):
    supervisor = Designation.objects.create(company=world["company"], code="SUP", title="Supervisor")
    employee.designation = supervisor
    employee.save(update_fields=["designation"])
    profile = services.employee_profile(employee)
    assert profile["role"] is None
    assert profile["designation"] == "Supervisor"


def test_current_week_roster_is_blank_for_an_unset_day(employee):
    roster = services.current_week_roster(employee)
    assert roster["days"]
    for day in roster["days"]:
        assert day["status"] is None
        assert day["branch"] is None
        assert day["shift1_start"] is None


def test_current_week_roster_shows_the_branch_local_time_not_utc(world, employee):
    """Regression, same class of bug as roster_payload's: a shift saved as
    07:00-23:00 Dubai time must read back as 07:00-23:00, not the raw UTC the
    DateTimeField is stored as (03:00-19:00, Dubai is UTC+4)."""
    today = timezone.localdate()
    zone = services.zone_for(world["company"])
    DutyRoster.objects.create(
        employee=employee, date=today, day_type=DutyRosterDayType.WORKING,
        branch=world["branch"],
        shift1_start=datetime.datetime.combine(today, datetime.time(7, 0), tzinfo=zone),
        shift1_end=datetime.datetime.combine(today, datetime.time(23, 0), tzinfo=zone),
    )
    roster = services.current_week_roster(employee)
    todays = next(d for d in roster["days"] if d["date"] == today.isoformat())
    assert todays["status"] == "Working"
    assert todays["branch"] == "Creek Park 1"
    assert todays["shift1_start"] == "07:00"
    assert todays["shift1_end"] == "23:00"


def test_current_week_roster_week_covers_today(employee):
    today = timezone.localdate()
    roster = services.current_week_roster(employee)
    dates = [d["date"] for d in roster["days"]]
    assert today.isoformat() in dates
    assert roster["week_start"] == dates[0]
    assert roster["week_end"] == dates[-1]
