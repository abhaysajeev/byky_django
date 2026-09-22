"""The app-auth API: /api/v1/{app}/auth/... -- design/03-login.md section 9.1.

`employee` and `operator` are wired up here; `manager` is not built yet.
Both channels now run device steps 7-8 (registered, approved, not
blocked/retired) -- decided with the client 22 Sep 2026, reversing the
21 Sep 2026 decision that Employee skipped all device steps. Employee still
does not run step 9 (branch mapping) or the device_settings check: the app
runs on staff's own phones, not a station till, and "which branch" comes
from the duty roster, never a device mapping. Operator runs the full set --
it's the station till, so "which branch" comes from the device's own
mapping, not a roster.

Views only translate HTTP to apps/portal/auth.py + apps/portal/jwt.py +
apps/crew/services.py/apps/devices/services.py and back, the same shape
apps/devices/api.py uses.
"""

from django.db import transaction
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.throttling import AnonRateThrottle, SimpleRateThrottle
from rest_framework.views import APIView

from apps.company.models import Company
from apps.crew import services as crew_services
from apps.devices import services as devices_services
from apps.devices.models import BillKind
from apps.portal import auth
from apps.portal import jwt as app_jwt
from apps.portal.authentication import AppJWTAuthentication
from apps.portal.serializers import LoginRequest, RefreshRequest, ServerTimeRequest
from apps.portal.session_models import AppSession, LogoutReason
from core.api import PublicAPIView, envelope, request_parts
from core.enums import Channel
from core.schema import RATE_LIMITED, SERVER_ERROR, envelope_request, envelope_responses
from core.timezones import utc_offset_string, zone_for


class LoginThrottle(AnonRateThrottle):
    """Per client IP -- the call has no user or token yet to key on."""

    scope = "app_login"


class RefreshThrottle(AnonRateThrottle):
    """Per client IP. Refresh is meant to be called often by legitimate
    clients (silently, on a timer) -- this only stops a runaway/broken one,
    not real traffic."""

    scope = "app_refresh"


def _issue_tokens(user, session, *, now=None):
    """Issue both tokens and write the refresh half onto the session row --
    every caller that creates or rotates a session needs exactly this."""
    now = now or timezone.now()
    access, access_expires = app_jwt.issue_access_token(user, session, now=now)
    refresh, refresh_expires = app_jwt.issue_refresh_token(user, session, now=now)

    session.refresh_token_hash = app_jwt.hash_token(refresh)
    session.refresh_expires_at = refresh_expires
    session.refresh_rotated_at = now
    # design/03-login.md section 6.3: "app_session.last_seen_at is updated"
    # on a refresh, same as it is on every web page load -- open_session only
    # sets it once, at login.
    session.last_seen_at = now
    session.save(update_fields=[
        "refresh_token_hash", "refresh_expires_at", "refresh_rotated_at",
        "last_seen_at", "modified_on",
    ])

    return {
        "access": access,
        "refresh": refresh,
        "access_expires_at": access_expires.isoformat(),
        "refresh_expires_at": refresh_expires.isoformat(),
    }


def _employee_login(request, form):
    installation_id = (form.validated_data.get("installation_id") or "").strip()
    if not installation_id:
        return envelope(
            "invalid_request", "installation_id is required.",
            {"errors": {"installation_id": "is required"}}, http_status=400,
        )

    try:
        user, session = auth.sign_in_employee(
            form.validated_data["username"], form.validated_data["password"], installation_id,
            ip_address=request.META.get("REMOTE_ADDR"),
            user_agent=request.META.get("HTTP_USER_AGENT", ""),
        )
    except auth.LoginRefused as refused:
        return envelope(refused.code, refused.message, http_status=refused.status)

    employee = user.employee
    data = {
        "tokens": _issue_tokens(user, session),
        "employee": crew_services.employee_profile(employee),
        "roster": crew_services.current_week_roster(employee),
    }
    return envelope("ok", f"Welcome, {employee.full_name}.", data)


