"""The device API the phone apps call: /api/v1/{app}/...

Views here only translate HTTP to a services.py call and back. The rules are in
services.py, where login step 0 and the App Releases screen use them too.
"""

import logging

from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.throttling import AnonRateThrottle, SimpleRateThrottle
from rest_framework.views import APIView

from apps.company import services as company_services
from apps.company.models import BranchWorkingTime, WeekDay
from apps.devices import services
from apps.devices.serializers import (
    DeviceBranchRequest,
    DeviceSettingsRequest,
    RegistrationRequest,
    UpdateCheckRequest,
)
from apps.portal.authentication import AppJWTAuthentication
from core.api import PublicAPIView, envelope, request_parts, session_station
from core.enums import Channel
from core.schema import RATE_LIMITED, SERVER_ERROR, envelope_request, envelope_responses
from core.timezones import business_date_for, zone_for

log = logging.getLogger(__name__)

_UPDATE_CHECK_DATA = {
    "update_required": True, "is_mandatory": True, "version_code": 81,
    "version_name": "1.12.81", "update_note": "Fixes receipt printing",
    "download_url": "https://files.byky.ae/apk/operator-1.12.81.apk",
}

HELD_BACK = {
    services.OUTSIDE_WORKING_HOURS: "Update available during branch working hours.",
    services.WORKING_TIME_NOT_SET: "Branch working time not set.",
}


class UpdateCheckThrottle(AnonRateThrottle):
    """Per client IP -- the call has no user and no token."""

    scope = "update_check"


class UpdateCheckView(PublicAPIView):
    """POST /api/v1/{app}/app/update-check -- design/03-login.md section 9A.

    The first call every app makes, before registration and login. A pure read:
    it changes nothing on the device. It fails open -- no readable version, or
    any error in the lookup, answers "no update", because a version check must
    never be what stops a station. Login step 0 runs the same rule again.
    """

    throttle_classes = [UpdateCheckThrottle]

    @extend_schema(
        tags=["App Update"],
        summary="Is there a newer build for this tablet?",
        description=(
            "The first call the app makes on every cold start, before "
            "registration and before the login screen. Public, read-only -- "
            "calling it any number of times changes nothing. "
            "design/registration/update-check-api.md."
        ),
        request=envelope_request("UpdateCheckEnvelope", UpdateCheckRequest),
        responses=envelope_responses(
            (200, "update_required", "Update required.", _UPDATE_CHECK_DATA),
            (200, "update_available", "Update available.",
             {**_UPDATE_CHECK_DATA, "is_mandatory": False}),
            (200, "up_to_date", "No update.", {"update_required": False}),
            (200, "outside_working_hours",
             "Update available during branch working hours.", {"update_required": False}),
            (200, "working_time_not_set", "Branch working time not set.",
             {"update_required": False}),
            (400, "invalid_request", "version_code must be a whole number.",
             {"errors": {"version_code": "must be a whole number"}}),
            RATE_LIMITED, SERVER_ERROR,
        ),
    )
    def post(self, request, app):
        credentials, request_data = request_parts(request)
        form = UpdateCheckRequest(data=request_data)
        form.is_valid(raise_exception=True)
        data = form.validated_data

        version_code = data.get("version_code")
        version_name = data.get("version_name") or ""
        if version_code is None and not version_name.strip():
            # Section 9A.7 row 12. Logged, so an app that forgets its version
            # gets noticed without a station ever being blocked by it.
            log.warning(
                "update check: no version sent (app %s, installation %s)",
                app, data["installation_id"],
            )
            return up_to_date()

        decision = services.update_decision_for_installation(
            installation_id=data["installation_id"], channel=app,
            version_code=version_code, version_name=version_name,
            company_code=data.get("company_id") or credentials.get("company_id") or "",
        )
        reason = decision.pop("reason", None)
        if reason in HELD_BACK:
            # An "Any time" release exists but the branch is not open, or has
            # no hours set (section 9A.3a). A normal answer, not an error.
            return envelope(reason, HELD_BACK[reason], decision)
        if not decision.get("update_required"):
            return up_to_date()
        if decision["is_mandatory"]:
            return envelope("update_required", "Update required.", decision)
        return envelope("update_available", "Update available.", decision)


