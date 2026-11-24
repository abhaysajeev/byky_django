"""Attendance by business day (owner, 24 Nov 2026).

* The day's attendance method: Working as cashier or labour on the duty roster
  -> QR at the station; any other day the permanent method. A punch-out follows
  its punch-in's method. POST /api/v1/employee/attendance/method tells the app.
* Auto-close: a punch-in nobody closed gets the server's punch-out once its
  business day is over -- at the rostered shift's end (minutes counted), else at
  the day's end (minutes not counted). A real punch-out that arrives later
  replaces it.
"""

import datetime

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.crew import services
from apps.crew.models import (
    Attendance,
    AttendanceMethod,
    AttendanceSource,
    ClosedBy,
    DutyRoster,
    DutyRosterDayType,
    PunchType,
    RosterCategory,
)
from apps.crew.tests import test_attendance as base
from apps.crew.tests.test_attendance import (
    DAY,
    aware,
    call,
    local,
    login,
    mark,
    qr,
    self_method,
    self_punch,
)
from apps.devices.models import Device, DeviceStatus
from core.enums import Channel
from core.models import User
from core.timezones import business_day_of

world, fresh_throttle, operator, manager, staff_app, day_start = (
    base.world, base.fresh_throttle, base.operator, base.manager, base.staff_app, base.day_start)

METHOD = "/api/v1/{}/attendance/method"
NEXT = DAY + datetime.timedelta(days=1)


def rostered(employee, day, role=RosterCategory.CASHIER, branch=None, start=(9, 0), end=(17, 0),
             second=None, day_type=DutyRosterDayType.WORKING):
    """One roster day. `second` = ((h, m), (h, m)) for a second shift."""
    if day_type != DutyRosterDayType.WORKING:
        return DutyRoster.objects.create(employee=employee, date=day, day_type=day_type)
    fields = {"shift1_start": aware(local(day, *start)), "shift1_end": aware(local(day, *end))}
    if second:
        fields.update(shift2_start=aware(local(day, *second[0])), shift2_end=aware(local(day, *second[1])))
    return DutyRoster.objects.create(employee=employee, date=day, day_type=DutyRosterDayType.WORKING, role=role,
                                     branch=branch or employee.company.branches.order_by("pk").first(), **fields)


@pytest.fixture
def mechanic(world):
    """EMP001's permanent method is Self (a Mechanic, say)."""
    self_method(world["staff"])
    return world["staff"]


def self_staff(kind, at, **overrides):
    """The employee app's own Mark / Close for EMP001."""
    return self_punch(kind, at, employee_code="EMP001", employee_branch_code="", rms_installation_id="phone-e",
                      **overrides)


# -- The day's method --------------------------------------------------------------------


@pytest.mark.parametrize("role, day_type, expected", [
    (RosterCategory.CASHIER, DutyRosterDayType.WORKING, AttendanceMethod.QR_STATION),
    (RosterCategory.LABOUR, DutyRosterDayType.WORKING, AttendanceMethod.QR_STATION),
    (None, DutyRosterDayType.WEEK_OFF, AttendanceMethod.SELF),
    (None, DutyRosterDayType.SICK_LEAVE, AttendanceMethod.SELF),
])
def test_the_roster_decides_the_days_method(mechanic, role, day_type, expected):
    rostered(mechanic, DAY, role=role, day_type=day_type)
    assert services.attendance_method_for(mechanic, aware(local(DAY, 12))) == expected


def test_a_day_not_rostered_keeps_the_permanent_method(mechanic):
    assert services.attendance_method_for(mechanic, aware(local(DAY, 12))) == AttendanceMethod.SELF


def test_nothing_is_written_back_to_the_employee(mechanic):
    rostered(mechanic, DAY)
    services.attendance_method_for(mechanic, aware(local(DAY, 12)))
    mechanic.refresh_from_db()
    assert mechanic.attendance_rule == AttendanceMethod.SELF


