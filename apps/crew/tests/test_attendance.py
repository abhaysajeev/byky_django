"""Attendance: the table's rules, the two device APIs as the apps call them,
manager login, and the read-only screen.

POST /api/v1/operator/attendance/mark   QR scanned by the RMS app
POST /api/v1/manager/attendance/mark    the manager's own button
POST /api/v1/{app}/attendance/history
"""

import csv
import datetime
import io
import zoneinfo
from decimal import Decimal

import pytest
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.company.models import Branch, BranchType, Company, Country, Location, State
from apps.crew import api
from apps.crew.models import Attendance, AttendanceSource, Designation, Employee, PunchType
from apps.devices.models import Device, DeviceMapping, DeviceSettings, DeviceStatus
from apps.fare.tests.test_api import shape
from apps.portal.models import Role, RolePermission
from apps.portal.services import grant_all
from apps.portal.session_models import AppSession
from core.enums import ApprovalStatus, Channel
from core.ids import uuid7
from core.models import User

PASSWORD = "Byky#2026"
DUBAI = zoneinfo.ZoneInfo("Asia/Dubai")
MARK = "/api/v1/{}/attendance/mark"
HISTORY = "/api/v1/{}/attendance/history"


@pytest.fixture(autouse=True)
def fresh_throttle():
    cache.clear()
    yield
    cache.clear()


def call(client, url, request_data=None, *, token=None):
    headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
    return client.post(url, {"credentials": {}, "request_data": request_data or {}},
                       content_type="application/json", **headers)


@pytest.fixture
def world(db):
    country = Country.objects.create(short_code="AE", name="United Arab Emirates")
    state = State.objects.create(country=country, short_code="AUH", name="Abu Dhabi")
    company = Company.objects.create(short_code="0598", name="BYKY", country=country, state=state,
                                     phone_number="+9710000000", email="ops@byky.test")
    other = Company.objects.create(short_code="OTH", name="Other", country=country, state=state,
                                   phone_number="+9711111111", email="ops@other.test")
    location = Location.objects.create(country=country, state=state, short_code="CORN", name="Corniche")

    def branch(co, code, name):
        return Branch.objects.create(company=co, location=location, short_code=code, name=name,
                                     branch_type=BranchType.STATION)

    auh1, auh2 = branch(company, "AUH01", "Creek Park 1"), branch(company, "AUH02", "Creek Park 2")
    branch(other, "OTH01", "Elsewhere")
    DeviceSettings.objects.create(branch=auh1, settings_code="S01", order_no_prefix="AUH",
                                  approval_status=ApprovalStatus.APPROVED)

    cashier = Designation.objects.create(company=company, code="CASH", title="Cashier")
    manager = Designation.objects.create(company=company, code="MGR", title="Manager")
    role = Role.objects.create(company=company, name="Staff")
    grant_all(role)

    def person(co, code, first, designation, channels=None):
        employee = Employee.objects.create(company=co, employee_code=code, first_name=first,
                                           last_name="K", designation=designation)
        if channels:
            User.objects.create_user(code, PASSWORD, display_name=first, company=co, role=role,
                                     employee=employee, allowed_channels=channels)
        return employee

    def device(installation_id, channel):
        return Device.objects.create(company=company, installation_id=installation_id, platform="android",
                                     channel=channel, status=DeviceStatus.APPROVED, approved_at=timezone.now())

    till = device("till-1", Channel.OPERATOR)
    DeviceMapping.objects.create(device=till, branch=auh1, from_date=timezone.now())
    device("phone-m", Channel.MANAGER)
    return {
        "company": company, "other": other, "auh1": auh1, "auh2": auh2, "role": role,
        "staff": person(company, "EMP001", "Anil", cashier),
        "operator": person(company, "OPR001", "Rashed", cashier, [Channel.OPERATOR]),
        "manager": person(company, "MGR001", "Meera", manager, [Channel.MANAGER]),
        "outsider": person(other, "EMP001", "Other", Designation.objects.create(
            company=other, code="CASH", title="Cashier")),
    }


def login(client, app, username, installation_id):
    response = call(client, f"/api/v1/{app}/auth/login",
                    {"username": username, "password": PASSWORD, "installation_id": installation_id})
    assert response.status_code == 200, response.json()
    return response.json()["data"]["tokens"]["access"]


@pytest.fixture
def operator(client, world):
    return login(client, "operator", "OPR001", "till-1")


@pytest.fixture
def manager(client, world):
    return login(client, "manager", "MGR001", "phone-m")