def up_to_date():
    return envelope("up_to_date", "No update.", dict(services.NO_UPDATE))


# -- Registration -------------------------------------------------------------


class RegistrationIPThrottle(AnonRateThrottle):
    scope = "registration_ip"


class RegistrationInstallationThrottle(SimpleRateThrottle):
    """Per installation_id, so one tablet cannot flood the call from many
    addresses. A body with no readable id is left to the IP limit."""

    scope = "registration_installation"

    def get_cache_key(self, request, view):
        body = request.data if isinstance(request.data, dict) else {}
        data = body.get("request_data")
        ident = data.get("installation_id") if isinstance(data, dict) else None
        if not isinstance(ident, str) or not ident.strip():
            return None
        return self.cache_format % {"scope": self.scope, "ident": ident.strip()[:64]}


# code -> (HTTP status, message). registration-api.md section 7. The schema
# below (RegistrationView.post's @extend_schema) generates its status/message
# rows from this dict rather than retyping them -- only the `data` example per
# outcome, which isn't here, is written out separately.
REGISTRATION_REPLIES = {
    services.APPROVED: (status.HTTP_200_OK, "Device approved."),
    services.PENDING: (status.HTTP_202_ACCEPTED, "Waiting for approval."),
    services.RECONNECT_PENDING: (status.HTTP_202_ACCEPTED, "Waiting for approval after reinstall."),
    services.REINSTALL_REJECTED: (status.HTTP_403_FORBIDDEN,
                                  "This reinstall was refused. Contact your administrator."),
    services.BLOCKED: (status.HTTP_403_FORBIDDEN, "This device is blocked. Contact your administrator."),
    services.RETIRED: (status.HTTP_403_FORBIDDEN, "This registration was replaced. Register again."),
    services.UNAVAILABLE: (status.HTTP_503_SERVICE_UNAVAILABLE,
                           "Registration is not available right now. Try again later."),
    services.UNKNOWN_COMPANY: (status.HTTP_400_BAD_REQUEST,
                               "That company is not set up on this server."),
}

# outcome -> example `data`, for the outcomes that carry one. Anything not
# listed here (a plain refusal) is documented with `{}`, matching
# _registration_data's own "a refusal carries nothing" rule below.
_REGISTRATION_EXAMPLE_DATA = {
    services.APPROVED: {
        "device_registration_id": 1548, "name": "Corniche-POS2",
        "approved_at": "2026-09-18T09:07:00Z",
    },
    services.PENDING: {
        "device_registration_id": 1549, "registered_at": "2026-09-18T09:03:00Z",
    },
    services.RECONNECT_PENDING: {
        "device_registration_id": 1548, "name": "Corniche-POS2",
        "reinstall_requested_at": "2026-10-09T05:10:00Z",
    },
}


def _iso(moment):
    return moment.isoformat().replace("+00:00", "Z") if moment else None


def _registration_data(outcome, device):
    """The payload for each outcome. A refusal carries nothing; no reply ever
    carries a branch -- the station reaches the app only at login."""
    if outcome == services.APPROVED:
        return {
            "device_registration_id": device.device_registration_id,
            "name": device.name,
            "approved_at": _iso(device.approved_at),
        }
    if outcome == services.PENDING:
        return {
            "device_registration_id": device.device_registration_id,
            "registered_at": _iso(device.created_on),
        }
    if outcome == services.RECONNECT_PENDING:
        # The device's own number and name: a reinstall waits on its own row.
        return {
            "device_registration_id": device.device_registration_id,
            "name": device.name,
            "reinstall_requested_at": _iso(device.pending_since),
        }
    return {}


