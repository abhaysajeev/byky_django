"""POST /api/v1/operator/card-discounts, through HTTP as the till app sees it.
Reuses the fares API's world: operator OPR001 on till-1, mapped to ADC1."""

import datetime
import zoneinfo
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.company.models import Branch, BranchType
from apps.discount import api
from apps.discount.models import (
    CardDiscount,
    CardDiscountClaim,
    CardDiscountDay,
    CardGrade,
    CardType,
    ClaimStatus,
    FareBasis,
    UsageType,
)
from apps.discount.tests.conftest import make_claim, make_order
from apps.fare.tests import test_api as fare_api
from apps.rental.models import Customer
from core.ids import uuid7
from core.timezones import business_date_for

# The signed-in till from the fare API tests, reused as fixtures here.
call, shape = fare_api.call, fare_api.shape
world, token, fresh_throttle = fare_api.world, fare_api.token, fare_api.fresh_throttle

URL = "/api/v1/operator/card-discounts"
D = datetime.date


def discount(grade, start, end, days, *, usage=UsageType.PER_WEEK, limit=2, approval=True,
             basis=FareBasis.BASE, active=True):
    made = CardDiscount.objects.create(
        company=grade.company, card_grade=grade, valid_from=start, valid_to=end, requires_approval=approval,
        fare_basis=basis, usage_type=usage, usage_limit=limit, is_active=active,
    )
    for day, percent in days.items():
        CardDiscountDay.objects.create(card_discount=made, weekday=day, discount_percent=percent)
    return made


def fetch(client, token):
    response = call(client, URL, token=token)
    assert response.status_code == 200, response.json()
    return response.json()


def test_every_type_grade_and_discount_comes_as_it_is(client, world, token):
    company = world["company"]
    corp = CardType.objects.create(company=company, code="CORP", name="Corporate")
    loyal = CardType.objects.create(company=company, code="LOY", name="Loyalty", is_active=False)
    gold = CardGrade.objects.create(company=company, card_type=corp, code="GOLD", name="Gold")
    silver = CardGrade.objects.create(company=company, card_type=corp, code="SILV", name="Silver", is_active=False)
    CardGrade.objects.create(company=company, card_type=corp, code="PLAT", name="Platinum")        # no discount
    expired = discount(gold, D(2025, 1, 1), D(2025, 12, 31), {day: "10" for day in range(7)}, active=False)
    current = discount(gold, D(2026, 9, 1), D(2026, 12, 31), {day: "15" for day in range(7)})
    once = discount(silver, D(2026, 10, 1), D(2026, 12, 31), {4: "20", 5: "25.5"},
                    usage=UsageType.ONE_TIME, limit=None, approval=False, basis=FareBasis.FULL)

    body = fetch(client, token)
    assert body["code"] == "ok" and body["message"] == "Card discounts."
    types = body["data"]["card_types"]
    assert [(t["name"], t["is_active"]) for t in types] == [("Corporate", True), ("Loyalty", False)]
    assert types[1]["card_type_id"] == loyal.pk and types[1]["grades"] == []

    grades = types[0]["grades"]
    assert [(g["name"], g["is_active"]) for g in grades] == [("Gold", True), ("Platinum", True), ("Silver", False)]
    assert grades[1]["discounts"] == []                                     # a grade with no discount is still sent

    gold_discounts = grades[0]["discounts"]
    assert [d["card_discount_id"] for d in gold_discounts] == [current.pk, expired.pk]     # newest first
    assert gold_discounts[1]["is_active"] is False and gold_discounts[1]["valid_to"] == "2025-12-31"
    assert gold_discounts[0] | {"days": None} == {
        "card_discount_id": current.pk, "valid_from": "2026-09-01", "valid_to": "2026-12-31",
        "is_active": True, "requires_approval": True, "fare_basis": "base",
        "usage_type": "per_week", "usage_limit": 2, "days": None,
    }
    assert gold_discounts[0]["days"][0] == {"weekday": 0, "day": "Monday", "discount_percent": "15.00"}
    assert len(gold_discounts[0]["days"]) == 7

    [silver_discount] = grades[2]["discounts"]
    assert silver_discount["card_discount_id"] == once.pk
    assert silver_discount["usage_type"] == "one_time" and silver_discount["usage_limit"] is None
    assert silver_discount["requires_approval"] is False and silver_discount["fare_basis"] == "full"
    assert silver_discount["days"] == [
        {"weekday": 4, "day": "Friday", "discount_percent": "20.00"},
        {"weekday": 5, "day": "Saturday", "discount_percent": "25.50"},
    ]
    # The Swagger example shows exactly what a device gets (compared on the
    # populated card type -- shape() merges a list, so a trailing empty one
    # would hide the rest).
    assert shape({"card_types": types[:1]}) == shape(api._CARD_DISCOUNTS_SAMPLE)


