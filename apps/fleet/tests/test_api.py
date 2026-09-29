"""POST /api/v1/operator/vehicles through HTTP, and vehicle.current_branch
itself: the form's scoping and the database's same-company guard."""

import pytest
from django.db import IntegrityError, connection, transaction

from apps.fare.tests import test_api as fare_api
from apps.fleet import api
from apps.fleet.models import UOM, Category, Vehicle, VehicleType
from core.enums import ApprovalStatus

# The signed-in till from the fare API tests, reused as fixtures here.
call = fare_api.call
world, token, fresh_throttle = fare_api.world, fare_api.token, fare_api.fresh_throttle

URL = "/api/v1/operator/vehicles"
APPROVED = ApprovalStatus.APPROVED


@pytest.fixture
def uom(world):
    return UOM.objects.create(company=world["company"], uom_code="NO", uom_name="Number",
                              approval_status=APPROVED)


def vehicle(world, uom, code, vehicle_type, branch, **fields):
    return Vehicle.objects.create(
        company=vehicle_type.company, vehicle_code=code, vehicle_name=code, vehicle_type=vehicle_type,
        uom=uom, current_branch=branch, approval_status=APPROVED, **fields,
    )


def test_vehicles_come_nested_by_category_and_type(client, world, token, uom):
    vehicle(world, uom, "MON-2", world["monaco"], world["adc1"], rfid_epc="E2801", is_available=False)
    vehicle(world, uom, "MON-1", world["monaco"], world["adc1"], rfid_epc="E2800")
    vehicle(world, uom, "BRG-1", world["berg"], world["adc1"])

    body = call(client, URL, token=token).json()

    assert body["code"] == "ok"
    data = body["data"]
    assert data["branch"]["code"] == "ADC1" and data["total_vehicles"] == 3
    assert data["filters"] == {"vehicle_category_id": None, "vehicle_type_id": None}
    [category] = data["categories"]
    assert category["vehicle_count"] == 3 and [t["vehicle_count"] for t in category["vehicle_types"]] == [1, 2]
    assert (category["name"], [t["name"] for t in category["vehicle_types"]]) == ("BYKY", ["Berg", "Monaco"])
    monaco = category["vehicle_types"][1]
    assert monaco["vehicle_type_id"] == world["monaco"].pk and monaco["tax_percentage"] == "5.00"
    first, second = (Vehicle.objects.get(vehicle_code=code) for code in ("MON-2", "MON-1"))
    assert monaco["vehicles"] == [                       # in identifier order: MON-2 was created first
        {"vehicle_id": first.pk, "vehicle_identifier": first.identifier, "vehicle_code": "MON-2",
         "vehicle_name": "MON-2", "rfid_epc": "E2801", "uom": "Number", "is_available": False},
        {"vehicle_id": second.pk, "vehicle_identifier": second.identifier, "vehicle_code": "MON-1",
         "vehicle_name": "MON-1", "rfid_epc": "E2800", "uom": "Number", "is_available": True},
    ]
    assert first.identifier.startswith("VB") and first.identifier_no < second.identifier_no
    # The Swagger example is exactly what a device gets.
    assert fare_api.shape(data) == fare_api.shape(api._VEHICLES_SAMPLE)


def test_each_vehicle_type_says_whether_it_is_direct_rent(client, world, token, uom):
    drift = VehicleType.objects.create(company=world["company"], category=world["monaco"].category,
                                       brand=world["monaco"].brand, vehicle_type_name="Drift Car",
                                       is_direct_rent=True, approval_status=APPROVED)
    vehicle(world, uom, "DC-1", drift, world["adc1"])
    vehicle(world, uom, "MON-1", world["monaco"], world["adc1"])

    [category] = call(client, URL, token=token).json()["data"]["categories"]

    assert {t["name"]: t["is_direct_rent"] for t in category["vehicle_types"]} == {
        "Drift Car": True, "Monaco": False,
    }
    # The Swagger example shows both values too.
    sample = api._VEHICLES_SAMPLE["categories"][0]["vehicle_types"]
    assert {t["is_direct_rent"] for t in sample} == {True, False}


@pytest.fixture
def fleet(world, uom):
    """Monaco and Berg (category BYKY) and a Kart type in a second category, all at ADC1."""
    byky = world["monaco"].category
    karts = Category.objects.create(company=world["company"], category_code="KART", category_name="Karts",
                                    approval_status=APPROVED)
    kart = VehicleType.objects.create(company=world["company"], category=karts, brand=world["monaco"].brand,
                                      vehicle_type_name="Kart", approval_status=APPROVED)
    for code, vehicle_type in (("MON-1", world["monaco"]), ("MON-2", world["monaco"]),
                               ("BRG-1", world["berg"]), ("KRT-1", kart)):
        vehicle(world, uom, code, vehicle_type, world["adc1"])
    vehicle(world, uom, "MON-ELSEWHERE", world["monaco"], world["adc2"])
    return {"byky": byky, "karts": karts, "kart": kart}


