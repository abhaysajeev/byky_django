"""Warehouse service (owner, 29 Oct 2026): an issue type that needs warehouse
service -- vehicle issue types only -- keeps a reported vehicle off rent once a
manager approves the request, until it is rejected or back in service. Also the
operator app's issue-types/create, and the station-sync push for service
requests."""

import pytest
from django.db import IntegrityError, transaction

from apps.fleet import service_requests
from apps.fleet.forms import IssueTypeForm
from apps.fleet.models import IssueTarget, IssueType, ServiceRequest
from apps.fleet.tests import test_service_requests as reports
from apps.rental.tests import test_order_api as orders
from apps.rental.tests import test_station_sync as sync_tests
from apps.rental.tests import test_test_rides as rides
from apps.rental.tests.test_order_change import ADD, add, mo50
from core.models import User

call = orders.call
world, token, fresh_throttle, shop = orders.world, orders.token, orders.fresh_throttle, orders.shop
manager = reports.manager
fcm, station = sync_tests.fcm, sync_tests.station
allowed = rides.allowed
_ = mo50
CREATE = "/api/v1/operator/issue-types/create"


@pytest.fixture
def workshop(world):
    return IssueType.objects.create(company=world["company"], name="Frame cracked", applies_to=IssueTarget.VEHICLE,
                                    needs_warehouse_service=True)


def report_with(client, token, vehicle, issue_type):
    return reports.reported(client, token, reports.vehicle_report(vehicle, issue_type_id=issue_type.pk))


def decide(client, manager, url, row):
    body = call(client, url, {"request_sync_id": row["request_sync_id"]}, token=manager).json()
    assert body["code"] == "ok", body
    return body["data"]


# -- The flag ----------------------------------------------------------------------------


def test_only_a_vehicle_issue_type_may_need_warehouse_service(world):
    for target in (IssueTarget.ASSET, IssueTarget.BOTH):
        with pytest.raises(IntegrityError), transaction.atomic():
            IssueType.objects.create(company=world["company"], name=f"Bad {target}", applies_to=target,
                                     needs_warehouse_service=True)


def test_the_web_form_says_so_in_words(world):
    admin = User.objects.create_user("ops.admin", "x", company=world["company"])
    data = {"name": "Camera broken", "code": "", "applies_to": "both", "needs_warehouse_service": "on",
            "description": "", "is_active": "on"}

    form = IssueTypeForm(data=data, user=admin)

    assert not form.is_valid()
    assert "vehicle issue types only" in str(form.errors["needs_warehouse_service"])
    assert IssueTypeForm(data={**data, "applies_to": "vehicle"}, user=admin).is_valid()


def test_the_request_copies_the_flag_when_reported(client, world, token, shop, workshop):
    row = report_with(client, token, shop["mo41"], workshop)
    workshop.needs_warehouse_service = False
    workshop.save()                                     # editing the type later changes nothing open

    assert ServiceRequest.objects.get(pk=row["request_sync_id"]).needs_warehouse_service is True
    assert row["needs_warehouse_service"] is True


# -- Off rent once approved ----------------------------------------------------------------


def test_pending_does_not_block_approved_does_until_back_in_service(client, world, token, shop, manager, workshop):
    row = report_with(client, token, shop["mo41"], workshop)
    vehicle = reports.vehicle_row(client, token, shop["mo41"])
    assert (vehicle["under_maintenance"], vehicle["warehouse_service"], vehicle["can_rent"]) == (True, False, True)

    decide(client, manager, reports.APPROVE, row)

    vehicle = reports.vehicle_row(client, token, shop["mo41"])
    assert (vehicle["warehouse_service"], vehicle["can_rent"]) == (True, False)
    body = call(client, orders.BOOK, orders.booking(world, shop), token=token).json()
    assert body["code"] == "vehicle_in_warehouse_service"
    assert row["request_no"] in body["message"]

    req = ServiceRequest.objects.get(pk=row["request_sync_id"])
    service_requests.complete(req.decided_by, req.pk, [world["company"].pk], "Frame welded")
    vehicle = reports.vehicle_row(client, token, shop["mo41"])
    assert (vehicle["warehouse_service"], vehicle["can_rent"]) == (False, True)
    assert call(client, orders.BOOK, orders.booking(world, shop), token=token).json()["code"] == "ok"


def test_a_rejected_request_does_not_block(client, world, token, shop, manager, workshop):
    row = report_with(client, token, shop["mo41"], workshop)
    decide(client, manager, reports.REJECT, row)

    assert reports.vehicle_row(client, token, shop["mo41"])["can_rent"] is True


def test_an_ordinary_issue_type_never_blocks(client, world, token, shop, manager):
    plain = IssueType.objects.create(company=world["company"], name="Bell missing", applies_to=IssueTarget.VEHICLE)
    row = report_with(client, token, shop["mo41"], plain)
    decide(client, manager, reports.APPROVE, row)

    vehicle = reports.vehicle_row(client, token, shop["mo41"])
    assert (vehicle["under_maintenance"], vehicle["warehouse_service"], vehicle["can_rent"]) == (True, False, True)


