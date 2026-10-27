"""Station sync for requests on an order (apps/rental/station_sync.py,
request_changed): an order request, credit note or card-discount claim raised,
withdrawn, decided or used reaches the order's station's other tablets -- never
the tablet that acted, and for a decision never the asking tablet (it has its
own alert). Missed ones are caught up with orders/changes."""

import datetime

from django.utils import timezone

from apps.discount import services as discount_services
from apps.discount.models import CardDiscountClaim
from apps.portal.session_models import AppSession
from apps.rental import credit_notes, station_sync
from apps.rental.models import Order, OrderEvent, OrderRequest
from apps.rental.tests import test_credit_notes as notes_tests
from apps.rental.tests import test_order_api as orders
from apps.rental.tests import test_order_requests as requests_tests
from apps.rental.tests import test_order_settle as settling
from apps.rental.tests import test_station_sync as sync_tests
from core.ids import uuid7
from core.timezones import zone_for

call = orders.call
world, token, fresh_throttle, shop = orders.world, orders.token, orders.fresh_throttle, orders.shop
booked, returned, settled = settling.booked, settling.returned, notes_tests.settled
manager, card = requests_tests.manager, requests_tests.card
fcm, station, tokens = sync_tests.fcm, sync_tests.station, sync_tests.tokens


def request_pushes(fcm):
    return [(sent[0], sent[1], sent[3]) for sent in fcm.sent if sent[3]["type"] == "request_changed"]


def user_of(installation_id):
    return AppSession.objects.filter(device__installation_id=installation_id).first().user


def test_a_discount_asked_then_approved(client, world, token, booked, manager, station, fcm,
                                        django_capture_on_commit_callbacks):
    order = Order.objects.get(pk=booked["sync_id"])
    with django_capture_on_commit_callbacks(execute=True):
        asked = requests_tests.asked(client, token, booked)
    [(to, title, data)] = request_pushes(fcm)
    assert (to, title) == ("tok-till-2", f"Order {order.order_no} discount requested")   # not till-1 (asked)
    assert data == {"type": "request_changed", "order_id": str(order.pk), "order_no": order.order_no,
                    "request_type": "order_request", "request_id": asked["request_sync_id"], "kind": "discount",
                    "status": "pending", "action": "raised", "branch_id": str(world["adc1"].pk), "at": data["at"]}
    assert fcm.sent[0][4:] == ("station_activity", f"order:{order.pk}", str(order.pk))

    fcm.sent.clear()
    with django_capture_on_commit_callbacks(execute=True):
        body = requests_tests.decide(client, manager, requests_tests.APPROVE, asked["request_sync_id"],
                                     discount_type="percent", discount_value="10.00")
        assert body["code"] == "ok", body
    [(to, title, data)] = request_pushes(fcm)
    assert (to, title, data["status"]) == ("tok-till-2", f"Order {order.order_no} discount approved", "approved")


def test_another_tablet_withdraws_and_the_asking_one_hears(client, world, token, booked, station, fcm,
                                                          django_capture_on_commit_callbacks):
    asked = requests_tests.asked(client, token, booked)
    fcm.sent.clear()
    with django_capture_on_commit_callbacks(execute=True):
        body = call(client, requests_tests.WITHDRAW, {
            "sync_id": str(uuid7()), "request_sync_id": asked["request_sync_id"],
            "withdrawn_at": "2026-10-02 16:40:00"}, token=station["mona"]).json()
        assert body["code"] == "ok", body
    assert [(to, data["action"]) for to, _, data in request_pushes(fcm)] == [("tok-till-1", "withdrawn")]


def test_rejected_and_revoked_skip_the_asking_tablet(client, world, token, booked, manager, station, fcm,
                                                    django_capture_on_commit_callbacks):
    asked = requests_tests.asked(client, token, booked)
    fcm.sent.clear()
    with django_capture_on_commit_callbacks(execute=True):
        requests_tests.decide(client, manager, requests_tests.REJECT, asked["request_sync_id"])
    assert [(to, data["status"]) for to, _, data in request_pushes(fcm)] == [("tok-till-2", "rejected")]

    approved = requests_tests.approved(client, token, manager, booked)
    fcm.sent.clear()
    with django_capture_on_commit_callbacks(execute=True):
        requests_tests.decide(client, manager, requests_tests.REVOKE, approved)
    [(to, title, data)] = request_pushes(fcm)
    assert (to, data["action"]) == ("tok-till-2", "revoked") and title.endswith("discount approval revoked")


