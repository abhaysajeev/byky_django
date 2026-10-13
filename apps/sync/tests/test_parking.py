"""Parking a refused tablet call, the calls parked behind it, and the office's
replay and dismiss (plan "Sync parking", 13 Oct 2026) -- through HTTP as the
till sends them, on the rental tests' shop."""

import datetime

import pytest
from django.utils import timezone

from apps.crew.tests import test_attendance as attendance
from apps.devices.models import DeviceMapping
from apps.monitoring.models import ErrorLog
from apps.portal.models import Role
from apps.portal.services import grant_all
from apps.portal.session_models import AppSession
from apps.rental import services as rental_services
from apps.rental.models import Order, OrderItem, OrderItemStatus, OrderRequest
from apps.rental.tests import test_order_api as orders
from apps.sync import services
from apps.sync.models import AttemptSource, IssueReason, IssueStatus, SyncIssue
from core.enums import Channel, UserScope
from core.ids import uuid7
from core.models import User

booking, receipt, counter, sign_in_tablet = orders.booking, orders.receipt, orders.counter, orders.sign_in_tablet
world, token, fresh_throttle, shop = orders.world, orders.token, orders.fresh_throttle, orders.shop

BOOK = orders.BOOK
RETURN = "/api/v1/operator/orders/return"
REQUEST = "/api/v1/operator/orders/requests"
WITHDRAW = "/api/v1/operator/orders/requests/withdraw"
DETAIL = "/api/v1/operator/orders/detail"
STATUS = "/api/v1/operator/sync/issues/status"


def send(client, url, request_data, *, token, park=False):
    headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"}
    credentials = {"park": True} if park else {}
    return client.post(url, {"credentials": credentials, "request_data": request_data},
                       content_type="application/json", **headers)


@pytest.fixture
def booked(client, world, token, shop):
    request_data = booking(world, shop)
    assert send(client, BOOK, request_data, token=token).json()["code"] == "ok"
    return request_data


def back(booked, index=0, **overrides):
    data = {
        "sync_id": str(uuid7()), "order_id": booked["sync_id"], "item_id": booked["items"][index]["sync_id"],
        "returned_at": "2026-10-02 17:00:00", "run_minutes": 60, "overtime_amount": "0.00", "total_amount": "50.00",
    }
    data.update(overrides)
    return data


@pytest.fixture
def office(world):
    """A system user, as Sync Issues is for (system-only screen)."""
    role = Role.objects.create(company=None, name="System Administrator")
    grant_all(role)
    return User.objects.create_user("platform.admin", orders.fare_api.PASSWORD, display_name="Platform",
                                    scope=UserScope.SYSTEM, role=role, allowed_channels=[Channel.WEB])


def every_issue():
    return SyncIssue.objects.all()


# -- Parking -----------------------------------------------------------------------------

def test_a_refused_call_sent_with_park_is_kept_and_answered_parked(client, token, booked):
    wrong = back(booked, total_amount="55.00")

    body = send(client, RETURN, wrong, token=token, park=True).json()

    assert body["code"] == "parked", body
    issue = SyncIssue.objects.get()
    assert body["data"] == {
        "issue_id": issue.pk, "sync_id": wrong["sync_id"], "order_id": booked["sync_id"], "waiting_on": None,
        "error": {"code": "amount_mismatch", "message": issue.error_message},
    }
    assert (issue.status, issue.reason, issue.kind, issue.error_code) == (
        IssueStatus.OPEN, IssueReason.REFUSED, "return", "amount_mismatch")
    assert issue.request_data == issue.original_request_data == wrong
    attempt = issue.attempts.get()
    assert (attempt.source, attempt.by, attempt.http_status, attempt.code) == (
        AttemptSource.PARK, None, 400, "amount_mismatch")
    # Nothing was recorded.
    assert OrderItem.objects.get(pk=booked["items"][0]["sync_id"]).status == OrderItemStatus.ACTIVE


