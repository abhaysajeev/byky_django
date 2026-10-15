"""The attendance APIs -- views only translate HTTP; the rules are in
apps/crew/services.py (mark_attendance, attendance_history).

POST /api/v1/{app}/attendance/mark     one punch, in or out
POST /api/v1/{app}/attendance/history  an employee's punches for a day or range
POST /api/v1/{app}/attendance/team     Team Attendance, manager and employee apps

`{app}` decides the source: `operator` (the RMS app) scans an employee's QR
code; `manager` marks its own user, and so does `employee` for staff whose
attendance method is Self (Supervisors, PROs). Every call also checks that
the session was signed in on that app.
"""

from django.core.paginator import Paginator
from django.utils import timezone
from drf_spectacular.utils import OpenApiExample, PolymorphicProxySerializer, extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.crew import services
from apps.crew.models import AttendanceSource, PunchType
from apps.crew.serializers import (
    AttendanceHistoryRequest,
    QrAttendanceRequest,
    SelfAttendanceRequest,
    TeamAttendanceRequest,
)
from apps.devices.models import Device
from apps.portal.authentication import AppJWTAuthentication
from apps.sync import parking
from apps.sync.models import IssueKind
from core.api import envelope, other_app, request_parts, station_of
from core.enums import Channel
from core.schema import SERVER_ERROR, envelope_request, envelope_responses
from core.timezones import business_date_for, zone_for

# {app} -> (source, request serializer)
_SOURCES = {
    Channel.OPERATOR: (AttendanceSource.QR_SCAN, QrAttendanceRequest),
    Channel.MANAGER: (AttendanceSource.SELF, SelfAttendanceRequest),
    # Only staff whose attendance method is Self -- services.mark_attendance.
    Channel.EMPLOYEE: (AttendanceSource.SELF, SelfAttendanceRequest),
}

# History is read by every app.
_HISTORY_APPS = (Channel.OPERATOR, Channel.MANAGER, Channel.EMPLOYEE)

# Team Attendance: the manager app (whole company) and the employee app (by
# the designation's Team Scope).
_TEAM_APPS = (Channel.MANAGER, Channel.EMPLOYEE)

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def _local(moment, zone):
    return moment.astimezone(zone).strftime(TIME_FORMAT) if moment else None


def _station_or_refusal(session, app, *, replay=False):
    """The operator app is a station till: its device must still be mapped
    to an open branch. The manager's and employee's phones have no station."""
    if app == Channel.OPERATOR:
        _, refused = station_of(session, replay=replay)
        return refused
    return None


# -- Mark -----------------------------------------------------------------------

_MARK_SAMPLE = {
    "sync_id": "01923f8e-5b2a-7c3d-9e4f-a1b2c3d4e5f6", "punch_type": "punch_out", "source": "qr_scan",
    "employee_code": "BYKY014", "rms_scan_time": "2026-09-30 01:05:12",
    "punch_in_sync_id": "01923e1c-0a11-7b22-8c33-d4e5f6a7b8c9", "status": "closed",
}

_MARK_IN_SAMPLE = {**_MARK_SAMPLE, "punch_type": "punch_in", "rms_scan_time": "2026-09-29 17:02:40",
                   "sync_id": "01923e1c-0a11-7b22-8c33-d4e5f6a7b8c9", "punch_in_sync_id": None, "status": "open"}

