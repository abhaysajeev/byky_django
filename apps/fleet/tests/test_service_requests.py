"""Service & Maintenance (apps/fleet/service_requests.py): the operator app
reports a vehicle or a station asset; a reported vehicle cannot be rented from
that moment; a manager approves or rejects (manager app or web); the web puts it
back in service."""

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.company.models import Branch, BranchType
from apps.fleet.models import Asset, AssetType, ServiceRequest, ServiceRequestStatus
from apps.notifications.models import Action, Audience, Notification
from apps.photos.tests.test_photos import uploaded
from apps.portal.models import Role, RolePermission
from apps.portal.session_models import AppSession
from apps.rental.tests import test_order_api as orders
from apps.rental.tests import test_order_change as changes
from apps.rental.tests import test_order_requests as requests_tests
from core.enums import ApprovalStatus, Channel
from core.ids import uuid7
from core.models import User

call, booking = orders.call, orders.booking
world, token, fresh_throttle, shop = orders.world, orders.token, orders.fresh_throttle, orders.shop
manager = requests_tests.manager

REPORT = "/api/v1/operator/service-requests"
MINE = "/api/v1/operator/service-requests/list"
ASSETS = "/api/v1/operator/assets"
VEHICLES = "/api/v1/operator/vehicles"
APPROVE = "/api/v1/manager/service-requests/approve"
REJECT = "/api/v1/manager/service-requests/reject"
MANAGER_LIST = "/api/v1/manager/requests/list"
COUNTS = "/api/v1/manager/requests/pending-counts"


@pytest.fixture
def assets(world):
    company = world["company"]
    cctv = AssetType.objects.create(company=company, asset_type_name="CCTV")

    def asset(code, branch):
        return Asset.objects.create(company=company, asset_code=code, asset_type=cctv, branch=branch,
                                    approval_status=ApprovalStatus.APPROVED)

    return {"cam": asset("CAM-1", world["adc1"]), "away": asset("CAM-9", world["adc2"]),
            "nowhere": asset("CAM-0", None)}


def vehicle_report(vehicle, **extra):
    return {"sync_id": str(uuid7()), "target": "vehicle", "vehicle_id": vehicle.pk,
            "issue": "Front brake not holding", "requested_at": "2026-10-26 10:15:00", **extra}


def asset_report(asset, **extra):
    return {"sync_id": str(uuid7()), "target": "asset", "asset_id": asset.pk,
            "issue": "Camera offline", "requested_at": "2026-10-26 10:20:00", **extra}


def reported(client, token, data):
    body = call(client, REPORT, data, token=token).json()
    assert body["code"] == "ok", body
    return body["data"]


def vehicle_row(client, token, vehicle):
    data = call(client, VEHICLES, {}, token=token).json()["data"]
    for category in data["categories"]:
        for vehicle_type in category["vehicle_types"]:
            for row in vehicle_type["vehicles"]:
                if row["vehicle_id"] == vehicle.pk:
                    return row
    raise AssertionError("vehicle not listed")


# -- Report ---------------------------------------------------------------------------


def test_a_vehicle_report_takes_it_off_rent_at_once(client, world, token, shop):
    row = reported(client, token, vehicle_report(shop["mo41"]))
    assert (row["request_no"], row["status"], row["target"], row["vehicle_name"]) == (
        "SRV-ADC1-00001", "pending", "vehicle", "MO 41")
    assert row["has_photo"] is False
    listed = vehicle_row(client, token, shop["mo41"])
    assert (listed["under_maintenance"], listed["service_request_no"], listed["can_rent"]) == (
        True, "SRV-ADC1-00001", False)
    # Booking it is refused on the server, whatever the tablet shows.
    body = call(client, orders.BOOK, booking(world, shop), token=token).json()
    assert body["code"] == "vehicle_under_maintenance"


