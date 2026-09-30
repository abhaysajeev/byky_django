"""Card Discount: the page's save (days, promotion, usage, overlap) and the
table's own rules."""

import datetime
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction

from apps.discount.models import CardDiscount, FareBasis, UsageType
from apps.discount.tests.conftest import make_discount, post

SAVE = "/discount/card-discount/save/"


def payload(world, **overrides):
    data = {
        "card_type": world["our"]["type"].pk, "card_grade": world["our"]["grade"].pk,
        "valid_from": "2026-10-01", "valid_to": "2026-12-31",
        "all_days": True, "all_days_percent": "15", "days": [],
        "promotion": "approval_full", "usage_type": "per_week", "usage_limit": 3, "is_active": True,
    }
    data.update(overrides)
    return data


def errors(response):
    return {(e["field"], e["message"]) for e in response.json()["errors"]}


def test_all_days_saves_seven_equal_days(client_in, world):
    body = post(client_in, SAVE, payload(world)).json()
    assert body["ok"] is True, body
    discount = CardDiscount.objects.get(pk=body["pk"])
    assert discount.company == world["ours"]
    assert discount.requires_approval is True and discount.fare_basis == FareBasis.FULL
    assert discount.usage_type == UsageType.PER_WEEK and discount.usage_limit == 3
    assert [(d.weekday, d.discount_percent) for d in discount.days.all()] == [(n, Decimal("15.00")) for n in range(7)]
    assert discount.created_by.username == "sara.k"


def test_selected_days_save_their_own_percent_and_edit_rewrites_them(client_in, world):
    body = post(client_in, SAVE, payload(world, all_days=False, days=[
        {"weekday": 4, "percent": "20"}, {"weekday": 5, "percent": "25.5"}])).json()
    discount = CardDiscount.objects.get(pk=body["pk"])
    assert [(d.weekday, str(d.discount_percent)) for d in discount.days.all()] == [(4, "20.00"), (5, "25.50")]

    post(client_in, SAVE, payload(world, pk=discount.pk, all_days=False, days=[{"weekday": 0, "percent": "5"}]))
    assert [d.weekday for d in discount.days.all()] == [0]


def test_one_time_needs_no_count(client_in, world):
    body = post(client_in, SAVE, payload(world, usage_type="one_time", usage_limit=None)).json()
    assert CardDiscount.objects.get(pk=body["pk"]).usage_limit is None


@pytest.mark.parametrize("overrides, expected", [
    ({"card_grade": None}, ("Card Grade", "Choose a card grade.")),
    ({"valid_to": "2026-09-01"}, ("To Date", "Must be on or after From Date.")),
    ({"all_days_percent": "0"}, ("All Days", "Enter a discount between 0.01 and 100.")),
    ({"all_days_percent": "100.5"}, ("All Days", "Enter a discount between 0.01 and 100.")),
    ({"all_days": False, "days": []}, ("Days", "Choose all days or at least one day.")),
    ({"promotion": ""}, ("Promotion Type", "Choose a promotion type.")),
    ({"usage_limit": 0}, ("Usage Type", "Enter how many times, 1 or more.")),
])
def test_bad_input_is_refused(client_in, world, overrides, expected):
    response = post(client_in, SAVE, payload(world, **overrides))
    assert response.status_code == 400
    assert expected in errors(response)
    assert not CardDiscount.objects.exists()


def test_a_grade_of_another_type_is_refused(client_in, world):
    other_type = world["our"]["type"].__class__.objects.create(company=world["ours"], code="X", name="X")
    response = post(client_in, SAVE, payload(world, card_type=other_type.pk))
    assert ("Card Grade", "This grade is not of the chosen card type.") in errors(response)


def test_another_companys_grade_is_refused(client_in, world):
    response = post(client_in, SAVE, payload(world, card_type=world["their"]["type"].pk,
                                             card_grade=world["their"]["grade"].pk))
    assert ("Card Grade", "Choose a card grade.") in errors(response)


def test_an_overlapping_active_discount_is_refused(client_in, world):
    make_discount(world["our"]["grade"], start=datetime.date(2026, 12, 1), end=datetime.date(2027, 1, 31))
    response = post(client_in, SAVE, payload(world))
    assert ("From Date", "Another active discount covers these dates for this grade.") in errors(response)


def test_an_inactive_or_separate_discount_does_not_block(client_in, world):
    make_discount(world["our"]["grade"], start=datetime.date(2026, 11, 1), end=datetime.date(2026, 11, 30),
                  is_active=False)
    make_discount(world["our"]["grade"], start=datetime.date(2027, 1, 1), end=datetime.date(2027, 1, 31))
    assert post(client_in, SAVE, payload(world)).json()["ok"] is True
    # Saved inactive, it may overlap anything.
    assert post(client_in, SAVE, payload(world, is_active=False)).json()["ok"] is True


def test_editing_a_discount_does_not_overlap_itself(client_in, world):
    discount = make_discount(world["our"]["grade"])
    body = post(client_in, SAVE, payload(world, pk=discount.pk, valid_to="2026-11-30")).json()
    assert body["ok"] is True, body


def test_the_database_refuses_an_overlap(world):
    make_discount(world["our"]["grade"])
    with pytest.raises(IntegrityError), transaction.atomic():
        make_discount(world["our"]["grade"], start=datetime.date(2026, 12, 31), end=datetime.date(2027, 1, 5))


def test_the_database_checks_the_usage_limit(world):
    discount = make_discount(world["our"]["grade"])
    discount.usage_type, discount.usage_limit = UsageType.PER_DAY, None
    with pytest.raises(IntegrityError), transaction.atomic():
        discount.save()


def test_the_list_and_form_render(client_in, world):
    discount = make_discount(world["our"]["grade"])
    listing = client_in.get("/discount/card-discount/list/").content.decode()
    assert "Ours Gold" in listing and "All days · 10%" in listing and "2 times a day" in listing
    form = client_in.get(f"/discount/card-discount/{discount.pk}/edit/").content.decode()
    assert 'id="cd-initial"' in form and "Automatic – Base Fare" in form


def test_another_companys_discount_is_not_found(client_in, world):
    theirs = make_discount(world["their"]["grade"])
    assert client_in.get(f"/discount/card-discount/{theirs.pk}/edit/").status_code == 404
    assert post(client_in, SAVE, payload(world, pk=theirs.pk)).status_code == 404
