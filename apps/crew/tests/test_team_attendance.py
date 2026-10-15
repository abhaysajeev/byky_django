"""Team Attendance -- POST /api/v1/{manager|employee}/attendance/team.

The manager app sees the whole company; in the employee app the designation's
Team Scope decides, and a Branch Supervisor sees the QR check-ins scanned at
their stations plus the staff rostered there who have not checked in
(owner decisions, 14-15 Oct 2026).
"""

import datetime

import pytest
from django.utils import timezone

from apps.company.models import AuthorityType, BranchApprovalAuthority
from apps.crew import services
from apps.crew.models import Designation, DutyRoster, DutyRosterDayType, Employee, TeamScope
from apps.crew.tests import test_attendance as base
from apps.crew.tests.test_attendance import DAY, aware, call, local, login, mark, qr, self_punch
from apps.devices.models import Device, DeviceStatus
from core.enums import Channel
from core.models import User
from core.timezones import business_date_for

world, fresh_throttle, operator, manager, staff_app = (
    base.world, base.fresh_throttle, base.operator, base.manager, base.staff_app)

TEAM = "/api/v1/{}/attendance/team"


def team(client, token, app="manager", **request_data):
    return call(client, TEAM.format(app), {"date": DAY.isoformat(), **request_data}, token=token)


def listed(client, token, app="manager", **request_data):
    body = team(client, token, app, **request_data).json()
    assert body["code"] == "ok", body
    return body["data"]


def codes(data):
    return [(row["employee"]["employee_code"], row["status"]) for row in data["rows"]]


def person(world, code, first, designation=None):
    return Employee.objects.create(company=world["company"], employee_code=code, first_name=first,
                                   last_name="K", designation=designation or world["staff"].designation)


def rostered(employee, branch, day_type=DutyRosterDayType.WORKING, day=DAY):
    shift = {}
    if day_type == DutyRosterDayType.WORKING:
        shift = {"branch": branch, "shift1_start": aware(local(day, 8)), "shift1_end": aware(local(day, 16))}
    return DutyRoster.objects.create(employee=employee, date=day, day_type=day_type, **shift)


@pytest.fixture
def supervisor_designation(world):
    return Designation.objects.create(company=world["company"], code="SUP", title="Supervisor",
                                      attendance_method="self", team_scope=TeamScope.BRANCHES)


@pytest.fixture
def supervisor(client, world, supervisor_designation):
    """SUP001 on the employee app, Branch Supervisor of AUH01."""
    employee = person(world, "SUP001", "Sara", supervisor_designation)
    User.objects.create_user("SUP001", base.PASSWORD, display_name="Sara", company=world["company"],
                             role=world["role"], employee=employee, allowed_channels=[Channel.EMPLOYEE])
    Device.objects.create(company=world["company"], installation_id="phone-s", platform="android",
                          channel=Channel.EMPLOYEE, status=DeviceStatus.APPROVED, approved_at=timezone.now())
    BranchApprovalAuthority.objects.create(branch=world["auh1"], employee=employee,
                                           authority_type=AuthorityType.BRANCH_SUPERVISOR)
    return login(client, "employee", "SUP001", "phone-s")


@pytest.fixture
def day(client, world, operator, manager):
    """On DAY: EMP001 in 09:00 / out 17:00 at AUH01; EMP002 in 10:00 at AUH02,
    never out; the manager's own check-in at 08:00; EMP003 rostered at AUH01
    and absent; EMP004 on a week off; EMP001 rostered too, but checked in."""
    emp2, emp3, emp4 = person(world, "EMP002", "Bilal"), person(world, "EMP003", "Chen"), person(world, "EMP004", "Dev")
    assert mark(client, operator, qr("punch_in", local(DAY, 9))).json()["code"] == "ok"
    assert mark(client, operator, qr("punch_out", local(DAY, 17))).json()["code"] == "ok"
    assert mark(client, operator, qr("punch_in", local(DAY, 10), employee_code="EMP002",
                                     rms_branch_code="AUH02")).json()["code"] == "ok"
    assert mark(client, manager, self_punch("punch_in", local(DAY, 8), employee_branch_code=""),
                app="manager").json()["code"] == "ok"
    rostered(emp3, world["auh1"])
    rostered(world["staff"], world["auh1"])
    rostered(emp4, None, DutyRosterDayType.WEEK_OFF)
    return {"emp2": emp2, "emp3": emp3}


