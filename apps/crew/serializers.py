"""Type checks for the attendance APIs' `request_data`. Types and field
presence only -- lookups and the punch in/out rules are
apps/crew/services.py::mark_attendance.

One serializer per source, because the source decides which fields exist:
the operator app scans a QR code (both sides), the manager app marks its own
user (no QR code, no scanning person).
"""

from rest_framework import serializers

from apps.crew.models import PunchType
from apps.crew.services import HISTORY_MAX_DAYS
from core.api import REQUIRED, coordinate_field, date_field, datetime_field, text_field, uuid7_field

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
    """Manager app: the person marking is the person marked."""

    sync_id = uuid7_field()
    punch_type = serializers.ChoiceField(choices=PunchType.choices, error_messages=PUNCH_TYPE)

    employee_code = text_field(max_length=20)
    employee_name = text_field(max_length=150)
    employee_branch_code = text_field(max_length=10)

    rms_installation_id = text_field(max_length=64)
    rms_scan_time = datetime_field()
    rms_latitude = coordinate_field(limit=90, required=False, allow_null=True)
    rms_longitude = coordinate_field(limit=180, required=False, allow_null=True)

    def validate(self, values):
        return _pairs(values, "rms")


class QrAttendanceRequest(SelfAttendanceRequest):
    """Operator (RMS) app: the employee side is what the QR code carried."""

    employee_installation_id = text_field(max_length=64)
    qr_generation_time = datetime_field()
    employee_latitude = coordinate_field(limit=90, required=False, allow_null=True)
    employee_longitude = coordinate_field(limit=180, required=False, allow_null=True)

    rms_employee_code = text_field(max_length=20)
    rms_employee_name = text_field(max_length=150)
    rms_branch_code = text_field(max_length=10)

    def validate(self, values):
        return _pairs(values, "employee", "rms")


class AttendanceHistoryRequest(serializers.Serializer):
    """One employee; one day (`date`), a range (`from_date` + `to_date`), or
    neither for today."""

    employee_code = text_field(max_length=20)
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
