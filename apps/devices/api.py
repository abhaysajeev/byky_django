"""The device API the phone apps call: /api/v1/{app}/...

Views here only translate HTTP to a services.py call and back. The rules are in
services.py, where login step 0 and the App Releases screen use them too.
"""

import logging

from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.throttling import AnonRateThrottle, SimpleRateThrottle

from apps.devices import services
from apps.devices.serializers import RegistrationRequest, UpdateCheckRequest
from core.api import PublicAPIView, envelope, request_parts
from core.schema import RATE_LIMITED, SERVER_ERROR, envelope_request, envelope_responses

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
    services.BLOCKED: (status.HTTP_403_FORBIDDEN, "This device is blocked. Contact your administrator."),
    services.RETIRED: (status.HTTP_403_FORBIDDEN, "This registration was replaced. Register again."),
    services.APP_MISMATCH: (status.HTTP_409_CONFLICT, "This device is registered for another app."),
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
        "device_registration_id": 1551,
        "matched_device": {
            "device_registration_id": 1548, "name": "Corniche-POS2",
            "last_seen_at": "2026-08-03T11:24:00Z",
        },
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
        matched = device.reconnect_of
        return {
            "device_registration_id": device.device_registration_id,
            "matched_device": {
                "device_registration_id": matched.device_registration_id,
                "name": matched.name,
                "last_seen_at": _iso(matched.last_seen_at),
            },
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
