"""Requests on running orders (apps/rental/requests.py): the operator asks for a
discount on a tablet, a manager approves it -- as a % or an AED amount -- or
rejects or revokes it, in the manager app or on the web; the settle must then
apply exactly what was approved."""

import copy
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.crew.models import Designation, Employee
from apps.devices.models import Device, DeviceStatus
from apps.discount.models import CardGrade, CardType, ClaimStatus
from apps.discount.tests.conftest import make_claim, make_discount
from apps.portal.models import Role, RolePermission
from apps.rental import api
from apps.rental.models import (
    DecisionChannel,
    Order,
    OrderEvent,
    OrderRequest,
    OrderRequestStatus,
    OrderStatus,
)
from apps.rental.tests import test_order_api as orders
from apps.rental.tests import test_order_settle as settles
from apps.rental.tests.test_order_return import RETURN, back
from core.enums import Channel
from core.ids import uuid7
from core.models import User

call, booking, shape, sign_in_tablet = orders.call, orders.booking, orders.shape, orders.sign_in_tablet
world, token, fresh_throttle, shop = orders.world, orders.token, orders.fresh_throttle, orders.shop
bill, SETTLE = settles.bill, settles.SETTLE

ASK = "/api/v1/operator/orders/requests"
WITHDRAW = "/api/v1/operator/orders/requests/withdraw"
STATUS = "/api/v1/operator/orders/requests/status"
LIST = "/api/v1/manager/requests/list"
APPROVE = "/api/v1/manager/requests/approve"
REJECT = "/api/v1/manager/requests/reject"
REVOKE = "/api/v1/manager/requests/revoke"


@pytest.fixture
def booked(client, world, token, shop):
    """MO 41 and DC 02 out for an hour, AED 100 down."""
    request_data = booking(world, shop)
    assert call(client, orders.BOOK, request_data, token=token).json()["code"] == "ok"
    return request_data


@pytest.fixture
def manager(client, world):
    """Meera, signed in to the manager app on her phone."""
    company = world["company"]
    employee = Employee.objects.create(company=company, employee_code="MGR001", first_name="Meera",
                                       designation=Designation.objects.get(company=company))
    User.objects.create_user("MGR001", orders.fare_api.PASSWORD, display_name="Meera", company=company,
                             role=Role.objects.get(company=company, name="Operator"), employee=employee,
                             allowed_channels=[Channel.MANAGER])
    Device.objects.create(company=company, installation_id="phone-m", platform="android",
                          channel=Channel.MANAGER, status=DeviceStatus.APPROVED, approved_at=timezone.now())
    response = call(client, "/api/v1/manager/auth/login",
                    {"username": "MGR001", "password": orders.fare_api.PASSWORD, "installation_id": "phone-m"})
    assert response.status_code == 200, response.json()
    return response.json()["data"]["tokens"]["access"]


def ask(booked, **overrides):
    data = {"sync_id": str(uuid7()), "order_id": booked["sync_id"], "kind": "discount",
            "reason": "Regular customer", "requested_at": "2026-10-02 16:30:00"}
    data.update(overrides)
    return data


def asked(client, token, booked, **overrides):
    body = call(client, ASK, ask(booked, **overrides), token=token).json()
    assert body["code"] == "ok", body
    return body["data"]


def decide(client, manager, url, request_sync_id, **fields):
    return call(client, url, {"request_sync_id": request_sync_id, **fields}, token=manager).json()


def approved(client, token, manager, booked, kind="percent", value="10.00"):
    data = asked(client, token, booked)
    body = decide(client, manager, APPROVE, data["request_sync_id"], discount_type=kind, discount_value=value)
    assert body["code"] == "ok", body
    return data["request_sync_id"]


def return_all(client, token, booked):
    """DC 02 back on time (50.00), MO 41 twelve minutes late (60.00): 110.00."""
    late = back(booked, 0, returned_at="2026-10-02 17:12:00", run_minutes=72, overtime_amount="10.00",
                total_amount="60.00")
    for request_data in (late, back(booked, 1)):
        assert call(client, RETURN, request_data, token=token).json()["code"] == "ok"


