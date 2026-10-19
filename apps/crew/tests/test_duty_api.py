"""The duty roster as the apps read it (apps/crew/duty.py) -- read-only.

POST /api/v1/employee/duty/my
POST /api/v1/{manager|employee}/duty/team   by the Team Attendance hierarchy
"""

import datetime

import pytest
from django.utils import timezone

from apps.company.models import BranchApprovalAuthority
from apps.crew import duty
from apps.crew.models import (
    Designation,
    DutyRoster,
    DutyRosterDayType,
    Employee,
    LeaveRequest,
    LeaveStatus,
    LeaveType,
    RosterCategory,
)
from apps.crew.tests import test_attendance as base
from apps.crew.tests import test_team_attendance as team_tests
from apps.crew.tests.test_attendance import call

world, fresh_throttle, operator, manager, staff_app = (
    base.world, base.fresh_throttle, base.operator, base.manager, base.staff_app)
supervisor, supervisor_designation = team_tests.supervisor, team_tests.supervisor_designation

D0 = datetime.date(2026, 8, 30)
D1, D2 = D0 + datetime.timedelta(days=1), D0 + datetime.timedelta(days=2)
RANGE = {"from_date": D0.isoformat(), "to_date": D2.isoformat()}


def at(day, hh):
    return timezone.make_aware(datetime.datetime.combine(day, datetime.time(hh)), base.DUBAI)


def working(employee, branch, day, start=8, hours=8):
    return DutyRoster.objects.create(employee=employee, date=day, day_type=DutyRosterDayType.WORKING, branch=branch,
                                     shift1_start=at(day, start), shift1_end=at(day, start) + datetime.timedelta(hours=hours))


def team(client, token, app="manager", **request_data):
    return call(client, f"/api/v1/{app}/duty/team", {**RANGE, **request_data}, token=token)


def listed(client, token, app="manager", **request_data):
    body = team(client, token, app, **request_data).json()
    assert body["code"] == "ok", body
    return body["data"]


def codes(data):
    return [row["employee"]["employee_code"] for row in data["employees"]]


@pytest.fixture
def crew(world):
    """EMP001 Anil (cashier): D0 AUH01 15:00-02:00, D1 AUH02, D2 week off.
    EMP002 Bilal (cashier): D0 AUH02. EMP003 Chen (labour): nothing rostered."""
    cashier = world["staff"].designation
    cashier.roster_category = RosterCategory.CASHIER
    cashier.save(update_fields=["roster_category"])
    labour = Designation.objects.create(company=world["company"], code="LAB", title="Labour",
                                        roster_category=RosterCategory.LABOUR)
    bilal = Employee.objects.create(company=world["company"], employee_code="EMP002", first_name="Bilal",
                                    last_name="K", designation=cashier)
    Employee.objects.create(company=world["company"], employee_code="EMP003", first_name="Chen", last_name="K",
                            designation=labour)
    working(world["staff"], world["auh1"], D0, start=15, hours=11)
    working(world["staff"], world["auh2"], D1)
    DutyRoster.objects.create(employee=world["staff"], date=D2, day_type=DutyRosterDayType.WEEK_OFF)
    working(bilal, world["auh2"], D0)
    return {"bilal": bilal}


# -- duty/team: manager app ------------------------------------------------------------


def test_the_manager_sees_every_cashier_and_labour_rostered_or_not(client, world, manager, crew):
    data = listed(client, manager)

    # The operator is a cashier too; the manager is not rostered staff.
    assert codes(data) == ["EMP001", "EMP002", "EMP003", "OPR001"]
    assert data["counts"] == {"working": 3, "week_off": 1, "sick_leave": 0, "casual_leave": 0, "on_leave": 0,
                              "not_set": 8}
    assert (data["scope"], data["from_date"], data["to_date"], data["total"]) == ("company", "2026-08-30",
                                                                                   "2026-09-01", 4)
    chen = data["employees"][2]
    assert chen["employee"] == {"employee_code": "EMP003", "name": "Chen K", "designation": "Labour",
                                "category": "labour"}
    assert [d["status"] for d in chen["days"]] == [None, None, None]