def test_another_companys_cards_are_never_sent(client, world, token):
    theirs = CardType.objects.create(company=world["other"], code="X", name="Theirs")
    CardGrade.objects.create(company=world["other"], card_type=theirs, code="G", name="Gold")
    assert fetch(client, token)["data"] == {"card_types": []}


def test_nothing_configured_is_an_empty_list(client, world, token):
    assert fetch(client, token)["data"] == {"card_types": []}


def test_only_the_operator_app(client, world, token):
    for app in ("employee", "manager"):
        response = call(client, f"/api/v1/{app}/card-discounts", token=token)
        assert response.status_code == 403
        assert response.json()["code"] == "wrong_channel"


def test_signed_out_is_refused(client, world):
    response = call(client, URL)
    assert response.status_code == 401
    assert response.json()["code"] == "not_authenticated"


def test_an_inactive_station_is_refused(client, world, token):
    world["adc1"].is_active = False
    world["adc1"].save(update_fields=["is_active"])
    response = call(client, URL, token=token)
    assert response.status_code == 409
    assert response.json()["code"] == "branch_inactive"


def test_the_endpoint_is_in_the_docs(client, world):
    schema = client.get("/api/schema/").content.decode()
    assert "/api/v1/{app}/card-discounts" in schema and "Operator Discount Cards" in schema


# -- POST /card-discounts/usage ------------------------------------------------------

USAGE = "/api/v1/operator/card-discounts/usage"
DUBAI = zoneinfo.ZoneInfo("Asia/Dubai")


def at(day, hh, mm=0):
    """A company-local moment."""
    return datetime.datetime.combine(day, datetime.time(hh, mm), tzinfo=DUBAI)


def ago(today, days):
    return today - datetime.timedelta(days=days)


@pytest.fixture
def usage_world(world):
    company = world["company"]
    today = business_date_for(company)
    corp = CardType.objects.create(company=company, code="CORP", name="Corporate")
    gold = CardGrade.objects.create(company=company, card_type=corp, code="GOLD", name="Gold")
    silver = CardGrade.objects.create(company=company, card_type=corp, code="SILV", name="Silver")
    retired = CardGrade.objects.create(company=company, card_type=corp, code="OLD", name="Old", is_active=False)
    week = {day: "15" for day in range(7)}
    current = discount(gold, ago(today, 30), ago(today, -30), week)
    once = discount(silver, ago(today, 5), ago(today, -5), week, usage=UsageType.ONE_TIME, limit=None, approval=False)
    # Not listed: one not valid today, one under an inactive grade, an inactive one.
    discount(silver, ago(today, 90), ago(today, 60), week)
    discount(retired, ago(today, 5), ago(today, -5), week)
    discount(gold, ago(today, -40), ago(today, -60), week, active=False)
    customer = Customer.objects.create(company=company, customer_code="CU001", first_name="Ahmed", last_name="Ali",
                                       mobile_country_code="+971", mobile_no="501234567")
    return {"today": today, "current": current, "once": once, "customer": customer}


def usage(client, token, full_number="971501234567"):
    return call(client, USAGE, {"full_number": full_number}, token=token)


