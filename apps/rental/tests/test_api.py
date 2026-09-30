"""POST /api/v1/operator/customers/lookup, tested through HTTP as the till app
sees it: sign in, then look up a customer by phone number with the access
token. Same login-then-call shape as apps/fare/tests/test_api.py."""

import datetime
import json
import uuid

import pytest
from django.core.cache import cache
from django.utils import timezone

from apps.company.models import Branch, BranchType, Company, Country, Location, State
from apps.crew.models import Designation, Employee
from apps.devices.models import Device, DeviceMapping, DeviceSettings, DeviceStatus
from apps.fare.tests.test_api import shape
from apps.portal.models import Role
from apps.portal.services import grant_all
from apps.rental import api
from apps.rental.models import Customer
from apps.rental.tests.conftest import make_customer
from core.enums import ApprovalStatus, Channel
from core.ids import uuid7
from core.models import User

PASSWORD = "Byky#2026"
URL = "/api/v1/operator/customers/lookup"
CREATE = "/api/v1/operator/customers/create"


@pytest.fixture(autouse=True)
def fresh_throttle():
    """Every test signs in; the login throttle's counts live in the cache."""
    cache.clear()
    yield
    cache.clear()


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


def phone(mobile_no="501234567", country_code="+971", full=None):
    """The three phone fields the app sends."""
    return {"mobile_country_code": country_code, "mobile_no": mobile_no,
            "full_number": full if full is not None else "".join(c for c in country_code + mobile_no if c.isdigit())}


def lookup(client, token, mobile_no="501234567", **kwargs):
    return call(client, URL, phone(mobile_no, **kwargs), token=token)


def new_customer(**overrides):
    data = {"sync_id": str(uuid7()), "first_name": "Ahmed", **phone()}
    data.update(overrides)
    return data


# -- Lookup ---------------------------------------------------------------------------


def test_lookup_finds_a_customer_by_full_number(client, world, token):
    make_customer(world, customer_code="CU001", mobile_country_code="+971", mobile_no="501234567",
                  date_of_birth=datetime.date(1990, 5, 14))
    body = lookup(client, token).json()
    assert body["code"] == "ok"
    assert body["data"]["customer_code"] == "CU001"
    assert body["data"]["mobile_full"] == "971501234567"
    assert body["data"]["is_blocked"] is False
    assert shape(body["data"]) == shape(api._CUSTOMER_SAMPLE)


def test_lookup_tells_the_same_number_under_two_country_codes_apart(client, world, token):
    make_customer(world, customer_code="CU001", mobile_country_code="+971", mobile_no="7025985344")
    make_customer(world, customer_code="CU002", mobile_country_code="+91", mobile_no="7025985344")
    assert lookup(client, token, "7025985344", country_code="+91").json()["data"]["customer_code"] == "CU002"


def test_a_blocked_customer_still_returns_full_data(client, world, token):
    make_customer(world, customer_code="CU001", mobile_no="501234567",
                  is_blocked=True, block_reason="Repeated late returns")
    body = lookup(client, token).json()
    assert body["code"] == "ok"
    assert body["data"]["is_blocked"] is True
    assert body["data"]["block_reason"] == "Repeated late returns"


def test_unknown_phone_number_is_404(client, world, token):
    response = lookup(client, token, "500000000")
    assert response.status_code == 404
    assert response.json()["code"] == "unknown_customer"


def test_a_phone_number_from_another_company_is_not_found(client, world, token):
    make_customer(world, company=world["other"], customer_code="CU001", mobile_no="501234567")
    assert lookup(client, token).status_code == 404


@pytest.mark.parametrize("request_data, field, message", [
    ({}, "mobile_country_code", "is required"),
    ({"mobile_country_code": "+971", "mobile_no": "501234567"}, "full_number", "is required"),
    (phone(full="+971501234567"), "full_number", "must be digits only"),
    (phone(full="971509999999"), "full_number", "does not match mobile_country_code and mobile_no"),
])
def test_the_phone_fields_are_checked(client, world, token, request_data, field, message):
    response = call(client, URL, request_data, token=token)
    assert response.status_code == 400
    assert response.json()["data"]["errors"][field] == message


