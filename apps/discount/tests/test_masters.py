"""Card Type and Card Grade: the drawer saves, company scoping, the unique
rules, in-use deletes and the screens."""

import pytest
from django.db import IntegrityError, transaction

from apps.discount.models import CardGrade, CardType
from apps.discount.tests.conftest import make_discount, post
from apps.portal.models import RolePermission

TYPE_SAVE, GRADE_SAVE = "/discount/card-type/save/", "/discount/card-grade/save/"


def test_a_card_type_saves_in_the_users_company_with_audit(client_in, world):
    body = post(client_in, TYPE_SAVE, {"company": world["theirs"].pk, "code": "CORP", "name": "Corporate",
                                       "is_active": True}).json()
    assert body["ok"] is True, body
    card_type = CardType.objects.get(pk=body["pk"])
    assert card_type.company == world["ours"]                  # posted company ignored
    assert card_type.created_by.username == "sara.k" == card_type.modified_by.username

    body = post(client_in, TYPE_SAVE, {"pk": card_type.pk, "code": "CORP", "name": "Corporate Plus",
                                       "is_active": True}).json()
    card_type.refresh_from_db()
    assert card_type.name == "Corporate Plus"


def test_a_duplicate_card_type_code_is_refused_per_company(client_in, world):
    response = post(client_in, TYPE_SAVE, {"code": "TOurs", "name": "Other", "is_active": True})
    assert response.status_code == 400
    assert "already exists" in response.json()["errors"][0]["message"]
    # The other company may use it.
    CardType.objects.create(company=world["theirs"], code="TOurs2", name="X")


def test_grade_codes_are_unique_within_their_card_type_only(world):
    our_type = world["our"]["type"]
    second = CardType.objects.create(company=world["ours"], code="T2", name="Second")
    CardGrade.objects.create(company=world["ours"], card_type=second, code="GOurs", name="Ours Gold")
    with pytest.raises(IntegrityError), transaction.atomic():
        CardGrade.objects.create(company=world["ours"], card_type=our_type, code="GOurs", name="Another")


def test_a_grade_saves_under_this_companys_card_type(client_in, world):
    body = post(client_in, GRADE_SAVE, {"card_type": world["our"]["type"].pk, "code": "SIL", "name": "Silver",
                                        "is_active": True}).json()
    assert body["ok"] is True, body
    assert CardGrade.objects.get(pk=body["pk"]).company == world["ours"]


def test_another_companys_card_type_cannot_be_chosen(client_in, world):
    response = post(client_in, GRADE_SAVE, {"card_type": world["their"]["type"].pk, "code": "SIL",
                                            "name": "Silver", "is_active": True})
    assert response.status_code == 400


def test_a_system_user_cannot_mix_companies(system_client, world):
    response = post(system_client, GRADE_SAVE, {"company": world["ours"].pk, "card_type": world["their"]["type"].pk,
                                                "code": "SIL", "name": "Silver", "is_active": True})
    assert response.status_code == 400
    assert "Choose a card type of this company." in str(response.json())


def test_another_companys_row_is_not_found(client_in, world):
    theirs = world["their"]["type"]
    assert post(client_in, TYPE_SAVE, {"pk": theirs.pk, "code": "X", "name": "X", "is_active": True}).status_code == 404
    assert post(client_in, f"/discount/card-type/{theirs.pk}/delete/", {}).status_code == 404


def test_a_card_type_with_grades_is_deactivated_not_deleted(client_in, world):
    card_type = world["our"]["type"]
    response = post(client_in, f"/discount/card-type/{card_type.pk}/delete/", {})
    assert response.status_code == 409
    assert response.json()["code"] == "in_use"
    card_type.refresh_from_db()
    assert card_type.is_active is False


def test_a_grade_used_by_a_discount_is_deactivated(client_in, world):
    grade = world["our"]["grade"]
    make_discount(grade)
    assert post(client_in, f"/discount/card-grade/{grade.pk}/delete/", {}).status_code == 409
    grade.refresh_from_db()
    assert grade.is_active is False


def test_an_unused_grade_is_deleted(client_in, world):
    grade = CardGrade.objects.create(company=world["ours"], card_type=world["our"]["type"], code="S", name="Silver")
    assert post(client_in, f"/discount/card-grade/{grade.pk}/delete/", {}).json()["ok"] is True
    assert not CardGrade.objects.filter(pk=grade.pk).exists()


@pytest.mark.parametrize("url, payload, action", [
    (TYPE_SAVE, {"code": "N", "name": "New", "is_active": True}, "can_create"),
    ("/discount/card-type/{pk}/delete/", {}, "can_delete"),
])
def test_each_write_needs_its_permission(client_in, world, url, payload, action):
    RolePermission.objects.filter(role=client_in.role, page__code="discount.card_type").update(**{action: False})
    response = post(client_in, url.format(pk=world["our"]["type"].pk), payload)
    assert response.status_code == 403


def test_the_screens_show_this_companys_rows_only(client_in, world):
    types = client_in.get("/discount/card-type/list/").content.decode()
    grades = client_in.get("/discount/card-grade/list/").content.decode()
    assert "Ours Card" in types and "Theirs Card" not in types
    assert "Ours Gold" in grades and "Theirs Gold" not in grades
    assert 'data-type="Ours Card"' in grades                    # the card type filter's key