def stamp(moment):
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def test_usage_counts_only_redeemed_claims_since_the_discounts_start(client, world, token, usage_world):
    today, current, customer = usage_world["today"], usage_world["current"], usage_world["customer"]
    make_claim(current, customer, status=ClaimStatus.REDEEMED, when=at(ago(today, 2), 18, 30))
    make_claim(current, customer, status=ClaimStatus.REDEEMED, when=at(ago(today, 9), 10, 4))
    make_claim(current, customer, status=ClaimStatus.REDEEMED, when=at(ago(today, 40), 9))   # before valid_from
    for status in (ClaimStatus.PENDING, ClaimStatus.APPROVED, ClaimStatus.REJECTED):         # never counted
        make_claim(current, customer, status=status, when=at(today, 0, 5))
    stranger = Customer.objects.create(company=world["company"], customer_code="CU002", first_name="Other",
                                       mobile_country_code="+971", mobile_no="509999999")
    make_claim(current, stranger, status=ClaimStatus.REDEEMED, when=at(today, 0, 5))

    body = usage(client, token).json()
    assert body["code"] == "ok" and body["message"] == "Card usage."
    data = body["data"]
    assert data["customer"] == {"customer_id": customer.pk, "customer_code": "CU001", "name": "Ahmed Ali"}
    assert [(g["card_type_name"], g["card_grade_name"]) for g in data["grades"]] == [
        ("Corporate", "Gold"), ("Corporate", "Silver")]

    gold, silver = data["grades"]
    assert gold["card_discount_id"] == current.pk and gold["card_grade_id"] == current.card_grade_id
    assert gold["usage_type"] == "per_week" and gold["usage_limit"] == 2
    assert gold["used"] == 2
    assert gold["redemptions"] == [stamp(at(ago(today, 9), 10, 4)), stamp(at(ago(today, 2), 18, 30))]
    assert silver["usage_type"] == "one_time" and silver["usage_limit"] is None
    assert silver["used"] == 0 and silver["redemptions"] == []
    assert shape(data) == shape(api._CARD_USAGE_SAMPLE)


def test_an_unknown_number_is_no_customer(client, world, token, usage_world):
    response = usage(client, token, "971500000000")
    assert response.status_code == 404
    assert response.json()["code"] == "unknown_customer"
    assert response.json()["message"] == "No customer found."


def test_another_companys_customer_is_no_customer(client, world, token, usage_world):
    Customer.objects.create(company=world["other"], customer_code="CU001", first_name="Theirs",
                            mobile_country_code="+971", mobile_no="508888888")
    assert usage(client, token, "971508888888").json()["code"] == "unknown_customer"


def test_a_blocked_customer_is_refused(client, world, token, usage_world):
    Customer.objects.filter(pk=usage_world["customer"].pk).update(is_blocked=True)
    response = usage(client, token)
    assert response.status_code == 403
    assert response.json() == {"code": "customer_blocked", "message": "The customer is blocked.", "data": {}}


@pytest.mark.parametrize("request_data, message", [
    ({}, "is required"),
    ({"full_number": "+971501234567"}, "must be digits only"),
])
def test_the_full_number_is_checked(client, world, token, request_data, message):
    response = call(client, USAGE, request_data, token=token)
    assert response.status_code == 400
    assert response.json()["data"]["errors"]["full_number"] == message


def test_usage_is_for_the_operator_app_only(client, world, token):
    response = call(client, "/api/v1/manager/card-discounts/usage", {"full_number": "971501234567"}, token=token)
    assert response.status_code == 403
    assert response.json()["code"] == "wrong_channel"
    assert call(client, USAGE, {"full_number": "971501234567"}).status_code == 401


def test_usage_is_in_the_docs(client, world):
    assert "/api/v1/{app}/card-discounts/usage" in client.get("/api/schema/").content.decode()


# -- POST /card-discounts/approval ---------------------------------------------------

APPROVAL = "/api/v1/operator/card-discounts/approval"


@pytest.fixture
def approval_world(usage_world):
    customer = usage_world["customer"]
    return {**usage_world, "order": make_order(customer, order_no="ORD-1001")}


def approval_data(approval_world, **overrides):
    data = {
        "sync_id": str(uuid7()), "card_discount_id": approval_world["current"].pk,
        "full_number": "971501234567", "order_id": str(approval_world["order"].pk),
        "bill_amount": "120.00", "discount_percent": "15", "discount_amount": "18.00", "net_amount": "102.00",
        "requested_at": "2026-09-30 17:05:00", "card_number": "4455",
        "card_photo": "https://photos.example/cards/4455.jpg",
    }
    data.update(overrides)
    return data


def request_approval(client, token, data):
    return call(client, APPROVAL, data, token=token)


def test_a_request_creates_a_pending_claim_with_the_devices_figures(client, world, token, approval_world):
    data = approval_data(approval_world)
    body = request_approval(client, token, data).json()
    assert body["code"] == "ok" and body["message"] == "Approval requested."
    assert body["data"] == {"sync_id": data["sync_id"], "status": "pending",
                            "order_id": data["order_id"], "card_discount_id": approval_world["current"].pk}
    assert shape(body["data"]) == shape(api._APPROVAL_SAMPLE)

    claim = CardDiscountClaim.objects.get(pk=data["sync_id"])
    assert claim.status == ClaimStatus.PENDING and claim.requires_approval is True
    assert claim.order == approval_world["order"] and claim.customer == approval_world["customer"]
    assert (claim.bill_amount, claim.discount_percent, claim.discount_amount, claim.net_amount) == (
        Decimal("120.00"), Decimal("15.00"), Decimal("18.00"), Decimal("102.00"))
    assert claim.card_type.name == "Corporate" and claim.card_grade.name == "Gold" and claim.fare_basis == "base"
    assert claim.card_number == "4455" and claim.card_photo.endswith("4455.jpg")
    assert claim.branch == world["adc1"] and claim.requested_by.username == "opr001"
    assert claim.rms_installation_id == "till-1"
    assert claim.requested_at == datetime.datetime(2026, 9, 30, 13, 5, tzinfo=datetime.UTC)   # 17:05 Dubai