def _operator_login(request, form):
    installation_id = (form.validated_data.get("installation_id") or "").strip()
    if not installation_id:
        return envelope(
            "invalid_request", "installation_id is required.",
            {"errors": {"installation_id": "is required"}}, http_status=400,
        )

    try:
        user, session, device, branch = auth.sign_in_operator(
            form.validated_data["username"], form.validated_data["password"], installation_id,
            ip_address=request.META.get("REMOTE_ADDR"),
            user_agent=request.META.get("HTTP_USER_AGENT", ""),
        )
    except auth.LoginRefused as refused:
        return envelope(refused.code, refused.message, http_status=refused.status)

    order_counter = devices_services.counter_for(device, branch, BillKind.ORDER)
    test_counter = devices_services.counter_for(device, branch, BillKind.TEST_RIDE)

    data = {
        "tokens": _issue_tokens(user, session),
        "first_name": user.employee.first_name,
        "branch": {
            "name": branch.name,
            "branch_code": branch.short_code,
            "is_hotel": branch.is_hotel,
            "hotel_commission": str(branch.hotel_commission),
            "accepts_app_payment": branch.accepts_app_payment,
            "is_multi_device": branch.is_multi_device,
            "allows_test_ride": branch.allows_test_ride,
        },
        # settings_payload(branch) cannot be None here -- sign_in_operator
        # already refused device_settings_not_done if it were.
        "device_settings": devices_services.settings_payload(branch),
        "order_no_prefix": order_counter.prefix,
        "next_order_number": order_counter.next_number,
        "next_test_number": test_counter.next_number,
    }
    return envelope("ok", "Logged in.", data)


_LOGIN_BY_CHANNEL = {
    Channel.EMPLOYEE: _employee_login,
    Channel.OPERATOR: _operator_login,
}


_EMPLOYEE_LOGIN_SUCCESS_DATA = {
    "tokens": {
        "access": "eyJhbGciOiJIUzI1NiIs...", "refresh": "eyJhbGciOiJIUzI1NiIs...",
        "access_expires_at": "2026-09-21T02:41:12.488309+00:00",
        "refresh_expires_at": "2026-10-21T02:11:12.488309+00:00",
    },
    "employee": {
        "employee_code": "TEST-001", "full_name": "TEST USER", "first_name": "TEST",
        "middle_name": "", "last_name": "USER", "designation": "Cashier", "role": "Cashier",
        "branch": None, "mobile": "", "email": "", "photo_url": None,
        "company": "BY KY SPORT & LEISURE EQUIPMENT RENTAL & TRADING LLC",
    },
    "roster": {
        "week_start": "2026-09-20", "week_end": "2026-09-26",
        "days": [
            {"date": "2026-09-20", "status": "Working", "branch": "Creek Park 1",
             "shift1_start": "07:00", "shift1_end": "23:00", "shift2_start": None, "shift2_end": None},
            {"date": "2026-09-21", "status": None, "branch": None,
             "shift1_start": None, "shift1_end": None, "shift2_start": None, "shift2_end": None},
        ],
    },
}

_OPERATOR_LOGIN_SUCCESS_DATA = {
    "tokens": {
        "access": "eyJhbGciOiJIUzI1NiIs...", "refresh": "eyJhbGciOiJIUzI1NiIs...",
        "access_expires_at": "2026-09-21T05:46:16.131807+00:00",
        "refresh_expires_at": "2026-10-21T05:16:16.131807+00:00",
    },
    "first_name": "Rashed",
    "branch": {
        "name": "Creek Park 1", "branch_code": "AUH01", "is_hotel": False,
        "hotel_commission": "0.00", "accepts_app_payment": False,
        "is_multi_device": False, "allows_test_ride": True,
    },
    "device_settings": {
        "settings_code": "S01", "station": "Creek Park 1",
        "order_no_prefix": "AUH01", "logo": "iVBORw0KGgoAAAANSUhEUgAA...",
    },
    "order_no_prefix": "AUH01", "next_order_number": 335, "next_test_number": 6,
}