def local(day, hh, mm=0, ss=0):
    """A company-local wall-clock time, as the apps send it."""
    return datetime.datetime.combine(day, datetime.time(hh, mm, ss))


def fmt(moment):
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def aware(moment):
    return moment.replace(tzinfo=DUBAI)


def qr(kind, at, **overrides):
    """What the RMS app sends after scanning EMP001's QR code at `at`."""
    data = {
        "sync_id": str(uuid7()), "punch_type": kind,
        "employee_code": "EMP001", "employee_name": "Anil K", "employee_branch_code": "AUH01",
        "employee_installation_id": "phone-e", "qr_generation_time": fmt(at - datetime.timedelta(seconds=40)),
        "employee_latitude": "24.453900", "employee_longitude": "54.377300",
        "rms_employee_code": "OPR001", "rms_employee_name": "Rashed K", "rms_branch_code": "AUH01",
        "rms_installation_id": "till-1", "rms_scan_time": fmt(at),
        "rms_latitude": 24.45391234, "rms_longitude": 54.37731299,
    }
    data.update(overrides)
    return data


def self_punch(kind, at, **overrides):
    """What the manager app sends when its user presses Mark attendance."""
    data = {
        "sync_id": str(uuid7()), "punch_type": kind,
        "employee_code": "MGR001", "employee_name": "Meera K", "employee_branch_code": "AUH02",
        "rms_installation_id": "phone-m", "rms_scan_time": fmt(at),
    }
    data.update(overrides)
    return data


def mark(client, token, data, app="operator"):
    return call(client, MARK.format(app), data, token=token)


DAY = datetime.date(2026, 8, 29)   # far enough back that an open punch-in is stale


# -- Marking: the two sources ------------------------------------------------------


def test_a_qr_punch_in_and_out_across_midnight_pair_up(client, world, operator):
    punch_in = qr("punch_in", local(DAY, 17, 2, 40))
    first = mark(client, operator, punch_in)
    assert first.status_code == 200, first.json()
    assert first.json()["code"] == "ok"
    assert first.json()["message"] == "Punch in recorded."

    second = mark(client, operator, qr("punch_out", local(DAY + datetime.timedelta(days=1), 1, 5, 12)))
    body = second.json()
    assert body["code"] == "ok" and body["message"] == "Punch out recorded."
    assert body["data"]["punch_in_sync_id"] == punch_in["sync_id"]
    assert body["data"]["status"] == "closed"
    assert body["data"]["rms_scan_time"] == "2026-08-30 01:05:12"

    row = Attendance.objects.get(pk=punch_in["sync_id"])
    assert row.source == AttendanceSource.QR_SCAN
    assert row.employee == world["staff"] and row.rms_employee == world["operator"]
    assert row.employee_branch == world["auh1"] and row.rms_branch == world["auh1"]
    # Company time as sent: 17:02:40 in Dubai is 13:02:40 UTC.
    assert row.rms_scan_time == datetime.datetime(2026, 8, 29, 13, 2, 40, tzinfo=datetime.UTC)
    assert row.qr_generation_time == aware(local(DAY, 17, 2, 0))
    # A phone's extra decimal places are rounded, not refused.
    assert row.rms_latitude == Decimal("24.453912") and row.rms_longitude == Decimal("54.377313")
    assert row.created_by.username == "opr001"
    assert row.punch_out.punch_type == PunchType.PUNCH_OUT


def test_self_attendance_from_the_manager_app(client, world, manager):
    response = mark(client, manager, self_punch("punch_in", local(DAY, 9), rms_latitude="24.1", rms_longitude="54.2"),
                    app="manager")
    assert response.json()["code"] == "ok", response.json()
    mark(client, manager, self_punch("punch_out", local(DAY, 18)), app="manager")

    punch_in, punch_out = Attendance.objects.order_by("rms_scan_time")
    assert punch_in.source == AttendanceSource.SELF
    assert punch_in.employee == world["manager"] and punch_in.employee_branch == world["auh2"]
    assert punch_in.rms_employee is None and punch_in.rms_branch is None and punch_in.qr_generation_time is None
    assert punch_in.rms_installation_id == "phone-m"
    assert punch_out.punch_in == punch_in


def test_qr_fields_sent_by_the_manager_app_are_not_stored(client, world, manager):
    data = self_punch("punch_in", local(DAY, 9), rms_employee_code="OPR001", qr_generation_time=fmt(local(DAY, 9)))
    assert mark(client, manager, data, app="manager").json()["code"] == "ok"
    row = Attendance.objects.get()
    assert row.rms_employee is None and row.qr_generation_time is None