class RegistrationView(PublicAPIView):
    """POST /api/v1/{app}/device/registration -- design/03-login.md section 9B.

    Registers, reports status and picks up an approval in one idempotent call.
    Public by necessity (a device has no credential before it is known), so it
    is rate-limited by IP and by installation id, and a pending reply reveals
    only a number and a time.
    """

    throttle_classes = [RegistrationIPThrottle, RegistrationInstallationThrottle]

    @extend_schema(
        tags=["Device Registration"],
        summary="Register, check status, and pick up an approval -- one idempotent call",
        description=(
            "Called before the login screen on first launch, and again while "
            "waiting for an admin to approve. No token: a device has no "
            "credential before it is known. design/registration/registration-api.md."
        ),
        request=envelope_request("RegistrationEnvelope", RegistrationRequest),
        responses=envelope_responses(
            # One row per REGISTRATION_REPLIES entry -- status and message
            # come from that dict, not retyped here.
            *(
                (http_status, outcome, message, _REGISTRATION_EXAMPLE_DATA.get(outcome, {}))
                for outcome, (http_status, message) in REGISTRATION_REPLIES.items()
            ),
            (400, "invalid_request", "installation_id is required.",
             {"errors": {"installation_id": "is required"}}),
            RATE_LIMITED, SERVER_ERROR,
        ),
    )
    def post(self, request, app):
        credentials, request_data = request_parts(request)
        form = RegistrationRequest(data=request_data)
        form.is_valid(raise_exception=True)
        data = form.validated_data

        outcome, device = services.register_device(
            installation_id=data["installation_id"],
            channel=app,
            platform=data["platform"],
            platform_id=data.get("platform_id") or "",
            device_model=data.get("device_model") or "",
            push_token=credentials.get("device_notification_id"),
            company_code=data.get("company_id") or credentials.get("company_id") or "",
        )
        http_status, message = REGISTRATION_REPLIES[outcome]
        return envelope(outcome, message, _registration_data(outcome, device), http_status)


_SETTINGS_SAMPLE = {
    "device_settings": {
        "settings_code": "S01", "station": "Dubai Parks",
        "logo_changed_at": "2026-09-20T09:12:00+04:00", "logo": "",
    },
    "order_no_prefix": "DUBPP",
    "last_order_no": "DUBPP60182000334",
    "next_order_number": 335,
    "next_test_number": 6,
}

_SETTINGS_DESCRIPTION = """
This tablet's station settings and receipt numbering -- the same block the
operator login returns, on its own, so the app can refresh it without signing
in again (on opening, after its offline queue has uploaded, or from a refresh
button).

**Receipt numbering:** build every receipt number from the top-level
`order_no_prefix` -- this tablet's own, kept even if the station's prefix is
changed later. `last_order_no` is the last receipt number the server has
seen from this tablet at this station (`null` before its first);
`next_order_number` is the one after it. Bookings still waiting in the
tablet's queue have not moved it yet, so the tablet carries on from whichever
is higher -- its own number or the server's.

**`since`** (optional, `YYYY-MM-DD HH:MM:SS`, company time) -- when the tablet
last synced. `device_settings.logo` is `""` when the logo has not changed since
then; leave `since` out to always get it.

The station and tablet are the signed-in session's own, never sent.
"""


