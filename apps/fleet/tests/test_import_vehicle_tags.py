"""manage.py import_vehicle_tags: the client's sheet sets each vehicle's RFID
tag, branch and active flag -- all or nothing, dry run unless --commit."""

import openpyxl
import pytest
from django.core.management import CommandError, call_command

from apps.company.models import Branch, BranchType, Company, Country, Location, State
from apps.fleet.management.commands.import_vehicle_tags import COLUMNS, SHEET
from apps.fleet.models import UOM, Brand, Category, Vehicle, VehicleType


@pytest.fixture
def fleet(db):
    uae = Country.objects.create(short_code="AE", name="United Arab Emirates")
    dubai = State.objects.create(country=uae, short_code="DXB", name="Dubai")
    company = Company.objects.create(short_code="BYKY", name="BYKY", country=uae, state=dubai,
                                     phone_number="+9710000000", email="ops@byky.test")
    location = Location.objects.create(country=uae, state=dubai, short_code="DXB", name="Dubai")

    def branch(code, name):
        return Branch.objects.create(company=company, location=location, short_code=code, name=name,
                                     branch_type=BranchType.STATION)

    byky = Category.objects.create(company=company, category_code="BYKY", category_name="BYKY")
    event = Category.objects.create(company=company, category_code="EVENT", category_name="event")
    brand = Brand.objects.create(company=company, brand_code="BERG", brand_name="BERG")
    uom = UOM.objects.create(company=company, uom_code="NO", uom_name="Number")

    def vtype(category, name):
        return VehicleType.objects.create(company=company, category=category, brand=brand, vehicle_type_name=name)

    types = {"monaco": vtype(byky, "Monaco"), "baby": vtype(byky, "Baby"), "event_monaco": vtype(event, "monaco")}

    def vehicle(code, name, vehicle_type, rfid="", active=True):
        return Vehicle.objects.create(company=company, vehicle_code=code, vehicle_name=name, vehicle_type=vehicle_type,
                                      uom=uom, rfid_epc=rfid, is_active=active)

    return {
        "company": company,
        "creek": branch("ck1", "creek park 1"), "zabeel": branch("za1", "ZABEEL PARK 1"),
        "mo1": vehicle("MO1", "MO 1", types["monaco"], rfid="2001"),
        "mo2": vehicle("MO2", "MO 2", types["monaco"], rfid="2002", active=False),
        "cy1": vehicle("CY01", "CY 01", types["baby"], rfid="CY01"),
        "event_mo1": vehicle("E-MO1", "MO 1", types["event_monaco"], rfid="9001"),   # same name, other type
        "untouched": vehicle("MO9", "MO 9", types["monaco"], rfid="2009"),
    }


def workbook(tmp_path, rows, *, sheet=SHEET, columns=COLUMNS):
    book = openpyxl.Workbook()
    book.active.title = sheet
    book.active.append(list(columns))
    for row in rows:
        book.active.append(list(row))
    path = tmp_path / "rms.xlsx"
    book.save(path)
    return str(path)


def run(path, *, commit=True, company="BYKY"):
    args = [path, "--company", company] + (["--commit"] if commit else [])
    call_command("import_vehicle_tags", *args)


GOOD = [
    ("creek park 1", "BYKY", "Monaco", "MO 1", 17348246),          # digits only: Excel keeps a number
    ("ZABEEL PARK 1", "BYKY", "Monaco", "mo  2", "170732ea"),      # hex text; spacing/case in the name
    ("Creek Park 1", "byky", "baby", "CY 01", 35475034),
]


def test_each_listed_vehicle_gets_its_tag_branch_and_is_made_active(tmp_path, fleet, capsys):
    run(workbook(tmp_path, GOOD))

    for key, tag, branch in (("mo1", "17348246", "creek"), ("mo2", "170732EA", "zabeel"), ("cy1", "35475034", "creek")):
        vehicle = Vehicle.objects.get(pk=fleet[key].pk)
        assert (vehicle.rfid_epc, vehicle.current_branch, vehicle.is_active) == (tag, fleet[branch], True), key
    assert "made active: 1" in capsys.readouterr().out                      # MO 2 was inactive
    # Not in the sheet: exactly as it was -- including the event "MO 1".
    for key in ("untouched", "event_mo1"):
        vehicle = Vehicle.objects.get(pk=fleet[key].pk)
        assert (vehicle.rfid_epc, vehicle.current_branch_id) == (fleet[key].rfid_epc, None), key