_MARK_DESCRIPTION = """
One punch -- the same call for **punch_in** and **punch_out**.

* `operator` (RMS app): an employee's QR code was scanned. Send the employee
  side from the QR code and the RMS side from this device.
* `manager`: the manager's own **Mark attendance** button -- always the
  signed-in manager's own punch. Send `sync_id`, `punch_type`, `rms_scan_time`
  (the button press) and optionally `rms_latitude/longitude`. The manager and
  the phone come from the session. `employee_code` is optional (if sent it must
  be the manager's own, else `not_your_attendance`); `employee_branch_code` is
  optional (a manager covers every station -- the manager's home branch is
  used when it is not sent, or none); `rms_installation_id` is optional.
  Always allowed.
* `employee`: the same body as `manager` -- the signed-in employee's own
  **Mark / Close attendance**, for staff whose attendance method is **Self**
  (Supervisors, PROs). Anyone else is refused `self_attendance_not_allowed`:
  the login reply's `employee.attendance_method` says which button the app
  shows (`qr_station` the QR code, `self` Mark / Close, `not_required`
  neither).

The request bodies differ per app: see the two schemas and examples below.

**Attendance method** -- set on the Designation (an employee may override it).
The operator app scans only staff whose method is `qr_station`; scanning
anyone else is refused `attendance_method_mismatch`.

**sync_id** -- a **UUIDv7** made on the device for this punch and resent
**unchanged** on every retry. It becomes the record's id; a second call with
the same sync_id is answered `duplicate` with the stored punch and writes
nothing, so an offline queue can resend safely.

**Times** -- `YYYY-MM-DD HH:MM:SS` in company time, no offset (the app's clock
is set from server/time).

**Codes, not names** -- people are found by `employee_code` and
`rms_employee_code` (the operator's own code is `employee_code` in the
operator login), branches by `employee_branch_code` and `rms_branch_code`.
`employee_name` and `rms_employee_name` are optional and **ignored**: the
server records each name from the employee master.

**Pairing** -- a punch_out closes the employee's open punch_in, even across
midnight. A punch_in stays open for 24 hours; after that it is a missing
punch-out and no longer blocks a new punch_in.

| Refusal | When |
|---|---|
| `punch_in_open` | already punched in and not out |
| `no_open_punch_in` | nothing to close -- if the punch_in is still queued offline, keep this punch_out queued and retry after it |
| `punch_out_before_punch_in` | the punch_out's time is before its punch_in |
| `qr_already_scanned` | this QR code was already used for this punch type |
| `attendance_method_mismatch` | operator: this employee's method is not QR scan |
| `self_attendance_not_allowed` | employee app: this employee's method is not Self |
| `wrong_channel` | the token was signed in on another app than the URL's |
"""