def test_without_park_a_refusal_is_answered_as_before(client, token, booked):
    response = send(client, RETURN, back(booked, total_amount="55.00"), token=token)

    assert (response.status_code, response.json()["code"]) == (400, "amount_mismatch")
    assert not every_issue().exists()


def test_a_park_call_that_now_goes_through_is_just_ok(client, token, booked):
    body = send(client, RETURN, back(booked), token=token, park=True).json()

    assert body["code"] == "ok" and not every_issue().exists()


def test_a_parked_call_resent_gets_the_same_parked_and_never_reruns(client, token, booked):
    wrong = back(booked, total_amount="55.00")
    first = send(client, RETURN, wrong, token=token, park=True).json()
    fixed = {**wrong, "total_amount": "50.00"}     # even a corrected resend: fixing is the office's now

    again = send(client, RETURN, fixed, token=token).json()

    assert (again["code"], again["data"]["issue_id"]) == ("parked", first["data"]["issue_id"])
    assert SyncIssue.objects.get().attempts.count() == 1
    assert OrderItem.objects.get(pk=booked["items"][0]["sync_id"]).status == OrderItemStatus.ACTIVE


def test_a_signed_out_tablet_is_never_parked(client, token, booked):
    AppSession.objects.update(logged_out_at=timezone.now())

    response = send(client, RETURN, back(booked, total_amount="55.00"), token=token, park=True)

    assert (response.status_code, response.json()["code"]) == (401, "not_authenticated")
    assert not every_issue().exists()


def test_park_on_a_lookup_is_ignored(client, token, booked):
    body = send(client, DETAIL, {"sync_id": booked["sync_id"]}, token=token, park=True).json()

    assert body["code"] == "ok" and not every_issue().exists()