def refund(shop, amount):
    return [{"sync_id": str(uuid7()), "kind": "refund", "payment_mode_id": shop["cash"].pk, "amount": amount,
             "paid_at": "2026-10-02 17:13:30"}]


# -- Tablet: ask, withdraw, status ---------------------------------------------------


def test_a_discount_is_asked_on_a_running_order(client, token, booked):
    request_data = ask(booked)

    body = call(client, ASK, request_data, token=token).json()

    assert body["code"] == "ok", body
    data = body["data"]
    assert (data["request_sync_id"], data["order_id"], data["kind"], data["status"], data["reason"]) == (
        request_data["sync_id"], booked["sync_id"], "discount", "pending", "Regular customer")
    assert (data["requested_at"], data["discount_type"], data["discount_value"]) == ("2026-10-02 16:30:00", None, None)
    assert shape(data) == shape(api._REQUEST_PENDING_SAMPLE)
    assert OrderEvent.objects.get(pk=request_data["sync_id"]).action == "request"


def test_a_resent_ask_is_a_duplicate_and_a_changed_one_a_conflict(client, token, booked):
    request_data = ask(booked)
    first = call(client, ASK, request_data, token=token).json()
    changed = copy.deepcopy(request_data)
    changed["reason"] = "Something else"

    again = call(client, ASK, request_data, token=token).json()

    assert (again["code"], again["data"]) == ("duplicate", first["data"])
    assert call(client, ASK, changed, token=token).json()["code"] == "sync_id_conflict"
    assert OrderRequest.objects.count() == 1


def test_all_vehicles_back_is_still_running_but_settled_is_closed(client, token, shop, booked):
    return_all(client, token, booked)
    assert call(client, ASK, ask(booked), token=token).json()["code"] == "ok"

    Order.objects.filter(pk=booked["sync_id"]).update(status=OrderStatus.COMPLETED)

    body = call(client, ASK, ask(booked, sync_id=str(uuid7())), token=token).json()
    assert (body["code"], body["data"]) == ("order_closed", {"retry": False})


def test_one_waiting_or_approved_discount_per_order(client, token, manager, booked):
    data = asked(client, token, booked)

    assert call(client, ASK, ask(booked), token=token).json()["code"] == "discount_request_pending"
    decide(client, manager, APPROVE, data["request_sync_id"], discount_type="percent", discount_value="10")
    assert call(client, ASK, ask(booked), token=token).json()["code"] == "discount_approved"


@pytest.mark.parametrize("ending", ["reject", "withdraw", "revoke"])
def test_after_a_rejection_withdrawal_or_revoke_it_may_be_asked_again(client, token, manager, booked, ending):
    data = asked(client, token, booked)
    if ending == "reject":
        decide(client, manager, REJECT, data["request_sync_id"])
    elif ending == "withdraw":
        call(client, WITHDRAW, {"sync_id": str(uuid7()), "request_sync_id": data["request_sync_id"],
                                "withdrawn_at": "2026-10-02 16:35:00"}, token=token)
    else:
        decide(client, manager, APPROVE, data["request_sync_id"], discount_type="amount", discount_value="5")
        decide(client, manager, REVOKE, data["request_sync_id"])

    assert call(client, ASK, ask(booked), token=token).json()["code"] == "ok"


def test_another_station_cannot_see_the_order(client, world, booked):
    other = sign_in_tablet(client, world, code="OPR002", installation_id="till-2", branch=world["adc2"])

    assert call(client, ASK, ask(booked), token=other).json()["code"] == "order_not_synced"


def test_another_tablet_at_the_station_may_ask_and_withdraw(client, world, token, booked):
    data = asked(client, token, booked)
    other = sign_in_tablet(client, world, code="OPR002", installation_id="till-2", branch=world["adc1"])

    body = call(client, WITHDRAW, {"sync_id": str(uuid7()), "request_sync_id": data["request_sync_id"],
                                   "withdrawn_at": "2026-10-02 16:35:00"}, token=other).json()

    assert (body["code"], body["data"]["status"]) == ("ok", "withdrawn")