def test_a_resent_sync_id_answers_with_the_current_status(client, world, token, approval_world):
    data = approval_data(approval_world)
    request_approval(client, token, data)
    claim = CardDiscountClaim.objects.get(pk=data["sync_id"])
    claim.status, claim.decided_at = ClaimStatus.APPROVED, claim.requested_at
    claim.save(update_fields=["status", "decided_at"])

    body = request_approval(client, token, data).json()
    assert body["code"] == "duplicate" and body["data"]["status"] == "approved"
    assert CardDiscountClaim.objects.count() == 1


def test_one_pending_request_per_order(client, world, token, approval_world):
    request_approval(client, token, approval_data(approval_world))
    response = request_approval(client, token, approval_data(approval_world))
    assert response.status_code == 409
    assert response.json()["code"] == "request_pending"


def test_a_decided_request_does_not_block_a_new_one(client, world, token, approval_world):
    first = approval_data(approval_world)
    request_approval(client, token, first)
    CardDiscountClaim.objects.filter(pk=first["sync_id"]).update(status=ClaimStatus.REJECTED,
                                                                 decided_at=timezone.now())
    assert request_approval(client, token, approval_data(approval_world)).json()["code"] == "ok"


@pytest.mark.parametrize("change, status, code", [
    ({"order_id": "01923e1c-0a11-7b22-8c33-d4e5f6a7b8c9"}, 404, "unknown_order"),
    ({"full_number": "971500000000"}, 404, "unknown_customer"),
    ({"card_discount_id": 999999}, 404, "unknown_card_discount"),
])
def test_unknown_references_are_refused(client, world, token, approval_world, change, status, code):
    response = request_approval(client, token, approval_data(approval_world, **change))
    assert response.status_code == status
    assert response.json()["code"] == code
    assert not CardDiscountClaim.objects.exists()


def test_an_automatic_discount_needs_no_approval(client, world, token, approval_world):
    response = request_approval(client, token, approval_data(approval_world,
                                                             card_discount_id=approval_world["once"].pk))
    assert response.status_code == 400
    assert response.json()["code"] == "approval_not_needed"


def test_a_blocked_customer_is_refused_a_request(client, world, token, approval_world):
    Customer.objects.filter(pk=approval_world["customer"].pk).update(is_blocked=True)
    assert request_approval(client, token, approval_data(approval_world)).json()["code"] == "customer_blocked"


def test_another_companys_order_is_unknown(client, world, token, approval_world):
    theirs = Customer.objects.create(company=world["other"], customer_code="CU009", first_name="Theirs",
                                     mobile_country_code="+971", mobile_no="507777777")
    Branch.objects.create(company=world["other"], location=world["adc1"].location, short_code="OT1",
                          name="Their Station", branch_type=BranchType.STATION)
    their_order = make_order(theirs)
    response = request_approval(client, token, approval_data(approval_world, order_id=str(their_order.pk)))
    assert response.json()["code"] == "unknown_order"


@pytest.mark.parametrize("field, value, message", [
    ("sync_id", "6f1c2b9e-3d4a-4b8c-9e2f-0a1b2c3d4e5f", "must be a UUIDv7"),
    ("discount_percent", "0", "must be more than 0"),
    ("net_amount", "-1", "must be 0 or more"),
    ("requested_at", "30/09/2026 17:05", "must be a date-time like 2026-09-30 17:05:00"),
])
def test_bad_request_fields_are_refused(client, world, token, approval_world, field, value, message):
    response = request_approval(client, token, approval_data(approval_world, **{field: value}))
    assert response.status_code == 400
    assert response.json()["data"]["errors"][field] == message


def test_approval_is_for_the_operator_app_only(client, world, token, approval_world):
    response = call(client, "/api/v1/manager/card-discounts/approval", approval_data(approval_world), token=token)
    assert response.json()["code"] == "wrong_channel"
    assert call(client, APPROVAL, approval_data(approval_world)).status_code == 401


