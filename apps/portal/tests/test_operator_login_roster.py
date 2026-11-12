"""Operator login and the duty roster (apps/crew/services.py::
operator_roster_refusal, owner 28 Oct 2026): an employee signs in on a station
tablet only when today's roster has them Working at that station, inside their
shift -- exact times. Every operator login is checked, whatever the designation
(owner, 7 Nov 2026 -- it was Cashier / Labour only). Every operator login also
returns the employee's roster for the current week.

The check is off in other apps' tests (conftest.py); this module turns it on."""

import datetime

import pytest
from django.utils import timezone

from apps.company.models import Branch, BranchType
from apps.crew import duty
from apps.crew.models import Designation, DutyRoster, DutyRosterDayType, RosterCategory
from apps.crew.services import current_week_roster, operator_roster_refusal
from apps.portal.tests import test_operator_login_api as login_api
from core.timezones import business_date_for, zone_for

world, employee, app_user, device = login_api.world, login_api.employee, login_api.app_user, login_api.device
fresh_throttle, _login = login_api.fresh_throttle, login_api._login

HOUR = datetime.timedelta(hours=1)

pytestmark = pytest.mark.roster_gate


@pytest.fixture
def rostered(world, employee):
    """Rashed is a Cashier: the roster applies to him."""
    employee.designation.roster_category = RosterCategory.CASHIER
    employee.designation.save()
    return employee


@pytest.fixture
def other_station(world):
    return Branch.objects.create(company=world["company"], location=world["branch"].location, short_code="AUH02",
                                 name="Creek Park 2", branch_type=BranchType.STATION)


def roster(employee, branch, start, end, *, day=None, day_type=DutyRosterDayType.WORKING, shift2=None,
           role=RosterCategory.CASHIER):
    day = day or business_date_for(employee.company, start)
    working = day_type == DutyRosterDayType.WORKING
    return DutyRoster.objects.create(
        employee=employee, date=day, day_type=day_type, branch=branch if working else None,
        role=role if working else "",
        shift1_start=start if working else None, shift1_end=end if working else None,
        shift2_start=shift2[0] if shift2 else None, shift2_end=shift2[1] if shift2 else None,
    )


def refused(client, app_user, device):
    body = _login(client, app_user, device).json()
    return body["code"], body["message"]


# -- Through the login call --------------------------------------------------------------


def test_working_here_inside_the_shift_signs_in(client, world, rostered, app_user, device):
    now = timezone.now()
    roster(rostered, world["branch"], now - HOUR, now + HOUR)
    assert _login(client, app_user, device).json()["code"] == "ok"


def test_no_roster_today(client, world, rostered, app_user, device):
    assert refused(client, app_user, device) == ("duty_roster_not_found", "Duty Roster not found today.")


def test_not_working_today(client, world, rostered, app_user, device):
    now = timezone.now()
    roster(rostered, None, now, now, day=business_date_for(world["company"], now),
           day_type=DutyRosterDayType.WEEK_OFF)
    assert refused(client, app_user, device) == ("not_working_today", "Not working today (Week Off).")


def test_rostered_at_another_station(client, world, rostered, app_user, device, other_station):
    now = timezone.now()
    roster(rostered, other_station, now - HOUR, now + HOUR)
    assert refused(client, app_user, device) == ("rostered_at_another_branch", "Rostered at Creek Park 2 today.")


def test_outside_the_shift(client, world, rostered, app_user, device):
    now = timezone.now()
    day = business_date_for(world["company"], now)
    zone = zone_for(world["company"])
    start = datetime.datetime.combine(day, datetime.time(0, 0), tzinfo=zone)
    roster(rostered, world["branch"], start, start + datetime.timedelta(minutes=1), day=day)
    code, message = refused(client, app_user, device)
    assert (code, message) == ("outside_shift_hours", "Your shift today is 00:00-00:01.")


def test_every_designation_is_checked(client, world, employee, app_user, device):
    """A manager, supervisor or admin on a tablet is checked too (owner, 7 Nov 2026)."""
    assert employee.designation.roster_category == ""          # neither cashier nor labour
    assert refused(client, app_user, device) == ("duty_roster_not_found", "Duty Roster not found today.")

    now = timezone.now()
    roster(employee, world["branch"], now - HOUR, now + HOUR)
    assert _login(client, app_user, device).json()["code"] == "ok"


