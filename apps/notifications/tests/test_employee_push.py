"""Pushes about people (apps/notifications/employee.py, owner 30 Oct 2026):
leave decided -> the employee; a new leave request -> the station's leave
approvers and the managers; the duty roster changed from today on -> the
employee, once per save; attendance marked by a tablet's QR scan -> the
employee. Only signed-in sessions holding a token are told; every
employee-app push rings."""

import pytest

from apps.company.models import Branch
from apps.crew import services as crew_services
from apps.crew.models import Employee, LeaveRequest, RosterCategory
from apps.crew.tests import test_attendance as attendance
from apps.crew.tests import test_leave as leave_tests
from apps.notifications.models import AndroidChannel, Audience, Notification, SubjectType
from apps.portal.session_models import AppSession
from core.enums import Channel

world, fresh_throttle, operator, manager, staff_app = (
    leave_tests.world, leave_tests.fresh_throttle, leave_tests.operator, leave_tests.manager, leave_tests.staff_app)
types, approver = leave_tests.types, leave_tests.approver
day, ask, submitted, act, roster = (
    leave_tests.day, leave_tests.ask, leave_tests.submitted, leave_tests.act, leave_tests.roster)


def pushable(username, channel, token):
    AppSession.objects.filter(user__username__iexact=username, channel=channel,
                              logged_out_at__isnull=True).update(push_token=token)


@pytest.fixture
def phones(staff_app, manager, approver):
    """Anil (employee app), Meera (manager app) and Sara, AUH01's leave
    approver (employee app), each holding a push token."""
    pushable("EMP001", Channel.EMPLOYEE, "fcm-anil")
    pushable("MGR001", Channel.MANAGER, "fcm-meera")
    pushable("SUP001", Channel.EMPLOYEE, "fcm-sara")
    return {"anil": staff_app, "meera": manager, "sara": approver}


def told(subject_type):
    return [(row.session.push_token, row.title, row.body, row.android_channel) for row in
            Notification.objects.filter(subject_type=subject_type).select_related("session")
            .order_by("audience", "created_on")]


# -- Leave ------------------------------------------------------------------------------

def test_a_new_leave_tells_the_stations_approvers_and_the_managers(client, world, types, phones):
    roster(world["staff"], world["auh1"], day(world, 1))
    data = ask(world, types)

    row = submitted(client, phones["anil"], data)
    again = leave_tests.submit(client, phones["anil"], data).json()   # resent: nobody told twice

    leave_row = LeaveRequest.objects.get(pk=row["leave_request_id"])
    body = f"Anil K · {_range(leave_row)} · Annual Leave"
    assert again["code"] == "duplicate"
    assert sorted(told(SubjectType.LEAVE_REQUEST)) == sorted([
        ("fcm-sara", "New leave request", body, AndroidChannel.EMPLOYEE_ALERTS),
        ("fcm-meera", "New leave request", body, AndroidChannel.APPROVALS_NEW),
    ])                                                   # not Anil, who asked


def test_another_stations_approver_is_not_told(client, world, types, phones):
    roster(world["staff"], world["auh2"], day(world, 1))

    submitted(client, phones["anil"], ask(world, types, start=1, end=1))

    tokens = [token for token, *_ in told(SubjectType.LEAVE_REQUEST)]
    assert "fcm-sara" not in tokens and "fcm-meera" in tokens


@pytest.mark.parametrize("action, title", [("approve", "Leave approved"), ("reject", "Leave rejected")])
def test_the_employee_hears_the_decision(client, world, types, phones, action, title):
    row = submitted(client, phones["anil"], ask(world, types))
    Notification.objects.all().delete()

    body = act(client, phones["meera"], "manager", action, row["leave_request_id"], remarks="Busy weekend").json()

    assert body["code"] == "ok", body
    [(token, got_title, text, channel)] = told(SubjectType.LEAVE_REQUEST)
    assert (token, got_title, channel) == ("fcm-anil", title, AndroidChannel.EMPLOYEE_ALERTS)
    assert text.endswith("Annual Leave · Busy weekend")


