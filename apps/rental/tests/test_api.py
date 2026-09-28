"""POST /api/v1/operator/customers/lookup, tested through HTTP as the till app
sees it: sign in, then look up a customer by phone number with the access
token. Same login-then-call shape as apps/fare/tests/test_api.py."""

import json

import pytest
from django.utils import timezone

from apps.company.models import Branch, BranchType, Company, Country, Location, State
from apps.crew.models import Designation, Employee
from apps.devices.models import Device, DeviceMapping, DeviceSettings, DeviceStatus
from apps.portal.models import Role
from apps.portal.services import grant_all
from apps.rental.tests.conftest import make_customer
from core.enums import ApprovalStatus, Channel
from core.models import User

PASSWORD = "Byky#2026"
URL = "/api/v1/operator/customers/lookup"


def call(client, url, request_data=None, *, token=None):
    headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
    return client.post(url, json.dumps({"credentials": {}, "request_data": request_data or {}}),
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
    here = Branch.objects.create(company=company, location=location, short_code="ADC1",
                                 name="Abu Dhabi Corniche 1", branch_type=BranchType.STATION)
    DeviceSettings.objects.create(branch=here, settings_code="S01", order_no_prefix="ADC",
                                  approval_status=ApprovalStatus.APPROVED)

    designation = Designation.objects.create(company=company, code="CASH", title="Cashier")
    role = Role.objects.create(company=company, name="Operator")
    grant_all(role)
    employee = Employee.objects.create(company=company, employee_code="OPR001", first_name="Rashed",
                                       designation=designation)
    User.objects.create_user("OPR001", PASSWORD, display_name="Rashed", company=company, role=role,
                             employee=employee, allowed_channels=[Channel.OPERATOR, Channel.EMPLOYEE])
    device = Device.objects.create(company=company, installation_id="till-1", platform="android",
                                   channel=Channel.OPERATOR, status=DeviceStatus.APPROVED,
                                   approved_at=timezone.now())
    DeviceMapping.objects.create(device=device, branch=here, from_date=timezone.now())
    return {"company": company, "other": other, "adc1": here}


@pytest.fixture
def token(client, world):
    response = call(client, "/api/v1/operator/auth/login",
                    {"username": "OPR001", "password": PASSWORD, "installation_id": "till-1"})
    assert response.status_code == 200, response.json()
    return response.json()["data"]["tokens"]["access"]


def lookup(client, token, mobile_no):
    return call(client, URL, {"mobile_no": mobile_no}, token=token)


def test_lookup_finds_a_customer_by_mobile_no_ignoring_country_code(client, world, token):
    make_customer(world, customer_code="CU001", mobile_country_code="+971", mobile_no="501234567")
    body = lookup(client, token, "501234567").json()
    assert body["code"] == "ok"
    assert body["data"]["customer_code"] == "CU001"
    assert body["data"]["mobile_country_code"] == "+971"
    assert body["data"]["is_blocked"] is False


def test_a_blocked_customer_still_returns_full_data(client, world, token):
    make_customer(world, customer_code="CU001", mobile_no="501234567",
                  is_blocked=True, block_reason="Repeated late returns")
    body = lookup(client, token, "501234567").json()
    assert body["code"] == "ok"
    assert body["data"]["is_blocked"] is True
    assert body["data"]["block_reason"] == "Repeated late returns"


def test_unknown_phone_number_is_404(client, world, token):
    response = lookup(client, token, "500000000")
    assert response.status_code == 404
    assert response.json()["code"] == "unknown_customer"


def test_a_phone_number_from_another_company_is_not_found(client, world, token):
    make_customer(world, company=world["other"], customer_code="CU001", mobile_no="501234567")
    assert lookup(client, token, "501234567").status_code == 404


def test_mobile_no_is_required(client, world, token):
    response = call(client, URL, {}, token=token)
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_request"


def test_signed_out_is_refused(client, world):
    response = lookup(client, None, "501234567")
    assert response.status_code == 401
