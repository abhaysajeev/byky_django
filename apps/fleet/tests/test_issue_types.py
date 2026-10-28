"""Issue types (apps/fleet IssueType): a web master; the operator app lists the
active ones and a service request names one (owner, 27 Oct 2026)."""

import pytest
from django.db import IntegrityError, transaction

from apps.company.models import Company
from apps.fleet.models import IssueTarget, IssueType, ServiceRequest
from apps.fleet.tests import test_image_shares as shares
from apps.fleet.tests import test_service_requests as reports
from apps.portal.models import RolePermission
from apps.rental.tests import test_order_api as orders

call = orders.call
world, token, fresh_throttle, shop = orders.world, orders.token, orders.fresh_throttle, orders.shop
manager, assets = reports.manager, reports.assets

TYPES = "/api/v1/operator/issue-types"


@pytest.fixture
def types(world):
    company = world["company"]

    def kind(name, applies_to=IssueTarget.BOTH, **extra):
        return IssueType.objects.create(company=company, name=name, applies_to=applies_to, **extra)

    return {"brake": kind("Brake failure", IssueTarget.VEHICLE), "offline": kind("Offline", IssueTarget.ASSET),
            "damage": kind("Damage"), "old": kind("Retired type", is_active=False)}


def test_the_tablet_lists_active_types_optionally_for_one_target(client, world, token, types):
    names = [t["name"] for t in call(client, TYPES, {}, token=token).json()["data"]["issue_types"]]
    assert names == ["Brake failure", "Damage", "Offline"]                    # active, by name
    vehicle = call(client, TYPES, {"target": "vehicle"}, token=token).json()["data"]["issue_types"]
    assert [t["name"] for t in vehicle] == ["Brake failure", "Damage"]
    assert vehicle[0] == {"issue_type_id": types["brake"].pk, "name": "Brake failure", "code": "",
                          "applies_to": "vehicle"}
    assert call(client, TYPES, {"target": "boat"}, token=token).json()["code"] == "invalid_request"


def test_the_issue_types_call_is_for_the_operator_app(client, world, manager, types):
    assert call(client, "/api/v1/manager/issue-types", {}, token=manager).json()["code"] == "wrong_channel"


def test_names_are_unique_per_company_ignoring_case(world, types):
    with pytest.raises(IntegrityError), transaction.atomic():
        IssueType.objects.create(company=world["company"], name="brake FAILURE")


def test_a_report_with_a_type_and_no_text(client, world, token, shop, types):
    data = reports.vehicle_report(shop["mo41"], issue_type_id=types["brake"].pk)
    del data["issue"]
    row = reports.reported(client, token, data)
    assert (row["issue_type_id"], row["issue_type"], row["issue"]) == (types["brake"].pk, "Brake failure", "")
    assert ServiceRequest.objects.get().issue_type == types["brake"]


def test_a_report_with_a_type_and_details(client, world, token, assets, types):
    row = reports.reported(client, token, reports.asset_report(assets["cam"], issue_type_id=types["damage"].pk))
    assert (row["issue_type"], row["issue"]) == ("Damage", "Camera offline")


@pytest.mark.parametrize("pick,code", [
    (lambda t: t["offline"].pk, "issue_type_not_for_target"),                  # an asset type on a vehicle
    (lambda t: t["old"].pk, "unknown_issue_type"),                             # inactive
    (lambda t: 999999, "unknown_issue_type"),
])
def test_a_wrong_type_is_refused(client, world, token, shop, types, pick, code):
    data = reports.vehicle_report(shop["mo41"], issue_type_id=pick(types))
    assert call(client, reports.REPORT, data, token=token).json()["code"] == code
    assert ServiceRequest.objects.count() == 0


def test_another_companys_type_is_unknown(client, world, token, shop, types):
    mine = world["company"]
    other = Company.objects.create(short_code="ZZQ", name="Other Co", country=mine.country, state=mine.state)
    foreign = IssueType.objects.create(company=other, name="Brake failure")
    data = reports.vehicle_report(shop["mo41"], issue_type_id=foreign.pk)
    assert call(client, reports.REPORT, data, token=token).json()["code"] == "unknown_issue_type"


def test_neither_type_nor_text_is_invalid(client, world, token, shop):
    data = reports.vehicle_report(shop["mo41"], issue="  ")
    assert call(client, reports.REPORT, data, token=token).json()["code"] == "invalid_request"


def test_the_manager_row_names_the_type(client, world, token, shop, manager, types):
    reports.reported(client, token, reports.vehicle_report(shop["mo41"], issue_type_id=types["brake"].pk))
    rows = call(client, reports.MANAGER_LIST, {"kind": "service"}, token=manager).json()["data"]["requests"]
    assert rows[0]["reason"] == "Brake failure -- Front brake not holding"


# -- Web master ----------------------------------------------------------------------------


def test_web_add_edit_and_rights(client, world, types):
    browser = shares.web_client(world)
    page = browser.get("/fleet/issue-type/list/")
    assert page.status_code == 200
    assert len(page.context["issue_types"]) == 4

    saved = browser.post("/fleet/issue-type/save/", {
        "company": world["company"].pk, "name": "Flat tyre", "code": "FT", "applies_to": "vehicle",
        "description": "", "is_active": "on"})
    assert saved.status_code == 200 and saved.json()["ok"] is True, saved.content
    flat = IssueType.objects.get(name="Flat tyre")
    assert (flat.code, flat.applies_to) == ("FT", "vehicle")

    RolePermission.objects.filter(role=browser.role, page__code="fleet.issue_type").update(can_read=False)
    assert browser.get("/fleet/issue-type/list/").status_code == 403


def test_a_type_in_use_cannot_be_deleted(client, world, token, shop, types):
    reports.reported(client, token, reports.vehicle_report(shop["mo41"], issue_type_id=types["brake"].pk))
    browser = shares.web_client(world)
    gone = browser.post(f"/fleet/issue-type/{types['damage'].pk}/delete/", data="{}",
                        content_type="application/json")
    assert gone.status_code == 200 and not IssueType.objects.filter(pk=types["damage"].pk).exists()
    kept = browser.post(f"/fleet/issue-type/{types['brake'].pk}/delete/", data="{}",
                        content_type="application/json")
    assert kept.status_code != 200 or kept.json().get("ok") is not True
    assert IssueType.objects.filter(pk=types["brake"].pk).exists()