def test_a_changed_rejection_tells_them_again_and_the_roster_write_is_not_a_second_push(
        client, world, types, phones):
    row = submitted(client, phones["anil"], ask(world, types))
    act(client, phones["meera"], "manager", "reject", row["leave_request_id"])
    act(client, phones["meera"], "manager", "approve", row["leave_request_id"])

    titles = [title for token, title, *_ in told(SubjectType.LEAVE_REQUEST) if token == "fcm-anil"]
    assert titles == ["Leave rejected", "Leave approved"]
    assert told(SubjectType.DUTY_ROSTER) == []             # the approval's own roster days


def test_a_signed_out_employee_is_not_told(client, world, types, phones):
    row = submitted(client, phones["anil"], ask(world, types))
    AppSession.objects.filter(user__username="emp001", channel=Channel.EMPLOYEE).update(push_token="")

    act(client, phones["meera"], "manager", "approve", row["leave_request_id"])

    assert not Notification.objects.filter(audience=Audience.EMPLOYEE, action="approved").exists()


# -- Duty roster ------------------------------------------------------------------------

@pytest.fixture
def rostered(world, phones):
    world["staff"].designation.roster_category = RosterCategory.CASHIER
    world["staff"].designation.save()
    return world["staff"]


def week_off(on_date):
    return {"date": on_date.isoformat(), "day_type": "week_off"}


def test_a_week_saved_is_one_push_with_its_range(world, rostered):
    days = [week_off(day(world, n)) for n in (2, 3, 5)]

    crew_services.save_week(rostered, days, user=None, branches_qs=Branch.objects.all())

    [(token, title, body, channel)] = told(SubjectType.DUTY_ROSTER)
    assert (token, title, channel) == ("fcm-anil", "Duty roster updated", AndroidChannel.EMPLOYEE_ALERTS)
    assert body == _range_dates(day(world, 2), day(world, 5))


def test_past_days_only_are_not_pushed(world, rostered):
    crew_services.save_week(rostered, [week_off(day(world, -3))], user=None, branches_qs=Branch.objects.all())

    assert told(SubjectType.DUTY_ROSTER) == []


def test_one_day_added_or_removed_pushes(world, rostered):
    crew_services.assign_day(rostered, week_off(day(world, 1)), branches_qs=Branch.objects.all(), user=None)
    crew_services.remove_day(rostered, day(world, 1), user=None)
    crew_services.remove_day(rostered, day(world, 1), user=None)          # nothing there: no push

    assert [title for _, title, *_ in told(SubjectType.DUTY_ROSTER)] == ["Duty roster updated"] * 2


def test_an_import_pushes_once_per_employee(world, rostered):
    rows = [{"employee_code": "EMP001", **week_off(day(world, n))} for n in (1, 2, 3)]

    written, _ = crew_services.import_rows(rows, user=None, employees_qs=Employee.objects.filter(company=rostered.company),
                                           branches_qs=Branch.objects.all())

    assert written == 3 and len(told(SubjectType.DUTY_ROSTER)) == 1


# -- Attendance -------------------------------------------------------------------------

def test_a_qr_scan_tells_the_employee(client, world, operator, phones):
    at = attendance.local(attendance.DAY, 9, 2)
    data = attendance.qr("punch_in", at)

    assert attendance.mark(client, operator, data).json()["code"] == "ok"
    attendance.mark(client, operator, data)                       # resent: one push

    assert told(SubjectType.ATTENDANCE) == [
        ("fcm-anil", "Punched in", "Creek Park 1 · 09:02", AndroidChannel.EMPLOYEE_ALERTS)]


def test_a_managers_own_punch_tells_nobody(client, world, phones):
    at = attendance.local(attendance.DAY, 9, 0)

    assert attendance.mark(client, phones["meera"], attendance.self_punch("punch_in", at),
                           app="manager").json()["code"] == "ok"

    assert told(SubjectType.ATTENDANCE) == []


# -- helpers ----------------------------------------------------------------------------

def _range_dates(start, end):
    from apps.notifications.employee import date_range

    return date_range(start, end)


def _range(row):
    return _range_dates(row.from_date, row.to_date)