def test_add_and_replace_refuse_a_reported_vehicle(client, world, token, shop):
    booked = booking(world, shop)
    assert call(client, orders.BOOK, booked, token=token).json()["code"] == "ok"
    reported(client, token, vehicle_report(shop["mo42"]))
    assert call(client, changes.ADD, changes.add(booked, shop["mo42"], shop), token=token).json()["code"] == \
        "vehicle_under_maintenance"
    assert call(client, changes.REPLACE, changes.replace(booked, shop["mo42"], shop), token=token).json()["code"] \
        == "vehicle_under_maintenance"


def test_a_vehicle_out_on_rent_may_be_reported_and_still_comes_back(client, world, token, shop):
    booked = booking(world, shop)
    assert call(client, orders.BOOK, booked, token=token).json()["code"] == "ok"
    reported(client, token, vehicle_report(shop["mo41"]))
    from apps.rental.tests.test_order_return import RETURN, back
    assert call(client, RETURN, back(booked, 0), token=token).json()["code"] == "ok"


def test_an_asset_report_and_the_station_assets(client, world, token, assets):
    listed = call(client, ASSETS, {}, token=token).json()["data"]["assets"]
    assert [a["asset_code"] for a in listed] == ["CAM-1"]                      # this station's only
    row = reported(client, token, asset_report(assets["cam"]))
    assert (row["target"], row["asset_code"], row["asset_type"]) == ("asset", "CAM-1", "CCTV")
    listed = call(client, ASSETS, {}, token=token).json()["data"]["assets"]
    assert listed[0]["open_request_no"] == row["request_no"]


@pytest.mark.parametrize("data,code", [
    (lambda s, a: vehicle_report(s["away"]), "vehicle_not_at_station"),
    (lambda s, a: asset_report(a["away"]), "asset_not_at_station"),
    (lambda s, a: asset_report(a["nowhere"]), "asset_not_at_station"),
    (lambda s, a: {**vehicle_report(s["mo41"]), "vehicle_id": 999999}, "unknown_vehicle"),
    (lambda s, a: {**asset_report(a["cam"]), "asset_id": 999999}, "unknown_asset"),
    (lambda s, a: {**vehicle_report(s["mo41"]), "asset_id": a["cam"].pk}, "invalid_request"),
    (lambda s, a: {**vehicle_report(s["mo41"]), "issue": "  "}, "invalid_request"),
    (lambda s, a: {"sync_id": str(uuid7()), "target": "asset", "issue": "x", "requested_at": "2026-10-26 10:00:00"},
     "invalid_request"),
])
def test_what_is_refused(client, world, token, shop, assets, data, code):
    assert call(client, REPORT, data(shop, assets), token=token).json()["code"] == code


def test_one_open_report_per_target_and_a_resend_is_a_duplicate(client, world, token, shop):
    first = vehicle_report(shop["mo41"])
    reported(client, token, first)
    again = call(client, REPORT, first, token=token).json()
    assert again["code"] == "duplicate" and again["data"]["request_no"] == "SRV-ADC1-00001"
    second = call(client, REPORT, vehicle_report(shop["mo41"]), token=token).json()
    assert second["code"] == "already_reported" and "SRV-ADC1-00001" in second["message"]
    assert ServiceRequest.objects.count() == 1


def test_numbers_count_per_station(client, world, token, shop):
    adc2 = Branch.objects.create(company=world["company"], location=world["adc1"].location, short_code="ZZ9",
                                 name="Other", branch_type=BranchType.STATION)
    a = reported(client, token, vehicle_report(shop["mo41"]))
    b = reported(client, token, vehicle_report(shop["dc02"]))
    assert (a["request_no"], b["request_no"]) == ("SRV-ADC1-00001", "SRV-ADC1-00002")
    from apps.fleet import service_requests
    assert service_requests.request_no(adc2, 1) == "SRV-ZZ9-00001"


