"""Writing Brand and Category rows: who may, what happens, and what a delete
means.

Same shape as apps/company/tests/test_crud.py -- reusing the generic
save/delete views from apps.company.writes directly, so these tests also
cover that reuse holds for a second app.
"""

import json

import pytest

from apps.company.models import Company, Country, State
from apps.fleet.models import Brand, Category
from apps.portal.models import Role, RolePermission
from apps.portal.services import grant_all
from core.enums import ApprovalStatus, Channel
from core.models import User

PASSWORD = "Byky#2026"


@pytest.fixture
def world(db):
    uae = Country.objects.create(short_code="AE", name="United Arab Emirates")
    abu_dhabi = State.objects.create(country=uae, short_code="AUH", name="Abu Dhabi")
    byky = Company.objects.create(
        short_code="BYKY", name="BYKY", country=uae, state=abu_dhabi,
        phone_number="+9710000000", email="ops@byky.test",
    )
    other = Company.objects.create(
        short_code="OTHER", name="Other Co", country=uae, state=abu_dhabi,
        phone_number="+9711111111", email="ops@other.test",
    )
    return {"company": byky, "other": other}


@pytest.fixture
def client_in(client, world):
    role = Role.objects.create(company=world["company"], name="Administrator")
    grant_all(role)
    User.objects.create_user(
        "sara.k", PASSWORD, display_name="Sara K", company=world["company"], role=role,
        allowed_channels=[Channel.WEB],
    )
    client.post("/login/", {"username": "sara.k", "password": PASSWORD})
    client.role = role
    return client


def post(client, url, payload):
    return client.post(url, data=json.dumps(payload), content_type="application/json")


# --- create, update, delete ---------------------------------------------------

def test_create_a_brand(client_in, world):
    response = post(client_in, "/fleet/brand/save/", {
        "brand_code": "BYK", "brand_name": "Byky", "manufacturer": "Byky Motors",
        "website_link": "https://byky.test",
    })

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["message"] == "Brand saved."

    brand = Brand.objects.get(pk=body["pk"])
    assert brand.company == world["company"]              # from the user, not the payload
    assert brand.is_active is True
    assert brand.approval_status == ApprovalStatus.APPROVED
    assert brand.manufacturer == "Byky Motors"


def test_update_a_brand(client_in, world):
    brand = Brand.objects.create(company=world["company"], brand_code="BYK", brand_name="Byky")

    post(client_in, "/fleet/brand/save/",
         {"pk": brand.pk, "brand_code": "BYK", "brand_name": "Byky Cycles"})

    brand.refresh_from_db()
    assert brand.brand_name == "Byky Cycles"
    assert Brand.objects.count() == 1                     # updated, not duplicated


def test_delete_an_unused_brand(client_in, world):
    brand = Brand.objects.create(company=world["company"], brand_code="BYK", brand_name="Byky")

    response = post(client_in, f"/fleet/brand/{brand.pk}/delete/", {})

    assert response.json()["ok"] is True
    assert Brand.objects.count() == 0


def test_description_and_website_are_optional(client_in, world):
    response = post(client_in, "/fleet/brand/save/", {"brand_code": "BYK", "brand_name": "Byky"})

    assert response.status_code == 200
    brand = Brand.objects.get(pk=response.json()["pk"])
    assert brand.description == ""
    assert brand.manufacturer == ""
    assert brand.website_link == ""


# --- validation -----------------------------------------------------------------

def test_a_duplicate_code_is_reported_in_words(client_in, world):
    Brand.objects.create(company=world["company"], brand_code="BYK", brand_name="Byky")

    response = post(client_in, "/fleet/brand/save/", {"brand_code": "BYK", "brand_name": "Byky Two"})

    assert response.status_code == 400
    assert any("already exists" in error["message"] for error in response.json()["errors"])


def test_missing_required_fields_are_reported_at_once(client_in):
    response = post(client_in, "/fleet/brand/save/", {"brand_code": "", "brand_name": ""})

    assert response.status_code == 400
    errors = response.json()["errors"]
    fields = {error["field"] for error in errors}

    assert len(errors) >= 2
    assert "Brand Code" in fields                          # labelled as the drawer labels it
    assert "brand_code" not in fields                       # never a column name