class AttendanceMarkView(APIView):
    """POST /api/v1/{app}/attendance/mark -- all three apps."""

    authentication_classes = [AppJWTAuthentication]
    permission_classes = [IsAuthenticated]
    park_kind = IssueKind.ATTENDANCE

    @extend_schema(
        tags=["Operator Attendance", "Manager Attendance", "Employee Attendance"],
        summary="Record a punch in or punch out",
        description=_MARK_DESCRIPTION,
        request=PolymorphicProxySerializer(
            component_name="AttendanceMarkEnvelope",
            serializers=[
                envelope_request("OperatorAttendanceMarkEnvelope", QrAttendanceRequest),
                envelope_request("SelfAttendanceMarkEnvelope", SelfAttendanceRequest),
            ],
            resource_type_field_name=None,
        ),
        examples=[
            OpenApiExample("operator (QR scan)", request_only=True, value={"credentials": {}, "request_data": {
                "sync_id": "01a1013d-fa97-7179-93ae-3ae25842f1ce", "punch_type": "punch_in",
                "employee_code": "BYKY014", "employee_branch_code": "AUH01",
                "employee_installation_id": "4b22c896-27f9-40eb-afab-3546f2105ae6",
                "qr_generation_time": "2026-10-05 09:01:40",
                "employee_latitude": "24.4539", "employee_longitude": "54.3773",
                "rms_employee_code": "BYKY003", "rms_branch_code": "AUH01",
                "rms_installation_id": "7a77cf72-1aef-4931-ab4f-faed221d236f",
                "rms_scan_time": "2026-10-05 09:02:00", "rms_latitude": "24.4539", "rms_longitude": "54.3773",
            }}),
            OpenApiExample("manager or employee (own punch)", request_only=True, value={
                "credentials": {}, "request_data": {
                    "sync_id": "01a1013d-fa97-7179-93ae-3ae25842f1cf", "punch_type": "punch_in",
                    "rms_scan_time": "2026-10-05 09:02:00", "rms_latitude": "25.2415", "rms_longitude": "55.3283",
                }}),
        ],
        responses=envelope_responses(
            (200, "ok", "Punch in recorded.", _MARK_IN_SAMPLE, "ok (punch_in)"),
            (200, "ok", "Punch out recorded.", _MARK_SAMPLE, "ok (punch_out)"),
            (200, "duplicate", "Already recorded.", _MARK_SAMPLE),
            (400, "invalid_request", "sync_id must be a UUIDv7.", {"errors": {"sync_id": "must be a UUIDv7"}}),
            (400, "unknown_employee", "No employee with that code.", {}),
            (400, "unknown_rms_employee", "No employee with that code.", {}),
            (400, "unknown_branch", "No branch with that employee_branch_code.", {}),
            (401, "not_authenticated", "Sign in first.", {}),
            (403, "wrong_channel", "Not allowed on this app.", {}),
            (403, "employee_blocked", "This employee is blocked.", {}),
            (403, "employee_inactive", "This employee is not active.", {}),
            (403, "not_your_attendance", "You can mark only your own attendance.", {},
             "not_your_attendance (manager, employee)"),
            (403, "attendance_method_mismatch", "This employee's attendance is not taken by QR scan.", {},
             "attendance_method_mismatch (operator)"),
            (403, "self_attendance_not_allowed", "Your attendance is not marked in this app.", {},
             "self_attendance_not_allowed (employee)"),
            (409, "punch_in_open", "Already punched in. Punch out first.", {}),
            (409, "no_open_punch_in", "No punch in to close. Punch in first.", {}),
            (409, "punch_out_before_punch_in", "Punch out is earlier than the punch in.", {}),
            (409, "qr_already_scanned", "That QR code has already been scanned.", {}),
            (409, "sync_id_conflict", "That sync_id is already used.", {}),
            (409, "device_not_mapped", "This device has no station.", {}, "device_not_mapped (operator)"),
            (409, "branch_inactive", "This station is closed.", {}, "branch_inactive (operator)"),
            *parking.SCHEMA_ROWS,
            SERVER_ERROR,
        ),
    )
    def post(self, request, app):
        if app not in _SOURCES:
            return envelope("wrong_channel", "Not allowed on this app.", http_status=403)
        refused = other_app(request, app)
        if refused:
            return refused
        return parking.handle(request, app, self.park_kind, self.perform)

    def perform(self, session, app, request_data, *, replay=False):
        """The call's rules; a replay of a parked call runs it as `session`.
        A punch is parked on its own: it belongs to no order."""
        refused = _station_or_refusal(session, app, replay=replay)
        if refused:
            return refused

        source, request_class = _SOURCES[app]
        form = request_class(data=request_data)
        form.is_valid(raise_exception=True)

        company = session.user.employee.company
        zone = zone_for(company)
        values = dict(form.validated_data)
        for key in ("rms_scan_time", "qr_generation_time"):
            if values.get(key) is not None:
                values[key] = timezone.make_aware(values[key], zone)

        try:
            row, created = services.mark_attendance(session.user, source, values, device=session.device,
                                                    channel=app)
        except services.AttendanceRefused as refusal:
            return envelope(refusal.code, refusal.message, http_status=refusal.status)

        data = _mark_json(row, zone)
        if not created:
            return envelope("duplicate", "Already recorded.", data)
        message = "Punch in recorded." if row.punch_type == PunchType.PUNCH_IN else "Punch out recorded."
        return envelope("ok", message, data)


def _mark_json(row, zone):
    return {
        "sync_id": str(row.pk), "punch_type": row.punch_type, "source": row.source,
        "employee_code": row.employee_code, "rms_scan_time": _local(row.rms_scan_time, zone),
        "punch_in_sync_id": str(row.punch_in_id) if row.punch_in_id else None,
        "status": services.SessionStatus.CLOSED if row.punch_type == PunchType.PUNCH_OUT
        else services.session_status(row),
    }


# -- History --------------------------------------------------------------------

_PUNCH_IN_SAMPLE = {
    "sync_id": "01923e1c-0a11-7b22-8c33-d4e5f6a7b8c9", "time": "2026-09-29 17:02:40", "source": "qr_scan",
    "employee_branch_code": "AUH01", "rms_branch_code": "AUH01",
    "rms_employee_code": "BYKY003", "rms_employee_name": "Rashed K",
}

_HISTORY_SAMPLE = {
    "employee": {"employee_code": "BYKY014", "employee_name": "Anil Kumar"},
    "from_date": "2026-09-29", "to_date": "2026-09-29",
    "days": [{
        "date": "2026-09-29",
        "sessions": [{
            "punch_in": _PUNCH_IN_SAMPLE,
            "punch_out": {**_PUNCH_IN_SAMPLE, "sync_id": "01923f8e-5b2a-7c3d-9e4f-a1b2c3d4e5f6",
                          "time": "2026-09-30 01:05:12"},
            "status": "closed", "worked_minutes": 482,
        }],
    }],
}