def test_a_night_punch_belongs_to_the_business_day_it_falls_in(mechanic, day_start):
    """01:00 is still the 29th's business day when the day starts at 04:00."""
    rostered(mechanic, DAY)
    assert services.attendance_method_for(mechanic, aware(local(NEXT, 1))) == AttendanceMethod.QR_STATION
    assert services.attendance_method_for(mechanic, aware(local(NEXT, 5))) == AttendanceMethod.SELF


def test_the_tablet_scans_a_self_employee_rostered_as_cashier(client, mechanic, operator):
    rostered(mechanic, DAY)
    assert mark(client, operator, qr("punch_in", local(DAY, 9))).json()["code"] == "ok"
    assert mark(client, operator, qr("punch_out", local(DAY, 17))).json()["code"] == "ok"


def test_off_the_roster_the_scan_is_refused_with_the_method_to_use(client, mechanic, operator):
    response = mark(client, operator, qr("punch_in", local(DAY, 9)))
    assert response.status_code == 403
    assert response.json() == {"code": "attendance_method_mismatch",
                               "message": "This employee's attendance is not taken by QR scan.",
                               "data": {"attendance_method": "self", "business_date": "2026-08-29"}}
    assert not Attendance.objects.exists()


def test_the_employee_app_cannot_self_mark_on_a_cashier_day(client, mechanic, staff_app):
    rostered(mechanic, DAY)
    response = mark(client, staff_app, self_staff("punch_in", local(DAY, 9)), app="employee")
    assert response.status_code == 403
    assert response.json()["code"] == "self_attendance_not_allowed"
    assert response.json()["data"] == {"attendance_method": "qr_station", "business_date": "2026-08-29"}


def test_a_punch_out_follows_its_punch_in_when_the_roster_changes(client, mechanic, staff_app, operator):
    """Self check-in on a free day, then the web rosters them as cashier: the
    check-out is still the app's own, and a scan cannot close it."""
    assert mark(client, staff_app, self_staff("punch_in", local(DAY, 9)), app="employee").json()["code"] == "ok"
    rostered(mechanic, DAY)

    scan = mark(client, operator, qr("punch_out", local(DAY, 17)))
    assert scan.status_code == 403 and scan.json()["code"] == "attendance_method_mismatch"
    assert scan.json()["data"]["attendance_method"] == "self"

    own = mark(client, staff_app, self_staff("punch_out", local(DAY, 17)), app="employee")
    assert own.json()["code"] == "ok", own.json()


def test_the_manager_app_is_always_allowed(client, world, manager):
    rostered(world["manager"], DAY)
    response = mark(client, manager, self_punch("punch_in", local(DAY, 9)), app="manager")
    assert response.json()["code"] == "ok"


# -- POST attendance/method --------------------------------------------------------------


def test_the_method_call_says_todays_method_and_next_punch(client, world, mechanic, staff_app):
    now = timezone.now()
    today = business_day_of(world["company"], now)
    rostered(mechanic, today, start=(0, 30), end=(23, 30))

    body = call(client, METHOD.format("employee"), {}, token=staff_app).json()
    assert body["code"] == "ok", body
    data = body["data"]
    assert (data["attendance_method"], data["permanent_method"], data["business_date"]) == (
        "qr_station", "self", today.isoformat())
    assert data["valid_until"] == f"{today + datetime.timedelta(days=1)} 00:00:00"
    assert data["roster"]["role"] == "cashier" and data["roster"]["branch"] == "Creek Park 1"
    assert (data["open_punch_in"], data["next_punch"], data["next_punch_method"]) == (None, "punch_in", "qr_station")
    assert set(data) == set(base.api._METHOD_SAMPLE)


def test_while_a_punch_in_is_open_the_next_punch_is_its_punch_out(client, world, mechanic, staff_app):
    now = timezone.localtime(timezone.now(), base.DUBAI)
    stamp = now.replace(tzinfo=None, microsecond=0) - datetime.timedelta(minutes=1)
    if business_day_of(world["company"], now - datetime.timedelta(minutes=1)) != business_day_of(world["company"], now):
        pytest.skip("too close to the day start")
    assert mark(client, staff_app, self_staff("punch_in", stamp), app="employee").json()["code"] == "ok"
    rostered(mechanic, business_day_of(world["company"], now), start=(0, 0), end=(23, 59))

    data = call(client, METHOD.format("employee"), {}, token=staff_app).json()["data"]
    assert data["attendance_method"] == "qr_station"            # the day's, now
    assert data["open_punch_in"]["source"] == "self"
    assert (data["next_punch"], data["next_punch_method"]) == ("punch_out", "self")   # the punch-in's