def test_a_day_carries_station_shift_and_overnight(client, world, manager, crew):
    anil = listed(client, manager)["employees"][0]["days"]

    assert anil[0] == {
        "date": "2026-08-30", "status": "Working", "branch_id": world["auh1"].pk, "branch": "Creek Park 1",
        "branch_code": "AUH01", "state": "Abu Dhabi", "shift1_start": "15:00", "shift1_end": "02:00",
        "shift1_ends_next_day": True, "shift2_start": None, "shift2_end": None, "shift2_ends_next_day": None,
        "leave_type": None, "leave_reference": None,
    }
    assert (anil[1]["branch_code"], anil[1]["shift1_ends_next_day"]) == ("AUH02", False)
    assert anil[2]["status"] == "Week Off"


@pytest.mark.parametrize("request_data, expected", [
    ({"branch_id": "auh2"}, ["EMP001", "EMP002"]),
    ({"category": "labour"}, ["EMP003"]),
    ({"search": "bil"}, ["EMP002"]),
    ({"state_id": "state"}, ["EMP001", "EMP002"]),
])
def test_filters(client, world, manager, crew, request_data, expected):
    values = dict(request_data)
    if values.get("branch_id") == "auh2":
        values["branch_id"] = world["auh2"].pk
    if values.get("state_id") == "state":
        values["state_id"] = world["auh1"].location.state_id

    assert codes(listed(client, manager, **values)) == expected


def test_the_list_is_paged_by_employee(client, world, manager, crew, monkeypatch):
    monkeypatch.setattr(duty, "PAGE_SIZE", 2)

    first, second = listed(client, manager), listed(client, manager, page=2)

    assert (codes(first), codes(second), second["pages"], second["counts"]["not_set"]) == (
        ["EMP001", "EMP002"], ["EMP003", "OPR001"], 2, 8)


def test_the_current_week_by_default(client, world, manager, crew):
    data = call(client, "/api/v1/manager/duty/team", {}, token=manager).json()["data"]
    week = duty.current_week(world["company"])

    assert (data["from_date"], data["to_date"]) == (week[0].isoformat(), week[-1].isoformat())
    assert len(data["employees"][0]["days"]) == len(week)


@pytest.mark.parametrize("request_data, field", [
    ({"from_date": "2026-08-01", "to_date": "2026-09-01"}, "to_date"),        # 32 days
    ({"from_date": "2026-08-30", "to_date": "2026-08-29"}, "to_date"),
    ({"from_date": "2026-08-30"}, "to_date"),
    ({"category": "manager"}, "category"),
])
def test_bad_requests(client, world, manager, request_data, field):
    response = call(client, "/api/v1/manager/duty/team", request_data, token=manager)

    assert (response.status_code, response.json()["code"]) == (400, "invalid_request")
    assert field in response.json()["data"]["errors"]


# -- duty/team: employee app -----------------------------------------------------------


def test_a_branch_supervisor_sees_whoever_works_at_their_stations_with_every_day(client, world, supervisor, crew):
    data = listed(client, supervisor, "employee")

    assert codes(data) == ["EMP001"]                      # Bilal works only at AUH02; Chen nowhere
    days = data["employees"][0]["days"]
    assert [(d["status"], d["branch_code"]) for d in days] == [
        ("Working", "AUH01"), ("Working", "AUH02"), ("Week Off", None)]
    assert (data["scope"], [b["code"] for b in data["branches"]]) == ("branches", ["AUH01"])


def test_a_supervisor_cannot_widen_the_scope(client, world, supervisor, crew):
    assert listed(client, supervisor, "employee", branch_id=world["auh2"].pk)["employees"] == []