# -- Manager app: the whole company ----------------------------------------------------


def test_the_manager_sees_everyone_newest_check_in_first(client, manager, day):
    data = listed(client, manager)

    assert codes(data) == [("EMP002", "checked_in"), ("EMP001", "checked_out"), ("MGR001", "checked_in"),
                           ("EMP003", "not_checked_in")]
    assert data["counts"] == {"all": 4, "checked_in": 2, "checked_out": 1, "not_checked_in": 1}
    assert (data["scope"], data["date"], data["total"], data["page"], data["pages"]) == (
        "company", DAY.isoformat(), 4, 1, 1)
    assert [b["code"] for b in data["branches"]] == ["AUH01", "AUH02"]


def test_a_row_carries_every_detail(client, world, manager, day):
    rows = {row["employee"]["employee_code"]: row for row in listed(client, manager)["rows"]}
    till = Device.objects.get(installation_id="till-1")

    assert rows["EMP001"] == {
        "date": "2026-08-29", "row_type": "session", "attendance_id": rows["EMP001"]["attendance_id"],
        "status": "checked_out",
        "missing_check_out": False,
        "employee": {"employee_code": "EMP001", "name": "Anil K", "designation": "Cashier"},
        "source": "qr_scan", "station": {"branch_id": world["auh1"].pk, "code": "AUH01", "name": "Creek Park 1"},
        "emirate": {"state_id": world["auh1"].location.state_id, "name": "Abu Dhabi"},
        "check_in": "2026-08-29 09:00:00", "check_out": "2026-08-29 17:00:00", "duration_minutes": 480,
        "device_no": till.device_registration_id, "roster": None,
    }
    assert (rows["MGR001"]["source"], rows["MGR001"]["station"], rows["MGR001"]["emirate"]) == ("self", None, None)
    assert rows["MGR001"]["device_no"] == Device.objects.get(installation_id="phone-m").device_registration_id
    assert rows["EMP003"]["roster"] == {
        "station": {"branch_id": world["auh1"].pk, "code": "AUH01", "name": "Creek Park 1"},
        "shift1_start": "08:00", "shift1_end": "16:00", "shift2_start": None, "shift2_end": None,
    }
    assert rows["EMP003"]["attendance_id"] is None and rows["EMP003"]["check_in"] is None
    assert set(rows["EMP001"]) == set(rows["EMP003"])


def test_a_check_in_open_past_a_day_reads_as_missing_check_out(client, manager, day):
    rows = {row["employee"]["employee_code"]: row for row in listed(client, manager)["rows"]}

    assert (rows["EMP002"]["status"], rows["EMP002"]["missing_check_out"]) == ("checked_in", True)
    assert rows["EMP002"]["check_out"] is None and rows["EMP002"]["duration_minutes"] is None


@pytest.mark.parametrize("status, expected", [
    ("checked_in", ["EMP002", "MGR001"]), ("checked_out", ["EMP001"]), ("not_checked_in", ["EMP003"]),
])
def test_the_status_tabs(client, manager, day, status, expected):
    data = listed(client, manager, status=status)

    assert [code for code, _ in codes(data)] == expected
    assert data["counts"]["all"] == 4                      # the tabs' counts ignore the tab


def test_station_and_emirate_filters_read_the_scan_station(client, world, manager, day):
    """A self check-in has no station, so any station or emirate filter drops it."""
    by_branch = listed(client, manager, branch_id=world["auh2"].pk)
    by_state = listed(client, manager, state_id=world["auh1"].location.state_id)

    assert [code for code, _ in codes(by_branch)] == ["EMP002"]
    assert [code for code, _ in codes(by_state)] == ["EMP002", "EMP001", "EMP003"]


