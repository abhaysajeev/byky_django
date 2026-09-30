"""POST /api/v1/operator/card-discounts, through HTTP as the till app sees it.
Reuses the fares API's world: operator OPR001 on till-1, mapped to ADC1."""

import datetime

from apps.discount import api
from apps.discount.models import (
    CardDiscount,
    CardDiscountDay,
    CardGrade,
    CardType,
    FareBasis,
    UsageType,
)
from apps.fare.tests import test_api as fare_api

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
