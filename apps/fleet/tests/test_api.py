"""POST /api/v1/operator/vehicles through HTTP, and vehicle.current_branch
itself: the form's scoping and the database's same-company guard."""

import pytest
from django.db import IntegrityError, connection, transaction

from apps.fare.tests.test_api import call, fresh_throttle, token, world  # noqa: F401 -- fixtures
from apps.fleet.models import UOM, Category, Vehicle
from core.enums import ApprovalStatus

URL = "/api/v1/operator/vehicles"
APPROVED = ApprovalStatus.APPROVED


@pytest.fixture
def uom(world):
    Category.objects.update(approval_status=APPROVED)   # the fare fixture leaves categories pending
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
    [category] = data["categories"]
    assert (category["name"], [t["name"] for t in category["vehicle_types"]]) == ("BYKY", ["Berg", "Monaco"])
    monaco = category["vehicle_types"][1]
    assert monaco["vehicle_type_id"] == world["monaco"].pk and monaco["tax_percentage"] == "5.00"
    assert monaco["vehicles"] == [
        {"vehicle_id": Vehicle.objects.get(vehicle_code="MON-1").pk, "vehicle_code": "MON-1",
         "vehicle_name": "MON-1", "rfid_epc": "E2800", "uom": "Number", "is_available": True},
        {"vehicle_id": Vehicle.objects.get(vehicle_code="MON-2").pk, "vehicle_code": "MON-2",
         "vehicle_name": "MON-2", "rfid_epc": "E2801", "uom": "Number", "is_available": False},
    ]


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
