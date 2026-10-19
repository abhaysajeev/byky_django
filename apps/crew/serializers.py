"""Type checks for the attendance APIs' `request_data`. Types and field
presence only -- lookups and the punch in/out rules are
apps/crew/services.py::mark_attendance.

One serializer per source, because the source decides which fields exist:
the operator app scans a QR code (both sides), the manager and employee apps
mark their own user (no QR code, no scanning person).
"""

from rest_framework import serializers

from apps.crew.models import LeaveStatus, PunchType, RosterCategory
from apps.crew.services import HISTORY_MAX_DAYS, TEAM_MAX_DAYS, TeamStatus
from core.api import (
    REQUIRED,
    coordinate_field,
    date_field,
    datetime_field,
    text_field,
    uuid7_field,
    whole_number_field,
)

PUNCH_TYPE = {**REQUIRED, "invalid_choice": "must be punch_in or punch_out"}


def _pairs(values, *prefixes):
    """A latitude without its longitude (or the reverse) is half a position."""
    errors = {}
    for prefix in prefixes:
        lat, lng = f"{prefix}_latitude", f"{prefix}_longitude"
        if (values.get(lat) is None) != (values.get(lng) is None):
            missing = lng if values.get(lng) is None else lat
            errors[missing] = "is required with the other coordinate"
    if errors:
        raise serializers.ValidationError(errors)
    return values


class SelfAttendanceRequest(serializers.Serializer):
    """Manager app, and employee app for staff whose attendance method is
    Self: the signed-in user's own punch. Who and which phone come from the
    session; it has no station, so the branch is optional (the user's home
    branch, if any, when not sent)."""

    sync_id = uuid7_field()
    punch_type = serializers.ChoiceField(choices=PunchType.choices, error_messages=PUNCH_TYPE)

    # Optional: if sent it must be the signed-in manager's own code.
    employee_code = text_field(max_length=20, required=False, allow_blank=True)
    # Optional and ignored: the server records the name from the employee
    # master, so a punch's name always matches the employee it is linked to.
    employee_name = text_field(max_length=150, required=False, allow_blank=True)
    employee_branch_code = text_field(max_length=10, required=False, allow_blank=True)

    # Optional: the signed-in phone's installation id when not sent.
    rms_installation_id = text_field(max_length=64, required=False, allow_blank=True)
    rms_scan_time = datetime_field()
    rms_latitude = coordinate_field(limit=90, required=False, allow_null=True)
    rms_longitude = coordinate_field(limit=180, required=False, allow_null=True)

    def validate(self, values):
        return _pairs(values, "rms")


class QrAttendanceRequest(SelfAttendanceRequest):
    """Operator (RMS) app: the employee side is what the QR code carried."""

    # Required here, unlike the manager's own punch.
    employee_code = text_field(max_length=20)
    employee_branch_code = text_field(max_length=10)
    rms_installation_id = text_field(max_length=64)

    employee_installation_id = text_field(max_length=64)
    qr_generation_time = datetime_field()
    employee_latitude = coordinate_field(limit=90, required=False, allow_null=True)
    employee_longitude = coordinate_field(limit=180, required=False, allow_null=True)

    rms_employee_code = text_field(max_length=20)
    rms_employee_name = text_field(max_length=150, required=False, allow_blank=True)    # ignored, as above
    rms_branch_code = text_field(max_length=10)

    def validate(self, values):
        return _pairs(values, "employee", "rms")


class AttendanceHistoryRequest(serializers.Serializer):
    """One employee -- the caller by default; one day (`date`), a range
    (`from_date` + `to_date`), or neither for today."""

    employee_code = text_field(max_length=20, required=False, allow_blank=True)
    date = date_field(required=False)
    from_date = date_field(required=False)
    to_date = date_field(required=False)

    def validate(self, values):
        has_range = "from_date" in values or "to_date" in values
        if "date" in values and has_range:
            raise serializers.ValidationError({"date": "cannot be sent with from_date or to_date"})
        if has_range:
            if "from_date" not in values:
                raise serializers.ValidationError({"from_date": "is required with to_date"})
            if "to_date" not in values:
                raise serializers.ValidationError({"to_date": "is required with from_date"})
            span = (values["to_date"] - values["from_date"]).days
            if span < 0:
                raise serializers.ValidationError({"to_date": "must be on or after from_date"})
            if span >= HISTORY_MAX_DAYS:
                raise serializers.ValidationError({"to_date": f"must be within {HISTORY_MAX_DAYS} days of from_date"})
        return values


# -- Leave (apps/crew/leave.py) ---------------------------------------------------------

LEAVE_STATUS_FILTER = ["all", *LeaveStatus.values]
LEAVE_STATUS_MESSAGE = {"invalid_choice": "must be all, pending, approved, rejected or cancelled"}


def _leave_status():
    return serializers.ChoiceField(choices=LEAVE_STATUS_FILTER, required=False, default="all",
                                   error_messages=LEAVE_STATUS_MESSAGE)