@pytest.mark.parametrize("search, expected", [("emp00", ["EMP002", "EMP001", "EMP003"]), ("MEERA", ["MGR001"])])
def test_search_by_code_or_name(client, manager, day, search, expected):
    assert [code for code, _ in codes(listed(client, manager, search=search))] == expected


def test_a_shift_across_midnight_stays_on_the_day_it_started(client, world, operator, manager):
    mark(client, operator, qr("punch_in", local(DAY, 22)))
    mark(client, operator, qr("punch_out", local(DAY + datetime.timedelta(days=1), 2)))

    [row] = listed(client, manager)["rows"]
    assert (row["status"], row["duration_minutes"], row["check_out"]) == ("checked_out", 240, "2026-08-30 02:00:00")
    assert listed(client, manager, date="2026-08-30")["rows"] == []


def test_the_list_is_paged(client, manager, day, monkeypatch):
    monkeypatch.setattr(services, "TEAM_PAGE_SIZE", 3)

    first, second = listed(client, manager), listed(client, manager, page=2)

    assert [code for code, _ in codes(first)] == ["EMP002", "EMP001", "MGR001"]
    assert [code for code, _ in codes(second)] == ["EMP003"]
    assert (second["page"], second["pages"], second["total"]) == (2, 2, 4)


def test_another_companys_attendance_is_never_listed(client, world, manager, day):
    Employee.objects.filter(employee_code="EMP003").update(company=world["other"])

    assert "EMP003" not in [code for code, _ in codes(listed(client, manager))]


def test_today_by_default(client, world, manager):
    data = call(client, TEAM.format("manager"), {}, token=manager).json()["data"]

    assert data["date"] == business_date_for(world["company"]).isoformat()


# -- Employee app: by Team Scope -------------------------------------------------------


def test_a_branch_supervisor_sees_only_qr_check_ins_at_their_stations(client, world, supervisor, day):
    data = listed(client, supervisor, "employee")

    assert codes(data) == [("EMP001", "checked_out"), ("EMP003", "not_checked_in")]
    assert data["scope"] == "branches"
    assert [b["code"] for b in data["branches"]] == ["AUH01"]
    assert data["counts"] == {"all": 2, "checked_in": 0, "checked_out": 1, "not_checked_in": 1}


def test_a_supervisor_cannot_widen_the_scope_with_a_filter(client, world, supervisor, day):
    assert listed(client, supervisor, "employee", branch_id=world["auh2"].pk)["rows"] == []


def test_a_supervisors_own_self_check_in_is_not_in_the_branch_list(client, world, supervisor, day):
    body = mark(client, supervisor, self_punch("punch_in", local(DAY, 7), employee_code=""), app="employee").json()
    assert body["code"] == "ok", body

    assert "SUP001" not in [code for code, _ in codes(listed(client, supervisor, "employee"))]


def test_a_supervisor_with_no_stations_gets_an_empty_list(client, world, supervisor, day):
    BranchApprovalAuthority.objects.all().delete()

    body = team(client, supervisor, "employee").json()

    assert (body["code"], body["message"]) == ("ok", "No stations are assigned to you.")
    assert (body["data"]["rows"], body["data"]["branches"], body["data"]["total"]) == ([], [], 0)


def test_whole_company_scope_in_the_employee_app(client, world, supervisor, supervisor_designation, day):
    supervisor_designation.team_scope = TeamScope.COMPANY
    supervisor_designation.save(update_fields=["team_scope"])

    data = listed(client, supervisor, "employee")

    assert data["scope"] == "company" and data["total"] == 4


def test_own_scope_is_refused(client, world, staff_app):
    response = team(client, staff_app, "employee")

    assert (response.status_code, response.json()["code"]) == (403, "no_team_scope")


@pytest.mark.parametrize("app, who", [("operator", "operator"), ("manager", "operator"), ("employee", "manager")])
def test_other_apps_tokens_are_refused(client, world, operator, manager, app, who):
    response = team(client, {"operator": operator, "manager": manager}[who], app)

    assert (response.status_code, response.json()["code"]) == (403, "wrong_channel")