def test_a_crash_on_a_park_call_is_parked_as_server_error(client, token, booked, monkeypatch):
    def crash(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(rental_services, "return_item", crash)

    body = send(client, RETURN, back(booked), token=token, park=True).json()

    assert body["code"] == "parked"
    issue = SyncIssue.objects.get()
    assert (issue.reason, issue.error_code, issue.attempts.get().http_status) == (
        IssueReason.SERVER_ERROR, "server_error", 500)
    # Logged like any crash, against the same request.
    assert ErrorLog.objects.filter(request_id=issue.request_id, exception_type__contains="RuntimeError").exists()


def test_a_crash_without_park_is_still_a_500(client, token, booked, monkeypatch):
    monkeypatch.setattr(rental_services, "return_item", lambda *a, **k: 1 / 0)

    response = send(client, RETURN, back(booked), token=token)

    assert (response.status_code, response.json()["code"]) == (500, "server_error")
    assert not every_issue().exists()


def test_a_parked_booking_still_counts_its_receipt(client, world, token, shop, booked):
    clash = booking(world, shop, number=240)             # MO 41 and DC 02 are already out

    body = send(client, BOOK, clash, token=token, park=True).json()

    assert (body["code"], body["data"]["error"]["code"]) == ("parked", "vehicle_already_rented")
    assert counter(world, shop) == 240


# -- Followers ---------------------------------------------------------------------------

def test_later_calls_for_the_order_wait_behind_it_from_any_tablet(client, world, token, booked):
    head = send(client, RETURN, back(booked, total_amount="55.00"), token=token, park=True).json()["data"]
    other = sign_in_tablet(client, world, code="OPR002", installation_id="till-2", branch=world["adc1"])
    good = back(booked, 1)

    body = send(client, RETURN, good, token=other).json()     # no park flag: parked anyway

    assert body["code"] == "parked"
    assert (body["data"]["waiting_on"], body["data"]["error"]) == (head["issue_id"], None)
    follower = SyncIssue.objects.get(sync_id=good["sync_id"])
    assert (follower.reason, follower.device.installation_id) == (IssueReason.FOLLOWER, "till-2")
    assert not follower.attempts.exists()                      # never run
    assert OrderItem.objects.get(pk=booked["items"][1]["sync_id"]).status == OrderItemStatus.ACTIVE


def test_calls_for_a_parked_booking_wait_behind_it(client, world, token, shop, booked):
    clash = booking(world, shop, number=240)
    send(client, BOOK, clash, token=token, park=True)

    body = send(client, RETURN, back(clash), token=token).json()

    assert body["code"] == "parked"
    assert body["data"]["waiting_on"] == SyncIssue.objects.get(sync_id=clash["sync_id"]).pk


def test_a_withdraw_waits_behind_its_parked_request(client, token, booked):
    # A request for an order this tablet has not uploaded: refused, parked.
    ask = {"sync_id": str(uuid7()), "order_id": str(uuid7()), "kind": "discount", "reason": "Regular",
           "requested_at": "2026-10-02 16:10:00"}
    parked = send(client, REQUEST, ask, token=token, park=True).json()
    assert parked["code"] == "parked", parked

    body = send(client, WITHDRAW, {"sync_id": str(uuid7()), "request_sync_id": ask["sync_id"],
                                   "withdrawn_at": "2026-10-02 16:12:00"}, token=token).json()

    assert (body["code"], body["data"]["waiting_on"]) == ("parked", parked["data"]["issue_id"])
    assert body["data"]["order_id"] == ask["order_id"]


def test_a_withdraw_for_a_request_on_the_server_follows_its_order(client, token, booked):
    ask = {"sync_id": str(uuid7()), "order_id": booked["sync_id"], "kind": "discount",
           "requested_at": "2026-10-02 16:10:00"}
    assert send(client, REQUEST, ask, token=token).json()["code"] == "ok"
    assert OrderRequest.objects.filter(pk=ask["sync_id"]).exists()
    head = send(client, RETURN, back(booked, total_amount="55.00"), token=token, park=True).json()["data"]

    body = send(client, WITHDRAW, {"sync_id": str(uuid7()), "request_sync_id": ask["sync_id"],
                                   "withdrawn_at": "2026-10-02 16:12:00"}, token=token).json()

    assert (body["code"], body["data"]["waiting_on"]) == ("parked", head["issue_id"])


@pytest.fixture
def punching(world):
    """Rental's world plus the employee whose QR the till scans."""
    from apps.crew.models import Designation, Employee

    company = world["company"]
    Employee.objects.create(company=company, employee_code="EMP001", first_name="Anil",
                            designation=Designation.objects.get(company=company), branch=world["adc1"])


def test_attendance_is_parked_alone(client, token, punching):
    day = datetime.date(2026, 8, 29)
    out = attendance.qr("punch_out", attendance.local(day, 18), employee_branch_code="ADC1",
                        rms_branch_code="ADC1")
    first = send(client, "/api/v1/operator/attendance/mark", out, token=token, park=True).json()
    assert (first["code"], first["data"]["error"]["code"]) == ("parked", "no_open_punch_in")
    assert first["data"]["order_id"] is None

    later = attendance.qr("punch_in", attendance.local(day, 19), employee_branch_code="ADC1",
                          rms_branch_code="ADC1")
    assert send(client, "/api/v1/operator/attendance/mark", later, token=token).json()["code"] == "ok"


# -- Replay ------------------------------------------------------------------------------

def test_replay_runs_the_corrected_call_then_its_followers_in_order(client, world, token, booked, office):
    wrong = back(booked, total_amount="55.00")
    head = send(client, RETURN, wrong, token=token, park=True).json()["data"]
    send(client, RETURN, back(booked, 1), token=token)

    outcome = services.replay(every_issue(), head["issue_id"], user=office,
                              request_data={**wrong, "total_amount": "50.00"})

    assert outcome["stopped_at"] is None and len(outcome["resolved"]) == 2
    assert set(OrderItem.objects.values_list("status", flat=True)) == {OrderItemStatus.RETURNED}
    issue = SyncIssue.objects.get(pk=head["issue_id"])
    assert (issue.status, issue.resolved_by, issue.reply["code"]) == (IssueStatus.RESOLVED, office, "ok")
    assert issue.original_request_data == wrong and issue.request_data["total_amount"] == "50.00"
    replay = issue.attempts.get(source=AttemptSource.REPLAY)
    assert (replay.by, replay.code) == (office, "ok")


def test_replay_stops_at_the_first_follower_that_fails(client, world, token, booked, office):
    wrong = back(booked, total_amount="55.00")
    head = send(client, RETURN, wrong, token=token, park=True).json()["data"]
    bad = back(booked, 1, total_amount="99.00")
    send(client, RETURN, bad, token=token)
    third = {"sync_id": str(uuid7()), "order_id": booked["sync_id"], "kind": "discount",
             "requested_at": "2026-10-02 17:10:00"}
    send(client, REQUEST, third, token=token)

    outcome = services.replay(every_issue(), head["issue_id"], user=office,
                              request_data={**wrong, "total_amount": "50.00"})

    stopped = SyncIssue.objects.get(sync_id=bad["sync_id"])
    assert (outcome["resolved"], outcome["stopped_at"]) == ([head["issue_id"]], stopped)
    assert (stopped.status, stopped.reason, stopped.error_code) == (
        IssueStatus.OPEN, IssueReason.REFUSED, "amount_mismatch")
    # It is now the head; the request still waits behind it.
    status = send(client, STATUS, {}, token=token).json()["data"]["issues"]
    waiting = {row["sync_id"]: row["waiting_on"] for row in status if row["status"] == "open"}
    assert waiting == {bad["sync_id"]: None, third["sync_id"]: stopped.pk}


def test_only_the_head_can_be_replayed_or_dismissed(client, token, booked, office):
    send(client, RETURN, back(booked, total_amount="55.00"), token=token, park=True)
    follower = back(booked, 1)
    send(client, RETURN, follower, token=token)
    pk = SyncIssue.objects.get(sync_id=follower["sync_id"]).pk

    with pytest.raises(services.SyncActionError, match="waits behind"):
        services.replay(every_issue(), pk, user=office)
    with pytest.raises(services.SyncActionError, match="waits behind"):
        services.dismiss(every_issue(), pk, user=office, reason="no")


def test_the_sync_id_cannot_be_changed_on_replay(client, token, booked, office):
    wrong = back(booked, total_amount="55.00")
    head = send(client, RETURN, wrong, token=token, park=True).json()["data"]

    with pytest.raises(services.SyncActionError, match="sync_id"):
        services.replay(every_issue(), head["issue_id"], user=office,
                        request_data={**wrong, "sync_id": str(uuid7())})


def test_a_replay_runs_as_the_original_session_after_logout_and_re_map(client, world, token, shop, booked,
                                                                        office):
    clash = booking(world, shop, number=240)
    head = send(client, BOOK, clash, token=token, park=True).json()["data"]
    # The operator logs out; the tablet is moved to Corniche 2.
    AppSession.objects.update(logged_out_at=timezone.now())
    DeviceMapping.objects.update(to_date=timezone.now())
    DeviceMapping.objects.create(device=shop["tablet"], branch=world["adc2"], from_date=timezone.now())
    world["adc1"].is_active = False                    # and Corniche 1 has since closed
    world["adc1"].save()
    fixed = {**clash, "items": clash["items"][:1]}
    fixed["items"][0] = {**fixed["items"][0], "vehicle_id": shop["mo42"].pk}
    fixed["payments"] = []

    outcome = services.replay(every_issue(), head["issue_id"], user=office, request_data=fixed)

    assert outcome["stopped_at"] is None, SyncIssue.objects.get().error_message
    order = Order.objects.get(pk=clash["sync_id"])
    assert (order.branch, order.device, order.created_by.username) == (world["adc1"], shop["tablet"], "opr001")


def test_a_failed_replay_keeps_the_issue_open_with_the_new_error(client, token, booked, office):
    wrong = back(booked, total_amount="55.00")
    head = send(client, RETURN, wrong, token=token, park=True).json()["data"]

    outcome = services.replay(every_issue(), head["issue_id"], user=office,
                              request_data={**wrong, "total_amount": "56.00"})

    issue = SyncIssue.objects.get()
    assert outcome == {"resolved": [], "stopped_at": issue}
    assert (issue.status, issue.attempts.count(), issue.request_data["total_amount"]) == (
        IssueStatus.OPEN, 2, "56.00")


# -- Dismiss, resend, status -------------------------------------------------------------

def test_dismissing_a_head_dismisses_its_followers(client, token, booked, office):
    head = send(client, RETURN, back(booked, total_amount="55.00"), token=token, park=True).json()["data"]
    follower = back(booked, 1)
    send(client, RETURN, follower, token=token)

    dismissed = services.dismiss(every_issue(), head["issue_id"], user=office, reason="Returned on paper")

    assert len(dismissed) == 2
    row = SyncIssue.objects.get(sync_id=follower["sync_id"])
    assert (row.status, row.dismissed_with_id, row.dismiss_reason) == (
        IssueStatus.DISMISSED, head["issue_id"], "Returned on paper")
    resent = send(client, RETURN, follower, token=token)
    assert (resent.status_code, resent.json()["code"]) == (409, "sync_dismissed")
    # The order is free again: a new call runs normally.
    assert send(client, RETURN, back(booked, 1), token=token).json()["code"] == "ok"


def test_a_dismiss_needs_a_reason(client, token, booked, office):
    head = send(client, RETURN, back(booked, total_amount="55.00"), token=token, park=True).json()["data"]

    with pytest.raises(services.SyncActionError, match="reason"):
        services.dismiss(every_issue(), head["issue_id"], user=office, reason="  ")


def test_a_resolved_call_resent_is_a_duplicate(client, token, booked, office):
    wrong = back(booked, total_amount="55.00")
    head = send(client, RETURN, wrong, token=token, park=True).json()["data"]
    services.replay(every_issue(), head["issue_id"], user=office, request_data={**wrong, "total_amount": "50.00"})

    assert send(client, RETURN, {**wrong, "total_amount": "50.00"}, token=token).json()["code"] == "duplicate"


def test_the_status_call_answers_this_devices_issues_only(client, world, token, booked, office):
    head = send(client, RETURN, back(booked, total_amount="55.00"), token=token, park=True).json()["data"]
    other = sign_in_tablet(client, world, code="OPR002", installation_id="till-2", branch=world["adc1"])
    follower = back(booked, 1)
    send(client, RETURN, follower, token=other)

    mine = send(client, STATUS, {}, token=token).json()["data"]["issues"]
    theirs = send(client, STATUS, {"issue_ids": [head["issue_id"]]}, token=other).json()["data"]["issues"]

    assert [(row["issue_id"], row["status"], row["error"]["code"]) for row in mine] == [
        (head["issue_id"], "open", "amount_mismatch")]
    assert theirs == []
    services.dismiss(every_issue(), head["issue_id"], user=office, reason="Paper")
    row = send(client, STATUS, {}, token=other).json()["data"]["issues"][0]
    assert (row["status"], row["dismissed_with"], row["dismiss_reason"]) == ("dismissed", head["issue_id"], "Paper")


def test_the_status_call_refuses_bad_ids(client, token):
    response = send(client, STATUS, {"issue_ids": "12"}, token=token)

    assert (response.status_code, response.json()["code"]) == (400, "invalid_request")


# -- Cleanup -----------------------------------------------------------------------------

def test_finished_issues_are_deleted_after_45_days_open_ones_never(client, token, booked, office):
    send(client, RETURN, back(booked, total_amount="55.00"), token=token, park=True)
    send(client, RETURN, back(booked, 1, total_amount="99.00"), token=token, park=True)
    first, second = SyncIssue.objects.order_by("id")
    services.dismiss(every_issue(), first.pk, user=office, reason="Paper")
    long_ago = timezone.now() - datetime.timedelta(days=46)
    SyncIssue.objects.filter(status=IssueStatus.DISMISSED).update(dismissed_at=long_ago)
    SyncIssue.objects.filter(pk=second.pk).update(received_at=long_ago, status=IssueStatus.OPEN,
                                                  dismissed_at=None)

    assert services.purge() == 1
    assert list(SyncIssue.objects.values_list("pk", flat=True)) == [second.pk]