def test_a_resent_sync_id_is_a_duplicate_not_a_second_punch(client, world, operator):
    data = qr("punch_in", local(DAY, 9))
    mark(client, operator, data)
    again = mark(client, operator, data)
    assert again.status_code == 200
    assert again.json()["code"] == "duplicate"
    assert again.json()["data"]["sync_id"] == data["sync_id"]
    assert Attendance.objects.count() == 1


def test_the_same_qr_code_cannot_punch_twice(client, world, operator):
    first = qr("punch_in", local(DAY, 9))
    mark(client, operator, first)
    mark(client, operator, qr("punch_out", local(DAY, 12)))
    # A new sync_id but the QR code of the first punch-in.
    rescan = qr("punch_in", local(DAY, 13), qr_generation_time=first["qr_generation_time"])
    response = mark(client, operator, rescan)
    assert response.status_code == 409
    assert response.json()["code"] == "qr_already_scanned"


# -- Marking: the sequence ---------------------------------------------------------


def test_a_second_punch_in_while_one_is_open_is_refused(client, world, operator):
    mark(client, operator, qr("punch_in", local(DAY, 9)))
    response = mark(client, operator, qr("punch_in", local(DAY, 11)))
    assert response.status_code == 409
    assert response.json()["code"] == "punch_in_open"


def test_a_forgotten_punch_out_does_not_lock_the_next_day(client, world, operator):
    mark(client, operator, qr("punch_in", local(DAY, 9)))
    response = mark(client, operator, qr("punch_in", local(DAY + datetime.timedelta(days=1), 10)))
    assert response.json()["code"] == "ok"

    history = call(client, HISTORY.format("operator"),
                   {"employee_code": "EMP001", "from_date": "2026-08-29", "to_date": "2026-08-30"},
                   token=operator).json()["data"]
    statuses = [s["status"] for d in history["days"] for s in d["sessions"]]
    assert statuses[0] == "missing_punch_out"


def test_a_punch_out_with_nothing_open_is_refused(client, world, operator):
    response = mark(client, operator, qr("punch_out", local(DAY, 18)))
    assert response.status_code == 409
    assert response.json()["code"] == "no_open_punch_in"


def test_a_punch_out_earlier_than_its_punch_in_is_refused(client, world, operator):
    mark(client, operator, qr("punch_in", local(DAY, 9)))
    response = mark(client, operator, qr("punch_out", local(DAY, 8)))
    assert response.status_code == 409
    assert response.json()["code"] == "punch_out_before_punch_in"


def test_a_punch_out_does_not_close_a_punch_in_older_than_a_day(client, world, operator):
    mark(client, operator, qr("punch_in", local(DAY, 9)))
    response = mark(client, operator, qr("punch_out", local(DAY + datetime.timedelta(days=1), 10)))
    assert response.json()["code"] == "no_open_punch_in"


# -- Marking: refusals ----------------------------------------------------------------


@pytest.mark.parametrize("field, value, message", [
    ("sync_id", "6f1c2b9e-3d4a-4b8c-9e2f-0a1b2c3d4e5f", "must be a UUIDv7"),   # a v4
    ("sync_id", "not-a-uuid", "must be a UUIDv7"),
    ("punch_type", "break", "must be punch_in or punch_out"),
    ("rms_scan_time", "2026-08-29T17:00:00+04:00", "must be a date-time like 2026-09-30 17:05:00"),
    ("rms_scan_time", "29/09/2026 17:00", "must be a date-time like 2026-09-30 17:05:00"),
    ("employee_latitude", "91", "must be 90 or less"),
    ("employee_code", "", "is required"),
])
def test_bad_fields_are_refused(client, world, operator, field, value, message):
    response = mark(client, operator, qr("punch_in", local(DAY, 9), **{field: value}))
    assert response.status_code == 400
    assert response.json()["data"]["errors"][field] == message


def test_a_latitude_needs_its_longitude(client, world, operator):
    data = qr("punch_in", local(DAY, 9))
    del data["employee_longitude"]
    response = mark(client, operator, data)
    assert response.json()["data"]["errors"] == {"employee_longitude": "is required with the other coordinate"}


def test_the_operator_must_send_the_qr_side(client, world, operator):
    response = mark(client, operator, self_punch("punch_in", local(DAY, 9)))
    assert response.status_code == 400
    assert "qr_generation_time" in response.json()["data"]["errors"]