class DeviceSettingsView(APIView):
    """POST /api/v1/{app}/device/settings -- operator app only."""

    authentication_classes = [AppJWTAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["Operator Device"],
        summary="This tablet's station settings and receipt numbering",
        description=_SETTINGS_DESCRIPTION,
        request=envelope_request("DeviceSettingsEnvelope", DeviceSettingsRequest, request_data_required=False),
        responses=envelope_responses(
            (200, "ok", "Device settings.", _SETTINGS_SAMPLE),
            (400, "invalid_request", "since must be a date-time like 2026-09-30 17:05:00.",
             {"errors": {"since": "must be a date-time like 2026-09-30 17:05:00"}}),
            (401, "not_authenticated", "Sign in first.", {}),
            (403, "wrong_channel", "Not allowed on this app.", {}),
            (409, "device_not_mapped", "This device has no station.", {}),
            (409, "branch_inactive", "This station is closed.", {}),
            (409, "device_settings_not_done", "This station's receipt settings are not set up yet.", {}),
            SERVER_ERROR,
        ),
    )
    def post(self, request, app):
        if app != Channel.OPERATOR:
            return envelope("wrong_channel", "Not allowed on this app.", http_status=403)
        branch, refused = session_station(request)
        if refused:
            return refused

        _, request_data = request_parts(request)
        form = DeviceSettingsRequest(data=request_data)
        form.is_valid(raise_exception=True)
        since = form.validated_data.get("since")
        if since is not None:
            since = timezone.make_aware(since, zone_for(branch.company))

        data = services.operator_settings(request.auth.device, branch, since=since)
        if data is None:
            return envelope("device_settings_not_done", "This station's receipt settings are not set up yet.",
                            http_status=409)
        return envelope("ok", "Device settings.", data)


# -- The operator app's station, before login ------------------------------------

DEVICE_BRANCH_REPLIES = {
    services.STATION: (status.HTTP_200_OK, "Device station."),
    services.NOT_MAPPED: (status.HTTP_409_CONFLICT, "This device has no station yet."),
    services.WAITING_APPROVAL: (status.HTTP_202_ACCEPTED, "Waiting for approval."),
    services.WAITING_REINSTALL: (status.HTTP_202_ACCEPTED, "Waiting for approval after reinstall."),
    services.BLOCKED: (status.HTTP_403_FORBIDDEN, "This device is blocked. Contact your administrator."),
    services.RETIRED: (status.HTTP_403_FORBIDDEN, "This registration was replaced. Register again."),
    services.REINSTALL_REJECTED: (status.HTTP_403_FORBIDDEN,
                                  "This reinstall was refused. Contact your administrator."),
    services.NOT_REGISTERED: (status.HTTP_409_CONFLICT, "This device is not registered."),
}

_DEVICE_SAMPLE = {"device_registration_id": 1018, "name": "Corniche-POS1", "status": "approved"}
_STATION_SAMPLE = {
    "device": _DEVICE_SAMPLE,
    "branch": {"branch_id": 3, "branch_code": "ADC1", "name": "Abu Dhabi Corniche 1", "location": "Corniche",
               "state": "Abu Dhabi", "is_active": True, "mapped_since": "2026-10-09 10:15:00"},
    "working_time": {
        "date": "2026-10-09", "week_day": 3, "week_day_name": "Thursday", "is_set": True,
        "shifts": [{"shift_number": 1, "start": "08:00", "end": "13:00"},
                   {"shift_number": 2, "start": "16:00", "end": "23:59"}],
        "open_now": "open",
    },
}

_DEVICE_BRANCH_DESCRIPTION = """
Which station this tablet is mapped to -- **before login**, e.g. to show
"Abu Dhabi Corniche 1 · Corniche-POS1" on the login screen. Call it after
registration answers `approved`.

**Request:** `installation_id` -- the same one sent to `device/registration`
(after a reinstall, the new one). `device_registration_id` is optional; when
sent it must be this tablet's, else `device_not_registered`. No token.

`date` is optional (`YYYY-MM-DD`, company time, default **today**): the day
whose working time is sent.

**Told here:** the station's identity -- `branch_id`, code, name, location,
state, whether it is active, since when this tablet is mapped to it -- and its
**working time** for `date`: that weekday's `shifts` (1-4, `HH:MM`, company
time; none = closed that day), `is_set` (false when the station has no working
time at all), and `open_now` -- `open` / `closed` / `not_set` at this moment
(today's state, whatever `date` asks). Settings, receipt numbering and the
rest still come at login. `branch.is_active: false` means the station is
closed (login will refuse).

Operator app only; rate-limited like registration.
"""