def _range(values):
    if values.get("from_date") and values.get("to_date") and values["to_date"] < values["from_date"]:
        raise serializers.ValidationError({"to_date": "must be on or after from_date"})
    return values


class LeaveSubmitRequest(serializers.Serializer):
    """Employee app: one request for whole days, today or later."""

    sync_id = uuid7_field()
    leave_type_id = whole_number_field(min_value=1)
    from_date = date_field()
    to_date = date_field()
    reason = text_field(max_length=500, required=False, allow_blank=True)
    # When the employee pressed submit on the phone (company time) -- a request
    # made offline keeps its own time. Left out: the time it reaches us.
    applied_at = datetime_field(required=False, allow_null=True)

    def validate(self, values):
        return _range(values)


class LeaveTypesRequest(serializers.Serializer):
    """No input: the company's active leave types."""


class LeaveIdRequest(serializers.Serializer):
    leave_request_id = serializers.UUIDField(error_messages={**REQUIRED, "invalid": "must be a UUID"})


class LeaveDecisionRequest(LeaveIdRequest):
    remarks = text_field(max_length=500, required=False, allow_blank=True)


class MyLeaveListRequest(serializers.Serializer):
    """Employee app: the signed-in employee's own requests."""

    status = _leave_status()
    from_date = date_field(required=False)
    to_date = date_field(required=False)
    page = whole_number_field(min_value=1, required=False, default=1)

    def validate(self, values):
        return _range(values)


class LeaveListRequest(MyLeaveListRequest):
    """Manager app and employee-app approvers: the requests they decide."""

    state_id = whole_number_field(min_value=1, required=False, allow_null=True)
    branch_id = whole_number_field(min_value=1, required=False, allow_null=True)
    search = text_field(max_length=100, required=False, allow_blank=True)


class TeamAttendanceRequest(serializers.Serializer):
    """Team Attendance -- one day (`date`, today when nothing is sent) or a
    range (`from_date` + `to_date`, at most TEAM_MAX_DAYS days); optionally
    one emirate, one station, one status tab, a name or code search, a page."""

    date = date_field(required=False)
    from_date = date_field(required=False)
    to_date = date_field(required=False)
    state_id = whole_number_field(min_value=1, required=False, allow_null=True)
    branch_id = whole_number_field(min_value=1, required=False, allow_null=True)
    status = serializers.ChoiceField(
        choices=TeamStatus.CHOICES, required=False, default=TeamStatus.ALL,
        error_messages={"invalid_choice": "must be all, checked_in, checked_out or not_checked_in"},
    )
    search = text_field(max_length=100, required=False, allow_blank=True)
    page = whole_number_field(min_value=1, required=False, default=1)

    def validate(self, values):
        has_range = "from_date" in values or "to_date" in values
        if "date" in values and has_range:
            raise serializers.ValidationError({"date": "cannot be sent with from_date or to_date"})
        if has_range:
            if "from_date" not in values:
                raise serializers.ValidationError({"from_date": "is required with to_date"})
            if "to_date" not in values:
                raise serializers.ValidationError({"to_date": "is required with from_date"})
            span = (values["to_date"] - values["from_date"]).days
            if span < 0:
                raise serializers.ValidationError({"to_date": "must be on or after from_date"})
            if span >= TEAM_MAX_DAYS:
                raise serializers.ValidationError({"to_date": f"must be within {TEAM_MAX_DAYS} days of from_date"})
        return values


# -- Duty roster (apps/crew/duty.py) ---------------------------------------------------

ROSTER_MAX_DAYS = 31


class MyRosterRequest(serializers.Serializer):
    """Employee app: the signed-in employee's own days. Both dates or neither
    (today and the next six days)."""

    from_date = date_field(required=False)
    to_date = date_field(required=False)

    def validate(self, values):
        if ("from_date" in values) != ("to_date" in values):
            missing, other = ("to_date", "from_date") if "from_date" in values else ("from_date", "to_date")
            raise serializers.ValidationError({missing: f"is required with {other}"})
        if "from_date" in values:
            span = (values["to_date"] - values["from_date"]).days
            if span < 0:
                raise serializers.ValidationError({"to_date": "must be on or after from_date"})
            if span >= ROSTER_MAX_DAYS:
                raise serializers.ValidationError({"to_date": f"must be within {ROSTER_MAX_DAYS} days of from_date"})
        return values


class TeamRosterRequest(MyRosterRequest):
    """Manager app and employee-app supervisors: the team's days. No dates:
    the current roster week."""

    state_id = whole_number_field(min_value=1, required=False, allow_null=True)
    branch_id = whole_number_field(min_value=1, required=False, allow_null=True)
    category = serializers.ChoiceField(
        choices=RosterCategory.values, required=False, allow_blank=True,
        error_messages={"invalid_choice": "must be cashier or labour"},
    )
    search = text_field(max_length=100, required=False, allow_blank=True)
    page = whole_number_field(min_value=1, required=False, default=1)