class LoginView(PublicAPIView):
    """POST /api/v1/{app}/auth/login -- password only; no device token yet
    (no channel has "remember device" built in this pass).

    One view, one route, branching on {app} -- design/03-login.md section
    9.1: "One login function, one route per app, so a log line names the
    app without parsing a body." `manager` isn't built yet and falls through
    to wrong_channel like any other unrecognised value.
    """

    throttle_classes = [LoginThrottle]

    @extend_schema(
        tags=["Operator Auth", "Employee Auth"],
        summary="Sign in -- one URL, a different response per {app}",
        description=(
            "`{app}` is `operator` or `employee` (`manager` isn't built yet -- "
            "falls through to `wrong_channel`). Both need `installation_id`: "
            "Employee checks it's registered/approved/not-blocked/not-retired; "
            "Operator additionally requires it be mapped to a branch with "
            "receipt settings configured. See the two success examples below "
            "for the very different response shapes. "
            "design/login/login-for-employee.md, design/login/login-for-operator.md."
        ),
        request=envelope_request("LoginEnvelope", LoginRequest),
        responses=envelope_responses(
            (200, "ok", "Welcome, Rashed K.", _EMPLOYEE_LOGIN_SUCCESS_DATA, "ok (employee)"),
            (200, "ok", "Logged in.", _OPERATOR_LOGIN_SUCCESS_DATA, "ok (operator)"),
            (400, "invalid_request", "installation_id is required.",
             {"errors": {"installation_id": "is required"}}),
            (401, "invalid_credentials", "Wrong username or password.", {}),
            (429, "account_locked", "Too many attempts. Try again in 5 minutes.", {}),
            (403, "company_inactive", "This account is not active.", {}),
            (403, "user_not_approved", "This account is not active.", {}),
            (403, "wrong_channel", "You are not allowed to use this app.", {}),
            (403, "wrong_channel", "Not allowed on this app.", {}, "wrong_channel (unbuilt app)"),
            (403, "user_blocked", "You are blocked. Contact HR.", {}),
            (409, "device_not_registered", "Setting up this device…", {}),
            (202, "device_pending_approval", "Waiting for approval.", {}),
            (403, "device_blocked", "This device is blocked.", {}),
            (403, "device_retired", "This device was replaced.", {}),
            (409, "device_not_mapped", "This device has no station.", {}, "device_not_mapped (operator only)"),
            (409, "device_settings_not_done",
             "This station's receipt settings are not set up yet.", {}),
            (409, "session_active_elsewhere", "Logged in on another device.", {},
             "session_active_elsewhere (operator only)"),
            RATE_LIMITED, SERVER_ERROR,
        ),
    )
    def post(self, request, app):
        handler = _LOGIN_BY_CHANNEL.get(app)
        if handler is None:
            return envelope(
                "wrong_channel", "Not allowed on this app.", http_status=403,
            )

        _, request_data = request_parts(request)
        form = LoginRequest(data=request_data)
        form.is_valid(raise_exception=True)

        return handler(request, form)


class AppRefreshView(PublicAPIView):
    """POST /api/v1/{app}/auth/refresh -- section 6.3. The refresh token
    travels only here, never as an Authorization header."""

    throttle_classes = [RefreshThrottle]

    @extend_schema(
        tags=["Operator Auth", "Employee Auth"],
        summary="Exchange a refresh token for a new pair -- both tokens rotate",
        description=(
            "Same code for every channel; the session row itself carries "
            "which one. Both tokens change on every call, not just the "
            "access token -- the one just spent stops working immediately. "
            "A failed call (network, timeout, 5xx) should never log the app "
            "out on its own -- only an explicit `session_closed` means show "
            "the login screen. login-for-employee.md section 4."
        ),
        request=envelope_request("RefreshEnvelope", RefreshRequest),
        responses=envelope_responses(
            (200, "ok", "Token refreshed.", {"tokens": {
                "access": "...new access token...", "refresh": "...new refresh token...",
                "access_expires_at": "2026-09-22T03:11:12.488309+00:00",
                "refresh_expires_at": "2026-10-22T02:41:12.488309+00:00",
            }}),
            (401, "session_closed", "You were logged out. Sign in again.", {}),
            RATE_LIMITED, SERVER_ERROR,
        ),
    )
    def post(self, request, app):
        _, request_data = request_parts(request)
        form = RefreshRequest(data=request_data)
        form.is_valid(raise_exception=True)
        token = form.validated_data["refresh_token"]

        try:
            payload = app_jwt.decode_token(token, expected_type="refresh")
        except app_jwt.TokenInvalid:
            return envelope("session_closed", "You were logged out. Sign in again.", http_status=401)

        now = timezone.now()
        # select_for_update, same discipline as auth.open_session: two
        # refresh calls racing on the same token (a retried request, a
        # double-tap that fired twice) must not both pass the "is this hash
        # still current" check before either writes the new one -- without
        # the lock, the second write would silently strand the first
        # caller's brand new refresh token, which would then fail the next
        # time it was used even though this call reported success.
        with transaction.atomic():
            session = (
                AppSession.objects
                .select_for_update()
                .filter(pk=payload.get("sid"), user_id=payload.get("sub"), logged_out_at__isnull=True)
                .select_related("user")
                .first()
            )
            stale = (
                session is None
                or app_jwt.hash_token(token) != session.refresh_token_hash
                or session.refresh_expires_at is None
                or session.refresh_expires_at <= now
                # Same re-check AppJWTAuthentication makes for the access
                # token -- a deactivated account must not be able to refresh
                # its way to a new, still-working token pair.
                or not session.user.is_active
            )
            if stale:
                return envelope("session_closed", "You were logged out. Sign in again.", http_status=401)

            tokens = _issue_tokens(session.user, session, now=now)

        return envelope("ok", "Token refreshed.", {"tokens": tokens})