def test_a_bad_status_is_refused(client, manager):
    response = team(client, manager, status="late")

    assert (response.status_code, response.json()["code"]) == (400, "invalid_request")


def test_it_is_in_the_docs(client, world):
    schema = client.get("/api/schema/").content.decode()

    assert "/api/v1/{app}/attendance/team" in schema and "no_team_scope" in schema


# -- A date range (from_date + to_date) --------------------------------------------------

NEXT = DAY + datetime.timedelta(days=1)


@pytest.fixture
def two_days(client, world, operator, day):
    """DAY as in `day`; on NEXT EMP003 checks in at AUH01 at 09:00, and
    EMP002 is rostered at AUH01 but does not come."""
    rostered(day["emp3"], world["auh1"], day=NEXT)
    rostered(day["emp2"], world["auh1"], day=NEXT)
    assert mark(client, operator, qr("punch_in", local(NEXT, 9), employee_code="EMP003")).json()["code"] == "ok"
    return day


def ranged(client, token, app="manager", **request_data):
    """DAY to NEXT -- sent without `date`, which a range may not carry."""
    body = call(client, TEAM.format(app), {"from_date": DAY.isoformat(), "to_date": NEXT.isoformat(),
                                           **request_data}, token=token).json()
    assert body["code"] == "ok", body
    return body["data"]


def test_a_range_lists_every_day_newest_first(client, manager, two_days):
    data = ranged(client, manager)

    assert [(r["date"], r["employee"]["employee_code"], r["status"]) for r in data["rows"]] == [
        ("2026-08-30", "EMP003", "checked_in"), ("2026-08-30", "EMP002", "not_checked_in"),
        ("2026-08-29", "EMP002", "checked_in"), ("2026-08-29", "EMP001", "checked_out"),
        ("2026-08-29", "MGR001", "checked_in"), ("2026-08-29", "EMP003", "not_checked_in"),
    ]
    assert (data["date"], data["from_date"], data["to_date"]) == (None, "2026-08-29", "2026-08-30")
    assert data["counts"] == {"all": 6, "checked_in": 3, "checked_out": 1, "not_checked_in": 2}


def test_not_checked_in_is_decided_per_day(client, manager, two_days):
    """EMP003 checked in on the 30th; that does not cover the 29th."""
    rows = ranged(client, manager, status="not_checked_in")["rows"]

    assert [(r["date"], r["employee"]["employee_code"]) for r in rows] == [
        ("2026-08-30", "EMP002"), ("2026-08-29", "EMP003")]


def test_a_range_keeps_the_supervisor_scope(client, world, supervisor, two_days):
    rows = ranged(client, supervisor, "employee")["rows"]

    assert [(r["date"], r["employee"]["employee_code"]) for r in rows] == [
        ("2026-08-30", "EMP003"), ("2026-08-30", "EMP002"), ("2026-08-29", "EMP001"), ("2026-08-29", "EMP003")]


def test_one_day_still_answers_as_before(client, manager, two_days):
    data = listed(client, manager)

    assert (data["date"], data["from_date"], data["to_date"]) == ("2026-08-29", "2026-08-29", "2026-08-29")
    assert all(r["date"] == "2026-08-29" for r in data["rows"]) and data["total"] == 4


@pytest.mark.parametrize("request_data, field", [
    ({"date": "2026-08-29", "from_date": "2026-08-29", "to_date": "2026-08-30"}, "date"),
    ({"from_date": "2026-08-29"}, "to_date"),
    ({"to_date": "2026-08-29"}, "from_date"),
    ({"from_date": "2026-08-30", "to_date": "2026-08-29"}, "to_date"),
    ({"from_date": "2026-08-01", "to_date": "2026-09-01"}, "to_date"),
])
def test_a_bad_range_is_refused(client, manager, request_data, field):
    response = call(client, TEAM.format("manager"), request_data, token=manager)

    assert (response.status_code, response.json()["code"]) == (400, "invalid_request")
    assert field in response.json()["data"]["errors"]
