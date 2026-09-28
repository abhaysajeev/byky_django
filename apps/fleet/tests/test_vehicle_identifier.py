"""Vehicle.identifier: VB0001 from a database sequence on every insert,
read-only everywhere, unique, and numbered in a readable order on migration."""

import importlib

import pytest
from django.db import IntegrityError, connection, transaction

from apps.fleet.models import Vehicle
from apps.fleet.tests import test_masters as masters

# The two companies from the fleet masters tests, reused as a fixture here.
world, save, sign_in = masters.world, masters.save, masters.sign_in

migration = importlib.import_module("apps.fleet.migrations.0011_vehicle_identifier")
VEHICLE = next(m for m in masters.MASTERS if m.model is Vehicle)


def make(world, code, **fields):
    refs = world["our"]
    return Vehicle.objects.create(company=world["ours"], vehicle_code=code, vehicle_name=code,
                                  vehicle_type=refs["vehicle_type"], uom=refs["uom"], **fields)


def test_every_insert_path_takes_the_next_number(world):
    a = make(world, "A")
    refs = world["our"]
    b, c = Vehicle.objects.bulk_create([
        Vehicle(company=world["ours"], vehicle_code=code, vehicle_name=code,
                vehicle_type=refs["vehicle_type"], uom=refs["uom"]) for code in ("B", "C")
    ])
    numbers = [a.identifier_no, b.identifier_no, c.identifier_no]
    assert numbers == sorted(numbers) and len(set(numbers)) == 3          # returned by the insert itself
    a.refresh_from_db()
    assert a.identifier == f"VB{a.identifier_no:04d}"


def test_the_identifier_grows_past_four_digits_instead_of_truncating(world):
    with connection.cursor() as cursor:
        cursor.execute("SELECT setval('vehicle_identifier_seq', 12344)")
    vehicle = make(world, "BIG")
    vehicle.refresh_from_db()
    assert vehicle.identifier == "VB12345"


def test_the_identifier_is_unique(world):
    vehicle = make(world, "A")
    with pytest.raises(IntegrityError), transaction.atomic():
        make(world, "B", identifier_no=vehicle.identifier_no)


def test_the_screen_assigns_it_and_never_takes_it_from_the_form(client, world):
    sign_in(client, "sara.k", company=world["ours"])
    payload = {**VEHICLE.payload(world["our"]), "identifier": "VB9999", "identifier_no": 9999}
    created = save(client, VEHICLE, payload)
    assert created.status_code == 200, created.json()
    vehicle = Vehicle.objects.get(pk=created.json()["pk"])
    assert vehicle.identifier != "VB9999" and vehicle.identifier_no != 9999
    before = vehicle.identifier
    save(client, VEHICLE, {**payload, "pk": vehicle.pk, "vehicle_name": "Renamed"})
    vehicle.refresh_from_db()
    assert (vehicle.vehicle_name, vehicle.identifier) == ("Renamed", before)
    page = client.get("/fleet/vehicle/list/").content.decode()
    assert before in page and 'data-field="identifier"' in page and "Assigned on save" in page


def test_existing_vehicles_are_numbered_by_category_type_then_name():
    """The migration's order: numbers inside names sort as numbers."""
    names = ["MO 10", "mo 2", "MO 07", "MO 1"]
    assert sorted(names, key=migration.natural) == ["MO 1", "mo 2", "MO 07", "MO 10"]
    assert migration.natural("BYKY") < migration.natural("event")        # case-insensitive