def codes(response):
    data = response.json()["data"]
    listed = [v["vehicle_code"] for c in data["categories"] for t in c["vehicle_types"] for v in t["vehicles"]]
    assert data["total_vehicles"] == len(listed)
    return listed


def test_filters_only_narrow_the_answer(client, world, token, fleet):
    monaco, byky, karts = world["monaco"].pk, fleet["byky"].pk, fleet["karts"].pk
    assert codes(call(client, URL, token=token)) == ["BRG-1", "MON-1", "MON-2", "KRT-1"]
    assert codes(call(client, URL, {"vehicle_category_id": byky}, token=token)) == ["BRG-1", "MON-1", "MON-2"]
    assert codes(call(client, URL, {"vehicle_category_id": karts}, token=token)) == ["KRT-1"]
    assert codes(call(client, URL, {"vehicle_type_id": monaco}, token=token)) == ["MON-1", "MON-2"]
    both = call(client, URL, {"vehicle_category_id": byky, "vehicle_type_id": monaco}, token=token)
    assert codes(both) == ["MON-1", "MON-2"]
    assert both.json()["data"]["filters"] == {"vehicle_category_id": byky, "vehicle_type_id": monaco}
    # A type outside the category, or one with nothing here: an empty answer, not an error.
    assert codes(call(client, URL, {"vehicle_category_id": karts, "vehicle_type_id": monaco}, token=token)) == []
    Vehicle.objects.filter(vehicle_type=fleet["kart"]).delete()
    assert codes(call(client, URL, {"vehicle_type_id": fleet["kart"].pk}, token=token)) == []


def test_a_category_or_type_this_station_cannot_use_is_refused(client, world, token, fleet):
    their_type = world["their_type"]
    VehicleType.objects.filter(pk=fleet["kart"].pk).update(is_active=False)
    for request_data, code in (
        ({"vehicle_category_id": their_type.category_id}, "unknown_vehicle_category"),
        ({"vehicle_category_id": 999999}, "unknown_vehicle_category"),
        ({"vehicle_type_id": their_type.pk}, "unknown_vehicle_type"),
        ({"vehicle_type_id": fleet["kart"].pk}, "unknown_vehicle_type"),          # inactive
    ):
        response = call(client, URL, request_data, token=token)
        assert (response.status_code, response.json()["code"]) == (400, code), request_data
    Category.objects.filter(pk=fleet["karts"].pk).update(approval_status=ApprovalStatus.PENDING)
    assert call(client, URL, {"vehicle_category_id": fleet["karts"].pk}, token=token).json()["code"] \
        == "unknown_vehicle_category"


@pytest.mark.parametrize("request_data", [{"vehicle_type_id": "x"}, {"vehicle_category_id": -1},
                                          {"vehicle_type_id": 1.5}])
def test_the_request_is_checked(client, world, token, request_data):
    response = call(client, URL, request_data, token=token)
    assert (response.status_code, response.json()["code"]) == (400, "invalid_request")


def test_a_station_closed_after_login_stops_both_downloads(client, world, token, fleet):
    world["adc1"].is_active = False
    world["adc1"].save(update_fields=["is_active"])
    for url, request_data in ((URL, None), (fare_api.URL, {"date": "2026-09-30"})):
        response = call(client, url, request_data, token=token)
        assert (response.status_code, response.json()["code"]) == (409, "branch_inactive"), url


def test_only_this_stations_active_approved_vehicles(client, world, token, uom):
    vehicle(world, uom, "ELSEWHERE", world["monaco"], world["adc2"])
    vehicle(world, uom, "NOWHERE", world["monaco"], None)
    vehicle(world, uom, "INACTIVE", world["monaco"], world["adc1"], is_active=False)
    Vehicle.objects.filter(pk=vehicle(world, uom, "PENDING", world["monaco"], world["adc1"]).pk) \
        .update(approval_status=ApprovalStatus.PENDING)
    data = call(client, URL, token=token).json()["data"]
    assert (data["total_vehicles"], data["categories"]) == (0, [])


def test_other_apps_and_strangers_are_refused(client, world, token):
    assert call(client, "/api/v1/employee/vehicles", token=token).json()["code"] == "wrong_channel"
    assert call(client, URL).status_code == 401


def test_the_endpoint_is_in_the_api_docs(client, db):
    schema = client.get("/api/schema/").content.decode()
    assert "/api/v1/{app}/vehicles" in schema and "Operator Vehicles" in schema


def test_a_vehicle_cannot_sit_at_another_companys_branch(world, uom):
    theirs = world["their_type"]
    other_uom = UOM.objects.create(company=theirs.company, uom_code="NO", uom_name="Number")
    with pytest.raises(IntegrityError), transaction.atomic():
        with connection.cursor() as cursor:
            # The FK is deferred to commit, like device_settings'; check it now.
            cursor.execute("SET CONSTRAINTS vehicle_current_branch_company_fk IMMEDIATE")
        Vehicle.objects.create(company=theirs.company, vehicle_code="X", vehicle_name="X", vehicle_type=theirs,
                               uom=other_uom, current_branch=world["adc1"])