@pytest.mark.parametrize("app, user, installation", [("manager", "MGR001", "phone-m"),
                                                      ("operator", "OPR001", "till-1")])
def test_the_method_call_is_the_employee_apps_only(client, world, app, user, installation):
    token = login(client, app, user, installation)
    response = call(client, METHOD.format(app), {}, token=token)
    assert response.status_code == 403 and response.json()["code"] == "wrong_channel"


def test_the_login_carries_todays_method(client, world, mechanic):
    today = business_day_of(world["company"], timezone.now())
    rostered(mechanic, today, start=(0, 0), end=(23, 59))
    User.objects.create_user("EMP001", base.PASSWORD, display_name="Anil", company=world["company"],
                             role=world["role"], employee=mechanic, allowed_channels=[Channel.EMPLOYEE])
    Device.objects.create(company=world["company"], installation_id="phone-e", platform="android",
                          channel=Channel.EMPLOYEE, status=DeviceStatus.APPROVED, approved_at=timezone.now())
    body = call(client, "/api/v1/employee/auth/login",
                {"username": "EMP001", "password": base.PASSWORD, "installation_id": "phone-e"}).json()
    employee = body["data"]["employee"]
    assert (employee["attendance_method"], employee["attendance_method_today"]) == ("self", "qr_station")


# -- Auto-close --------------------------------------------------------------------------


def punch_in(world, at, source=AttendanceSource.QR_SCAN):
    """A stored punch-in for EMP001 at `at` (company time)."""
    fields = {}
    if source == AttendanceSource.QR_SCAN:
        fields = {"qr_generation_time": aware(at), "rms_employee": world["operator"], "rms_employee_code": "OPR001",
                  "rms_employee_name": "Rashed K", "rms_branch": world["auh1"], "employee_installation_id": "phone-e"}
    row = Attendance(id=base.uuid7(), source=source, punch_type=PunchType.PUNCH_IN, employee=world["staff"],
                     employee_code="EMP001", employee_name="Anil K", employee_branch=world["auh1"],
                     rms_installation_id="till-1", rms_scan_time=aware(at), **fields)
    row.save(force_insert=True)
    return row


def closed(row):
    """The punch-out that closed `row`, or None."""
    row = Attendance.objects.get(pk=row.pk)
    return services._punch_out_of(row)


def test_auto_close_takes_the_rostered_shifts_end(world, day_start):
    rostered(world["staff"], DAY, start=(9, 0), end=(13, 0), second=((17, 0), (23, 0)))
    first = punch_in(world, local(DAY, 9, 5))
    assert services.auto_close_due() == 1
    out = closed(first)
    assert (out.source, out.closed_by, out.rms_scan_time) == (
        AttendanceSource.AUTO_CLOSE, ClosedBy.DUTY_ROSTER, aware(local(DAY, 13)))
    assert out.employee_branch == world["auh1"] and out.created_by is None

    second = punch_in(world, local(DAY, 17, 10))
    services.auto_close_due()
    assert closed(second).rms_scan_time == aware(local(DAY, 23))       # the shift it fell in


def test_auto_close_counts_roster_minutes_but_not_a_day_end(world, day_start):
    rostered(world["staff"], DAY, start=(9, 0), end=(13, 0))
    on_shift = punch_in(world, local(DAY, 9))
    services.auto_close_due()
    after_shift = punch_in(world, local(DAY, 14))                     # after the last shift ended
    services.auto_close_due()

    [(_, out1, state1, worked1), (_, out2, state2, worked2)] = services.attendance_sessions(
        Attendance.objects.filter(pk__in=[on_shift.pk, after_shift.pk]).order_by("rms_scan_time"))
    assert (state1, out1.closed_by, worked1) == ("auto_closed", ClosedBy.DUTY_ROSTER, 240)
    assert (state2, out2.closed_by, worked2) == ("auto_closed", ClosedBy.DAY_END, None)
    assert out2.rms_scan_time == aware(local(NEXT, 4))                # the business day's end