def test_a_receipt_reprint_asked_approved_and_printed(client, world, token, booked, manager, station, fcm,
                                                      django_capture_on_commit_callbacks):
    from apps.rental.tests.test_order_requests_kinds import used

    with django_capture_on_commit_callbacks(execute=True):
        asked = requests_tests.asked(client, token, booked, kind="order_reprint")
        assert requests_tests.decide(client, manager, requests_tests.APPROVE, asked["request_sync_id"])[
            "code"] == "ok"
        assert used(client, token, asked["request_sync_id"])["code"] == "ok"
    pushes = request_pushes(fcm)
    assert [(to, data["kind"], data["action"]) for to, _, data in pushes] == [
        ("tok-till-2", "order_reprint", "raised"), ("tok-till-2", "order_reprint", "approved"),
        ("tok-till-2", "order_reprint", "used")]
    assert pushes[-1][1].endswith("receipt reprint printed")


def test_an_approved_cancel_is_pushed_once_as_the_order(client, world, token, booked, manager, station, fcm,
                                                       django_capture_on_commit_callbacks):
    asked = requests_tests.asked(client, token, booked, kind="cancel")
    fcm.sent.clear()
    with django_capture_on_commit_callbacks(execute=True):
        assert requests_tests.decide(client, manager, requests_tests.APPROVE, asked["request_sync_id"])[
            "code"] == "ok"
    assert request_pushes(fcm) == []
    assert sorted((s[0], s[3]["action"]) for s in fcm.sent) == [("tok-till-1", "cancel"), ("tok-till-2", "cancel")]


def test_a_card_discount_claim(client, world, token, shop, booked, card, station, fcm,
                               django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        body = call(client, "/api/v1/operator/card-discounts/approval", {
            "sync_id": str(uuid7()), "card_discount_id": card.pk, "full_number": shop["ahmed"].mobile_full,
            "order_id": booked["sync_id"], "bill_amount": "100.000", "discount_percent": "10.00",
            "discount_amount": "10.000", "net_amount": "90.000", "requested_at": "2026-10-02 16:31:00",
        }, token=token).json()
        assert body["code"] == "ok", body
    [(to, title, data)] = request_pushes(fcm)
    assert (to, data["request_type"], data["kind"], data["action"]) == ("tok-till-2", "card_discount", "", "raised")
    assert title.endswith("card discount requested")

    fcm.sent.clear()
    claim = CardDiscountClaim.objects.get()
    with django_capture_on_commit_callbacks(execute=True):
        discount_services.decide_claim(user_of("till-1"), claim, approve=True)
    assert [(to, data["status"]) for to, _, data in request_pushes(fcm)] == [("tok-till-2", "approved")]


def test_a_credit_note(client, world, token, settled, station, fcm, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        body = call(client, notes_tests.REQUEST, notes_tests.ask(settled), token=token).json()
        assert body["code"] == "ok", body
    [(to, title, data)] = request_pushes(fcm)
    assert (to, data["request_type"], data["action"]) == ("tok-till-2", "credit_note", "raised")
    assert title == f"Order {settled.order_no} credit note requested"

    fcm.sent.clear()
    note = settled.credit_notes.get()
    with django_capture_on_commit_callbacks(execute=True):
        credit_notes.approve(user_of("till-1"), note.pk, [world["company"].pk], "5.00")
    assert [(to, data["status"]) for to, _, data in request_pushes(fcm)] == [("tok-till-2", "approved")]


def test_a_manager_decision_is_caught_up(client, world, token, booked, manager, station):
    asked = requests_tests.asked(client, token, booked)
    # Make every trace of the order three hours old; a since of an hour ago misses it.
    old = timezone.now() - datetime.timedelta(hours=3)
    Order.objects.filter(pk=booked["sync_id"]).update(created_on=old, modified_on=old)
    OrderEvent.objects.filter(order_id=booked["sync_id"]).update(received_at=old)
    OrderRequest.objects.filter(pk=asked["request_sync_id"]).update(modified_on=old)
    since = (timezone.now() - datetime.timedelta(hours=1)).astimezone(zone_for(world["company"]))
    since = since.strftime("%Y-%m-%d %H:%M:%S")

    def changed():
        data = call(client, sync_tests.CHANGES, {"since": since}, token=station["mona"]).json()["data"]
        return [o["sync_id"] for o in data["orders"]]

    assert changed() == []
    requests_tests.decide(client, manager, requests_tests.APPROVE, asked["request_sync_id"],
                          discount_type="percent", discount_value="10.00")
    assert changed() == [booked["sync_id"]]


def test_every_request_action_has_words():
    assert set(station_sync.REQUEST_VERBS) == {"raised", "withdrawn", "cancelled", "approved", "rejected",
                                               "revoked", "used"}