def test_a_withdrawn_request_answers_the_same_and_a_decided_one_is_closed(client, token, manager, booked):
    data = asked(client, token, booked)

    def withdraw():
        return call(client, WITHDRAW, {"sync_id": str(uuid7()), "request_sync_id": data["request_sync_id"],
                                       "withdrawn_at": "2026-10-02 16:35:00"}, token=token).json()

    assert withdraw()["data"]["status"] == "withdrawn"
    assert withdraw()["data"]["status"] == "withdrawn"
    req = OrderRequest.objects.get()
    assert (req.status, req.closed_at is not None) == (OrderRequestStatus.WITHDRAWN, True)

    second = asked(client, token, booked)
    decide(client, manager, REJECT, second["request_sync_id"])
    body = call(client, WITHDRAW, {"sync_id": str(uuid7()), "request_sync_id": second["request_sync_id"],
                                   "withdrawn_at": "2026-10-02 16:40:00"}, token=token).json()
    assert body["code"] == "request_closed"
    assert call(client, WITHDRAW, {"sync_id": str(uuid7()), "request_sync_id": str(uuid7()),
                                   "withdrawn_at": "2026-10-02 16:40:00"}, token=token).json()["code"] == "unknown_request"


def test_status_shows_the_rule_and_who_decided_where(client, token, manager, booked):
    request_id = approved(client, token, manager, booked, "amount", "15.5")

    body = call(client, STATUS, {"order_ids": [booked["sync_id"], str(uuid7())]}, token=token).json()

    [row] = body["data"]["requests"]
    assert (row["request_sync_id"], row["order_no"], row["status"]) == (request_id, booked["order_no"], "approved")
    assert (row["discount_type"], row["discount_value"], row["decided_by"], row["decided_channel"]) == (
        "amount", "15.500", "Meera", "manager")
    assert shape(row) == shape(api._REQUEST_APPROVED_SAMPLE)


# -- Card discount and manager discount exclude each other --------------------------


@pytest.fixture
def card(world):
    card_type = CardType.objects.create(company=world["company"], code="FAM", name="Family Card")
    grade = CardGrade.objects.create(company=world["company"], card_type=card_type, code="GOLD", name="Gold")
    return make_discount(grade, requires_approval=True)


@pytest.mark.parametrize("status", [ClaimStatus.PENDING, ClaimStatus.APPROVED])
def test_a_live_card_discount_request_blocks_a_manager_discount(client, token, shop, booked, card, status):
    make_claim(card, shop["ahmed"], status=status, order=Order.objects.get(pk=booked["sync_id"]))

    assert call(client, ASK, ask(booked), token=token).json()["code"] == "card_discount_requested"


def test_a_live_manager_discount_blocks_a_card_discount_request(client, token, shop, booked, card):
    asked(client, token, booked)

    body = call(client, "/api/v1/operator/card-discounts/approval", {
        "sync_id": str(uuid7()), "card_discount_id": card.pk, "full_number": shop["ahmed"].mobile_full,
        "order_id": booked["sync_id"], "bill_amount": "100.000", "discount_percent": "10.00",
        "discount_amount": "10.000", "net_amount": "90.000", "requested_at": "2026-10-02 16:31:00",
    }, token=token).json()

    assert body["code"] == "discount_requested"


# -- Manager app ----------------------------------------------------------------------


def test_the_manager_lists_requests_with_the_order(client, token, manager, booked):
    data = asked(client, token, booked)
    call(client, RETURN, back(booked, 1, returned_at="2026-10-02 16:31:00", run_minutes=31), token=token)

    body = call(client, LIST, {}, token=manager).json()

    assert body["code"] == "ok", body
    [row] = body["data"]["requests"]
    assert (row["request_sync_id"], row["station"], row["requested_by"]) == (
        data["request_sync_id"], "Abu Dhabi Corniche 1", "Rashed")
    order = row["order"]
    assert (order["order_no"], order["customer_name"], order["advance_paid"]) == (
        booked["order_no"], "Ahmed Al Mansoori", "100.000")
    assert {(v["vehicle"], v["status"]) for v in order["vehicles"]} == {("MO 41", "active"), ("DC 02", "returned")}
    assert next(v for v in order["vehicles"] if v["vehicle"] == "DC 02")["minutes_run"] == 31
    assert (body["data"]["page"], body["data"]["pages"], body["data"]["total"]) == (1, 1, 1)
    assert shape(row) == shape(api._MANAGER_SAMPLE)


