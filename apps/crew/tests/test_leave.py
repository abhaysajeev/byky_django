"""Leave: the employee asks, a manager / the web / a station's Leave Request
approver decides, the duty roster shows it (apps/crew/leave.py, owner
decisions 15 Oct 2026).

POST /api/v1/employee/leave/{types,submit,my,cancel}
POST /api/v1/{manager|employee}/leave/{requests,approve,reject}
"""

import datetime
import json

import pytest
from django.utils import timezone

from apps.company.models import AuthorityType, BranchApprovalAuthority
from apps.crew import leave, services
from apps.crew.models import (
    DutyRoster,
    DutyRosterDayType,
    Employee,
    LeaveRequest,
    LeaveStatus,
    LeaveStatusLog,
    LeaveType,
)
from apps.crew.tests import test_attendance as base
from apps.crew.tests.test_attendance import call, login
from apps.devices.models import Device, DeviceStatus
from apps.portal.models import Role, RolePermission
from apps.portal.services import grant_all
from core.enums import Channel
from core.ids import uuid7
from core.models import User
from core.timezones import business_date_for, zone_for

world, fresh_throttle, operator, manager, staff_app = (
    base.world, base.fresh_throttle, base.operator, base.manager, base.staff_app)

URL = "/api/v1/{}/leave/{}"


def today(world):
    return business_date_for(world["company"])


def day(world, offset):
    return today(world) + datetime.timedelta(days=offset)


@pytest.fixture
def types(world):
    made = {code: LeaveType.objects.create(company=world["company"], code=code, name=name)
            for code, name in [("ANNUAL", "Annual Leave"), ("SICK", "Sick Leave")]}
    made["old"] = LeaveType.objects.create(company=world["company"], code="OLD", name="Old Leave", is_active=False)
    made["theirs"] = LeaveType.objects.create(company=world["other"], code="ANNUAL", name="Annual Leave")
    return made


def ask(world, types, start=1, end=3, kind="ANNUAL", **extra):
    return {"sync_id": str(uuid7()), "leave_type_id": types[kind].pk, "from_date": day(world, start).isoformat(),
            "to_date": day(world, end).isoformat(), "reason": "Family visit", **extra}


def submit(client, token, data):
    return call(client, URL.format("employee", "submit"), data, token=token)


def submitted(client, token, data):
    body = submit(client, token, data).json()
    assert body["code"] == "ok", body
    return body["data"]


def act(client, token, app, action, leave_id, **extra):
    return call(client, URL.format(app, action), {"leave_request_id": leave_id, **extra}, token=token)


def roster(employee, branch, on_date, day_type=DutyRosterDayType.WORKING):
    shift = {}
    if day_type == DutyRosterDayType.WORKING:
        start = timezone.make_aware(datetime.datetime.combine(on_date, datetime.time(8)), base.DUBAI)
        shift = {"branch": branch, "shift1_start": start, "shift1_end": start + datetime.timedelta(hours=8)}
    return DutyRoster.objects.create(employee=employee, date=on_date, day_type=day_type, **shift)


@pytest.fixture
def approver(client, world):
    """SUP001 on the employee app: Leave Request approver of AUH01."""
    employee = Employee.objects.create(company=world["company"], employee_code="SUP001", first_name="Sara",
                                       last_name="K", designation=world["staff"].designation)
    User.objects.create_user("SUP001", base.PASSWORD, display_name="Sara", company=world["company"],
                             role=world["role"], employee=employee, allowed_channels=[Channel.EMPLOYEE])
    Device.objects.create(company=world["company"], installation_id="phone-s", platform="android",
                          channel=Channel.EMPLOYEE, status=DeviceStatus.APPROVED, approved_at=timezone.now())
    BranchApprovalAuthority.objects.create(branch=world["auh1"], employee=employee,
                                           authority_type=AuthorityType.LEAVE_REQUEST)
    return login(client, "employee", "SUP001", "phone-s")


# -- Submit ----------------------------------------------------------------------------


def test_an_employee_asks_for_leave(client, world, types, staff_app):
    world["staff"].branch = world["auh2"]
    world["staff"].save(update_fields=["branch"])
    roster(world["staff"], world["auh1"], day(world, 2))

    data = submitted(client, staff_app, ask(world, types))

    assert (data["status"], data["days"], data["leave_type"]["name"]) == ("pending", 3, "Annual Leave")
    assert data["reference"].startswith(f"LEV-{timezone.now().year}-")
    assert [s["code"] for s in data["stations"]] == ["AUH01", "AUH02"]      # rostered + home
    assert data["working_days_rostered"] == 1 and data["can_cancel"] and not data["can_decide"]
    assert [entry["status"] for entry in data["log"]] == ["pending"]