def test_approval_is_in_the_docs(client, world):
    assert "/api/v1/{app}/card-discounts/approval" in client.get("/api/schema/").content.decode()


# -- POST /card-discounts/approval/status --------------------------------------------

STATUS = "/api/v1/operator/card-discounts/approval/status"


@pytest.fixture
def requested(client, world, token, approval_world):
    data = approval_data(approval_world)
    assert request_approval(client, token, data).json()["code"] == "ok"
    return {**approval_world, "data": data, "claim": CardDiscountClaim.objects.get(pk=data["sync_id"])}


def status(client, token, order_id, sync_id):
    return call(client, STATUS, {"order_id": str(order_id), "sync_id": str(sync_id)}, token=token)


def test_a_pending_request(client, world, token, requested):
    data = requested["data"]
    body = status(client, token, data["order_id"], data["sync_id"]).json()
    assert body["code"] == "ok" and body["message"] == "Approval status."
    assert body["data"] == {
        "sync_id": data["sync_id"], "order_id": data["order_id"], "status": "pending",
        "card_discount_id": requested["current"].pk, "card_type_name": "Corporate", "card_grade_name": "Gold",
        "bill_amount": "120.00", "discount_percent": "15.00", "discount_amount": "18.00", "net_amount": "102.00",
        "requested_at": "2026-09-30 17:05:00", "decided_at": None, "remarks": "",
    }
    assert shape({**body["data"], "decided_at": "x"}) == shape(api._STATUS_SAMPLE)


@pytest.mark.parametrize("approve, expected", [(True, "approved"), (False, "rejected")])
def test_a_decided_request(client, world, token, requested, approve, expected):
    from apps.discount import services

    approver = requested["claim"].requested_by
    services.decide_claim(approver, requested["claim"], approve, "Card checked")
    data = requested["data"]
    body = status(client, token, data["order_id"], data["sync_id"]).json()["data"]
    assert body["status"] == expected and body["remarks"] == "Card checked"
    assert body["decided_at"] is not None


def test_a_cancelled_request(client, world, token, requested):
    from apps.discount import services

    services.cancel_pending_for_order(requested["order"])
    data = requested["data"]
    body = status(client, token, data["order_id"], data["sync_id"]).json()["data"]
    assert body["status"] == "cancelled" and body["decided_at"] is not None


def test_the_order_and_request_must_agree(client, world, token, requested):
    other_order = make_order(requested["customer"], order_no="ORD-2002")
    for order_id, sync_id in [
        (other_order.pk, requested["data"]["sync_id"]),             # right request, wrong order
        (requested["data"]["order_id"], uuid7()),                    # unknown request
    ]:
        response = status(client, token, order_id, sync_id)
        assert response.status_code == 404
        assert response.json() == {"code": "request_not_found", "message": "No approval request found.",
                                   "data": {}}


def test_another_companys_request_is_not_found(client, world, token, requested):
    CardDiscountClaim.objects.filter(pk=requested["data"]["sync_id"]).update(company=world["other"])
    data = requested["data"]
    assert status(client, token, data["order_id"], data["sync_id"]).json()["code"] == "request_not_found"


@pytest.mark.parametrize("request_data, field, message", [
    ({"sync_id": "01923f8e-5b2a-7c3d-9e4f-a1b2c3d4e5f6"}, "order_id", "is required"),
    ({"order_id": "01923f8e-5b2a-7c3d-9e4f-a1b2c3d4e5f6"}, "sync_id", "is required"),
    ({"order_id": "nope", "sync_id": "01923f8e-5b2a-7c3d-9e4f-a1b2c3d4e5f6"}, "order_id", "must be an order's sync_id"),
])
def test_status_fields_are_checked(client, world, token, request_data, field, message):
    response = call(client, STATUS, request_data, token=token)
    assert response.status_code == 400
    assert response.json()["data"]["errors"][field] == message


def test_status_is_for_the_operator_app_only(client, world, token):
    ids = {"order_id": str(uuid7()), "sync_id": str(uuid7())}
    assert call(client, "/api/v1/manager/card-discounts/approval/status", ids, token=token).json()["code"] == \
        "wrong_channel"
    assert call(client, STATUS, ids).status_code == 401


def test_status_is_in_the_docs(client, world):
    assert "/api/v1/{app}/card-discounts/approval/status" in client.get("/api/schema/").content.decode()
