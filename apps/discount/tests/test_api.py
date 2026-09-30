"""POST /api/v1/operator/card-discounts, through HTTP as the till app sees it.
Reuses the fares API's world: operator OPR001 on till-1, mapped to ADC1."""

import datetime
import zoneinfo

import pytest

from apps.discount import api
from apps.discount.models import (
    CardDiscount,
    CardDiscountDay,
    CardGrade,
    CardType,
    ClaimStatus,
    FareBasis,
    UsageType,
)
from apps.discount.tests.conftest import make_claim
from apps.fare.tests import test_api as fare_api
from apps.rental.models import Customer
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