_HISTORY_DESCRIPTION = """
One employee's punches. `request_data`: `employee_code` -- optional, the
signed-in user's own by default. The employee app sees only its own; the
operator and manager apps may name any employee of the company. Then one of

* `date` -- one day;
* `from_date` + `to_date` -- a range, at most 31 days;
* neither -- today.

A day lists the punch-ins made on it, each with its punch_out **even when that
fell the next day** (a 17:00 to 01:00 shift stays on the day it started).
`status` is `closed`, `open` (punched in, within 24 hours) or
`missing_punch_out`. Days without a punch are left out. Dates are
`YYYY-MM-DD`, times `YYYY-MM-DD HH:MM:SS`, company time.
"""


class AttendanceHistoryView(APIView):
    """POST /api/v1/{app}/attendance/history -- all three apps; the employee
    app only for the signed-in employee."""

    authentication_classes = [AppJWTAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["Operator Attendance", "Manager Attendance", "Employee Attendance"],
        summary="An employee's punches for a day or a date range",
        description=_HISTORY_DESCRIPTION,
        request=envelope_request("AttendanceHistoryEnvelope", AttendanceHistoryRequest),
        responses=envelope_responses(
            (200, "ok", "Attendance.", _HISTORY_SAMPLE),
            (400, "invalid_request", "to_date is required with from_date.",
             {"errors": {"to_date": "is required with from_date"}}),
            (400, "unknown_employee", "No employee with that code.", {}),
            (401, "not_authenticated", "Sign in first.", {}),
            (403, "forbidden", "You can only see your own attendance.", {}, "forbidden (employee app)"),
            (403, "wrong_channel", "Not allowed on this app.", {}),
            (409, "device_not_mapped", "This device has no station.", {}, "device_not_mapped (operator)"),
            (409, "branch_inactive", "This station is closed.", {}, "branch_inactive (operator)"),
            SERVER_ERROR,
        ),
    )
    def post(self, request, app):
        if app not in _HISTORY_APPS:
            return envelope("wrong_channel", "Not allowed on this app.", http_status=403)
        refused = other_app(request, app) or _station_or_refusal(request.auth, app)
        if refused:
            return refused

        _, request_data = request_parts(request)
        form = AttendanceHistoryRequest(data=request_data)
        form.is_valid(raise_exception=True)
        values = form.validated_data

        own = request.user.employee
        code = values.get("employee_code") or own.employee_code
        if app == Channel.EMPLOYEE and code != own.employee_code:
            return envelope("forbidden", "You can only see your own attendance.", http_status=403)

        company = own.company
        today = business_date_for(company)
        from_date = values.get("date") or values.get("from_date") or today
        to_date = values.get("date") or values.get("to_date") or today

        try:
            employee, days = services.attendance_history(company, code, from_date, to_date)
        except services.AttendanceRefused as refusal:
            return envelope(refusal.code, refusal.message, http_status=refusal.status)

        zone = zone_for(company)
        return envelope("ok", "Attendance.", {
            "employee": {"employee_code": employee.employee_code, "employee_name": employee.full_name},
            "from_date": from_date.isoformat(), "to_date": to_date.isoformat(),
            "days": [
                {"date": day.isoformat(), "sessions": [
                    {"punch_in": _punch_json(punch_in, zone),
                     "punch_out": _punch_json(punch_out, zone) if punch_out else None,
                     "status": status, "worked_minutes": worked}
                    for punch_in, punch_out, status, worked in sessions
                ]}
                for day, sessions in days
            ],
        })


def _punch_json(row, zone):
    return {
        "sync_id": str(row.pk), "time": _local(row.rms_scan_time, zone), "source": row.source,
        "employee_branch_code": row.employee_branch.short_code if row.employee_branch_id else None,
        "rms_branch_code": row.rms_branch.short_code if row.rms_branch_id else None,
        "rms_employee_code": row.rms_employee_code or None,
        "rms_employee_name": row.rms_employee_name or None,
    }


# -- Team Attendance -----------------------------------------------------------

_TEAM_STATION = {"branch_id": 12, "code": "AUH01", "name": "Creek Park 1"}

_TEAM_SESSION_ROW = {
    "row_type": "session", "attendance_id": "01923e1c-0a11-7b22-8c33-d4e5f6a7b8c9",
    "status": "checked_out", "missing_check_out": False,
    "employee": {"employee_code": "BYKY014", "name": "Anil Kumar", "designation": "Cashier"},
    "source": "qr_scan", "station": _TEAM_STATION, "emirate": {"state_id": 1, "name": "Abu Dhabi"},
    "check_in": "2026-10-15 07:58:10", "check_out": "2026-10-15 16:02:44", "duration_minutes": 484,
    "device_no": 1000023, "roster": None,
}

