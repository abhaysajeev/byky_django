"""The manager app's asset masters (owner, 30 Oct 2026): asset-types, assets
across the company with every field and optional filters, and the asset-type
and serial-number filters on asset-checks/list."""

import datetime

import pytest

from apps.crew.models import Employee
from apps.fleet import maintenance_api
from apps.fleet.models import Asset, AssetType, Brand
from apps.fleet.tests import test_asset_checks as checks
from apps.fleet.tests import test_service_requests as reports
from core.enums import ApprovalStatus

call = reports.call
world, token, fresh_throttle, shop = reports.world, reports.token, reports.fresh_throttle, reports.shop
manager = reports.manager

TYPES = "/api/v1/{}/asset-types"
ASSETS = "/api/v1/{}/assets"
APPROVED = ApprovalStatus.APPROVED


@pytest.fixture
def fleet(world, manager):
    company = world["company"]
    cctv = AssetType.objects.create(company=company, asset_type_code="CCTV", asset_type_name="CCTV",
                                    approval_status=APPROVED)
    printer = AssetType.objects.create(company=company, asset_type_code="PRN", asset_type_name="Printer",
                                       approval_status=APPROVED)
    AssetType.objects.create(company=company, asset_type_name="Draft type")                  # not approved
    AssetType.objects.create(company=world["other"], asset_type_name="Theirs", approval_status=APPROVED)
    brand = Brand.objects.create(company=company, brand_name="Hikvision", approval_status=APPROVED)
    keeper = Employee.objects.get(company=company, employee_code="MGR001")

    def asset(code, kind, branch, **fields):
        return Asset.objects.create(company=company, asset_code=code, asset_type=kind, branch=branch,
                                    approval_status=APPROVED, **fields)

    return {
        "cctv": cctv, "printer": printer,
        "cam": asset("CAM-1", cctv, world["adc1"], brand=brand, serial_no="HK2231-778",
                     manufacturer="Hikvision Digital", supplier="Gulf Security", purchase_invoice_no="INV-117",
                     description="Entrance camera", custodian=keeper,
                     warranty_from_date=datetime.date(2024, 3, 1), warranty_to_date=datetime.date(2027, 2, 28)),
        "away": asset("CAM-9", cctv, world["adc2"], serial_no="hk9000"),
        "printer_asset": asset("PRN-1", printer, world["adc1"]),
        "retired": asset("CAM-X", cctv, world["adc1"], is_active=False),
    }


def manager_assets(client, manager, **filters):
    body = call(client, ASSETS.format("manager"), filters, token=manager).json()
    assert body["code"] == "ok", body
    return body["data"]


def test_the_asset_types_of_this_company_approved_and_active(client, manager, fleet):
    body = call(client, TYPES.format("manager"), {}, token=manager).json()

    assert body["code"] == "ok"
    assert [t["asset_type_name"] for t in body["data"]["asset_types"]] == ["CCTV", "Printer"]
    assert body["data"]["asset_types"][0] == {"asset_type_id": fleet["cctv"].pk, "asset_type_code": "CCTV",
                                              "asset_type_name": "CCTV", "description": ""}


def test_with_no_filters_every_active_asset_of_the_company(client, manager, fleet):
    data = manager_assets(client, manager)

    assert [a["asset_code"] for a in data["assets"]] == ["CAM-1", "CAM-9", "PRN-1"]
    assert (data["page"], data["pages"], data["total"]) == (1, 1, 3)


def test_each_asset_carries_every_field(client, world, manager, fleet):
    [cam] = [a for a in manager_assets(client, manager)["assets"] if a["asset_code"] == "CAM-1"]

    assert cam == {
        "asset_id": fleet["cam"].pk, "asset_code": "CAM-1", "asset_type_id": fleet["cctv"].pk,
        "asset_type_code": "CCTV", "asset_type": "CCTV", "brand_id": fleet["cam"].brand_id, "brand": "Hikvision",
        "serial_no": "HK2231-778", "manufacturer": "Hikvision Digital", "supplier": "Gulf Security",
        "purchase_invoice_no": "INV-117", "description": "Entrance camera",
        "warranty_from_date": "2024-03-01", "warranty_to_date": "2027-02-28",
        "custodian_id": fleet["cam"].custodian_id, "custodian_code": "MGR001", "custodian_name": "Meera",
        "branch_id": world["adc1"].pk, "station": world["adc1"].name,
        "state_id": world["adc1"].location.state_id, "state": world["adc1"].location.state.name,
        "open_request_no": None, "last_check_status": None, "last_checked_at": None,
    }
    assert set(cam) == set(maintenance_api._MANAGER_ASSET_ROW)


@pytest.mark.parametrize("filters, codes", [
    ({"branch_id": "adc2"}, ["CAM-9"]),
    ({"asset_type_id": "printer"}, ["PRN-1"]),
    ({"serial_no": "hk"}, ["CAM-1", "CAM-9"]),                 # any part, ignoring case
    ({"serial_no": "2231"}, ["CAM-1"]),
    ({"serial_no": "HK", "branch_id": "adc1"}, ["CAM-1"]),     # filters combine
    ({"serial_no": "", "state_id": None}, ["CAM-1", "CAM-9", "PRN-1"]),   # empty means no filter
    ({"state_id": 999}, []),
])
def test_every_filter_is_optional_and_they_combine(client, world, manager, fleet, filters, codes):
    ids = {"adc1": world["adc1"].pk, "adc2": world["adc2"].pk, "printer": fleet["printer"].pk}
    filters = {k: ids.get(v, v) if isinstance(v, str) and k != "serial_no" else v for k, v in filters.items()}

    assert [a["asset_code"] for a in manager_assets(client, manager, **filters)["assets"]] == codes


def test_the_operator_assets_reply_is_unchanged(client, token, fleet):
    listed = call(client, ASSETS.format("operator"), {}, token=token).json()["data"]["assets"]

    assert [a["asset_code"] for a in listed] == ["CAM-1", "PRN-1"]          # this station only
    assert set(listed[0]) == set(maintenance_api._ASSETS_SAMPLE["assets"][0])


def test_asset_checks_filter_by_asset_type_and_serial_no(client, token, manager, fleet):
    checks.checked(client, token, fleet["cam"])
    checks.checked(client, token, fleet["printer_asset"])
    url = checks.LIST.format("manager")

    def codes(**filters):
        return [c["asset_code"] for c in call(client, url, filters, token=manager).json()["data"]["checks"]]

    assert sorted(codes()) == ["CAM-1", "PRN-1"]
    assert codes(asset_type_id=fleet["printer"].pk) == ["PRN-1"]
    assert codes(serial_no="hk2231") == ["CAM-1"]
    assert codes(serial_no="nothing-like-it") == []


@pytest.mark.parametrize("url", [TYPES, ASSETS])
def test_the_masters_need_the_right_app(client, token, manager, fleet, url):
    assert call(client, TYPES.format("operator"), {}, token=token).json()["code"] == "wrong_channel"
    assert call(client, url.format("manager"), {}, token=token).json()["code"] == "wrong_channel"