def test_a_resent_submit_is_a_duplicate(client, world, types, staff_app):
    data = ask(world, types)
    submitted(client, staff_app, data)

    assert submit(client, staff_app, data).json()["code"] == "duplicate"
    assert LeaveRequest.objects.count() == 1


@pytest.mark.parametrize("change, status, code", [
    ({"from_date": "past"}, 400, "leave_in_past"),
    ({"kind": "old"}, 400, "unknown_leave_type"),
    ({"kind": "theirs"}, 400, "unknown_leave_type"),
    ({"to_before_from": True}, 400, "invalid_request"),
])
def test_bad_requests_are_refused(client, world, types, staff_app, change, status, code):
    data = ask(world, types, kind=change.get("kind", "ANNUAL"))
    if change.get("from_date"):
        data["from_date"] = day(world, -1).isoformat()
    if change.get("to_before_from"):
        data["to_date"] = day(world, 0).isoformat()

    response = submit(client, staff_app, data)

    assert (response.status_code, response.json()["code"]) == (status, code)


def test_today_is_allowed(client, world, types, staff_app):
    assert submitted(client, staff_app, ask(world, types, start=0, end=0))["days"] == 1


def test_the_phone_may_send_when_it_was_applied(client, world, types, staff_app):
    """Made offline an hour ago: it keeps the phone's time, not the time it
    reached us."""
    zone = zone_for(world["company"])
    then = (timezone.now() - datetime.timedelta(hours=1)).astimezone(zone).replace(microsecond=0)

    data = submitted(client, staff_app, ask(world, types, applied_at=then.strftime("%Y-%m-%d %H:%M:%S")))

    assert data["applied_at"] == then.strftime("%Y-%m-%d %H:%M:%S")
    assert LeaveRequest.objects.get().applied_at == then


def test_a_future_applied_time_is_taken_as_now(client, world, types, staff_app):
    zone = zone_for(world["company"])
    later = (timezone.now() + datetime.timedelta(days=2)).astimezone(zone)

    before = timezone.now()
    submitted(client, staff_app, ask(world, types, applied_at=later.strftime("%Y-%m-%d %H:%M:%S")))

    assert before <= LeaveRequest.objects.get().applied_at <= timezone.now()


@pytest.mark.parametrize("sent", ["absent", None])
def test_without_an_applied_time_it_is_when_it_reached_us(client, world, types, staff_app, sent):
    data = ask(world, types) if sent == "absent" else ask(world, types, applied_at=None)
    before = timezone.now()

    submitted(client, staff_app, data)

    assert before <= LeaveRequest.objects.get().applied_at <= timezone.now()


def test_a_badly_written_applied_time_is_refused(client, world, types, staff_app):
    response = submit(client, staff_app, ask(world, types, applied_at="yesterday"))

    assert (response.status_code, response.json()["code"]) == (400, "invalid_request")
    assert "applied_at" in response.json()["data"]["errors"]


def test_overlap_with_pending_or_approved_is_refused_not_cancelled(client, world, types, staff_app):
    first = submitted(client, staff_app, ask(world, types, 1, 3))

    response = submit(client, staff_app, ask(world, types, 3, 5))
    assert (response.status_code, response.json()["code"]) == (409, "leave_overlaps")

    act(client, staff_app, "employee", "cancel", first["leave_request_id"])
    assert submitted(client, staff_app, ask(world, types, 3, 5))["status"] == "pending"


# -- Cancel ----------------------------------------------------------------------------


def test_only_the_applicant_cancels_and_only_while_pending(client, world, types, staff_app, manager):
    data = submitted(client, staff_app, ask(world, types))
    act(client, manager, "manager", "approve", data["leave_request_id"])

    response = act(client, staff_app, "employee", "cancel", data["leave_request_id"])
    assert (response.status_code, response.json()["code"]) == (409, "leave_not_pending")

    other = submitted(client, staff_app, ask(world, types, 5, 6))
    body = act(client, staff_app, "employee", "cancel", other["leave_request_id"]).json()
    assert (body["code"], body["data"]["status"], body["data"]["can_cancel"]) == ("ok", "cancelled", False)