# --- permission on the write -----------------------------------------------------

@pytest.mark.parametrize("action,payload", [
    ("create", {"brand_code": "X", "brand_name": "X"}),
    ("update", {"pk": 0, "brand_code": "X", "brand_name": "X"}),
])
def test_a_role_without_the_action_is_refused(client_in, world, action, payload):
    brand = Brand.objects.create(company=world["company"], brand_code="BYK", brand_name="Byky")
    if payload.get("pk") == 0:
        payload["pk"] = brand.pk
    RolePermission.objects.filter(
        role=client_in.role, page__code="fleet.brand"
    ).update(**{f"can_{action}": False})

    response = post(client_in, "/fleet/brand/save/", payload)

    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"


def test_a_role_without_delete_is_refused(client_in, world):
    brand = Brand.objects.create(company=world["company"], brand_code="BYK", brand_name="Byky")
    RolePermission.objects.filter(
        role=client_in.role, page__code="fleet.brand"
    ).update(can_delete=False)

    response = post(client_in, f"/fleet/brand/{brand.pk}/delete/", {})

    assert response.status_code == 403
    assert Brand.objects.count() == 1


# --- scope on the write -----------------------------------------------------------

def test_a_company_user_cannot_edit_another_companys_row(client_in, world):
    theirs = Brand.objects.create(company=world["other"], brand_code="BYK", brand_name="Their Byky")

    response = post(client_in, "/fleet/brand/save/",
                     {"pk": theirs.pk, "brand_code": "BYK", "brand_name": "Hijacked"})

    assert response.status_code == 404                    # not even acknowledged
    theirs.refresh_from_db()
    assert theirs.brand_name == "Their Byky"


def test_a_company_user_cannot_post_a_different_company(client_in, world):
    response = post(client_in, "/fleet/brand/save/",
                     {"brand_code": "BYK", "brand_name": "Byky", "company": world["other"].pk})

    assert response.json()["ok"] is True
    brand = Brand.objects.get(pk=response.json()["pk"])
    assert brand.company == world["company"]               # the payload was ignored


def test_a_company_user_cannot_delete_another_companys_row(client_in, world):
    theirs = Brand.objects.create(company=world["other"], brand_code="BYK", brand_name="Their Byky")

    response = post(client_in, f"/fleet/brand/{theirs.pk}/delete/", {})

    assert response.status_code == 404
    assert Brand.objects.filter(pk=theirs.pk).exists()


def test_two_companies_may_use_the_same_brand_code(world):
    ours = Brand.objects.create(company=world["company"], brand_code="BYK", brand_name="Ours")
    theirs = Brand.objects.create(company=world["other"], brand_code="BYK", brand_name="Theirs")

    assert (ours.brand_code, theirs.brand_code) == ("BYK", "BYK")


def test_one_company_cannot_reuse_its_own_brand_code(world):
    from django.db import IntegrityError

    Brand.objects.create(company=world["company"], brand_code="BYK", brand_name="First")
    with pytest.raises(IntegrityError):
        Brand.objects.create(company=world["company"], brand_code="BYK", brand_name="Second")


# =============================== Category ========================================
# --- create, update, delete ---------------------------------------------------

def test_create_a_category(client_in, world):
    response = post(client_in, "/fleet/category/save/", {
        "category_code": "BYKY", "category_name": "Byky", "description": "Main vehicle line",
    })

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["message"] == "Category saved."

    category = Category.objects.get(pk=body["pk"])
    assert category.company == world["company"]           # from the user, not the payload
    assert category.is_active is True
    assert category.approval_status == ApprovalStatus.APPROVED
    assert category.description == "Main vehicle line"


def test_update_a_category(client_in, world):
    category = Category.objects.create(company=world["company"], category_code="BYKY", category_name="Byky")

    post(client_in, "/fleet/category/save/",
         {"pk": category.pk, "category_code": "BYKY", "category_name": "Byky Cycles"})

    category.refresh_from_db()
    assert category.category_name == "Byky Cycles"
    assert Category.objects.count() == 1                  # updated, not duplicated


def test_delete_an_unused_category(client_in, world):
    category = Category.objects.create(company=world["company"], category_code="BYKY", category_name="Byky")

    response = post(client_in, f"/fleet/category/{category.pk}/delete/", {})

    assert response.json()["ok"] is True
    assert Category.objects.count() == 0