_TEAM_NOT_CHECKED_IN_ROW = {
    "row_type": "not_checked_in", "attendance_id": None, "status": "not_checked_in", "missing_check_out": False,
    "employee": {"employee_code": "BYKY031", "name": "Ravi S", "designation": "Labour"},
    "source": None, "station": None, "emirate": {"state_id": 1, "name": "Abu Dhabi"},
    "check_in": None, "check_out": None, "duration_minutes": None, "device_no": None,
    "roster": {"station": _TEAM_STATION, "shift1_start": "08:00", "shift1_end": "16:00",
               "shift2_start": None, "shift2_end": None},
}

_TEAM_SAMPLE = {
    "date": "2026-10-15", "scope": "branches",
    "branches": [{**_TEAM_STATION, "state_id": 1, "state": "Abu Dhabi"}],
    "counts": {"all": 2, "checked_in": 0, "checked_out": 1, "not_checked_in": 1},
    "rows": [_TEAM_SESSION_ROW, _TEAM_NOT_CHECKED_IN_ROW],
    "page": 1, "pages": 1, "total": 2,
}

_TEAM_DESCRIPTION = """
Team Attendance for one day -- who checked in, who checked out, and who is
rostered but has not checked in.

**Whose** -- the **manager** app sees the whole company. In the **employee**
app the designation's **Team Scope** decides (the login reply's
`employee.team_scope`):

* `company` -- the whole company;
* `branches` -- the stations the employee is a **Branch Supervisor** of (Branch
  screen). None assigned: an empty list and `branches: []`;
* `own` -- refused `no_team_scope`.

`request_data`, all optional: `date` (`YYYY-MM-DD`, today by default),
`state_id` (emirate), `branch_id` (station), `status` (`all` | `checked_in` |
`checked_out` | `not_checked_in`, default `all`), `search` (employee name or
code), `page` (50 rows a page).

**Rows** -- one per session: a check-in made that day with its check-out, even
when that fell the next day (a 17:00 to 01:00 shift stays on the day it
started). Newest check-in first, then the not-checked-in rows by name. Each
row carries everything the detail screen shows -- there is no second call.

* `status` -- `checked_in`, `checked_out` or `not_checked_in`. A check-in
  left open for more than 24 hours stays `checked_in` with
  `missing_check_out: true`.
* `source` -- `qr_scan` (scanned at a station) or `self` (Mark attendance in
  the manager or employee app). A self check-in has no station: `station` is
  null, and it appears only in the whole-company list with no station or
  emirate filter. A Branch Supervisor's list holds QR check-ins scanned at
  their stations.
* `emirate` -- the station's emirate; for a self check-in the branch the punch
  names (sent, or the home branch; null when none); for a not-checked-in row
  the rostered station's.
* `duration_minutes` -- check-in to check-out; null while open.
* `device_no` -- the registration number of the device that recorded the
  check-in (the station tablet, or the phone), null when not found.
* **Not checked in** -- staff rostered **Working** that day (Duty Roster) at
  a station in scope, with no check-in that day anywhere. The roster covers
  cashier and labour only. `roster` gives the station and shift times.

`counts` follow every filter but `status`, for the tabs. `branches` lists the
stations in scope (active stations for the whole company), for the station and
emirate pickers. Times are `YYYY-MM-DD HH:MM:SS`, company time.

**Setup** -- set Supervisor / PRO designations to *Attendance Method: Self
check-in* and *Team Scope: Assigned branches*, then pick them as **Branch
Supervisors** on each station they look after.
"""