@pytest.mark.parametrize("overrides, code", [
    ({"employee_code": "NOBODY"}, "unknown_employee"),
    ({"rms_employee_code": "NOBODY"}, "unknown_rms_employee"),
    ({"employee_branch_code": "OTH01"}, "unknown_branch"),     # another company's branch
    ({"rms_branch_code": "NOPE"}, "unknown_branch"),
])
def test_unknown_codes_are_refused(client, world, operator, overrides, code):
    response = mark(client, operator, qr("punch_in", local(DAY, 9), **overrides))
    assert response.status_code == 400
    assert response.json()["code"] == code
    assert not Attendance.objects.exists()


def test_the_code_is_matched_within_the_devices_company(client, world, operator):
    """EMP001 exists in both companies; the punch is for this company's."""
    mark(client, operator, qr("punch_in", local(DAY, 9)))
    assert Attendance.objects.get().employee == world["staff"]


@pytest.mark.parametrize("change, code", [
    ({"is_blocked": True}, "employee_blocked"),
    ({"is_active": False}, "employee_inactive"),
])
def test_a_blocked_or_inactive_employee_is_refused(client, world, operator, change, code):
    Employee.objects.filter(pk=world["staff"].pk).update(**change)
    response = mark(client, operator, qr("punch_in", local(DAY, 9)))
    assert response.status_code == 403
    assert response.json()["code"] == code


def test_the_employee_app_has_no_attendance_endpoint(client, world, operator):
    for url in (MARK.format("employee"), HISTORY.format("employee")):
        response = call(client, url, {}, token=operator)
        assert response.status_code == 403
        assert response.json()["code"] == "wrong_channel"


def test_no_token_is_refused(client, world):
    response = mark(client, None, qr("punch_in", local(DAY, 9)))
    assert response.status_code == 401
    assert response.json()["code"] == "not_authenticated"


def test_the_mark_reply_matches_the_docs(client, world, operator):
    data = mark(client, operator, qr("punch_in", local(DAY, 9))).json()["data"]
    assert shape(data) == shape(api._MARK_IN_SAMPLE)


# -- History ---------------------------------------------------------------------------


def test_history_keeps_a_next_day_punch_out_on_the_day_it_started(client, world, operator):
    mark(client, operator, qr("punch_in", local(DAY, 9)))
    mark(client, operator, qr("punch_out", local(DAY, 13, 30)))
    mark(client, operator, qr("punch_in", local(DAY, 17)))
    mark(client, operator, qr("punch_out", local(DAY + datetime.timedelta(days=1), 1, 15)))

    body = call(client, HISTORY.format("operator"), {"employee_code": "EMP001", "date": "2026-08-29"},
                token=operator).json()
    assert body["code"] == "ok"
    data = body["data"]
    assert data["employee"] == {"employee_code": "EMP001", "employee_name": "Anil K"}
    [day] = data["days"]
    assert day["date"] == "2026-08-29"
    assert [(s["punch_in"]["time"], s["punch_out"]["time"], s["worked_minutes"]) for s in day["sessions"]] == [
        ("2026-08-29 09:00:00", "2026-08-29 13:30:00", 270),
        ("2026-08-29 17:00:00", "2026-08-30 01:15:00", 495),
    ]
    assert shape(data) == shape(api._HISTORY_SAMPLE)

    # The next day, alone, has nothing: that punch-out belongs to the 29th.
    next_day = call(client, HISTORY.format("operator"), {"employee_code": "EMP001", "date": "2026-08-30"},
                    token=operator).json()["data"]
    assert next_day["days"] == []


def test_history_over_a_range_and_for_self_attendance(client, world, manager):
    for offset in (0, 2):
        day = DAY + datetime.timedelta(days=offset)
        mark(client, manager, self_punch("punch_in", local(day, 9)), app="manager")
        mark(client, manager, self_punch("punch_out", local(day, 17)), app="manager")

    data = call(client, HISTORY.format("manager"),
                {"employee_code": "MGR001", "from_date": "2026-08-28", "to_date": "2026-08-31"},
                token=manager).json()["data"]
    assert [d["date"] for d in data["days"]] == ["2026-08-29", "2026-08-31"]
    session = data["days"][0]["sessions"][0]
    assert session["punch_in"]["source"] == "self" and session["punch_in"]["rms_branch_code"] is None