def test_a_photo_is_optional_and_must_be_a_service_photo(client, world, token, shop):
    photo_id = uploaded(client, token, purpose="service_request")
    row = reported(client, token, vehicle_report(shop["mo41"], photo_id=photo_id))
    assert row["has_photo"] is True
    card_photo = uploaded(client, token, purpose="card_discount")
    assert call(client, REPORT, vehicle_report(shop["dc02"], photo_id=card_photo), token=token).json()["code"] == \
        "unknown_photo"
    assert call(client, REPORT, vehicle_report(shop["dc02"], photo_id=str(uuid7())), token=token).json()["code"] \
        == "photo_not_uploaded"


# -- Decide ---------------------------------------------------------------------------


def test_reject_puts_the_vehicle_back_on_rent(client, world, token, shop, manager):
    row = reported(client, token, vehicle_report(shop["mo41"]))
    body = call(client, REJECT, {"request_sync_id": row["request_sync_id"], "note": "Brakes fine"},
                token=manager).json()
    assert body["code"] == "ok" and body["data"]["status"] == "rejected"
    assert vehicle_row(client, token, shop["mo41"])["can_rent"] is True
    assert call(client, orders.BOOK, booking(world, shop), token=token).json()["code"] == "ok"


def test_approve_keeps_it_out_until_back_in_service_on_the_web(client, world, token, shop, manager):
    row = reported(client, token, vehicle_report(shop["mo41"]))
    body = call(client, APPROVE, {"request_sync_id": row["request_sync_id"]}, token=manager).json()
    assert body["code"] == "ok" and body["data"]["status"] == "approved"
    again = call(client, REJECT, {"request_sync_id": row["request_sync_id"]}, token=manager).json()
    assert again["code"] == "request_decided"
    assert vehicle_row(client, token, shop["mo41"])["can_rent"] is False

    from apps.fleet import service_requests
    req = ServiceRequest.objects.get(pk=row["request_sync_id"])
    company_ids = [world["company"].pk]
    with pytest.raises(service_requests.ServiceRefused):
        service_requests.complete(req.decided_by, req.pk, company_ids, "  ")         # a note is required
    service_requests.complete(req.decided_by, req.pk, company_ids, "Pads replaced")
    req.refresh_from_db()
    assert req.status == ServiceRequestStatus.COMPLETED and req.completion_note == "Pads replaced"
    assert vehicle_row(client, token, shop["mo41"])["can_rent"] is True


def test_only_an_approved_request_goes_back_in_service(client, world, token, shop):
    from apps.fleet import service_requests
    row = reported(client, token, vehicle_report(shop["mo41"]))
    user = User.objects.filter(company=world["company"]).first()
    with pytest.raises(service_requests.ServiceRefused) as refused:
        service_requests.complete(user, row["request_sync_id"], [world["company"].pk], "Done")
    assert refused.value.code == "request_not_approved"


def test_another_company_cannot_decide(client, world, token, shop, manager):
    row = reported(client, token, vehicle_report(shop["mo41"]))
    from apps.fleet import service_requests
    with pytest.raises(service_requests.ServiceRefused) as refused:
        service_requests.approve(None, row["request_sync_id"], [world["other"].pk], "web")
    assert refused.value.code == "unknown_request"


# -- Lists and notifications ----------------------------------------------------------


def test_managers_see_it_with_every_other_kind(client, world, token, shop, manager):
    row = reported(client, token, vehicle_report(shop["mo41"]))
    data = call(client, MANAGER_LIST, {}, token=manager).json()["data"]
    [listed] = [r for r in data["requests"] if r["kind"] == "service"]
    assert listed["request_sync_id"] == row["request_sync_id"] and listed["order"] is None
    assert (listed["details"]["request_no"], listed["details"]["target"], listed["reason"]) == (
        "SRV-ADC1-00001", "vehicle", "Front brake not holding")
    only = call(client, MANAGER_LIST, {"kind": "service", "pending": True}, token=manager).json()["data"]
    assert [r["kind"] for r in only["requests"]] == ["service"]
    assert call(client, COUNTS, {}, token=manager).json()["data"]["service"] == 1