def _working_time(branch, day):
    """The station's shifts on `day`, and whether it is open right now."""
    rows = list(BranchWorkingTime.objects.filter(branch=branch, is_active=True)
                .values_list("week_day", "shift_number", "start_time", "end_time"))
    week_day = company_services.week_day_of(day)
    return {
        "date": day.isoformat(), "week_day": week_day, "week_day_name": WeekDay(week_day).label,
        "is_set": bool(rows),
        "shifts": [{"shift_number": n, "start": start.strftime("%H:%M"), "end": end.strftime("%H:%M")}
                   for wd, n, start, end in sorted(rows, key=lambda r: r[1]) if wd == week_day],
        "open_now": company_services.branch_open_state(branch),
    }


def _device_branch_data(outcome, device, mapping, zone, day=None):
    if outcome == services.STATION:
        branch = mapping.branch
        return {
            "device": {"device_registration_id": device.device_registration_id, "name": device.name,
                       "status": device.status},
            "branch": {
                "branch_id": branch.pk, "branch_code": branch.short_code, "name": branch.name,
                "location": branch.location.name, "state": branch.location.state.name,
                "is_active": branch.is_active,
                "mapped_since": timezone.localtime(mapping.from_date, zone).strftime("%Y-%m-%d %H:%M:%S"),
            },
            "working_time": _working_time(branch, day or business_date_for(device.company)),
        }
    if outcome == services.NOT_MAPPED:
        return {"device": {"device_registration_id": device.device_registration_id, "name": device.name,
                           "status": device.status}}
    if outcome in (services.WAITING_APPROVAL, services.WAITING_REINSTALL):
        return {"device_registration_id": device.device_registration_id}
    return {}


class DeviceBranchView(PublicAPIView):
    """POST /api/v1/{app}/device/branch -- operator app only, before login."""

    throttle_classes = [RegistrationIPThrottle, RegistrationInstallationThrottle]

    @extend_schema(
        tags=["Device Registration"],
        summary="This tablet's station, before login",
        description=_DEVICE_BRANCH_DESCRIPTION,
        request=envelope_request("DeviceBranchEnvelope", DeviceBranchRequest),
        responses=envelope_responses(
            (200, "ok", "Device station.", _STATION_SAMPLE),
            (409, "device_not_mapped", "This device has no station yet.", {"device": _DEVICE_SAMPLE}),
            (202, "device_pending_approval", "Waiting for approval.", {"device_registration_id": 1018}),
            (202, "device_reconnect_pending", "Waiting for approval after reinstall.",
             {"device_registration_id": 1018}),
            (403, "device_blocked", "This device is blocked. Contact your administrator.", {}),
            (403, "device_retired", "This registration was replaced. Register again.", {}),
            (403, "reinstall_rejected", "This reinstall was refused. Contact your administrator.", {}),
            (409, "device_not_registered", "This device is not registered.", {}),
            (400, "invalid_request", "installation_id is required.", {"errors": {"installation_id": "is required"}}),
            (403, "wrong_channel", "Not allowed on this app.", {}),
            RATE_LIMITED, SERVER_ERROR,
        ),
    )
    def post(self, request, app):
        if app != Channel.OPERATOR:
            return envelope("wrong_channel", "Not allowed on this app.", http_status=403)
        _, request_data = request_parts(request)
        form = DeviceBranchRequest(data=request_data)
        form.is_valid(raise_exception=True)
        data = form.validated_data
        outcome, device, mapping = services.device_branch(data["installation_id"],
                                                          data.get("device_registration_id"))
        http_status, message = DEVICE_BRANCH_REPLIES[outcome]
        zone = zone_for(device.company) if device is not None else None
        return envelope(outcome, message, _device_branch_data(outcome, device, mapping, zone, data.get("date")),
                        http_status)