def test_a_supervisor_with_no_stations_gets_an_empty_list(client, world, supervisor, crew):
    BranchApprovalAuthority.objects.all().delete()

    body = team(client, supervisor, "employee").json()

    assert (body["message"], body["data"]["employees"], body["data"]["branches"]) == (
        "No stations are assigned to you.", [], [])


def test_whole_company_scope_in_the_employee_app(client, world, supervisor, supervisor_designation, crew):
    supervisor_designation.team_scope = "company"
    supervisor_designation.save(update_fields=["team_scope"])

    assert codes(listed(client, supervisor, "employee")) == ["EMP001", "EMP002", "EMP003", "OPR001"]


def test_own_scope_is_refused(client, world, staff_app):
    response = team(client, staff_app, "employee")

    assert (response.status_code, response.json()["code"]) == (403, "no_team_scope")


def test_another_companys_staff_are_never_listed(client, world, manager, crew):
    Employee.objects.filter(employee_code="EMP003").update(company=world["other"])

    assert "EMP003" not in codes(listed(client, manager))


# -- duty/my ---------------------------------------------------------------------------


def test_my_roster_over_a_range(client, world, staff_app, crew):
    body = call(client, "/api/v1/employee/duty/my", RANGE, token=staff_app).json()

    assert body["code"] == "ok", body
    assert [(d["date"], d["status"]) for d in body["data"]["days"]] == [
        ("2026-08-30", "Working"), ("2026-08-31", "Working"), ("2026-09-01", "Week Off")]


def test_my_roster_defaults_to_today_and_the_next_six_days(client, world, staff_app):
    data = call(client, "/api/v1/employee/duty/my", {}, token=staff_app).json()["data"]
    today, last = duty.upcoming(world["company"])

    assert (data["from_date"], data["to_date"], len(data["days"])) == (today.isoformat(), last.isoformat(), 7)
    assert all(d["status"] is None for d in data["days"])


def test_an_approved_leave_day_shows_its_type_and_reference(client, world, staff_app, manager):
    kind = LeaveType.objects.create(company=world["company"], code="ANNUAL", name="Annual Leave")
    approver = Employee.objects.get(employee_code="MGR001").user
    request = LeaveRequest.objects.create(
        id="01923e1c-0a11-7b22-8c33-d4e5f6a7b8c9", company=world["company"], employee=world["staff"],
        leave_type=kind, from_date=D0, to_date=D0, days=1, applied_at=timezone.now(), status=LeaveStatus.APPROVED,
        decided_by=approver, decided_at=timezone.now(), decided_channel="manager",
    )
    request.refresh_from_db(fields=["number"])
    DutyRoster.objects.create(employee=world["staff"], date=D0, day_type=DutyRosterDayType.ON_LEAVE,
                              leave_request=request)

    day = call(client, "/api/v1/employee/duty/my", {"from_date": "2026-08-30", "to_date": "2026-08-30"},
               token=staff_app).json()["data"]["days"][0]

    assert (day["status"], day["leave_type"], day["leave_reference"], day["branch"]) == (
        "On Leave", "Annual Leave", request.reference, None)


@pytest.mark.parametrize("url, who", [
    ("/api/v1/manager/duty/my", "manager"), ("/api/v1/operator/duty/team", "operator"),
    ("/api/v1/manager/duty/team", "operator"),
])
def test_other_apps_are_refused(client, world, operator, manager, url, who):
    response = call(client, url, {}, token={"operator": operator, "manager": manager}[who])

    assert (response.status_code, response.json()["code"]) == (403, "wrong_channel")


def test_the_login_roster_uses_the_same_day_shape(world, crew):
    from apps.crew import services

    week = services.current_week_roster(world["staff"], at=at(D0, 12))

    assert week["week_start"] <= "2026-08-30" <= week["week_end"]
    day = next(d for d in week["days"] if d["date"] == "2026-08-30")
    assert (day["status"], day["state"], day["shift1_ends_next_day"]) == ("Working", "Abu Dhabi", True)