def test_the_list_can_show_only_pending(client, world, token, manager, shop, booked):
    approved(client, token, manager, booked)
    other = booking(world, shop, number=232)
    other["items"] = [{**other["items"][0], "vehicle_id": shop["mo42"].pk}]
    call(client, orders.BOOK, other, token=token)
    waiting = asked(client, token, other)

    rows = call(client, LIST, {"pending": True}, token=manager).json()["data"]["requests"]
    everything = call(client, LIST, {}, token=manager).json()["data"]["requests"]

    assert [r["request_sync_id"] for r in rows] == [waiting["request_sync_id"]]
    assert len(everything) == 2


def test_the_manager_approves_as_a_percentage(client, token, manager, booked):
    data = asked(client, token, booked)

    body = decide(client, manager, APPROVE, data["request_sync_id"], discount_type="percent",
                  discount_value="12.5", note="Fine")

    assert body["code"] == "ok", body
    assert (body["data"]["status"], body["data"]["discount_type"], body["data"]["discount_value"]) == (
        "approved", "percent", "12.50")
    req = OrderRequest.objects.get()
    assert (req.decided_by.username, req.decided_channel, req.decision_note) == (
        "mgr001", DecisionChannel.MANAGER, "Fine")


def test_a_decided_request_answers_request_decided(client, token, manager, booked):
    data = asked(client, token, booked)
    decide(client, manager, REJECT, data["request_sync_id"], note="No")

    body = decide(client, manager, APPROVE, data["request_sync_id"], discount_type="percent", discount_value="5")

    assert (body["code"], body["message"]) == ("request_decided", "This request is already rejected.")
    assert OrderRequest.objects.get().decision_note == "No"


@pytest.mark.parametrize(("kind", "value", "code"), [
    ("percent", "150", "invalid_request"),
    ("percent", "10.125", "invalid_request"),
    ("amount", "10.1255", "invalid_request"),
    ("amount", "0", "invalid_request"),
    ("flat", "10", "invalid_request"),
], ids=["over-100", "percent-3-places", "amount-4-places", "zero", "unknown-type"])
def test_the_rule_is_checked(client, token, manager, booked, kind, value, code):
    data = asked(client, token, booked)

    body = decide(client, manager, APPROVE, data["request_sync_id"], discount_type=kind, discount_value=value)

    assert body["code"] == code
    assert OrderRequest.objects.get().status == OrderRequestStatus.PENDING


def test_an_approval_is_revoked_only_while_the_order_runs(client, token, manager, booked):
    request_id = approved(client, token, manager, booked)

    body = decide(client, manager, REVOKE, request_id, note="Changed my mind")

    assert (body["code"], body["data"]["status"], body["data"]["revoked_channel"]) == ("ok", "revoked", "manager")
    assert decide(client, manager, REVOKE, request_id)["code"] == "request_not_approved"
    second = approved(client, token, manager, booked)
    Order.objects.filter(pk=booked["sync_id"]).update(status=OrderStatus.COMPLETED)
    assert decide(client, manager, REVOKE, second)["code"] == "order_closed"


def test_the_manager_calls_need_the_manager_app_and_the_right(client, world, token, manager, booked):
    data = asked(client, token, booked)

    assert call(client, LIST, {}, token=token).json()["code"] == "wrong_channel"
    RolePermission.objects.filter(role__company=world["company"], page__code="rental.request").update(
        can_approve=False)
    body = decide(client, manager, APPROVE, data["request_sync_id"], discount_type="percent", discount_value="5")
    assert body["code"] == "forbidden"
    assert call(client, LIST, {}, token=manager).json()["code"] == "ok"


def test_another_companys_request_is_unknown(client, world, token, manager, booked):
    data = asked(client, token, booked)
    OrderRequest.objects.update(company=world["other"])

    assert decide(client, manager, REJECT, data["request_sync_id"])["code"] == "unknown_request"