def test_auto_close_is_idempotent(world):
    old = punch_in(world, local(DAY, 9))
    assert services.auto_close_due() == 1
    assert services.auto_close_due() == 0
    assert closed(old).source == AttendanceSource.AUTO_CLOSE


def test_auto_close_waits_for_the_business_day_to_end(world, day_start):
    """Run at 03:59 the next morning, the 29th's business day is not over yet."""
    open_in = punch_in(world, local(DAY, 9))
    assert services.auto_close_due(now=aware(local(NEXT, 3, 59))) == 0
    assert closed(open_in) is None
    assert services.auto_close_due(now=aware(local(NEXT, 4, 0))) == 1


def test_a_late_real_punch_out_replaces_the_auto_close(client, world, operator, day_start):
    """The tablet was offline: its 22:30 punch-out reaches the server after
    the day start, and takes the auto-close's place."""
    rostered(world["staff"], DAY, start=(9, 0), end=(22, 0))
    assert mark(client, operator, qr("punch_in", local(DAY, 9))).json()["code"] == "ok"
    services.auto_close_due()
    first_in = Attendance.objects.get(punch_type=PunchType.PUNCH_IN)
    assert closed(first_in).source == AttendanceSource.AUTO_CLOSE

    late = mark(client, operator, qr("punch_out", local(DAY, 22, 30)))
    assert late.json()["code"] == "ok", late.json()
    assert late.json()["data"]["punch_in_sync_id"] == str(first_in.pk)
    out = closed(first_in)
    assert (out.source, out.rms_scan_time) == (AttendanceSource.QR_SCAN, aware(local(DAY, 22, 30)))
    assert not Attendance.objects.filter(source=AttendanceSource.AUTO_CLOSE).exists()


def test_a_punch_out_with_nothing_in_its_day_is_still_refused(client, world, operator, day_start):
    assert mark(client, operator, qr("punch_in", local(DAY, 9))).json()["code"] == "ok"
    services.auto_close_due()
    # The next business day: yesterday's auto-close is not this punch-out's.
    response = mark(client, operator, qr("punch_out", local(NEXT, 10)))
    assert response.json()["code"] == "no_open_punch_in"


def test_one_open_punch_in_per_business_day(client, world, operator, day_start):
    assert mark(client, operator, qr("punch_in", local(DAY, 17))).json()["code"] == "ok"
    # 02:00 the next calendar day is still the same business day.
    response = mark(client, operator, qr("punch_in", local(NEXT, 2)))
    assert response.json()["code"] == "punch_in_open"


@pytest.mark.parametrize("fields", [
    {"closed_by": ""},                                                     # an auto-close says why
    {"punch_type": PunchType.PUNCH_IN, "punch_in": None},                  # only ever a punch-out
    {"rms_latitude": 24, "rms_longitude": 54},                             # no position
])
def test_the_database_keeps_auto_closes_honest(world, fields):
    real = punch_in(world, local(DAY, 9))
    values = {"id": base.uuid7(), "source": AttendanceSource.AUTO_CLOSE, "punch_type": PunchType.PUNCH_OUT,
              "punch_in": real, "employee": world["staff"], "employee_code": "EMP001", "employee_name": "Anil K",
              "rms_installation_id": "", "rms_scan_time": aware(local(DAY, 17)), "closed_by": ClosedBy.DAY_END}
    values.update(fields)
    with pytest.raises(IntegrityError), transaction.atomic():
        Attendance(**values).save(force_insert=True)


def test_a_real_punch_never_has_closed_by(world):
    with pytest.raises(IntegrityError), transaction.atomic():
        Attendance(id=base.uuid7(), source=AttendanceSource.SELF, punch_type=PunchType.PUNCH_IN,
                   employee=world["staff"], employee_code="EMP001", employee_name="Anil K",
                   rms_installation_id="phone-e", rms_scan_time=aware(local(DAY, 9)),
                   closed_by=ClosedBy.DAY_END).save(force_insert=True)