class TeamAttendanceView(APIView):
    """POST /api/v1/{app}/attendance/team -- manager and employee apps."""

    authentication_classes = [AppJWTAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["Manager Attendance", "Employee Attendance"],
        summary="Team Attendance for a day",
        description=_TEAM_DESCRIPTION,
        request=envelope_request("TeamAttendanceEnvelope", TeamAttendanceRequest),
        responses=envelope_responses(
            (200, "ok", "Team attendance.", _TEAM_SAMPLE),
            (200, "ok", "No stations are assigned to you.",
             {**_TEAM_SAMPLE, "branches": [], "rows": [], "total": 0, "pages": 1,
              "counts": {"all": 0, "checked_in": 0, "checked_out": 0, "not_checked_in": 0}},
             "ok (no stations assigned)"),
            (400, "invalid_request", "status must be all, checked_in, checked_out or not_checked_in.",
             {"errors": {"status": "must be all, checked_in, checked_out or not_checked_in"}}),
            (401, "not_authenticated", "Sign in first.", {}),
            (403, "no_team_scope", "You do not see a team's attendance.", {}, "no_team_scope (employee)"),
            (403, "wrong_channel", "Not allowed on this app.", {}),
            SERVER_ERROR,
        ),
    )
    def post(self, request, app):
        if app not in _TEAM_APPS:
            return envelope("wrong_channel", "Not allowed on this app.", http_status=403)
        refused = other_app(request, app)
        if refused:
            return refused

        _, request_data = request_parts(request)
        form = TeamAttendanceRequest(data=request_data)
        form.is_valid(raise_exception=True)
        values = form.validated_data

        employee = request.user.employee
        company = employee.company
        try:
            scope, branch_ids = services.team_scope_of(employee, app)
        except services.AttendanceRefused as refusal:
            return envelope(refusal.code, refusal.message, http_status=refusal.status)

        day = values.get("date") or business_date_for(company)
        rows, counts = services.team_attendance(
            company, branch_ids, day, state_id=values.get("state_id"), branch_id=values.get("branch_id"),
            status=values["status"], search=values.get("search", ""),
        )
        page = Paginator(rows, services.TEAM_PAGE_SIZE).get_page(values["page"])
        zone = zone_for(company)
        devices = _device_numbers(page.object_list)
        message = "No stations are assigned to you." if branch_ids == set() else "Team attendance."
        return envelope("ok", message, {
            "date": day.isoformat(), "scope": scope,
            "branches": [
                {"branch_id": b.pk, "code": b.short_code, "name": b.name,
                 "state_id": b.location.state_id, "state": b.location.state.name}
                for b in services.team_branches(company, branch_ids)
            ],
            "counts": counts,
            "rows": [_team_row_json(row, zone, devices) for row in page.object_list],
            "page": page.number, "pages": page.paginator.num_pages, "total": page.paginator.count,
        })


def _device_numbers(rows):
    """installation id -> device registration number, for one page."""
    installs = {row["punch_in"].rms_installation_id for row in rows if row["punch_in"] is not None}
    return dict(Device.objects.filter(installation_id__in=installs)
                .values_list("installation_id", "device_registration_id"))


def _station_json(branch):
    return {"branch_id": branch.pk, "code": branch.short_code, "name": branch.name} if branch else None


def _emirate_json(branch):
    if branch is None:
        return None
    return {"state_id": branch.location.state_id, "name": branch.location.state.name}


def _team_row_json(row, zone, devices):
    employee, punch_in, punch_out, roster = row["employee"], row["punch_in"], row["punch_out"], row["roster"]
    person = {"employee_code": employee.employee_code, "name": employee.full_name,
              "designation": employee.designation.title}
    if punch_in is None:
        branch = roster.branch
        return {
            "row_type": "not_checked_in", "attendance_id": None, "status": row["status"],
            "missing_check_out": False, "employee": person, "source": None, "station": None,
            "emirate": _emirate_json(branch), "check_in": None, "check_out": None, "duration_minutes": None,
            "device_no": None,
            "roster": {
                "station": _station_json(branch),
                "shift1_start": services._hhmm(roster.shift1_start, zone),
                "shift1_end": services._hhmm(roster.shift1_end, zone),
                "shift2_start": services._hhmm(roster.shift2_start, zone),
                "shift2_end": services._hhmm(roster.shift2_end, zone),
            },
        }
    station = punch_in.rms_branch if punch_in.rms_branch_id else None
    named = punch_in.employee_branch if punch_in.employee_branch_id else None
    return {
        "row_type": "session", "attendance_id": str(punch_in.pk), "status": row["status"],
        "missing_check_out": row["missing_check_out"], "employee": person, "source": punch_in.source,
        "station": _station_json(station), "emirate": _emirate_json(station or named),
        "check_in": _local(punch_in.rms_scan_time, zone),
        "check_out": _local(punch_out.rms_scan_time, zone) if punch_out else None,
        "duration_minutes": row["worked_minutes"],
        "device_no": devices.get(punch_in.rms_installation_id), "roster": None,
    }