def test_signed_out_is_refused(client, world):
    response = lookup(client, None)
    assert response.status_code == 401


# -- Create ---------------------------------------------------------------------------


def test_create_registers_a_customer_in_the_devices_company(client, world, token):
    data = new_customer(last_name="Al Mansoori", gender="male", date_of_birth="1990-05-14",
                        id_type="emirates_id", id_no="784-1990-1234567-1", email="ahmed@example.com")
    response = call(client, CREATE, data, token=token)
    body = response.json()
    assert response.status_code == 200, body
    assert body["code"] == "ok" and body["message"] == "Customer created."
    assert body["data"]["sync_id"] == data["sync_id"]
    assert body["data"]["customer_code"] == "CU001"
    assert shape(body["data"]) == shape(api._CREATED_SAMPLE)

    customer = Customer.objects.get()
    assert customer.company == world["company"]
    assert customer.mobile_full == "971501234567"
    assert customer.created_by.username == "opr001"
    assert customer.is_active and customer.approval_status == ApprovalStatus.APPROVED
    assert customer.is_blocked is False


def test_a_resent_sync_id_is_a_duplicate_not_a_second_customer(client, world, token):
    data = new_customer()
    call(client, CREATE, data, token=token)
    again = call(client, CREATE, data, token=token).json()
    assert again["code"] == "duplicate"
    assert again["data"]["customer_code"] == "CU001"
    assert Customer.objects.count() == 1


def test_a_registered_phone_returns_that_customer_with_block_data(client, world, token):
    make_customer(world, customer_code="CU007", is_blocked=True, block_reason="Unpaid damage")
    response = call(client, CREATE, new_customer(), token=token)
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "customer_exists"
    assert body["data"]["customer_code"] == "CU007"
    assert body["data"]["is_blocked"] is True and body["data"]["block_reason"] == "Unpaid damage"
    assert Customer.objects.count() == 1


def test_another_companys_phone_is_refused_without_details(client, world, token):
    make_customer(world, company=world["other"], customer_code="CU001")
    response = call(client, CREATE, new_customer(), token=token)
    assert response.status_code == 409
    assert response.json() == {"code": "customer_exists",
                               "message": "A customer with this phone number already exists.", "data": {}}


def test_a_sync_id_used_by_another_company_is_a_conflict(client, world, token):
    taken = uuid7()
    make_customer(world, company=world["other"], customer_code="CU001", mobile_no="509999999", sync_id=taken)
    response = call(client, CREATE, new_customer(sync_id=str(taken)), token=token)
    assert response.status_code == 409
    assert response.json()["code"] == "sync_id_conflict"


@pytest.mark.parametrize("overrides, field, message", [
    ({"sync_id": str(uuid.UUID("6f1c2b9e-3d4a-4b8c-9e2f-0a1b2c3d4e5f"))}, "sync_id", "must be a UUIDv7"),
    ({"first_name": ""}, "first_name", "is required"),
    ({"gender": "x"}, "gender", "must be one of male, female, others"),
    ({"email": "not-an-email"}, "email", "must be a valid email"),
    ({"date_of_birth": "14/05/1990"}, "date_of_birth", "must be a date like 2026-09-30"),
    ({"full_number": "971500000000"}, "full_number", "does not match mobile_country_code and mobile_no"),
])
def test_create_checks_its_fields(client, world, token, overrides, field, message):
    response = call(client, CREATE, new_customer(**overrides), token=token)
    assert response.status_code == 400
    assert response.json()["data"]["errors"][field] == message
    assert not Customer.objects.exists()


def test_create_is_for_the_operator_app_only(client, world, token):
    response = call(client, "/api/v1/employee/customers/create", new_customer(), token=token)
    assert response.status_code == 403
    assert response.json()["code"] == "wrong_channel"


def test_the_customer_endpoints_are_in_the_docs(client, world):
    schema = client.get("/api/schema/").content.decode()
    assert "/api/v1/{app}/customers/create" in schema and "full_number" in schema