def test_nobody_else_cancels(client, world, types, staff_app, approver):
    data = submitted(client, staff_app, ask(world, types))

    response = act(client, approver, "employee", "cancel", data["leave_request_id"])

    assert (response.status_code, response.json()["code"]) == (403, "not_your_leave")
    assert LeaveRequest.objects.get().status == LeaveStatus.PENDING


def test_my_list_counts_every_status(client, world, types, staff_app, manager):
    a = submitted(client, staff_app, ask(world, types, 1, 1))
    submitted(client, staff_app, ask(world, types, 2, 2))
    act(client, manager, "manager", "reject", a["leave_request_id"])

    body = call(client, URL.format("employee", "my"), {"status": "pending"}, token=staff_app).json()

    assert body["data"]["counts"] == {"pending": 1, "approved": 0, "rejected": 1, "cancelled": 0, "all": 2}
    assert [r["status"] for r in body["data"]["requests"]] == ["pending"]


def test_leave_types_list_only_active_own(client, world, types, staff_app):
    body = call(client, URL.format("employee", "types"), {}, token=staff_app).json()

    assert [t["name"] for t in body["data"]["leave_types"]] == ["Annual Leave", "Sick Leave"]


# -- Decide ----------------------------------------------------------------------------


def test_a_manager_approves_and_the_roster_shows_leave(client, world, types, staff_app, manager):
    roster(world["staff"], world["auh1"], day(world, 1))
    roster(world["staff"], None, day(world, 2), DutyRosterDayType.WEEK_OFF)
    data = submitted(client, staff_app, ask(world, types, 1, 3))

    body = act(client, manager, "manager", "approve", data["leave_request_id"], remarks="Cover arranged").json()

    assert body["code"] == "ok", body
    assert (body["data"]["status"], body["data"]["decided_channel"], body["data"]["working_days_replaced"]) == (
        "approved", "manager", 1)
    days = DutyRoster.objects.filter(employee=world["staff"]).order_by("date")
    assert [(d.date, d.day_type, d.branch_id, d.leave_request_id) for d in days] == [
        (day(world, i), DutyRosterDayType.ON_LEAVE, None, LeaveRequest.objects.get().pk) for i in (1, 2, 3)]
    assert [e["status"] for e in body["data"]["log"]] == ["pending", "approved"]

    again = act(client, manager, "manager", "reject", data["leave_request_id"])
    assert (again.status_code, again.json()["code"]) == (409, "leave_decided")


def test_a_first_rejection_may_be_changed_once(client, world, types, staff_app, manager):
    data = submitted(client, staff_app, ask(world, types))
    leave_id = data["leave_request_id"]

    rejected = act(client, manager, "manager", "reject", leave_id, remarks="Short staffed").json()["data"]
    assert (rejected["status"], rejected["can_decide"], rejected["status_edited"]) == ("rejected", True, False)
    approved = act(client, manager, "manager", "approve", leave_id).json()["data"]
    assert (approved["status"], approved["status_edited"], approved["can_decide"]) == ("approved", True, False)
    assert [e["status"] for e in approved["log"]] == ["pending", "rejected", "approved"]


def test_a_changed_rejection_cannot_change_again(client, world, types, staff_app, manager):
    leave_id = submitted(client, staff_app, ask(world, types))["leave_request_id"]
    act(client, manager, "manager", "reject", leave_id)
    act(client, manager, "manager", "reject", leave_id)                   # the one edit: kept rejected

    response = act(client, manager, "manager", "approve", leave_id)

    assert (response.status_code, response.json()["code"]) == (409, "leave_decided")
    assert LeaveStatusLog.objects.filter(leave_request_id=leave_id).count() == 3


def test_an_old_rejection_cannot_be_approved_over_newer_leave(client, world, types, staff_app, manager):
    old = submitted(client, staff_app, ask(world, types, 1, 3))["leave_request_id"]
    act(client, manager, "manager", "reject", old)
    new = submitted(client, staff_app, ask(world, types, 2, 4))["leave_request_id"]
    act(client, manager, "manager", "approve", new)

    response = act(client, manager, "manager", "approve", old)

    assert (response.status_code, response.json()["code"]) == (409, "leave_overlaps")