class AppLogoutView(APIView):
    """POST /api/v1/{app}/auth/logout -- closes the caller's own session."""

    authentication_classes = [AppJWTAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["Operator Auth", "Employee Auth"],
        summary="Close this session",
        description=(
            "No `request_data` -- the body isn't read at all. Only closes "
            "this one session; does not touch the employee record. Treat a "
            "401 here the same as a successful logout on the app side, "
            "there is nothing left to clean up if the token was already "
            "dead. login-for-employee.md section 5."
        ),
        request=None,
        responses=envelope_responses(
            (200, "ok", "Signed out.", {}),
            (401, "not_authenticated", "Sign in first.", {}),
            SERVER_ERROR,
        ),
    )
    def post(self, request, app):
        auth.close_session(request.auth, LogoutReason.USER_LOGOUT)
        return envelope("ok", "Signed out.")


# -- Server time ---------------------------------------------------------------


class ServerTimeIPThrottle(AnonRateThrottle):
    """Per client IP -- called before login as often as after, no token to
    key on."""

    scope = "server_time_ip"


class ServerTimeInstallationThrottle(SimpleRateThrottle):
    """Per installation_id, same shape as
    apps/devices/api.py:RegistrationInstallationThrottle. A body with no
    readable id is left to the IP limit."""

    scope = "server_time_installation"

    def get_cache_key(self, request, view):
        body = request.data if isinstance(request.data, dict) else {}
        data = body.get("request_data")
        ident = data.get("installation_id") if isinstance(data, dict) else None
        if not isinstance(ident, str) or not ident.strip():
            return None
        return self.cache_format % {"scope": self.scope, "ident": ident.strip()[:64]}


def _server_time_payload(company):
    """design/registration/server-time-api.md section 4. `company` is a resolved,
    matched row -- picking it out of an unvalidated string happens in
    ServerTimeView.post, not here."""
    zone = zone_for(company)
    now = timezone.now()
    return {
        "server_utc_date_time": now.isoformat().replace("+00:00", "Z"),
        "company_timezone": zone.key,
        "utc_offset": utc_offset_string(zone, now),
    }


class ServerTimeView(PublicAPIView):
    """POST /api/v1/{app}/server/time -- design/registration/server-time-api.md.

    The APK's trusted clock: licence and other date-sensitive checks must not
    depend on the device's own clock.

    Several companies share this database (multi-tenant, not one deployment
    per company), so there is no safe single-company guess when `company_id`
    is missing -- unlike registration and the update check, which can fall
    back to "the one active company" for a single-tenant install. Here
    `company_id` is required and validated against `Company.short_code`
    (apps/devices/services.py::company_for_code), same match registration
    uses, and an unmatched code is refused rather than silently answered with
    someone else's timezone.
    """

    throttle_classes = [ServerTimeIPThrottle, ServerTimeInstallationThrottle]

    @extend_schema(
        tags=["Server Time"],
        summary="The APK's trusted clock -- never the device's own",
        description=(
            "`company_id` is required and picks whose timezone comes back -- "
            "several companies share this database, so there is no single "
            "deployment to guess when it's missing (unlike registration and "
            "the update check). design/registration/server-time-api.md."
        ),
        request=envelope_request("ServerTimeEnvelope", ServerTimeRequest),
        responses=envelope_responses(
            (200, "ok", "Server time.", {
                "server_utc_date_time": "2026-09-22T10:00:00Z",
                "company_timezone": "Asia/Dubai", "utc_offset": "+04:00",
            }),
            (400, "invalid_request", "company_id is required.",
             {"errors": {"company_id": "is required"}}),
            (400, "unknown_company", "That company is not set up on this server.", {}),
            RATE_LIMITED, SERVER_ERROR,
        ),
    )
    def post(self, request, app):
        credentials, request_data = request_parts(request)
        company_code = request_data.get("company_id") or credentials.get("company_id") or ""
        if not isinstance(company_code, str):
            company_code = ""
        form = ServerTimeRequest(data={"company_id": company_code})
        form.is_valid(raise_exception=True)

        company_id = devices_services.company_for_code(form.validated_data["company_id"])
        if company_id is None:
            return envelope(
                devices_services.UNKNOWN_COMPANY,
                "That company is not set up on this server.",
                http_status=400,
            )
        company = Company.objects.filter(id=company_id).only("timezone").first()
        return envelope("ok", "Server time.", _server_time_payload(company))