def test_a_dry_run_reports_and_writes_nothing(tmp_path, fleet, capsys):
    run(workbook(tmp_path, GOOD), commit=False)
    out = capsys.readouterr().out
    assert "3 vehicles: RFID tag and branch set." in out and "Dry run" in out
    assert Vehicle.objects.get(pk=fleet["mo1"].pk).rfid_epc == "2001"
    assert not Vehicle.objects.filter(current_branch__isnull=False).exists()


def test_the_type_decides_between_vehicles_of_the_same_name(tmp_path, fleet):
    run(workbook(tmp_path, [("creek park 1", "event", "monaco", "MO 1", 12345678)]))
    assert Vehicle.objects.get(pk=fleet["event_mo1"].pk).rfid_epc == "12345678"
    assert Vehicle.objects.get(pk=fleet["mo1"].pk).rfid_epc == "2001"


def test_the_active_vehicle_wins_over_an_inactive_one_of_the_same_name(tmp_path, fleet):
    old = Vehicle.objects.create(company=fleet["company"], vehicle_code="CY01-OLD", vehicle_name="CY 01",
                                 vehicle_type=fleet["cy1"].vehicle_type, uom=fleet["cy1"].uom, is_active=False)
    run(workbook(tmp_path, [("creek park 1", "BYKY", "Baby", "CY 01", 35475034)]))
    assert Vehicle.objects.get(pk=fleet["cy1"].pk).rfid_epc == "35475034"
    assert Vehicle.objects.get(pk=old.pk).rfid_epc == ""


def test_an_old_barcode_equal_to_a_new_tag_is_cleared(tmp_path, fleet, capsys):
    run(workbook(tmp_path, [("creek park 1", "BYKY", "Monaco", "MO 1", 2009)]))   # MO 9 holds "2009"
    assert Vehicle.objects.get(pk=fleet["mo1"].pk).rfid_epc == "2009"
    assert Vehicle.objects.get(pk=fleet["untouched"].pk).rfid_epc == ""
    assert "old barcode cleared from 1 other vehicle(s): MO9" in capsys.readouterr().out


def test_two_listed_vehicles_may_swap_values(tmp_path, fleet):
    run(workbook(tmp_path, [("creek park 1", "BYKY", "Monaco", "MO 1", 2002),
                            ("creek park 1", "BYKY", "Monaco", "MO 2", 2001)]))
    assert (Vehicle.objects.get(pk=fleet["mo1"].pk).rfid_epc, Vehicle.objects.get(pk=fleet["mo2"].pk).rfid_epc) \
        == ("2002", "2001")


def test_every_unresolved_row_is_listed_and_nothing_is_written(tmp_path, fleet, capsys):
    rows = [
        GOOD[0],
        ("creek park 1", "BYKY", "Monaco", "MO 404", 11111111),          # no such vehicle
        ("creek park 1", "BYKY", "Baby", "MO 2", 22222222),              # wrong type for MO 2
        ("Nowhere Park", "BYKY", "Baby", "CY 01", 33333333),             # unknown branch
        ("creek park 1", "event", "monaco", "MO 1", 17348246),           # tag repeated in the sheet
        ("creek park 1", "BYKY", "Monaco", "MO 9", None),                # no tag
    ]
    with pytest.raises(CommandError, match="nothing was written"):
        run(workbook(tmp_path, rows))
    err = capsys.readouterr().err
    for text in ("no vehicle of that name", "is BYKY/Monaco here, not BYKY/Baby", "'Nowhere Park' not found",
                 "tag 17348246 appears 2 times", "no RFTagID"):
        assert text in err, text
    assert Vehicle.objects.get(pk=fleet["mo1"].pk).rfid_epc == "2001"
    assert not Vehicle.objects.filter(current_branch__isnull=False).exists()


def test_the_same_vehicle_twice_is_refused(tmp_path, fleet, capsys):
    with pytest.raises(CommandError):
        run(workbook(tmp_path, [GOOD[0], ("ZABEEL PARK 1", "BYKY", "Monaco", "MO 1", 99999999)]))
    assert "already on another line" in capsys.readouterr().err


@pytest.mark.parametrize("kwargs,message", [
    ({"sheet": "Vehicles"}, "No sheet named"),
    ({"columns": ("Branch", "Vehicle", "RFTagID")}, "lacks column(s): Category, VehicleType"),
])
def test_a_workbook_of_the_wrong_shape_is_refused(tmp_path, fleet, kwargs, message):
    with pytest.raises(CommandError, match=message.replace("(", r"\(").replace(")", r"\)")):
        run(workbook(tmp_path, [], **kwargs))


def test_an_unknown_company_is_refused(tmp_path, fleet):
    with pytest.raises(CommandError, match="No company"):
        run(workbook(tmp_path, GOOD), company="NOPE")