def test_a_station_approver_sees_and_decides_only_their_stations(client, world, types, staff_app, approver):
    roster(world["staff"], world["auh1"], day(world, 1))
    here = submitted(client, staff_app, ask(world, types, 1, 1))
    elsewhere = submitted(client, staff_app, ask(world, types, 5, 5))         # no roster, no home branch

    body = call(client, URL.format("employee", "requests"), {}, token=approver).json()
    assert [r["leave_request_id"] for r in body["data"]["requests"]] == [here["leave_request_id"]]
    assert body["data"]["requests"][0]["can_decide"] is True

    assert act(client, approver, "employee", "approve", here["leave_request_id"]).json()["data"]["decided_channel"] \
        == "employee"
    response = act(client, approver, "employee", "approve", elsewhere["leave_request_id"])
    assert (response.status_code, response.json()["code"]) == (404, "unknown_leave")


def test_an_approver_never_decides_their_own(client, world, types, approver, manager):
    approver_employee = Employee.objects.get(employee_code="SUP001")
    approver_employee.branch = world["auh1"]
    approver_employee.save(update_fields=["branch"])
    own = submitted(client, approver, ask(world, types))

    listed = call(client, URL.format("employee", "requests"), {}, token=approver).json()["data"]["requests"]
    response = act(client, approver, "employee", "approve", own["leave_request_id"])

    assert listed == []
    assert (response.status_code, response.json()["code"]) == (403, "own_leave")
    assert act(client, manager, "manager", "approve", own["leave_request_id"]).json()["code"] == "ok"


def test_someone_who_approves_nothing_is_refused(client, world, types, staff_app):
    response = call(client, URL.format("employee", "requests"), {}, token=staff_app)

    assert (response.status_code, response.json()["code"]) == (403, "not_a_leave_approver")


def test_the_manager_list_filters(client, world, types, staff_app, manager):
    roster(world["staff"], world["auh1"], day(world, 1))
    submitted(client, staff_app, ask(world, types, 1, 1))
    submitted(client, staff_app, ask(world, types, 9, 9))

    def listed(**request_data):
        return call(client, URL.format("manager", "requests"), request_data, token=manager).json()["data"]

    assert listed()["counts"]["pending"] == 2
    assert listed(branch_id=world["auh1"].pk)["total"] == 1
    assert listed(from_date=day(world, 8).isoformat())["total"] == 1
    assert listed(search="anil")["total"] == 2 and listed(search="nobody")["total"] == 0


@pytest.mark.parametrize("app, path, who", [
    ("operator", "my", "operator"), ("manager", "submit", "manager"), ("manager", "requests", "operator"),
])
def test_other_apps_are_refused(client, world, operator, manager, app, path, who):
    response = call(client, URL.format(app, path), {}, token={"operator": operator, "manager": manager}[who])

    assert (response.status_code, response.json()["code"]) == (403, "wrong_channel")


# -- Roster, login, badges -------------------------------------------------------------


@pytest.fixture
def on_leave(client, world, types, staff_app, manager):
    roster(world["staff"], world["auh1"], day(world, 1))
    leave_id = submitted(client, staff_app, ask(world, types, 1, 2))["leave_request_id"]
    act(client, manager, "manager", "approve", leave_id)
    return LeaveRequest.objects.get(pk=leave_id)


def test_leave_days_are_locked_on_the_roster(world, on_leave):
    world["staff"].designation.roster_category = "cashier"
    world["staff"].designation.save(update_fields=["roster_category"])
    user = User.objects.get(username="mgr001")
    entry = {"date": day(world, 1).isoformat(), "day_type": "week_off"}

    for attempt in (
        lambda: services.save_week(world["staff"], [entry], user=user, branches_qs=world["company"].branches.all()),
        lambda: services.save_week(world["staff"], [{"date": entry["date"], "day_type": "not_set"}], user=user,
                                   branches_qs=world["company"].branches.all()),
        lambda: services.assign_day(world["staff"], entry, user=user, branches_qs=world["company"].branches.all()),
        lambda: services.remove_day(world["staff"], day(world, 1), user=user),
    ):
        with pytest.raises(services.Invalid, match=on_leave.reference):
            attempt()
    written, results = services.import_rows([{"employee_code": "EMP001", **entry}], user=user,
                                            employees_qs=Employee.objects.all(),
                                            branches_qs=world["company"].branches.all())
    assert written == 0 and on_leave.reference in results[0]["message"]


