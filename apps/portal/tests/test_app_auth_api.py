"""POST /api/v1/employee/auth/{login,refresh,logout}, tested through HTTP as
the app sees it. design/03-login.md section 9.1/9.2.
"""

import datetime

import pytest
from django.core.cache import cache
from django.utils import timezone

from apps.company.models import Branch, BranchType, Company, Country, Location, State
from apps.crew.models import Designation, Employee, RosterCategory
from apps.devices.models import Device, DeviceStatus
from apps.portal.models import Role
from apps.portal.services import grant_all
from apps.portal.session_models import AppSession
from core.enums import Channel
from core.models import User

PASSWORD = "Byky#2026"
LOGIN_URL = "/api/v1/employee/auth/login"
REFRESH_URL = "/api/v1/employee/auth/refresh"
LOGOUT_URL = "/api/v1/employee/auth/logout"


@pytest.fixture(autouse=True)
def fresh_throttle():
    """Login/refresh are now throttled (apps/portal/api.py); the counts live
    in the cache, which outlives a test -- same pattern as
    apps/devices/tests/test_api_update_check.py."""
    cache.clear()
    yield
    cache.clear()


def call(client, url, request_data=None, *, headers=None):
    return client.post(
        url, {"credentials": {}, "request_data": request_data or {}},
        content_type="application/json", **(headers or {}),
    )


@pytest.fixture
def world(db):
    country = Country.objects.create(short_code="AE", name="United Arab Emirates")
    state = State.objects.create(country=country, short_code="AUH", name="Abu Dhabi")
    company = Company.objects.create(
        short_code="BYKY", name="BYKY", country=country, state=state,
        phone_number="+9710000000", email="ops@byky.test",
    )
    location = Location.objects.create(
        country=country, state=state, short_code="CORN", name="Corniche"
    )
    branch = Branch.objects.create(
        company=company, location=location, short_code="AUH01",
        name="Creek Park 1", branch_type=BranchType.STATION,
    )
    cashier = Designation.objects.create(
        company=company, code="CASH", title="Cashier", roster_category=RosterCategory.CASHIER,
    )
    role = Role.objects.create(company=company, name="Staff")
    grant_all(role)
    return {"company": company, "branch": branch, "cashier": cashier, "role": role}


@pytest.fixture
def employee(world):
    return Employee.objects.create(
        company=world["company"], employee_code="BYKY001",
        first_name="Rashed", last_name="K", designation=world["cashier"],
    )


@pytest.fixture
def app_user(world, employee):
    return User.objects.create_user(
        "BYKY001", PASSWORD, display_name="Rashed K",
        company=world["company"], role=world["role"], employee=employee,
        allowed_channels=[Channel.EMPLOYEE],
    )


@pytest.fixture
def device(world):
    """An approved device for the employee channel -- registration/approval
    is required at login (decided with the client 22 Sep 2026), but unlike
    operator this device carries no branch mapping; none is needed."""
    return Device.objects.create(
        company=world["company"], installation_id="phone-1", platform="android",
        channel=Channel.EMPLOYEE, status=DeviceStatus.APPROVED, approved_at=timezone.now(),
    )


def login_data(device, **overrides):
    data = {"username": "BYKY001", "password": PASSWORD, "installation_id": device.installation_id}
    data.update(overrides)
    return data


# -- Login ----------------------------------------------------------------


def test_login_returns_tokens_profile_and_roster(client, app_user, device):
    response = call(client, LOGIN_URL, login_data(device))
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == "ok"

    data = body["data"]
    assert set(data) == {"tokens", "employee", "roster"}
    assert data["tokens"]["access"] and data["tokens"]["refresh"]
    assert data["employee"]["employee_code"] == "BYKY001"
    assert data["employee"]["full_name"] == "Rashed K"
    assert "days" in data["roster"]

    assert AppSession.objects.filter(
        user=app_user, channel=Channel.EMPLOYEE, device=device, logged_out_at__isnull=True,
    ).exists()


def test_login_username_is_case_insensitive(client, app_user, device):
    response = call(client, LOGIN_URL, login_data(device, username="byky001"))
    assert response.status_code == 200


def test_wrong_password_is_refused(client, app_user, device):
    response = call(client, LOGIN_URL, login_data(device, password="wrong"))
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_credentials"


def test_unknown_username_gives_the_same_message_as_a_wrong_password(client, app_user, device):
    known = call(client, LOGIN_URL, login_data(device, password="wrong"))
    unknown = call(client, LOGIN_URL, login_data(device, username="nobody", password="wrong"))
    assert known.json()["message"] == unknown.json()["message"]


def test_a_blocked_employee_is_refused(client, employee, app_user, device):
    employee.is_blocked = True
    employee.save(update_fields=["is_blocked"])
    response = call(client, LOGIN_URL, login_data(device))
    assert response.status_code == 403
    assert response.json()["code"] == "user_blocked"