def test_history_defaults_to_today(client, world, operator):
    now = timezone.now().astimezone(DUBAI).replace(tzinfo=None, microsecond=0)
    mark(client, operator, qr("punch_in", now - datetime.timedelta(minutes=5)))
    data = call(client, HISTORY.format("operator"), {"employee_code": "EMP001"}, token=operator).json()["data"]
    assert data["from_date"] == data["to_date"] == now.date().isoformat()
    [session] = data["days"][-1]["sessions"]
    assert session["status"] == "open" and session["punch_out"] is None


@pytest.mark.parametrize("request_data, field", [
    ({}, "employee_code"),
    ({"employee_code": "EMP001", "date": "2026-08-29", "from_date": "2026-08-29"}, "date"),
    ({"employee_code": "EMP001", "from_date": "2026-08-29"}, "to_date"),
    ({"employee_code": "EMP001", "from_date": "2026-08-29", "to_date": "2026-08-01"}, "to_date"),
    ({"employee_code": "EMP001", "from_date": "2026-07-01", "to_date": "2026-08-30"}, "to_date"),
])
def test_history_request_is_checked(client, world, operator, request_data, field):
    response = call(client, HISTORY.format("operator"), request_data, token=operator)
    assert response.status_code == 400
    assert field in response.json()["data"]["errors"]


def test_history_of_an_unknown_employee(client, world, operator):
    response = call(client, HISTORY.format("operator"), {"employee_code": "NOBODY"}, token=operator)
    assert response.json()["code"] == "unknown_employee"


def test_the_endpoints_are_in_the_docs(client, world):
    schema = client.get("/api/schema/").content.decode()
    assert "/api/v1/{app}/attendance/mark" in schema and "/api/v1/{app}/attendance/history" in schema
    assert "Manager Attendance" in schema and "UUIDv7" in schema


# -- Manager login ---------------------------------------------------------------------


def test_manager_login_returns_the_tokens_only(client, world):
    response = call(client, "/api/v1/manager/auth/login",
                    {"username": "MGR001", "password": PASSWORD, "installation_id": "phone-m"})
    body = response.json()
    assert body["code"] == "ok"
    assert set(body["data"]) == {"tokens"}
    assert set(body["data"]["tokens"]) == {"access", "refresh", "access_expires_at", "refresh_expires_at"}
    session = AppSession.objects.get(user__username="mgr001", logged_out_at__isnull=True)
    assert session.channel == Channel.MANAGER and session.branch is None


@pytest.mark.parametrize("installation_id, status, code", [
    ("unknown-phone", 409, "device_not_registered"),
])
def test_manager_login_needs_a_registered_device(client, world, installation_id, status, code):
    response = call(client, "/api/v1/manager/auth/login",
                    {"username": "MGR001", "password": PASSWORD, "installation_id": installation_id})
    assert response.status_code == status
    assert response.json()["code"] == code


def test_manager_login_refuses_a_blocked_device(client, world):
    Device.objects.filter(installation_id="phone-m").update(status=DeviceStatus.BLOCKED)
    response = call(client, "/api/v1/manager/auth/login",
                    {"username": "MGR001", "password": PASSWORD, "installation_id": "phone-m"})
    assert response.json()["code"] == "device_blocked"


def test_an_operator_account_cannot_use_the_manager_app(client, world):
    response = call(client, "/api/v1/manager/auth/login",
                    {"username": "OPR001", "password": PASSWORD, "installation_id": "phone-m"})
    assert response.status_code == 403
    assert response.json()["code"] == "wrong_channel"


# -- The table's own rules ---------------------------------------------------------------


def _row(world, **fields):
    values = {
        "id": uuid7(), "source": AttendanceSource.SELF, "punch_type": PunchType.PUNCH_IN,
        "employee": world["manager"], "employee_code": "MGR001", "employee_name": "Meera K",
        "employee_branch": world["auh1"], "rms_installation_id": "phone-m",
        "rms_scan_time": aware(local(DAY, 9)),
    }
    values.update(fields)
    return Attendance(**values)


@pytest.mark.parametrize("fields", [
    {"source": AttendanceSource.QR_SCAN},                                 # QR with no QR side
    {"qr_generation_time": aware(local(DAY, 9))},                         # self with a QR time
    {"punch_type": PunchType.PUNCH_OUT},                                  # an out closing nothing
    {"rms_latitude": Decimal("24.1")},                                    # half a position
    {"rms_latitude": Decimal("95"), "rms_longitude": Decimal("54")},      # off the map
])
def test_the_database_refuses_inconsistent_rows(world, fields):
    with pytest.raises(IntegrityError), transaction.atomic():
        _row(world, **fields).save(force_insert=True)