def test_the_tablet_lists_its_station_reports(client, world, token, shop):
    reported(client, token, vehicle_report(shop["mo41"]))
    reported(client, token, vehicle_report(shop["dc02"]))
    data = call(client, MINE, {"mine": True}, token=token).json()["data"]
    assert data["total"] == 2 and data["requests"][0]["vehicle_name"] == "DC 02"     # newest first
    assert call(client, MINE, {"status": "approved"}, token=token).json()["data"]["total"] == 0


def test_managers_hear_of_a_report_and_the_tablet_of_the_decision(client, world, token, shop, manager):
    AppSession.objects.filter(logged_out_at__isnull=True).update(push_token="tok")
    row = reported(client, token, vehicle_report(shop["mo41"]))
    raised = Notification.objects.filter(subject_id=row["request_sync_id"], action=Action.RAISED)
    assert [n.audience for n in raised] == [Audience.MANAGER]
    assert raised[0].title == "New service request"
    call(client, APPROVE, {"request_sync_id": row["request_sync_id"]}, token=manager)
    decided = Notification.objects.filter(subject_id=row["request_sync_id"], action=Action.APPROVED,
                                          audience=Audience.OPERATOR)
    assert decided.count() == 1 and decided[0].title == "Service request approved"


# -- Database rules -------------------------------------------------------------------


def test_the_database_keeps_one_target_and_one_open_request(world, shop):
    company, branch = world["company"], world["adc1"]

    def make(number, **fields):
        return ServiceRequest.objects.create(id=uuid7(), company=company, branch=branch, number=number,
                                             request_no=f"SRV-ADC1-{number:05d}", issue="x",
                                             requested_at=timezone.now(), **fields)

    make(1, vehicle=shop["mo41"])
    for fields in ({"vehicle": shop["mo41"]}, {}, {"number_clash": True}):
        with pytest.raises(IntegrityError), transaction.atomic():
            if fields.pop("number_clash", False):
                make(1, vehicle=shop["dc02"])                                  # number used at the station
            else:
                make(2, **fields)                                              # second open / no target


# -- Web --------------------------------------------------------------------------------


def test_web_list_detail_and_actions_follow_the_rights(client, world, token, shop):
    row = reported(client, token, vehicle_report(shop["mo41"]))
    from django.test import Client
    browser = Client()
    role = Role.objects.get(company=world["company"], name="Operator")
    User.objects.create_user("sara.k", orders.fare_api.PASSWORD, display_name="Sara K", company=world["company"],
                             role=role, allowed_channels=[Channel.WEB])
    browser.post("/login/", {"username": "sara.k", "password": orders.fare_api.PASSWORD})

    listed = browser.get("/fleet/service-request/list/")
    assert listed.status_code == 200 and [t["count"] for t in listed.context["tabs"]] == [1, 0, 0, 0]
    detail = browser.get(f"/fleet/service-request/{row['request_sync_id']}/")
    assert detail.status_code == 200 and b"SRV-ADC1-00001" in detail.content

    RolePermission.objects.filter(role=role, page__code="fleet.service_request").update(can_approve=False)
    url = f"/fleet/service-request/{row['request_sync_id']}/approve/"
    assert browser.post(url, data="{}", content_type="application/json").status_code == 403
    RolePermission.objects.filter(role=role, page__code="fleet.service_request").update(can_approve=True)
    assert browser.post(url, data="{}", content_type="application/json").json()["ok"] is True

    complete = f"/fleet/service-request/{row['request_sync_id']}/complete/"
    assert browser.post(complete, data='{"note": ""}', content_type="application/json").status_code == 400
    RolePermission.objects.filter(role=role, page__code="fleet.service_request").update(can_update=False)
    assert browser.post(complete, data='{"note": "Fixed"}', content_type="application/json").status_code == 403
    RolePermission.objects.filter(role=role, page__code="fleet.service_request").update(can_update=True)
    assert browser.post(complete, data='{"note": "Fixed"}', content_type="application/json").json()["ok"] is True
    vehicles = browser.get("/fleet/vehicle/list/")
    assert b"Under maintenance" not in vehicles.content