def test_a_user_without_the_employee_channel_is_refused(client, world, employee, device):
    user = User.objects.create_user(
        "BYKY002", PASSWORD, display_name="No App", company=world["company"],
        role=world["role"], employee=employee, allowed_channels=[Channel.WEB],
    )
    response = call(client, LOGIN_URL, login_data(device, username="BYKY002"))
    assert response.status_code == 403
    assert response.json()["code"] == "wrong_channel"


def test_a_web_only_user_with_no_employee_is_refused(client, world, device):
    User.objects.create_user(
        "sara.k", PASSWORD, display_name="Sara K", company=world["company"],
        role=world["role"], allowed_channels=[Channel.EMPLOYEE],
    )
    response = call(client, LOGIN_URL, login_data(device, username="sara.k"))
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_credentials"


def test_lockout_after_15_failed_attempts(client, app_user, device):
    # Account lockout (15/5min, keyed on username, apps/portal/auth.py) is a
    # separate mechanism from the IP throttle (10/min, apps/portal/api.py) --
    # clearing the throttle between calls isolates this test to the one it's
    # named for, rather than tripping the throttle's own 429 first.
    for _ in range(15):
        call(client, LOGIN_URL, login_data(device, password="wrong"))
        cache.clear()
    response = call(client, LOGIN_URL, login_data(device))
    assert response.status_code == 429
    assert response.json()["code"] == "account_locked"


def test_the_login_throttle_refuses_a_burst_of_calls(client, app_user, device):
    for _ in range(10):
        call(client, LOGIN_URL, login_data(device, password="wrong"))
    response = call(client, LOGIN_URL, login_data(device))
    assert response.status_code == 429
    assert response.json()["code"] == "rate_limited"


def test_an_overlong_password_is_refused_before_hashing(client, app_user, device):
    """max_length on the serializer (apps/portal/serializers.py) -- refused
    as invalid_request, distinct from invalid_credentials, so this is proven
    to be a type check that runs before check_credentials/check_password at
    all, not just a very long wrong password."""
    response = call(client, LOGIN_URL, login_data(device, password="x" * 129))
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_request"


def test_the_manager_channel_is_refused_not_built_yet(client, app_user, device):
    """operator now has its own login (test_operator_login_api.py); manager
    doesn't exist yet and falls through LoginView's own channel dict."""
    response = client.post(
        "/api/v1/manager/auth/login",
        {"credentials": {}, "request_data": login_data(device)},
        content_type="application/json",
    )
    assert response.status_code == 403
    assert response.json()["code"] == "wrong_channel"


def test_an_employee_account_is_refused_on_the_operator_endpoint(client, app_user, device):
    """A real, distinct code path from the manager case above: the app is
    recognised (operator is built), but this account isn't allowed on it --
    refused by check_account, not by LoginView's own channel guard."""
    response = client.post(
        "/api/v1/operator/auth/login",
        {"credentials": {}, "request_data": {
            "username": "BYKY001", "password": PASSWORD, "installation_id": "does-not-matter",
        }},
        content_type="application/json",
    )
    assert response.status_code == 403
    assert response.json()["code"] == "wrong_channel"


def test_a_second_login_replaces_the_first_session(client, app_user, device):
    call(client, LOGIN_URL, login_data(device))
    first = AppSession.objects.get(user=app_user, logged_out_at__isnull=True)
    call(client, LOGIN_URL, login_data(device))
    first.refresh_from_db()
    assert first.logged_out_at is not None
    assert AppSession.objects.filter(user=app_user, logged_out_at__isnull=True).count() == 1


# -- Device: registered, approved, not blocked/retired ------------------------
# No device_not_mapped / device_settings_not_done here, unlike operator --
# this channel has neither concept. design/login/login-for-employee.md section 3.


def test_missing_installation_id_is_a_type_error_not_a_login_refusal(client, app_user):
    response = call(client, LOGIN_URL, {"username": "BYKY001", "password": PASSWORD})
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_request"


def test_an_unregistered_device_is_refused(client, app_user):
    response = call(client, LOGIN_URL, {
        "username": "BYKY001", "password": PASSWORD, "installation_id": "never-registered",
    })
    assert response.status_code == 409
    assert response.json()["code"] == "device_not_registered"


def test_a_pending_device_is_refused(client, app_user, world):
    pending = Device.objects.create(
        company=world["company"], installation_id="phone-2", platform="android",
        channel=Channel.EMPLOYEE, status=DeviceStatus.PENDING,
    )
    response = call(client, LOGIN_URL, login_data(pending))
    assert response.status_code == 202
    assert response.json()["code"] == "device_pending_approval"


def test_a_blocked_device_is_refused(client, app_user, world):
    blocked = Device.objects.create(
        company=world["company"], installation_id="phone-3", platform="android",
        channel=Channel.EMPLOYEE, status=DeviceStatus.BLOCKED, approved_at=timezone.now(),
    )
    response = call(client, LOGIN_URL, login_data(blocked))
    assert response.status_code == 403
    assert response.json()["code"] == "device_blocked"


