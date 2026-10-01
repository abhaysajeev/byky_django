"""The attendance APIs -- views only translate HTTP; the rules are in
apps/crew/services.py (mark_attendance, attendance_history).

POST /api/v1/{app}/attendance/mark     one punch, in or out
POST /api/v1/{app}/attendance/history  an employee's punches for a day or range

`{app}` decides the source: `operator` (the RMS app) scans an employee's QR
code, `manager` marks its own user. The employee app only shows the QR code
and has no attendance endpoint.
"""

from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.crew import services
from apps.crew.models import AttendanceSource, PunchType
from apps.crew.serializers import (
    AttendanceHistoryRequest,
    QrAttendanceRequest,
    SelfAttendanceRequest,
)
from apps.portal.authentication import AppJWTAuthentication
from core.api import envelope, request_parts, session_station
from core.enums import Channel
from core.schema import SERVER_ERROR, envelope_request, envelope_responses
from core.timezones import business_date_for, zone_for

# {app} -> (source, request serializer)
_SOURCES = {
    Channel.OPERATOR: (AttendanceSource.QR_SCAN, QrAttendanceRequest),
    Channel.MANAGER: (AttendanceSource.SELF, SelfAttendanceRequest),
}

# History is read by every app; only the operator and manager apps mark.
_HISTORY_APPS = (Channel.OPERATOR, Channel.MANAGER, Channel.EMPLOYEE)

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def _local(moment, zone):
    return moment.astimezone(zone).strftime(TIME_FORMAT) if moment else None


def _station_or_refusal(request, app):
    """The operator app is a station till: its device must still be mapped
    to an open branch. The manager's phone has no station."""
    if app == Channel.OPERATOR:
        _, refused = session_station(request)
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
* `manager`: the manager's own **Mark attendance** button. Send only the
  employee side (the manager), `rms_installation_id`, `rms_scan_time` (the
  button press) and optionally `rms_latitude/longitude`.

**sync_id** -- a **UUIDv7** made on the device for this punch and resent
**unchanged** on every retry. It becomes the record's id; a second call with
the same sync_id is answered `duplicate` with the stored punch and writes
nothing, so an offline queue can resend safely.

**Times** -- `YYYY-MM-DD HH:MM:SS` in company time, no offset (the app's clock
is set from server/time).

**Pairing** -- a punch_out closes the employee's open punch_in, even across
midnight. A punch_in stays open for 24 hours; after that it is a missing
punch-out and no longer blocks a new punch_in.

| Refusal | When |
|---|---|
| `punch_in_open` | already punched in and not out |
| `no_open_punch_in` | nothing to close -- if the punch_in is still queued offline, keep this punch_out queued and retry after it |
| `punch_out_before_punch_in` | the punch_out's time is before its punch_in |
| `qr_already_scanned` | this QR code was already used for this punch type |
"""


class AttendanceMarkView(APIView):
    """POST /api/v1/{app}/attendance/mark -- operator and manager apps."""

    authentication_classes = [AppJWTAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["Operator Attendance", "Manager Attendance"],
        summary="Record a punch in or punch out",
        description=_MARK_DESCRIPTION,
        request=envelope_request("AttendanceMarkEnvelope", QrAttendanceRequest),
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
            (409, "punch_in_open", "Already punched in. Punch out first.", {}),
            (409, "no_open_punch_in", "No punch in to close. Punch in first.", {}),
            (409, "punch_out_before_punch_in", "Punch out is earlier than the punch in.", {}),
            (409, "qr_already_scanned", "That QR code has already been scanned.", {}),
            (409, "sync_id_conflict", "That sync_id is already used.", {}),
            (409, "device_not_mapped", "This device has no station.", {}, "device_not_mapped (operator)"),
            (409, "branch_inactive", "This station is closed.", {}, "branch_inactive (operator)"),
            SERVER_ERROR,
        ),
    )
    def post(self, request, app):
        if app not in _SOURCES:
            return envelope("wrong_channel", "Not allowed on this app.", http_status=403)
        refused = _station_or_refusal(request, app)
        if refused:
            return refused

        source, request_class = _SOURCES[app]
        _, request_data = request_parts(request)
        form = request_class(data=request_data)
        form.is_valid(raise_exception=True)

        company = request.user.employee.company
        zone = zone_for(company)
        values = dict(form.validated_data)
        for key in ("rms_scan_time", "qr_generation_time"):
            if values.get(key) is not None:
                values[key] = timezone.make_aware(values[key], zone)

        try:
            row, created = services.mark_attendance(request.user, source, values)
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
        refused = _station_or_refusal(request, app)
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
        "employee_branch_code": row.employee_branch.short_code,
        "rms_branch_code": row.rms_branch.short_code if row.rms_branch_id else None,
        "rms_employee_code": row.rms_employee_code or None,
        "rms_employee_name": row.rms_employee_name or None,
    }