# -- Settle ---------------------------------------------------------------------------


def test_a_percentage_approval_is_applied_at_settle(client, token, manager, shop, booked):
    request_id = approved(client, token, manager, booked, "percent", "10")
    return_all(client, token, booked)

    body = call(client, SETTLE, bill(
        booked, shop, tax_amount="4.714", net_amount="99.000", payments=refund(shop, "1.000"),
        manager_discount={"request_sync_id": request_id, "discount_percentage": "10.00",
                          "discount_amount": "11.000"}), token=token).json()

    assert body["code"] == "ok", body
    assert body["data"]["discount"] == {"source": "manager", "request_sync_id": request_id,
                                        "discount_percentage": "10.00", "discount_amount": "11.000"}
    req = OrderRequest.objects.get()
    assert (req.status, req.applied_amount, req.applied_at is not None) == (
        OrderRequestStatus.APPROVED, Decimal("11.000"), True)
    order = Order.objects.get(pk=booked["sync_id"])
    assert (order.discount_claim_id, order.discount_percentage, order.net_amount) == (
        None, Decimal("10.00"), Decimal("99.000"))


def test_an_amount_above_the_bill_takes_it_to_zero(client, token, manager, shop, booked):
    request_id = approved(client, token, manager, booked, "amount", "500")
    return_all(client, token, booked)
    short = bill(booked, shop, tax_amount="0.476", net_amount="10.000", payments=refund(shop, "90.000"),
                 manager_discount={"request_sync_id": request_id, "discount_amount": "100.000"})

    body = call(client, SETTLE, short, token=token).json()
    assert (body["code"], body["data"]["discount_amount"]) == ("discount_mismatch", "110.000")

    body = call(client, SETTLE, bill(
        booked, shop, tax_amount="0.000", net_amount="0.000", payments=refund(shop, "100.000"),
        manager_discount={"request_sync_id": request_id, "discount_amount": "110.000"}), token=token).json()
    assert body["code"] == "ok", body
    assert (body["data"]["net_amount"], body["data"]["discount"]["discount_percentage"]) == ("0.000", None)


def test_the_settle_must_match_the_approval(client, token, manager, shop, booked):
    request_id = approved(client, token, manager, booked, "percent", "10")
    return_all(client, token, booked)

    body = call(client, SETTLE, bill(
        booked, shop, tax_amount="4.500", net_amount="94.500", payments=refund(shop, "5.500"),
        manager_discount={"request_sync_id": request_id, "discount_percentage": "14.00",
                          "discount_amount": "15.500"}), token=token).json()

    assert body["code"] == "discount_mismatch"
    assert (body["data"]["discount_type"], body["data"]["discount_value"], body["data"]["discount_percentage"]) == (
        "percent", "10.00", "10.00")


def test_an_approved_discount_must_be_applied(client, token, manager, shop, booked):
    request_id = approved(client, token, manager, booked, "amount", "15")
    return_all(client, token, booked)

    body = call(client, SETTLE, bill(booked, shop), token=token).json()

    assert body["code"] == "approved_discount_not_applied"
    assert body["data"] == {"retry": False, "request_sync_id": request_id, "kind": "discount", "discount_type": "amount",
                            "discount_value": "15.000"}
    assert Order.objects.get(pk=booked["sync_id"]).status == OrderStatus.ACTIVE


def test_a_revoked_or_unknown_request_cannot_be_applied(client, token, manager, shop, booked):
    request_id = approved(client, token, manager, booked, "percent", "10")
    decide(client, manager, REVOKE, request_id)
    return_all(client, token, booked)

    def settle_with(request_sync_id):
        return call(client, SETTLE, bill(
            booked, shop, tax_amount="4.714", net_amount="99.000", payments=refund(shop, "1.000"),
            manager_discount={"request_sync_id": request_sync_id, "discount_percentage": "10.00",
                              "discount_amount": "11.000"}), token=token).json()["code"]

    assert settle_with(request_id) == "discount_request_not_approved"
    assert settle_with(str(uuid7())) == "unknown_discount_request"
    # Revoked: the order settles without it.
    assert call(client, SETTLE, bill(booked, shop), token=token).json()["code"] == "ok"


