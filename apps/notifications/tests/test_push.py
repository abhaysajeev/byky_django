"""Push notifications for approval requests (apps/notifications): the token on
the app session, who is told what, and the sending.

Owner decisions, 21 Oct 2026: the token comes with login; a signed-out device
gets nothing; managers hear about every branch of their company; a decision
goes only to the device that asked; the text is what happened and where."""

import datetime

import pytest
from django.utils import timezone

from apps.crew.models import Designation, Employee
from apps.devices.models import Device, DeviceStatus
from apps.discount.models import ClaimStatus
from apps.discount.services import decide_claim
from apps.discount.tests.conftest import make_claim
from apps.notifications import services
from apps.notifications.models import Notification, NotificationStatus
from apps.notifications.sender import PushError
from apps.portal.auth import close_session
from apps.portal.models import Role
from apps.portal.session_models import AppSession, LogoutReason
from apps.rental.models import CreditNote, OrderRequest
from apps.rental.tests import test_manager_approvals as approvals
from apps.rental.tests import test_order_requests as requests_tests
from apps.rental.tests.test_order_api import call
from core.enums import Channel
from core.ids import uuid7
from core.models import User

world, token, fresh_throttle, shop = approvals.world, approvals.token, approvals.fresh_throttle, approvals.shop
booked, returned, settled, manager, card = (
    approvals.booked, approvals.returned, approvals.settled, approvals.manager, approvals.card)
asked_note, claim = approvals.asked, approvals.claim
ask, asked, decide = requests_tests.ask, requests_tests.asked, requests_tests.decide

TILL, MEERA = "fcm-till-1", "fcm-meera"


def session(username, channel):
    return AppSession.objects.get(user__username__iexact=username, channel=channel, logged_out_at__isnull=True)


@pytest.fixture
def signed_in(token, manager):
    """The till and Meera's phone, each holding a push token."""
    for username, channel, value in (("OPR001", Channel.OPERATOR, TILL), ("MGR001", Channel.MANAGER, MEERA)):
        AppSession.objects.filter(pk=session(username, channel).pk).update(push_token=value)
    return {"till": session("OPR001", Channel.OPERATOR), "meera": session("MGR001", Channel.MANAGER)}


class FakeSender:
    def __init__(self):
        self.sent, self.fail = [], None

    def send(self, notification, token):
        if self.fail:
            raise self.fail
        self.sent.append((token, notification.title, notification.body, notification.tag))
        return f"projects/x/messages/{len(self.sent)}"


@pytest.fixture
def fcm(monkeypatch):
    sender = FakeSender()
    monkeypatch.setattr(services, "get_sender", lambda: sender)
    return sender


def told(**filters):
    return [(row.session.push_token, row.title, row.body) for row in
            Notification.objects.filter(**filters).select_related("session").order_by("audience", "created_on")]


def manager_of(company, code, device_id):
    """Another manager-app session, signed in and holding a token."""
    employee = Employee.objects.create(company=company, employee_code=code, first_name=code,
                                       designation=Designation.objects.filter(company=company).first()
                                       or Designation.objects.create(company=company, code="MGR", title="Manager"))
    user = User.objects.create_user(code, "x", company=company, employee=employee,
                                    role=Role.objects.filter(company=company).first(),
                                    allowed_channels=[Channel.MANAGER])
    now = timezone.now()
    return AppSession.objects.create(user=user, channel=Channel.MANAGER, logged_in_at=now,
                                     refresh_expires_at=now + datetime.timedelta(days=30),
                                     push_token=f"fcm-{code}")


# -- The token -----------------------------------------------------------------------------------

def test_login_keeps_the_apps_push_token_on_its_session(client, world):
    response = client.post("/api/v1/operator/auth/login", {
        "credentials": {"device_notification_id": " fcm-new "},
        "request_data": {"username": "OPR001", "password": approvals.notes.PASSWORD, "installation_id": "till-1"},
    }, content_type="application/json")

    assert response.json()["code"] == "ok", response.json()
    assert session("OPR001", Channel.OPERATOR).push_token == "fcm-new"