def test_a_retired_device_is_refused(client, app_user, world):
    retired = Device.objects.create(
        company=world["company"], installation_id="phone-4", platform="android",
        channel=Channel.EMPLOYEE, status=DeviceStatus.RETIRED, approved_at=timezone.now(),
    )
    response = call(client, LOGIN_URL, login_data(retired))
    assert response.status_code == 403
    assert response.json()["code"] == "device_retired"


def test_an_approved_device_with_no_branch_mapping_still_logs_in(client, app_user, device):
    """The point of the whole exercise: unlike operator, an employee device
    needs no device_mapping row at all -- the duty roster decides the
    station, not this device."""
    response = call(client, LOGIN_URL, login_data(device))
    assert response.status_code == 200
    assert response.json()["code"] == "ok"


# -- Refresh ----------------------------------------------------------------


def _tokens(client, app_user, device):
    return call(client, LOGIN_URL, login_data(device)).json()["data"]["tokens"]


def test_refresh_rotates_tokens(client, app_user, device):
    tokens = _tokens(client, app_user, device)
    response = call(client, REFRESH_URL, {"refresh_token": tokens["refresh"]})
    assert response.status_code == 200
    new_tokens = response.json()["data"]["tokens"]
    assert new_tokens["access"] != tokens["access"]
    assert new_tokens["refresh"] != tokens["refresh"]


def test_a_rotated_out_refresh_token_stops_working(client, app_user, device):
    tokens = _tokens(client, app_user, device)
    call(client, REFRESH_URL, {"refresh_token": tokens["refresh"]})
    reused = call(client, REFRESH_URL, {"refresh_token": tokens["refresh"]})
    assert reused.status_code == 401
    assert reused.json()["code"] == "session_closed"


def test_refresh_after_logout_is_refused(client, app_user, device):
    tokens = _tokens(client, app_user, device)
    call(client, LOGOUT_URL, headers={"HTTP_AUTHORIZATION": f"Bearer {tokens['access']}"})
    response = call(client, REFRESH_URL, {"refresh_token": tokens["refresh"]})
    assert response.status_code == 401
    assert response.json()["code"] == "session_closed"


def test_a_malformed_refresh_token_is_refused(client, app_user):
    response = call(client, REFRESH_URL, {"refresh_token": "not-a-real-token"})
    assert response.status_code == 401
    assert response.json()["code"] == "session_closed"


def test_refresh_updates_last_seen_at(client, app_user, device):
    tokens = _tokens(client, app_user, device)
    session = AppSession.objects.get(user=app_user, logged_out_at__isnull=True)
    first_seen = session.last_seen_at
    response = call(client, REFRESH_URL, {"refresh_token": tokens["refresh"]})
    assert response.status_code == 200
    session.refresh_from_db()
    assert session.last_seen_at >= first_seen


def test_a_deactivated_users_refresh_token_stops_working(client, app_user, device):
    tokens = _tokens(client, app_user, device)
    app_user.is_active = False
    app_user.save(update_fields=["is_active"])
    response = call(client, REFRESH_URL, {"refresh_token": tokens["refresh"]})
    assert response.status_code == 401
    assert response.json()["code"] == "session_closed"


def test_the_refresh_throttle_refuses_a_burst_of_calls(client, app_user, device):
    tokens = _tokens(client, app_user, device)
    for _ in range(30):
        call(client, REFRESH_URL, {"refresh_token": tokens["refresh"]})
    response = call(client, REFRESH_URL, {"refresh_token": tokens["refresh"]})
    assert response.status_code == 429
    assert response.json()["code"] == "rate_limited"


# -- Logout -------------------------------------------------------------------


def test_logout_closes_the_session(client, app_user, device):
    tokens = _tokens(client, app_user, device)
    response = call(client, LOGOUT_URL, headers={"HTTP_AUTHORIZATION": f"Bearer {tokens['access']}"})
    assert response.status_code == 200
    assert not AppSession.objects.filter(user=app_user, logged_out_at__isnull=True).exists()


def test_the_access_token_stops_working_after_logout(client, app_user, device):
    tokens = _tokens(client, app_user, device)
    headers = {"HTTP_AUTHORIZATION": f"Bearer {tokens['access']}"}
    call(client, LOGOUT_URL, headers=headers)
    second = call(client, LOGOUT_URL, headers=headers)
    assert second.status_code == 401


def test_logout_with_no_token_is_refused(client, app_user):
    response = call(client, LOGOUT_URL)
    assert response.status_code == 401


def test_a_deactivated_users_access_token_stops_authenticating(client, app_user, device):
    """core/middleware.py's web session check re-verifies is_active on every
    request; AppJWTAuthentication now does the same for the app (design/
    03-login.md section 5.1's "a force logout takes effect immediately")."""
    tokens = _tokens(client, app_user, device)
    app_user.is_active = False
    app_user.save(update_fields=["is_active"])
    response = call(client, LOGOUT_URL, headers={"HTTP_AUTHORIZATION": f"Bearer {tokens['access']}"})
    assert response.status_code == 401