# -- The week's roster in the login reply (owner, 29 Oct 2026) ----------------------------


def test_the_login_returns_the_weeks_roster_like_the_employee_login(client, world, rostered, app_user, device):
    now = timezone.now()
    today = business_date_for(world["company"], now)
    roster(rostered, world["branch"], now - HOUR, now + HOUR)
    week = duty.current_week(world["company"])
    off = next((day for day in week if day != today), None)
    if off:
        roster(rostered, None, now, now, day=off, day_type=DutyRosterDayType.WEEK_OFF)

    sent = _login(client, app_user, device).json()["data"]["roster"]

    assert sent == current_week_roster(rostered)
    assert (sent["week_start"], sent["week_end"]) == (week[0].isoformat(), week[-1].isoformat())
    days = {day["date"]: day for day in sent["days"]}
    assert (days[today.isoformat()]["status"], days[today.isoformat()]["branch_id"]) == (
        "Working", world["branch"].pk)
    if off:
        assert days[off.isoformat()]["status"] == "Week Off"


def test_any_designation_rostered_today_gets_the_week_with_only_today_set(client, world, employee, app_user,
                                                                         device):
    now = timezone.now()
    today = business_date_for(world["company"], now)
    roster(employee, world["branch"], now - HOUR, now + HOUR)

    body = _login(client, app_user, device).json()

    assert body["code"] == "ok"
    sent = body["data"]["roster"]
    assert [day["date"] for day in sent["days"]] == [day.isoformat() for day in duty.current_week(world["company"])]
    assert {day["date"]: day["status"] for day in sent["days"]}[today.isoformat()] == "Working"
    assert {day["status"] for day in sent["days"] if day["date"] != today.isoformat()} <= {None}


# -- The rule itself, at fixed times -----------------------------------------------------


def at(world, day, hhmm):
    hour, minute = (int(part) for part in hhmm.split(":"))
    return datetime.datetime.combine(day, datetime.time(hour, minute), tzinfo=zone_for(world["company"]))


def test_exact_shift_edges_and_a_second_shift(world, rostered):
    day = datetime.date(2026, 10, 28)
    branch = world["branch"]
    roster(rostered, branch, at(world, day, "08:00"), at(world, day, "12:00"), day=day,
           shift2=(at(world, day, "16:00"), at(world, day, "20:00")))
    assert operator_roster_refusal(rostered, branch, at(world, day, "08:00")) is None
    assert operator_roster_refusal(rostered, branch, at(world, day, "12:00")) is None
    assert operator_roster_refusal(rostered, branch, at(world, day, "17:30")) is None
    assert operator_roster_refusal(rostered, branch, at(world, day, "07:59")) == (
        "outside_shift_hours", "Your shift today is 08:00-12:00 and 16:00-20:00.")
    assert operator_roster_refusal(rostered, branch, at(world, day, "14:00"))[0] == "outside_shift_hours"


def test_an_overnight_shift_from_yesterday_counts(world, rostered):
    yesterday, today = datetime.date(2026, 10, 27), datetime.date(2026, 10, 28)
    branch = world["branch"]
    roster(rostered, branch, at(world, yesterday, "22:00"), at(world, today, "02:00"), day=yesterday)
    assert operator_roster_refusal(rostered, branch, at(world, today, "01:00")) is None
    assert operator_roster_refusal(rostered, branch, at(world, today, "03:00")) == (
        "duty_roster_not_found", "Duty Roster not found today.")


def test_leave_days_read_their_type(world, rostered):
    day = datetime.date(2026, 10, 28)
    roster(rostered, None, None, None, day=day, day_type=DutyRosterDayType.SICK_LEAVE)
    assert operator_roster_refusal(rostered, world["branch"], at(world, day, "10:00")) == (
        "not_working_today", "Not working today (Sick Leave).")