def test_a_new_token_is_sent_and_a_token_has_one_session(client, world, token, manager):
    meera = session("MGR001", Channel.MANAGER)
    AppSession.objects.filter(pk=meera.pk).update(push_token="fcm-shared")

    body = call(client, "/api/v1/operator/device/push-token", {"push_token": "fcm-shared"}, token=token).json()

    assert body["code"] == "ok"
    assert session("OPR001", Channel.OPERATOR).push_token == "fcm-shared"
    assert session("MGR001", Channel.MANAGER).push_token == ""          # let go: one token, one session


def test_something_that_is_not_a_token_is_ignored(client, world, token, signed_in):
    call(client, "/api/v1/operator/device/push-token", {"push_token": "   "}, token=token)

    assert session("OPR001", Channel.OPERATOR).push_token == TILL


# -- Who is told ---------------------------------------------------------------------------------

def test_a_new_request_tells_every_manager_of_the_company_only(client, world, token, booked, signed_in):
    other_branch_manager = manager_of(world["company"], "MGR002", None)
    manager_of(world["other"], "MGR900", None)                                # another company

    data = asked(client, token, booked)

    rows = Notification.objects.filter(subject_id=data["request_sync_id"])
    assert sorted(row.session.push_token for row in rows) == sorted([MEERA, other_branch_manager.push_token])
    row = rows.first()
    assert (row.title, row.body, row.android_channel, row.tag) == (
        "New discount request", "Abu Dhabi Corniche 1", "approvals_new", f"order_request:{data['request_sync_id']}")
    assert row.data == {"notification_id": str(row.pk), "subject_type": "order_request",
                        "subject_id": data["request_sync_id"], "action": "raised", "order_id": booked["sync_id"],
                        "branch_id": str(world["adc1"].pk), "kind": "discount"}


def test_a_decision_goes_to_the_asking_device_and_updates_managers(client, world, token, booked, manager,
                                                                   signed_in):
    data = asked(client, token, booked)
    Notification.objects.all().delete()

    body = decide(client, manager, requests_tests.APPROVE, data["request_sync_id"], discount_type="percent",
                  discount_value="10.00")

    assert body["code"] == "ok", body
    assert told() == [(MEERA, "Discount request approved", "Abu Dhabi Corniche 1"),
                      (TILL, "Discount approved", "Abu Dhabi Corniche 1")]
    assert Notification.objects.get(audience="manager").android_channel == "approvals_updates"


def test_only_the_asking_device_is_told_not_the_stations_other_tablets(client, world, token, booked, manager,
                                                                      signed_in):
    till_2 = Device.objects.create(company=world["company"], installation_id="till-2", platform="android",
                                   channel=Channel.OPERATOR, status=DeviceStatus.APPROVED,
                                   approved_at=timezone.now())
    colleague = User.objects.create_user("OPR002", "x", company=world["company"], allowed_channels=[Channel.OPERATOR])
    now = timezone.now()
    AppSession.objects.create(user=colleague, channel=Channel.OPERATOR, device=till_2, branch=world["adc1"],
                              logged_in_at=now, refresh_expires_at=now + datetime.timedelta(days=1),
                              push_token="fcm-till-2")
    data = asked(client, token, booked)

    decide(client, manager, requests_tests.REJECT, data["request_sync_id"], note="No")

    assert [token for token, *_ in told(action="rejected", audience="operator")] == [TILL]


def test_a_request_with_no_device_tells_no_tablet(client, world, token, booked, manager, signed_in):
    data = asked(client, token, booked)
    OrderRequest.objects.filter(pk=data["request_sync_id"]).update(device=None)

    decide(client, manager, requests_tests.REJECT, data["request_sync_id"], note="No")

    assert [r.audience for r in Notification.objects.filter(action="rejected")] == ["manager"]


