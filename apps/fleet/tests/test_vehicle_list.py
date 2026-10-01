"""The Vehicle list at fleet scale: searched, filtered and paged by the server,
and exported whole as CSV."""

import csv
import io
import re

import pytest

from apps.fleet.models import Vehicle
from apps.fleet.tests import test_masters as masters
from apps.portal.models import RolePermission
from core.ordering import recent_first

# The two companies (and their branches, types, UOMs) from the masters tests.
world, sign_in = masters.world, masters.sign_in
URL, EXPORT = "/fleet/vehicle/list/", "/fleet/vehicle/export/"


@pytest.fixture
def fleet(world):
    """60 of ours -- two types, half at a branch, some unavailable or inactive -- and 5 of theirs."""
    our, their = world["our"], world["their"]
    other_type = masters.VehicleType.objects.create(company=world["ours"], category=our["category"],
                                                    brand=our["brand"], vehicle_type_name="Ours Chopper")
    Vehicle.objects.bulk_create([
        Vehicle(company=world["ours"], vehicle_code=f"V{n:03d}", vehicle_name=f"Veh {n}",
                vehicle_type=other_type if n % 3 == 0 else our["vehicle_type"], uom=our["uom"],
                rfid_epc=f"TAG{n:03d}", current_branch=our["branch"] if n % 2 == 0 else None,
                is_available=n % 5 != 0, is_active=n % 7 != 0)
        for n in range(60)
    ])
    Vehicle.objects.bulk_create([
        Vehicle(company=world["theirs"], vehicle_code=f"T{n}", vehicle_name=f"Theirs {n}",
                vehicle_type=their["vehicle_type"], uom=their["uom"]) for n in range(5)
    ])
    return other_type


@pytest.fixture
def client_in(client, world):
    return sign_in(client, "sara.k", company=world["ours"])


def names(response):
    return [line.split("<td>")[1].split("</td>")[0] for line in response.content.decode().splitlines()
            if "<td>Veh " in line]


def test_the_list_is_paged_by_the_server_fifty_at_a_time(client_in, fleet):
    first = client_in.get(URL)
    body = first.content.decode()
    assert len(names(first)) == 50 and "Showing 1–50 of 60" in body and "60 vehicles" in body
    assert "data-scr-server-paged" in body and "Theirs" not in body
    second = client_in.get(URL + "?page=2")
    assert len(names(second)) == 10 and "Showing 51–60 of 60" in second.content.decode()
    assert set(names(first)).isdisjoint(names(second))
    # The last saved first.
    ids = list(recent_first(Vehicle.objects.filter(company__short_code="BYKY"))
               .values_list("vehicle_name", flat=True))
    assert names(first) + names(second) == ids


@pytest.mark.parametrize("query,expected", [
    ("q=VEH%201", {n for n in range(60) if str(n).startswith("1")}),              # name
    ("q=TAG007", {7}),                                                            # RFID tag
    ("q=ours%20station", {n for n in range(60) if n % 2 == 0}),                   # branch name
    ("branch=none", {n for n in range(60) if n % 2}),
    ("available=no", {n for n in range(60) if n % 5 == 0}),
    ("status=inactive", {n for n in range(60) if n % 7 == 0}),
    ("available=yes&status=active&branch=none",
     {n for n in range(60) if n % 2 and n % 5 and n % 7}),
])
def test_search_and_filters_combine(client_in, fleet, query, expected):
    found = client_in.get(f"{URL}?{query}")
    got = {int(name.split()[1]) for name in names(found)}
    if len(expected) <= 50:
        assert got == expected
    assert f"{len(expected)} vehicle" in found.content.decode()


def test_filter_by_type_and_by_identifier(client_in, fleet):
    choppers = client_in.get(f"{URL}?type={fleet.pk}")
    assert {int(n.split()[1]) for n in names(choppers)} == {n for n in range(60) if n % 3 == 0}
    one = Vehicle.objects.get(vehicle_code="V042")
    assert names(client_in.get(f"{URL}?q={one.identifier}")) == ["Veh 42"]


def test_paging_keeps_the_filters(client_in, fleet):
    body = client_in.get(f"{URL}?status=active").content.decode()      # 51 active: two pages
    assert "status=active&amp;page=2" in body
    assert "Clear" in body and 'value="active" selected' in body


def test_another_companys_vehicles_never_appear_whatever_the_search(client_in, fleet):
    for query in ("q=Theirs", "q=T1", "branch=none"):
        body = client_in.get(f"{URL}?{query}").content.decode()     # the search term itself is echoed back
        assert not re.search(r"Theirs \d", body), query
        assert "0 vehicles" in body or query == "branch=none"


def export_rows(response):
    text = b"".join(response.streaming_content).decode("utf-8-sig")
    return list(csv.reader(io.StringIO(text)))


def test_the_export_is_every_filtered_vehicle_not_one_page(client_in, fleet):
    response = client_in.get(EXPORT)
    assert response["Content-Type"].startswith("text/csv") and "vehicles-" in response["Content-Disposition"]
    rows = export_rows(response)
    assert rows[0][0] == "Vehicle Identifier" and len(rows) == 1 + 60          # all pages, ours only
    assert all(row[0].startswith("VB") for row in rows[1:])
    unavailable = export_rows(client_in.get(f"{EXPORT}?available=no"))
    assert len(unavailable) == 1 + len([n for n in range(60) if n % 5 == 0])
    assert {row[7] for row in unavailable[1:]} == {"No"}


def test_export_needs_print(client_in, fleet):
    RolePermission.objects.filter(role=client_in.role, page__code="fleet.vehicle").update(can_print=False)
    assert client_in.get(EXPORT).status_code == 403
    assert "Export" not in client_in.get(URL).content.decode().split("scr-head-actions")[1].split("</div>")[0]
