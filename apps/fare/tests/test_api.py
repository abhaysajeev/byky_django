"""POST /api/v1/operator/fares, tested through HTTP as the till app sees it:
sign in, then fetch the day's fares with the access token. The exhaustive
check -- every fare set, date and minute against pricing.resolve -- is in
test_device_fares.py."""

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
                                                  defaults={"category_name": "BYKY",
                                                            "approval_status": ApprovalStatus.APPROVED})[0]
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


APPROVED = {"approval_status": ApprovalStatus.APPROVED}


def fares(client, token, **request_data):
    return call(client, URL, {"date": "2026-09-30", **request_data}, token=token)


def fares_of(body, vehicle_type=None, package=30):
    """The fare ids of one package in the answer."""
    for entry in body["data"]["vehicle_types"]:
        if vehicle_type is None or entry["vehicle_type_id"] == vehicle_type.pk:
            for pkg in entry["packages"]:
                if pkg["package_minutes"] == package:
                    return [f["fare_id"] for f in pkg["fares"]]
    return []


def test_the_days_fares_come_whole_in_first_match_order(client, world, token):
    fare = make_fare(world, **APPROVED)
    make_rule(fare, "every_day", 480, 1200, base_fare=Decimal("60"))                  # 08-20
    make_rule(fare, "every_day", 720, 840, base_fare=Decimal("90"))                   # 12-14, inside it
    make_rule(fare, "selected_days", 840, 1440, weekdays=[WeekDay.SATURDAY, WeekDay.SUNDAY])
    make_rule(fare, "single_date", 720, 1320, on_date=D(2026, 12, 2), base_fare=Decimal("100"))
    summer = make_season(fare, D(2026, 2, 1), D(2026, 9, 30), name="Summer")
    make_season(fare, D(2026, 4, 10), D(2026, 5, 15), name="Eid", base_fare=Decimal("95"))
    make_rule(fare, "every_day", 960, 1200, season=summer)
    own = make_fare(world, branches=[world["adc1"]], valid_from=D(2026, 10, 1), **APPROVED)   # starts tomorrow

    body = fares(client, token).json()

    assert body["code"] == "ok"
    data = body["data"]
    assert data["date"] == "2026-09-30"
    [monaco] = data["vehicle_types"]
    assert monaco["vehicle_type_id"] == world["monaco"].pk
    [package] = monaco["packages"]
    assert package["package_minutes"] == 30
    assert [f["fare_id"] for f in package["fares"]] == [own.pk, fare.pk]          # the station's first
    company = package["fares"][1]
    assert set(company) == {"fare_id", "version", "valid_from", "valid_to", "price", "special_prices", "seasons"}
    assert company["price"] == {"base_fare": "50.00", "grace_minutes": 5, "concurrent_interval_minutes": 10,
                                "concurrent_fare": "10.00", "concurrent_grace_minutes": 0}
    order = [(r["kind"], r["start"], r["end"]) for r in company["special_prices"]]
    assert order == [("single_date", "12:00", "22:00"), ("selected_days", "14:00", "24:00"),
                     ("every_day", "12:00", "14:00"), ("every_day", "08:00", "20:00")]
    assert company["special_prices"][1] == {
        "id": company["special_prices"][1]["id"], "kind": "selected_days", "on_date": None,
        "weekdays": [5, 6], "start": "14:00", "end": "24:00",
        "price": {"base_fare": "50.00", "grace_minutes": 5, "concurrent_interval_minutes": 10,
                  "concurrent_fare": "10.00", "concurrent_grace_minutes": 0},
    }
    assert [s["name"] for s in company["seasons"]] == ["Eid", "Summer"]            # the shorter first
    assert company["seasons"][1]["special_prices"][0]["start"] == "16:00"


def test_only_fares_this_station_may_use_on_that_day(client, world, token):
    kept = make_fare(world, valid_from=D(2026, 9, 1), valid_to=D(2026, 9, 30), **APPROVED)   # ends today
    make_fare(world, branches=[world["adc2"]], **APPROVED)                        # another station's
    make_fare(world, package_minutes=45, is_active=False, **APPROVED)             # inactive
    make_fare(world, package_minutes=60)                                          # not approved
    make_fare(world, package_minutes=90, valid_to=D(2026, 9, 29), **APPROVED)     # ended yesterday
    make_fare(world, package_minutes=90, valid_from=D(2026, 10, 2), **APPROVED)   # starts the day after tomorrow
    body = fares(client, token).json()
    assert fares_of(body) == [kept.pk]
    assert [p["package_minutes"] for p in body["data"]["vehicle_types"][0]["packages"]] == [30]


def test_filters_narrow_the_answer(client, world, token):
    monaco30 = make_fare(world, **APPROVED)
    monaco60 = make_fare(world, package_minutes=60, **APPROVED)
    berg30 = make_fare(world, vehicle_type=world["berg"], **APPROVED)

    def ids(**filters):
        body = fares(client, token, **filters).json()
        return sorted(f["fare_id"] for v in body["data"]["vehicle_types"] for p in v["packages"] for f in p["fares"])

    assert ids() == sorted([monaco30.pk, monaco60.pk, berg30.pk])
    assert ids(vehicle_type_id=world["monaco"].pk) == sorted([monaco30.pk, monaco60.pk])
    assert ids(package_minutes=30) == sorted([monaco30.pk, berg30.pk])
    assert ids(vehicle_type_id=world["berg"].pk, package_minutes=30) == [berg30.pk]
    assert ids(vehicle_type_id=world["berg"].pk, package_minutes=60) == []       # known type, no fare: empty


def test_a_vehicle_type_of_another_company_is_refused(client, world, token):
    response = fares(client, token, vehicle_type_id=world["their_type"].pk)
    assert response.status_code == 400
    assert response.json()["code"] == "unknown_vehicle_type"


@pytest.mark.parametrize("request_data,field", [
    ({}, "date"),
    ({"date": "30/09/2026"}, "date"),
    ({"date": "2026-02-30"}, "date"),
    ({"date": "2026-09-30", "package_minutes": 0}, "package_minutes"),
    ({"date": "2026-09-30", "package_minutes": 1441}, "package_minutes"),
    ({"date": "2026-09-30", "vehicle_type_id": "abc"}, "vehicle_type_id"),
])
def test_the_request_is_checked(client, world, token, request_data, field):
    response = call(client, URL, request_data, token=token)
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_request"
    assert field in response.json()["data"]["errors"]


def test_no_token_is_refused(client, world):
    response = fares(client, None)
    assert response.status_code == 401
    assert response.json()["code"] == "not_authenticated"


def test_other_apps_are_refused(client, world, token):
    response = call(client, "/api/v1/employee/fares", {"date": "2026-09-30"}, token=token)
    assert response.status_code == 403
    assert response.json()["code"] == "wrong_channel"


def test_the_endpoint_is_in_the_api_docs(client, db):
    schema = client.get("/api/schema/").content.decode()
    assert "/api/v1/{app}/fares" in schema and "Operator Fares" in schema