def test_add_vehicle_refuses_it_too(client, world, token, shop, manager, workshop, mo50):
    booked = orders.booking(world, shop)
    booked["items"] = booked["items"][:1]
    assert call(client, orders.BOOK, booked, token=token).json()["code"] == "ok"
    decide(client, manager, reports.APPROVE, report_with(client, token, mo50, workshop))

    body = call(client, ADD, add(booked, mo50, shop), token=token).json()

    assert body["code"] == "vehicle_in_warehouse_service"


def test_a_test_ride_refuses_it_too(client, world, token, shop, manager, workshop, allowed):
    decide(client, manager, reports.APPROVE, report_with(client, token, shop["mo41"], workshop))

    body = call(client, rides.START, rides.ride(world, shop), token=token).json()

    assert (body["code"], body["message"]) == ("vehicle_in_warehouse_service", "That vehicle is in warehouse service.")


def test_a_vehicle_out_on_rent_keeps_its_rental(client, world, token, shop, manager, workshop):
    booked = orders.booking(world, shop)
    assert call(client, orders.BOOK, booked, token=token).json()["code"] == "ok"

    decide(client, manager, reports.APPROVE, report_with(client, token, shop["mo41"], workshop))

    vehicle = reports.vehicle_row(client, token, shop["mo41"])
    assert (vehicle["on_rent"], vehicle["warehouse_service"], vehicle["can_rent"]) == (True, True, False)


# -- issue-types/create -------------------------------------------------------------------


def test_the_tablet_adds_an_issue_type_and_can_pick_it_at_once(client, world, token, shop):
    body = call(client, CREATE, {"name": "  Seat   torn ", "applies_to": "vehicle",
                                 "needs_warehouse_service": True}, token=token).json()

    assert body["code"] == "ok" and body["data"]["created"] is True
    created = IssueType.objects.get(pk=body["data"]["issue_type_id"])
    assert (created.name, created.is_active, created.needs_warehouse_service) == ("Seat torn", True, True)
    assert created.created_by.username == "opr001"
    listed = call(client, "/api/v1/operator/issue-types", {}, token=token).json()["data"]["issue_types"]
    assert "Seat torn" in [t["name"] for t in listed]
    row = report_with(client, token, shop["mo41"], created)
    assert row["needs_warehouse_service"] is True


def test_an_existing_name_comes_back_instead_of_an_error(client, world, token, workshop):
    body = call(client, CREATE, {"name": "frame CRACKED", "applies_to": "asset"}, token=token).json()

    assert body["code"] == "ok" and body["data"]["created"] is False
    assert body["data"]["issue_type_id"] == workshop.pk and IssueType.objects.count() == 1


@pytest.mark.parametrize(("data", "field"), [
    ({"name": "Lamp", "applies_to": "both", "needs_warehouse_service": True}, "needs_warehouse_service"),
    ({"name": "", "applies_to": "vehicle"}, "name"),
    ({"name": "Lamp", "applies_to": "boat"}, "applies_to"),
])
def test_bad_issue_types_are_refused(client, world, token, data, field):
    body = call(client, CREATE, data, token=token).json()

    assert body["code"] == "invalid_request" and field in body["data"]["errors"]


def test_adding_an_issue_type_is_for_the_operator_app(client, world, manager):
    body = call(client, "/api/v1/manager/issue-types/create", {"name": "X", "applies_to": "vehicle"},
                token=manager).json()

    assert body["code"] == "wrong_channel"


# -- Station sync ---------------------------------------------------------------------------


def pushes(fcm):
    return [(sent[0], sent[1], sent[3]) for sent in fcm.sent if sent[3]["type"] == "service_request_changed"]


def test_the_other_tablets_hear_of_each_step(client, world, token, shop, manager, station, fcm, workshop,
                                            django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        row = report_with(client, token, shop["mo41"], workshop)
    [(to, title, data)] = pushes(fcm)
    assert (to, title) == ("tok-till-2", f"Service {row['request_no']} reported")   # not the reporting till-1
    assert data == {"type": "service_request_changed", "service_request_id": row["request_sync_id"],
                    "request_no": row["request_no"], "target": "vehicle", "vehicle_id": str(shop["mo41"].pk),
                    "asset_id": "", "action": "raised", "status": "pending", "needs_warehouse_service": "true",
                    "branch_id": str(world["adc1"].pk), "at": data["at"]}

    fcm.sent.clear()
    with django_capture_on_commit_callbacks(execute=True):
        decide(client, manager, reports.APPROVE, row)
    assert [(to, d["action"], d["status"]) for to, _, d in pushes(fcm)] == [("tok-till-2", "approved", "approved")]

    fcm.sent.clear()
    req = ServiceRequest.objects.get(pk=row["request_sync_id"])
    with django_capture_on_commit_callbacks(execute=True):
        service_requests.complete(req.decided_by, req.pk, [world["company"].pk], "Welded")
    assert [(to, d["action"]) for to, _, d in pushes(fcm)] == [("tok-till-2", "completed")]


def test_a_rejection_reaches_the_other_tablets(client, world, token, shop, manager, station, fcm, workshop,
                                              django_capture_on_commit_callbacks):
    row = report_with(client, token, shop["mo41"], workshop)
    fcm.sent.clear()
    with django_capture_on_commit_callbacks(execute=True):
        decide(client, manager, reports.REJECT, row)

    assert [(to, d["action"]) for to, _, d in pushes(fcm)] == [("tok-till-2", "rejected")]