def test_reject_and_revoke(client, world, token, booked, manager, signed_in):
    first = asked(client, token, booked)
    decide(client, manager, requests_tests.REJECT, first["request_sync_id"], note="No")
    second = asked(client, token, booked)
    decide(client, manager, requests_tests.APPROVE, second["request_sync_id"], discount_type="percent",
           discount_value="10.00")
    decide(client, manager, requests_tests.REVOKE, second["request_sync_id"], note="Changed mind")

    assert told(session__push_token=TILL) == [
        (TILL, "Discount rejected", "Abu Dhabi Corniche 1"), (TILL, "Discount approved", "Abu Dhabi Corniche 1"),
        (TILL, "Discount approval revoked", "Abu Dhabi Corniche 1")]


def test_a_withdrawal_tells_managers_once(client, world, token, booked, signed_in):
    data = asked(client, token, booked)
    for _ in range(2):                      # withdrawing twice answers the same, and tells nobody again
        body = call(client, requests_tests.WITHDRAW, {"sync_id": str(uuid7()), "request_sync_id":
                    data["request_sync_id"], "withdrawn_at": "2026-10-02 16:40:00"}, token=token).json()
        assert body["code"] == "ok", body

    assert told(action="withdrawn") == [(MEERA, "Discount request withdrawn", "Abu Dhabi Corniche 1")]


def test_a_resent_request_tells_nobody_again(client, world, token, booked, signed_in):
    data = ask(booked)
    for _ in range(2):
        call(client, requests_tests.ASK, data, token=token)

    assert Notification.objects.filter(action="raised").count() == 1


def test_a_request_closed_by_the_settle_tells_managers(client, world, token, shop, booked, signed_in):
    asked(client, token, booked)
    requests_tests.return_all(client, token, booked)

    assert call(client, requests_tests.SETTLE, requests_tests.bill(booked, shop), token=token).json()["code"] == "ok"
    assert told(action="closed") == [(MEERA, "Discount request closed", "Abu Dhabi Corniche 1")]


def test_signed_out_or_expired_sessions_are_not_told(client, world, token, booked, signed_in):
    close_session(signed_in["meera"], LogoutReason.USER_LOGOUT)
    expired = manager_of(world["company"], "MGR003", None)
    AppSession.objects.filter(pk=expired.pk).update(refresh_expires_at=timezone.now())

    asked(client, token, booked)

    assert not Notification.objects.exists()


def test_no_customer_or_money_in_any_push(client, world, token, booked, manager, signed_in):
    data = asked(client, token, booked)
    decide(client, manager, requests_tests.APPROVE, data["request_sync_id"], discount_type="amount",
           discount_value="5.00")

    for row in Notification.objects.all():
        # The words the phone shows and the data's words -- not its ids: a
        # random UUID may well contain "971".
        words = [row.title, row.body] + [v for k, v in row.data.items() if not k.endswith("_id")]
        text = " ".join(words)
        assert "Ahmed" not in text and "5.00" not in text and "971" not in text


# -- Card discounts and credit notes ------------------------------------------------------------

def test_card_discount_decision_goes_to_the_asking_device(world, signed_in, claim):
    CardDiscountClaim = type(claim)
    CardDiscountClaim.objects.filter(pk=claim.pk).update(device=signed_in["till"].device)
    claim.refresh_from_db()

    decide_claim(signed_in["meera"].user, claim, approve=True, channel="manager")

    assert told() == [(MEERA, "Card discount request approved", claim.branch.name),
                      (TILL, "Card discount approved", claim.branch.name)]


def test_card_discount_waiting_at_settle_is_closed_for_managers(world, signed_in, card, shop, settled):
    waiting = make_claim(card, shop["ahmed"], order=settled, branch=settled.branch)
    make_claim(card, shop["ahmed"], order=settled, branch=settled.branch, status=ClaimStatus.APPROVED)
    from apps.discount.services import cancel_unused_for_order

    cancel_unused_for_order(settled)

    assert [(r.subject_id, r.title) for r in Notification.objects.all()] == [
        (waiting.pk, "Card discount request closed")]


