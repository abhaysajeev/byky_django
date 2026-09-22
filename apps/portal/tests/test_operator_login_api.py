"""POST /api/v1/operator/auth/login, tested through HTTP as the RMS till
app sees it. design/login/login-for-operator.md.

Refresh and logout are deliberately NOT re-implemented per channel
(apps/portal/api.py::AppRefreshView/AppLogoutView) -- the tests here prove
that claim rather than just assert it: an operator session refreshes and
logs out through the exact same code already shipped for the Employee App.
"""

import pytest
from django.core.cache import cache
from django.utils import timezone

from apps.company.models import Branch, BranchType, Company, Country, Location, State
from apps.crew.models import Designation, Employee
from apps.devices.models import Device, DeviceMapping, DeviceSettings, DeviceStatus
from apps.portal.models import Role
from apps.portal.services import grant_all
from apps.portal.session_models import AppSession
from core.enums import ApprovalStatus, Channel
from core.models import User

PASSWORD = "Byky#2026"
LOGIN_URL = "/api/v1/operator/auth/login"
REFRESH_URL = "/api/v1/operator/auth/refresh"
LOGOUT_URL = "/api/v1/operator/auth/logout"


@pytest.fixture(autouse=True)
def fresh_throttle():
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
        country=country, state=state, short_code="CORN", name="Corniche",
    )
    branch = Branch.objects.create(
        company=company, location=location, short_code="AUH01",
        name="Creek Park 1", branch_type=BranchType.STATION,
        is_hotel=True, hotel_commission="12.50", accepts_app_payment=True,
        is_multi_device=True, allows_test_ride=True,
    )
    designation = Designation.objects.create(company=company, code="CASH", title="Cashier")
    role = Role.objects.create(company=company, name="Operator")
    grant_all(role)
    DeviceSettings.objects.create(
        branch=branch, settings_code="S01", order_no_prefix="CREEK",
        header_1="BYKY", header_2="Creek Park", footer_1="Thank you",
        approval_status=ApprovalStatus.APPROVED,
    )
    return {"company": company, "branch": branch, "designation": designation, "role": role}


@pytest.fixture
def employee(world):
    return Employee.objects.create(
        company=world["company"], employee_code="OPR001",
        first_name="Rashed", last_name="K", designation=world["designation"],
    )


@pytest.fixture
def app_user(world, employee):
    return User.objects.create_user(
        "OPR001", PASSWORD, display_name="Rashed K",
        company=world["company"], role=world["role"], employee=employee,
        allowed_channels=[Channel.OPERATOR],
    )


@pytest.fixture
def device(world):
    device = Device.objects.create(
        company=world["company"], installation_id="till-1", platform="android",
        channel=Channel.OPERATOR, status=DeviceStatus.APPROVED, approved_at=timezone.now(),
    )
    DeviceMapping.objects.create(device=device, branch=world["branch"], from_date=timezone.now())
    return device


def _login(client, app_user, device):
    return call(client, LOGIN_URL, {
        "username": app_user.username, "password": PASSWORD, "installation_id": device.installation_id,
    })


def test_login_returns_the_full_response_shape(client, app_user, device, world):
    response = _login(client, app_user, device)
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == "ok"

    data = body["data"]
    assert set(data) == {
        "tokens", "first_name", "branch", "device_settings",
        "order_no_prefix", "next_order_number", "next_test_number",
    }
    assert data["tokens"]["access"] and data["tokens"]["refresh"]
    assert data["first_name"] == "Rashed"

    branch = data["branch"]
    assert branch["name"] == "Creek Park 1"
    assert branch["branch_code"] == "AUH01"
    assert branch["is_hotel"] is True
    assert branch["hotel_commission"] == "12.50"
    assert branch["accepts_app_payment"] is True
    assert branch["is_multi_device"] is True
    assert branch["allows_test_ride"] is True

    assert data["device_settings"]["settings_code"] == "S01"
    assert data["order_no_prefix"] == "CREEK"
    assert data["next_order_number"] == 1
    assert data["next_test_number"] == 1

    assert AppSession.objects.filter(
        user=app_user, channel=Channel.OPERATOR, device=device, logged_out_at__isnull=True,
    ).exists()


def test_a_second_login_advances_the_bill_numbers(client, app_user, device):
    """Confirms the counter is a real, persisted row, not recomputed from
    nothing each time -- last_number moves only when something raises it,
    never by the read itself."""
    from apps.devices.models import BillContinuity

    _login(client, app_user, device)
    counter = BillContinuity.objects.get(device=device, kind="order")
    counter.last_number = 40
    counter.save(update_fields=["last_number"])

    response = _login(client, app_user, device)
    assert response.json()["data"]["next_order_number"] == 41


def test_missing_installation_id_is_a_type_error_not_a_login_refusal(client, app_user):
    response = call(client, LOGIN_URL, {"username": app_user.username, "password": PASSWORD})
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_request"


def test_an_unregistered_device_is_refused(client, app_user):
    response = call(client, LOGIN_URL, {
        "username": app_user.username, "password": PASSWORD, "installation_id": "never-registered",
    })
    assert response.status_code == 409
    assert response.json()["code"] == "device_not_registered"


def test_a_device_with_no_station_is_refused(client, app_user, world):
    unmapped = Device.objects.create(
        company=world["company"], installation_id="till-2", platform="android",
        channel=Channel.OPERATOR, status=DeviceStatus.APPROVED, approved_at=timezone.now(),
    )
    response = _login(client, app_user, unmapped)
    assert response.status_code == 409
    assert response.json()["code"] == "device_not_mapped"


def test_a_branch_with_no_settings_is_refused(client, app_user, world):
    other_branch = Branch.objects.create(
        company=world["company"], location=world["branch"].location, short_code="AUH02",
        name="Zabeel Park 1", branch_type=BranchType.STATION,
    )
    unset = Device.objects.create(
        company=world["company"], installation_id="till-3", platform="android",
        channel=Channel.OPERATOR, status=DeviceStatus.APPROVED, approved_at=timezone.now(),
    )
    DeviceMapping.objects.create(device=unset, branch=other_branch, from_date=timezone.now())

    response = _login(client, app_user, unset)
    assert response.status_code == 409
    assert response.json()["code"] == "device_settings_not_done"


def test_session_active_elsewhere_is_refused(client, app_user, device, world):
    _login(client, app_user, device)

    second = Device.objects.create(
        company=world["company"], installation_id="till-4", platform="android",
        channel=Channel.OPERATOR, status=DeviceStatus.APPROVED, approved_at=timezone.now(),
    )
    DeviceMapping.objects.create(device=second, branch=world["branch"], from_date=timezone.now())

    response = _login(client, app_user, second)
    assert response.status_code == 409
    assert response.json()["code"] == "session_active_elsewhere"


# -- Refresh/logout: unmodified, shared code -----------------------------------


def test_an_operator_session_refreshes_through_the_shared_endpoint(client, app_user, device):
    tokens = _login(client, app_user, device).json()["data"]["tokens"]
    response = call(client, REFRESH_URL, {"refresh_token": tokens["refresh"]})
    assert response.status_code == 200
    assert response.json()["data"]["tokens"]["access"] != tokens["access"]


def test_an_operator_session_logs_out_through_the_shared_endpoint(client, app_user, device):
    tokens = _login(client, app_user, device).json()["data"]["tokens"]
    response = call(client, LOGOUT_URL, headers={"HTTP_AUTHORIZATION": f"Bearer {tokens['access']}"})
    assert response.status_code == 200
    assert not AppSession.objects.filter(user=app_user, logged_out_at__isnull=True).exists()
