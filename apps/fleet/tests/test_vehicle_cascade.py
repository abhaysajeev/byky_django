"""The Vehicle drawer's Category -> Brand -> Vehicle Type dropdowns (owner,
22 Nov 2026): Category and Brand only narrow the types, are never saved, and a
system user cannot pair a vehicle with another company's type."""

import re

import pytest

from apps.fleet.models import Vehicle
from apps.fleet.tests import test_masters as masters

world, sign_in, save = masters.world, masters.sign_in, masters.save
VEHICLE = next(m for m in masters.MASTERS if m.model is Vehicle)


@pytest.fixture
def client_in(client, world):
    return sign_in(client, "sara.k", company=world["ours"])


def option(body, select_id, value):
    """The <option value="value"> of <select id="select_id">."""
    select = re.search(rf'<select id="{select_id}"[^>]*>(.*?)</select>', body, re.S)
    assert select, select_id
    found = re.search(rf'<option value="{value}"([^>]*)>', select.group(1))
    assert found, (select_id, value)
    return found.group(1)


def test_the_drawer_chains_category_brand_and_type(client_in, world):
    our = world["our"]
    body = client_in.get("/fleet/vehicle/list/").content.decode()

    assert re.search(r'<select id="category"[^>]*data-filter-only="true"', body)
    assert re.search(r'<select id="brand"[^>]*data-parents="company category"', body)
    assert re.search(r'<select id="vehicle_type"[^>]*data-parents="company category brand"', body)
    attrs = option(body, "vehicle_type", our["vehicle_type"].pk)
    assert f'data-parent-category="{our["category"].pk}"' in attrs
    assert f'data-parent-brand="{our["brand"].pk}"' in attrs
    assert f'data-parent-category="{our["category"].pk}"' in option(body, "brand", our["brand"].pk)
    assert "All categories" in body and "Pick a category and brand" in body


def test_category_and_brand_are_not_saved(client_in, world):
    our = world["our"]
    payload = {**VEHICLE.payload(our), "category": our["category"].pk, "brand": our["brand"].pk}
    assert save(client_in, VEHICLE, payload).status_code == 200
    vehicle = Vehicle.objects.get(vehicle_code=payload["vehicle_code"])
    assert vehicle.vehicle_type == our["vehicle_type"]


def test_a_system_user_cannot_pair_a_company_with_anothers_type(client, world):
    system = sign_in(client, "platform.admin", scope=masters.UserScope.SYSTEM)
    payload = {**VEHICLE.payload(world["our"]), "company": world["ours"].pk,
               "vehicle_type": world["their"]["vehicle_type"].pk}
    refused = save(system, VEHICLE, payload)
    assert refused.status_code == 400
    assert any("another company" in e["message"] for e in refused.json()["errors"])


def test_a_drawer_without_parents_renders_as_before(client_in):
    body = client_in.get("/fleet/vehicle-type/list/").content.decode()
    assert re.search(r'<select id="category" class="byky-input byky-select" data-field="category">', body)