def test_one_punch_in_is_closed_once(world):
    punch_in = _row(world)
    punch_in.save(force_insert=True)
    _row(world, punch_type=PunchType.PUNCH_OUT, punch_in=punch_in).save(force_insert=True)
    with pytest.raises(IntegrityError), transaction.atomic():
        _row(world, punch_type=PunchType.PUNCH_OUT, punch_in=punch_in).save(force_insert=True)


# -- The screen ----------------------------------------------------------------------


def client_in(client, world):
    """The same test client, signed in to the web screens."""
    User.objects.create_user("sara.k", PASSWORD, display_name="Sara K", company=world["company"],
                             role=world["role"], allowed_channels=[Channel.WEB])
    client.post("/login/", {"username": "sara.k", "password": PASSWORD})
    return client


def seed_day(client, token):
    """EMP001: two shifts on the 29th, the second ending after midnight."""
    mark(client, token, qr("punch_in", local(DAY, 9)))
    mark(client, token, qr("punch_out", local(DAY, 13)))
    mark(client, token, qr("punch_in", local(DAY, 17)))
    mark(client, token, qr("punch_out", local(DAY + datetime.timedelta(days=1), 1, 30)))


def test_the_screen_shows_one_row_per_employee_day(client, world, operator, manager):
    seed_day(client, operator)
    mark(client, manager, self_punch("punch_in", local(DAY, 8)), app="manager")

    web = client_in(client, world)
    body = web.get("/crew/attendance/list/", {"from": "2026-08-29", "to": "2026-08-29"}).content.decode()

    assert body.count('class="scr-expand-row"') == 2
    assert "Anil K" in body and "Meera K" in body
    assert "01:30:00" in body and "+1 day" in body            # the second shift's punch-out
    assert "12h 30m" in body                                  # worked: 4h + 8h30m
    assert 'scr-badge-mandatory"><i></i>Missing punch-out' in body   # Meera never punched out
    assert 'data-scr-open="attendance:add"' not in body        # read-only


@pytest.mark.parametrize("params, shown, hidden", [
    ({"source": "self"}, "Meera K", "Anil K"),
    ({"q": "EMP001"}, "Anil K", "Meera K"),
    ({"status": "complete"}, "Anil K", "Meera K"),
    ({"status": "missing"}, "Meera K", "Anil K"),
])
def test_the_screen_filters(client, world, operator, manager, params, shown, hidden):
    seed_day(client, operator)
    mark(client, manager, self_punch("punch_in", local(DAY, 8)), app="manager")

    web = client_in(client, world)
    body = web.get("/crew/attendance/list/", {"from": "2026-08-29", "to": "2026-08-29", **params}).content.decode()
    assert shown in body and hidden not in body


def test_the_branch_filter_matches_either_branch(client, world, manager):
    mark(client, manager, self_punch("punch_in", local(DAY, 8)), app="manager")        # AUH02
    web = client_in(client, world)
    url = "/crew/attendance/list/"
    base = {"from": "2026-08-29", "to": "2026-08-29"}
    assert "Meera K" in web.get(url, {**base, "branch": world["auh2"].pk}).content.decode()
    assert "Meera K" not in web.get(url, {**base, "branch": world["auh1"].pk}).content.decode()


def test_an_empty_screen_says_so(client, world):
    body = client_in(client, world).get("/crew/attendance/list/").content.decode()
    assert "No attendance today" in body


def test_export_is_every_pair_the_filters_match(client, world, operator):
    seed_day(client, operator)
    web = client_in(client, world)
    response = web.get("/crew/attendance/export/", {"from": "2026-08-29", "to": "2026-08-29"})
    rows = list(csv.reader(io.StringIO(b"".join(response.streaming_content).decode("utf-8-sig"))))
    assert rows[0][:5] == ["Date", "Employee Code", "Employee Name", "Punch In", "Punch Out"]
    assert [(r[3], r[4], r[5]) for r in rows[1:]] == [
        ("2026-08-29 09:00:00", "2026-08-29 13:00:00", "240"),
        ("2026-08-29 17:00:00", "2026-08-30 01:30:00", "510"),
    ]


def test_export_needs_print(client, world):
    RolePermission.objects.filter(role=world["role"], page__code="crew.attendance").update(can_print=False)
    web = client_in(client, world)
    assert web.get("/crew/attendance/export/").status_code == 403
    assert "/crew/attendance/export/" not in web.get("/crew/attendance/list/").content.decode()