def test_category_description_is_optional(client_in, world):
    response = post(client_in, "/fleet/category/save/", {"category_code": "BYKY", "category_name": "Byky"})

    assert response.status_code == 200
    category = Category.objects.get(pk=response.json()["pk"])
    assert category.description == ""


# --- validation -----------------------------------------------------------------

def test_a_duplicate_category_code_is_reported_in_words(client_in, world):
    Category.objects.create(company=world["company"], category_code="BYKY", category_name="Byky")

    response = post(client_in, "/fleet/category/save/", {"category_code": "BYKY", "category_name": "Byky Two"})

    assert response.status_code == 400
    assert any("already exists" in error["message"] for error in response.json()["errors"])


def test_missing_required_category_fields_are_reported_at_once(client_in):
    response = post(client_in, "/fleet/category/save/", {"category_code": "", "category_name": ""})

    assert response.status_code == 400
    errors = response.json()["errors"]
    fields = {error["field"] for error in errors}

    assert len(errors) >= 2
    assert "Category Code" in fields                       # labelled as the drawer labels it
    assert "category_code" not in fields                    # never a column name


# --- permission on the write -----------------------------------------------------

@pytest.mark.parametrize("action,payload", [
    ("create", {"category_code": "X", "category_name": "X"}),
    ("update", {"pk": 0, "category_code": "X", "category_name": "X"}),
])
def test_a_role_without_the_category_action_is_refused(client_in, world, action, payload):
    category = Category.objects.create(company=world["company"], category_code="BYKY", category_name="Byky")
    if payload.get("pk") == 0:
        payload["pk"] = category.pk
    RolePermission.objects.filter(
        role=client_in.role, page__code="fleet.category"
    ).update(**{f"can_{action}": False})

    response = post(client_in, "/fleet/category/save/", payload)

    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"


def test_a_role_without_category_delete_is_refused(client_in, world):
    category = Category.objects.create(company=world["company"], category_code="BYKY", category_name="Byky")
    RolePermission.objects.filter(
        role=client_in.role, page__code="fleet.category"
    ).update(can_delete=False)

    response = post(client_in, f"/fleet/category/{category.pk}/delete/", {})

    assert response.status_code == 403
    assert Category.objects.count() == 1


# --- scope on the write -----------------------------------------------------------

def test_a_company_user_cannot_edit_another_companys_category(client_in, world):
    theirs = Category.objects.create(company=world["other"], category_code="BYKY", category_name="Their Byky")

    response = post(client_in, "/fleet/category/save/",
                     {"pk": theirs.pk, "category_code": "BYKY", "category_name": "Hijacked"})

    assert response.status_code == 404                    # not even acknowledged
    theirs.refresh_from_db()
    assert theirs.category_name == "Their Byky"


def test_a_company_user_cannot_post_a_different_company_for_category(client_in, world):
    response = post(client_in, "/fleet/category/save/",
                     {"category_code": "BYKY", "category_name": "Byky", "company": world["other"].pk})

    assert response.json()["ok"] is True
    category = Category.objects.get(pk=response.json()["pk"])
    assert category.company == world["company"]            # the payload was ignored


def test_a_company_user_cannot_delete_another_companys_category(client_in, world):
    theirs = Category.objects.create(company=world["other"], category_code="BYKY", category_name="Their Byky")

    response = post(client_in, f"/fleet/category/{theirs.pk}/delete/", {})

    assert response.status_code == 404
    assert Category.objects.filter(pk=theirs.pk).exists()


def test_two_companies_may_use_the_same_category_code(world):
    ours = Category.objects.create(company=world["company"], category_code="BYKY", category_name="Ours")
    theirs = Category.objects.create(company=world["other"], category_code="BYKY", category_name="Theirs")

    assert (ours.category_code, theirs.category_code) == ("BYKY", "BYKY")


def test_one_company_cannot_reuse_its_own_category_code(world):
    from django.db import IntegrityError

    Category.objects.create(company=world["company"], category_code="BYKY", category_name="First")
    with pytest.raises(IntegrityError):
        Category.objects.create(company=world["company"], category_code="BYKY", category_name="Second")