def test_on_leave_cannot_be_typed_on_the_roster(world):
    errors, _ = services._check_day({"date": day(world, 1).isoformat(), "day_type": "on_leave"},
                                    branches_qs=world["company"].branches.all())

    assert "approved leave request" in errors[0]["message"]


def test_the_login_roster_shows_the_leave(world, on_leave):
    week = services.current_week_roster(world["staff"], at=timezone.now() + datetime.timedelta(days=1))
    leave_day = next(d for d in week["days"] if d["date"] == day(world, 1).isoformat())

    assert (leave_day["status"], leave_day["leave_type"], leave_day["leave_reference"]) == (
        "On Leave", "Annual Leave", on_leave.reference)


def test_login_says_who_approves_leave(client, world, approver):
    employee = call(client, "/api/v1/employee/auth/login",
                    {"username": "SUP001", "password": base.PASSWORD, "installation_id": "phone-s"}).json()
    boss = call(client, "/api/v1/manager/auth/login",
                {"username": "MGR001", "password": base.PASSWORD, "installation_id": "phone-m"}).json()

    assert employee["data"]["employee"]["can_approve_leave"] is True
    assert boss["data"]["employee"]["can_approve_leave"] is True


def test_pending_leave_is_on_the_managers_badges(client, world, types, staff_app, manager):
    submitted(client, staff_app, ask(world, types))

    body = call(client, "/api/v1/manager/requests/pending-counts", {}, token=manager).json()

    assert (body["data"]["leave"], body["data"]["total"]) == (1, 1)


def test_the_database_keeps_on_leave_linked(world, on_leave):
    from django.db import IntegrityError, transaction

    with pytest.raises(IntegrityError), transaction.atomic():
        DutyRoster.objects.filter(leave_request=on_leave).update(leave_request=None)


# -- Web -------------------------------------------------------------------------------


@pytest.fixture
def web(client, world):
    """An HR user on the web with every crew right."""
    role = Role.objects.create(company=world["company"], name="HR")
    grant_all(role)
    User.objects.create_user("hr.user", base.PASSWORD, display_name="HR", company=world["company"], role=role,
                             allowed_channels=[Channel.WEB])
    client.post("/login/", {"username": "hr.user", "password": base.PASSWORD})
    return client


def post(client, url, payload=None):
    return client.post(url, data=json.dumps(payload or {}), content_type="application/json")


def test_the_web_lists_and_approves(client, world, types, staff_app, web):
    leave_id = submitted(client, staff_app, ask(world, types))["leave_request_id"]
    client.post("/login/", {"username": "hr.user", "password": base.PASSWORD})

    page = client.get("/crew/leave-request/list/")
    assert page.status_code == 200 and page.context["tabs"][0]["count"] == 1
    detail = client.get(f"/crew/leave-request/{leave_id}/")
    assert detail.status_code == 200 and detail.context["can_decide"]

    body = post(client, f"/crew/leave-request/{leave_id}/approve/", {"remarks": "ok"}).json()

    assert body["ok"] is True, body
    row = LeaveRequest.objects.get(pk=leave_id)
    assert (row.status, row.decided_channel, row.decided_by.username) == ("approved", "web", "hr.user")


def test_the_web_needs_the_approve_right(client, world, types, staff_app, web):
    leave_id = submitted(client, staff_app, ask(world, types))["leave_request_id"]
    client.post("/login/", {"username": "hr.user", "password": base.PASSWORD})
    RolePermission.objects.filter(role__name="HR", page__code="crew.leave_request").update(can_approve=False)

    response = post(client, f"/crew/leave-request/{leave_id}/approve/")

    assert response.status_code == 403
    assert LeaveRequest.objects.get(pk=leave_id).status == LeaveStatus.PENDING


def test_the_leave_type_screen_saves(client, world, web):
    response = post(client, "/crew/leave-type/save/", {"code": "HAJJ", "name": "Hajj Leave", "is_active": True})

    assert response.json()["ok"] is True, response.content
    assert client.get("/crew/leave-type/list/").context["leave_types"][0]["name"] == "Hajj Leave"


def test_pending_count_service_scopes_by_station(world, types, client, staff_app):
    roster(world["staff"], world["auh1"], day(world, 1))
    submitted(client, staff_app, ask(world, types, 1, 1))

    assert leave.pending_count([world["company"].pk], branch_id=world["auh1"].pk) == 1
    assert leave.pending_count([world["company"].pk], branch_id=world["auh2"].pk) == 0