def test_a_request_still_waiting_is_closed_by_the_settle(client, token, shop, booked):
    asked(client, token, booked)
    return_all(client, token, booked)

    assert call(client, SETTLE, bill(booked, shop), token=token).json()["code"] == "ok"

    req = OrderRequest.objects.get()
    assert (req.status, req.closed_at is not None) == (OrderRequestStatus.CLOSED, True)


def test_a_card_and_a_manager_discount_together_are_refused(client, token, shop, booked):
    body = call(client, SETTLE, bill(
        booked, shop,
        card_discount={"card_discount_id": 1, "discount_percentage": "5.00", "discount_amount": "5.500"},
        manager_discount={"request_sync_id": str(uuid7()), "discount_amount": "5.500"}), token=token).json()

    assert body["code"] == "invalid_request" and "manager_discount" in body["data"]["errors"]


# -- Web --------------------------------------------------------------------------------


WEB_LIST = "/rental/request/list/"


def test_the_web_list_has_a_tab_per_state(client_in, client, token, manager, booked):
    approved(client_in, token, manager, booked)

    response = client_in.get(WEB_LIST, {"tab": "approved"})

    assert response.status_code == 200
    counts = {t["value"]: t["count"] for t in response.context["tabs"]}
    assert counts == {"pending": 0, "approved": 1, "rejected": 0, "revoked": 0, "closed": 0}
    [row] = response.context["rows"]
    assert (row["order_no"], row["rule"], row["status"]) == (booked["order_no"], "10.00%", "approved")


def test_the_web_approves_as_an_amount_and_records_the_channel(client_in, token, booked):
    data = asked(client_in, token, booked)
    detail = f"/rental/request/{data['request_sync_id']}/"
    assert "Approve discount" in client_in.get(detail).content.decode()

    response = client_in.post(f"{detail}approve/", {"discount_type": "amount", "discount_value": "20.250"},
                              content_type="application/json")

    assert response.json()["ok"], response.json()
    req = OrderRequest.objects.get()
    assert (req.status, req.discount_value, req.decided_channel, req.decided_by.username) == (
        OrderRequestStatus.APPROVED, Decimal("20.250"), DecisionChannel.WEB, "sara.k")
    order_page = client_in.get(f"/rental/order/{booked['sync_id']}/").content.decode()
    assert detail in order_page and "AED 20.250" in order_page


def test_the_web_rejects_and_revokes(client_in, token, booked):
    first = asked(client_in, token, booked)
    url = f"/rental/request/{first['request_sync_id']}/"
    assert client_in.post(f"{url}reject/", {"note": "No"}, content_type="application/json").json()["ok"]
    second = asked(client_in, token, booked)
    url = f"/rental/request/{second['request_sync_id']}/"
    client_in.post(f"{url}approve/", {"discount_type": "percent", "discount_value": "5"},
                   content_type="application/json")

    response = client_in.post(f"{url}revoke/", {"note": "Oops"}, content_type="application/json")

    assert response.json()["ok"]
    assert OrderRequest.objects.get(pk=second["request_sync_id"]).revoked_channel == DecisionChannel.WEB
    again = client_in.post(f"{url}revoke/", {}, content_type="application/json").json()
    assert again["errors"][0]["message"] == "Only an approved request can be revoked."


def test_the_web_needs_approve_to_decide(client_in, token, booked):
    data = asked(client_in, token, booked)
    RolePermission.objects.filter(role=client_in.role, page__code="rental.request").update(can_approve=False)

    response = client_in.post(f"/rental/request/{data['request_sync_id']}/approve/",
                              {"discount_type": "percent", "discount_value": "5"}, content_type="application/json")

    assert response.status_code == 403
    assert OrderRequest.objects.get().status == OrderRequestStatus.PENDING


def test_the_request_endpoints_are_in_the_api_docs(client, db):
    schema = client.get("/api/schema/").content.decode()
    for path in ("orders/requests", "orders/requests/withdraw", "orders/requests/status", "requests/list",
                 "requests/approve", "requests/reject", "requests/revoke"):
        assert f"/api/v1/{{app}}/{path}" in schema, path
