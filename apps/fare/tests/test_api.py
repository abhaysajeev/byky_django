"""POST /api/v1/operator/fares, tested through HTTP as the till app sees it:
sign in, then fetch one vehicle type's fares with the access token."""

from decimal import Decimal

import pytest
from django.core.cache import cache
from django.utils import timezone

from apps.company.models import Branch, BranchType, Company, Country, Location, State, WeekDay
from apps.crew.models import Designation, Employee
from apps.devices.models import Device, DeviceMapping, DeviceSettings, DeviceStatus
from apps.fare.tests.conftest import D, make_fare, make_rule, make_season
from apps.fleet.models import Brand, Category, VehicleType
from apps.portal.models import Role
from apps.portal.services import grant_all
from core.enums import ApprovalStatus, Channel
from core.models import User

PASSWORD = "Byky#2026"
URL = "/api/v1/operator/fares"


@pytest.fixture(autouse=True)
def fresh_throttle():
    cache.clear()
    yield
    cache.clear()


def call(client, url, request_data=None, *, token=None):
    headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
    return client.post(url, {"credentials": {}, "request_data": request_data or {}},
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

    def branch(co, code, name):
        return Branch.objects.create(company=co, location=location, short_code=code, name=name,
                                     branch_type=BranchType.STATION)

    here, there = branch(company, "ADC1", "Abu Dhabi Corniche 1"), branch(company, "ADC2", "Abu Dhabi Corniche 2")
    DeviceSettings.objects.create(branch=here, settings_code="S01", order_no_prefix="ADC",
                                  approval_status=ApprovalStatus.APPROVED)

    def vehicle_type(co, name):
        category = Category.objects.get_or_create(company=co, category_code="BYKY",
                                                  defaults={"category_name": "BYKY"})[0]
        brand = Brand.objects.get_or_create(company=co, brand_code="BYK", defaults={"brand_name": "Byky"})[0]
        return VehicleType.objects.create(company=co, category=category, brand=brand, vehicle_type_name=name,
                                          vehicle_type_code=name[:3].upper(), tax_percentage=Decimal("5.00"),
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
    return {"company": company, "other": other, "adc1": here, "adc2": there,
            "monaco": vehicle_type(company, "Monaco"), "berg": vehicle_type(company, "Berg"),
            "their_type": vehicle_type(other, "Kart")}


@pytest.fixture
def token(client, world):
    response = call(client, "/api/v1/operator/auth/login",
                    {"username": "OPR001", "password": PASSWORD, "installation_id": "till-1"})
    assert response.status_code == 200, response.json()
    return response.json()["data"]["tokens"]["access"]


def fares_for(client, token, vehicle_type):
    return call(client, URL, {"vehicle_type_id": vehicle_type.pk}, token=token)


def test_every_package_comes_whole(client, world, token):
    fare = make_fare(world, approval_status=ApprovalStatus.APPROVED, valid_to=D(2099, 12, 31))
    make_rule(fare, "single_date", 720, 1320, on_date=D(2099, 12, 2), base_fare=Decimal("100"))
    make_rule(fare, "selected_days", 840, 1440, weekdays=[WeekDay.SATURDAY, WeekDay.SUNDAY])
    spring = make_season(fare, D(2099, 4, 10), D(2099, 5, 15), base_fare=Decimal("55"))
    make_rule(fare, "every_day", 960, 1200, season=spring)
    own = make_fare(world, branches=[world["adc1"]], approval_status=ApprovalStatus.APPROVED,
                    valid_from=D(2099, 10, 1), valid_to=D(2099, 12, 31))

    body = fares_for(client, token, world["monaco"]).json()

    assert body["code"] == "ok"
    data = body["data"]
    assert data["branch"] == {"id": world["adc1"].pk, "code": "ADC1", "name": "Abu Dhabi Corniche 1"}
    assert data["company"]["code"] == "0598" and data["company"]["timezone"] == "Asia/Dubai"
    assert data["vehicle_type"] == {"id": world["monaco"].pk, "name": "Monaco", "code": "MON",
                                    "category": "BYKY", "tax_percentage": "5.00"}
    assert [f["fare_id"] for f in data["fares"]] == [fare.pk, own.pk]
    company_fare = data["fares"][0]
    assert company_fare["level"] == "company" and company_fare["base_price"]["base_fare"] == "50.00"
    assert [r["kind"] for r in company_fare["special_prices"]] == ["single_date", "selected_days"]
    assert company_fare["special_prices"][1] == {
        "id": company_fare["special_prices"][1]["id"], "kind": "selected_days", "on_date": None,
        "weekdays": [5, 6], "start": "14:00", "end": "24:00",
        "price": {"base_fare": "50.00", "grace_minutes": 5, "concurrent_interval_minutes": 10,
                  "concurrent_fare": "10.00", "concurrent_grace_minutes": 0},
    }
    season = company_fare["seasons"][0]
    assert (season["name"], season["start_date"], season["base_price"]["base_fare"]) == ("Spring", "2099-04-10", "55.00")
    assert season["special_prices"][0]["start"] == "16:00"
    assert data["fares"][1]["level"] == "branch"


def test_only_fares_this_station_may_use(client, world, token):
    approved = {"approval_status": ApprovalStatus.APPROVED, "valid_to": D(2099, 12, 31)}
    make_fare(world, branches=[world["adc2"]], **approved)                        # another station's
    make_fare(world, package_minutes=45, is_active=False, **approved)             # inactive
    make_fare(world, package_minutes=60)                                          # not approved
    make_fare(world, package_minutes=90, approval_status=ApprovalStatus.APPROVED,
              valid_from=D(2020, 1, 1), valid_to=D(2020, 12, 31))                 # already ended
    make_fare(world, vehicle_type=world["berg"], **approved)                      # another vehicle type
    assert fares_for(client, token, world["monaco"]).json()["data"]["fares"] == []


def test_a_vehicle_type_of_another_company_is_refused(client, world, token):
    response = fares_for(client, token, world["their_type"])
    assert response.status_code == 400
    assert response.json()["code"] == "unknown_vehicle_type"


def test_vehicle_type_id_is_required(client, world, token):
    response = call(client, URL, {}, token=token)
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_request"
    assert "vehicle_type_id" in response.json()["data"]["errors"]


def test_no_token_is_refused(client, world):
    response = fares_for(client, None, world["monaco"])
    assert response.status_code == 401
    assert response.json()["code"] == "not_authenticated"


def test_other_apps_are_refused(client, world, token):
    response = call(client, "/api/v1/employee/fares", {"vehicle_type_id": world["monaco"].pk}, token=token)
    assert response.status_code == 403
    assert response.json()["code"] == "wrong_channel"


def test_the_endpoint_is_in_the_api_docs(client, db):
    schema = client.get("/api/schema/").content.decode()
    assert "/api/v1/{app}/fares" in schema and "Operator Fares" in schema