def test_labour_with_no_roster_is_told_there_is_none(world, rostered):
    rostered.designation.roster_category = RosterCategory.LABOUR
    rostered.designation.save()
    assert operator_roster_refusal(rostered, world["branch"])[0] == "duty_roster_not_found"
    assert Designation.objects.get(pk=rostered.designation_id).roster_category == "labour"


# -- Only a Cashier signs in (owner, 11 Nov 2026) ------------------------------------------

LABOUR_REFUSAL = ("not_rostered_as_cashier",
                  "You are rostered as Labour today. Only a Cashier can sign in on this tablet.")
NO_ROLE_REFUSAL = ("not_rostered_as_cashier",
                   "You are not rostered as a Cashier today. Only a Cashier can sign in on this tablet.")


def test_a_labour_day_is_refused_through_the_login(client, world, rostered, app_user, device):
    now = timezone.now()
    roster(rostered, world["branch"], now - HOUR, now + HOUR, role=RosterCategory.LABOUR)

    assert refused(client, app_user, device) == LABOUR_REFUSAL


def test_a_cashier_day_signs_in(client, world, rostered, app_user, device):
    now = timezone.now()
    roster(rostered, world["branch"], now - HOUR, now + HOUR, role=RosterCategory.CASHIER)

    assert _login(client, app_user, device).json()["code"] == "ok"


def test_a_labour_day_gets_the_real_reason_outside_the_shift_too(world, rostered):
    day = datetime.date(2026, 10, 28)
    roster(rostered, world["branch"], at(world, day, "08:00"), at(world, day, "16:00"), day=day,
           role=RosterCategory.LABOUR)

    assert operator_roster_refusal(rostered, world["branch"], at(world, day, "20:00")) == LABOUR_REFUSAL


def test_the_days_role_beats_the_designation(world, rostered):
    day = datetime.date(2026, 10, 28)
    roster(rostered, world["branch"], at(world, day, "08:00"), at(world, day, "16:00"), day=day,
           role=RosterCategory.LABOUR)                     # a Cashier by designation, working as Labour today

    assert operator_roster_refusal(rostered, world["branch"], at(world, day, "10:00")) == LABOUR_REFUSAL


def test_an_older_day_with_no_role_reads_the_designation(world, rostered):
    day = datetime.date(2026, 10, 28)
    roster(rostered, world["branch"], at(world, day, "08:00"), at(world, day, "16:00"), day=day, role="")

    assert operator_roster_refusal(rostered, world["branch"], at(world, day, "10:00")) is None


def test_no_role_anywhere_is_refused(world, employee):
    day = datetime.date(2026, 10, 28)
    assert employee.designation.roster_category == ""
    roster(employee, world["branch"], at(world, day, "08:00"), at(world, day, "16:00"), day=day, role="")

    assert operator_roster_refusal(employee, world["branch"], at(world, day, "10:00")) == NO_ROLE_REFUSAL


def test_an_overnight_shift_uses_its_own_days_role(world, rostered):
    yesterday, today = datetime.date(2026, 10, 27), datetime.date(2026, 10, 28)
    branch = world["branch"]
    roster(rostered, branch, at(world, yesterday, "22:00"), at(world, today, "02:00"), day=yesterday,
           role=RosterCategory.LABOUR)
    roster(rostered, branch, at(world, today, "10:00"), at(world, today, "18:00"), day=today)

    assert operator_roster_refusal(rostered, branch, at(world, today, "01:00")) == LABOUR_REFUSAL
    assert operator_roster_refusal(rostered, branch, at(world, today, "11:00")) is None


def test_a_cashier_shift_from_yesterday_still_signs_in(world, rostered):
    yesterday, today = datetime.date(2026, 10, 27), datetime.date(2026, 10, 28)
    roster(rostered, world["branch"], at(world, yesterday, "22:00"), at(world, today, "02:00"), day=yesterday)

    assert operator_roster_refusal(rostered, world["branch"], at(world, today, "01:00")) is None


def test_a_cashier_outside_the_shift_still_hears_the_times(world, rostered):
    day = datetime.date(2026, 10, 28)
    roster(rostered, world["branch"], at(world, day, "08:00"), at(world, day, "16:00"), day=day)

    assert operator_roster_refusal(rostered, world["branch"], at(world, day, "20:00"))[0] == "outside_shift_hours"