def test_credit_note_flow(client, world, token, manager, signed_in, asked_note):
    assert told(action="raised") == [(MEERA, "New credit note request", "Abu Dhabi Corniche 1")]

    body = call(client, approvals.CN_APPROVE, {"credit_note_id": asked_note, "net_amount": "52.50"},
                token=manager).json()

    assert body["code"] == "ok", body
    assert CreditNote.objects.get(pk=asked_note).status == "approved"
    assert told(action="approved") == [(MEERA, "Credit note request approved", "Abu Dhabi Corniche 1"),
                                       (TILL, "Credit note approved", "Abu Dhabi Corniche 1")]


# -- Sending -------------------------------------------------------------------------------------

def test_sending_marks_rows_sent(client, world, token, booked, signed_in, fcm):
    asked(client, token, booked)
    row = Notification.objects.get()

    assert services.send_one(row.pk) is None

    row.refresh_from_db()
    assert (row.status, row.attempts, row.fcm_message_id) == (NotificationStatus.SENT, 1, "projects/x/messages/1")
    assert fcm.sent == [(MEERA, "New discount request", "Abu Dhabi Corniche 1", row.tag)]


def test_a_dead_token_is_dropped(client, world, token, booked, signed_in, fcm):
    asked(client, token, booked)
    fcm.fail = PushError("unregistered")

    services.send_one(Notification.objects.get().pk)

    assert Notification.objects.get().status == NotificationStatus.FAILED
    assert session("MGR001", Channel.MANAGER).push_token == ""


def test_firebase_busy_is_tried_again_later(client, world, token, booked, signed_in, fcm):
    asked(client, token, booked)
    fcm.fail = PushError("unavailable", retry_after=30)

    retry_in = services.send_one(Notification.objects.get().pk)

    row = Notification.objects.get()
    assert row.status == NotificationStatus.PENDING and row.attempts == 1 and retry_in >= 30
    assert row.next_attempt_at > timezone.now()

    row.attempts = 5
    row.save()
    Notification.objects.filter(pk=row.pk).update(next_attempt_at=timezone.now())
    services.send_one(row.pk)
    assert Notification.objects.get().status == NotificationStatus.FAILED       # gave up after 5


def test_a_session_signed_out_before_sending_is_skipped(client, world, token, booked, signed_in, fcm):
    asked(client, token, booked)
    close_session(signed_in["meera"], LogoutReason.USER_LOGOUT)

    services.send_one(Notification.objects.get().pk)

    assert Notification.objects.get().status == NotificationStatus.SKIPPED and fcm.sent == []


def test_without_firebase_set_up_nothing_is_sent(client, world, token, booked, signed_in, settings, monkeypatch):
    from apps.notifications import sender

    settings.FCM_CREDENTIALS_FILE = ""
    monkeypatch.setattr(sender, "_SENDER", None)
    asked(client, token, booked)

    services.send_one(Notification.objects.get().pk)

    row = Notification.objects.get()
    assert (row.status, row.error.split(":")[0]) == (NotificationStatus.SKIPPED, "not_configured")


def test_the_sweep_sends_what_was_left_and_the_queue_runs_after_commit(
        client, world, token, booked, signed_in, fcm, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        asked(client, token, booked)
    assert Notification.objects.get().status == NotificationStatus.SENT        # queued after commit, eager

    Notification.objects.update(status=NotificationStatus.PENDING)
    assert list(services.due()) == [Notification.objects.get().pk]


def test_old_rows_are_purged(client, world, token, booked, signed_in):
    asked(client, token, booked)
    Notification.objects.update(status=NotificationStatus.SENT,
                                created_on=timezone.now() - datetime.timedelta(days=91))

    assert services.purge() == 1


def test_the_push_token_call_is_in_the_docs(client, db):
    schema = client.get("/api/schema/").content.decode()

    assert "/api/v1/{app}/device/push-token" in schema
