"""Writing Brand and Category rows: who may, what happens, and what a delete
means.

Same shape as apps/company/tests/test_crud.py -- reusing the generic
save/delete views from apps.company.writes directly, so these tests also
cover that reuse holds for a second app.
"""

import json

import pytest

from apps.company.models import Company, Country, State
from apps.fleet.models import UOM, Asset, AssetType, Brand, Category, Vehicle, VehicleType
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


# =============================== Vehicle Type =====================================

@pytest.fixture
def taxonomy(world):
    """A category and a brand for each company, so a vehicle type has
    something real to point at -- and so the cross-company rejection tests
    have another company's category/brand to try posting."""
    return {
        "category": Category.objects.create(
            company=world["company"], category_code="BYKY", category_name="Byky"
        ),
        "brand": Brand.objects.create(
            company=world["company"], brand_code="BYK", brand_name="Byky"
        ),
        "their_category": Category.objects.create(
            company=world["other"], category_code="OTH", category_name="Their Category"
        ),
        "their_brand": Brand.objects.create(
            company=world["other"], brand_code="OTH", brand_name="Their Brand"
        ),
    }


# --- create, update, delete ---------------------------------------------------

def test_create_a_vehicle_type(client_in, world, taxonomy):
    response = post(client_in, "/fleet/vehicle-type/save/", {
        "category": taxonomy["category"].pk, "brand": taxonomy["brand"].pk,
        "vehicle_type_name": "Monaco", "vehicle_type_code": "MON",
    })

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["message"] == "Vehicle Type saved."

    vehicle_type = VehicleType.objects.get(pk=body["pk"])
    assert vehicle_type.company == world["company"]        # from the user, not the payload
    assert vehicle_type.is_active is True
    assert vehicle_type.approval_status == ApprovalStatus.APPROVED
    assert vehicle_type.category == taxonomy["category"]
    assert vehicle_type.brand == taxonomy["brand"]


def test_vehicle_type_code_is_optional(client_in, world, taxonomy):
    response = post(client_in, "/fleet/vehicle-type/save/", {
        "category": taxonomy["category"].pk, "brand": taxonomy["brand"].pk,
        "vehicle_type_name": "Monaco",
    })

    assert response.status_code == 200
    vehicle_type = VehicleType.objects.get(pk=response.json()["pk"])
    assert vehicle_type.vehicle_type_code == ""


def test_tax_percentage_and_other_tax_are_optional(client_in, world, taxonomy):
    response = post(client_in, "/fleet/vehicle-type/save/", {
        "category": taxonomy["category"].pk, "brand": taxonomy["brand"].pk,
        "vehicle_type_name": "Monaco",
    })

    assert response.status_code == 200
    vehicle_type = VehicleType.objects.get(pk=response.json()["pk"])
    assert vehicle_type.tax_percentage is None
    assert vehicle_type.other_tax is None


def test_tax_percentage_and_other_tax_save_when_given(client_in, world, taxonomy):
    response = post(client_in, "/fleet/vehicle-type/save/", {
        "category": taxonomy["category"].pk, "brand": taxonomy["brand"].pk,
        "vehicle_type_name": "Monaco", "tax_percentage": "5.00", "other_tax": "1.50",
    })

    assert response.status_code == 200
    vehicle_type = VehicleType.objects.get(pk=response.json()["pk"])
    assert str(vehicle_type.tax_percentage) == "5.00"
    assert str(vehicle_type.other_tax) == "1.50"


def test_update_a_vehicle_type(client_in, world, taxonomy):
    vehicle_type = VehicleType.objects.create(
        company=world["company"], category=taxonomy["category"], brand=taxonomy["brand"],
        vehicle_type_name="Monaco",
    )

    post(client_in, "/fleet/vehicle-type/save/", {
        "pk": vehicle_type.pk, "category": taxonomy["category"].pk, "brand": taxonomy["brand"].pk,
        "vehicle_type_name": "Monaco XL",
    })

    vehicle_type.refresh_from_db()
    assert vehicle_type.vehicle_type_name == "Monaco XL"
    assert VehicleType.objects.count() == 1                # updated, not duplicated


def test_delete_an_unused_vehicle_type(client_in, world, taxonomy):
    vehicle_type = VehicleType.objects.create(
        company=world["company"], category=taxonomy["category"], brand=taxonomy["brand"],
        vehicle_type_name="Monaco",
    )

    response = post(client_in, f"/fleet/vehicle-type/{vehicle_type.pk}/delete/", {})

    assert response.json()["ok"] is True
    assert VehicleType.objects.count() == 0


# --- validation -----------------------------------------------------------------

def test_missing_required_vehicle_type_fields_are_reported_at_once(client_in, world, taxonomy):
    response = post(client_in, "/fleet/vehicle-type/save/", {
        "category": taxonomy["category"].pk, "brand": taxonomy["brand"].pk, "vehicle_type_name": "",
    })

    assert response.status_code == 400
    errors = response.json()["errors"]
    fields = {error["field"] for error in errors}

    assert "Vehicle Type Name" in fields                    # labelled as the drawer labels it
    assert "vehicle_type_name" not in fields                 # never a column name


def test_a_company_user_cannot_pick_another_companys_category(client_in, world, taxonomy):
    """VehicleTypeForm scopes the category/brand querysets to the signed-in
    user's own company (apps/fleet/forms.py) -- posting another company's id
    must be rejected the same way an out-of-range choice is, not silently
    accepted and left pointing at data the user cannot even see."""
    response = post(client_in, "/fleet/vehicle-type/save/", {
        "category": taxonomy["their_category"].pk, "brand": taxonomy["brand"].pk,
        "vehicle_type_name": "Monaco",
    })

    assert response.status_code == 400
    assert any("valid choice" in error["message"] for error in response.json()["errors"])
    assert VehicleType.objects.count() == 0


def test_a_company_user_cannot_pick_another_companys_brand(client_in, world, taxonomy):
    response = post(client_in, "/fleet/vehicle-type/save/", {
        "category": taxonomy["category"].pk, "brand": taxonomy["their_brand"].pk,
        "vehicle_type_name": "Monaco",
    })

    assert response.status_code == 400
    assert any("valid choice" in error["message"] for error in response.json()["errors"])
    assert VehicleType.objects.count() == 0


# --- permission on the write -----------------------------------------------------

@pytest.mark.parametrize("action", ["create", "update"])
def test_a_role_without_the_vehicle_type_action_is_refused(client_in, world, taxonomy, action):
    vehicle_type = VehicleType.objects.create(
        company=world["company"], category=taxonomy["category"], brand=taxonomy["brand"],
        vehicle_type_name="Monaco",
    )
    payload = {"category": taxonomy["category"].pk, "brand": taxonomy["brand"].pk, "vehicle_type_name": "X"}
    if action == "update":
        payload["pk"] = vehicle_type.pk
    RolePermission.objects.filter(
        role=client_in.role, page__code="fleet.vehicle_type"
    ).update(**{f"can_{action}": False})

    response = post(client_in, "/fleet/vehicle-type/save/", payload)

    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"


def test_a_role_without_vehicle_type_delete_is_refused(client_in, world, taxonomy):
    vehicle_type = VehicleType.objects.create(
        company=world["company"], category=taxonomy["category"], brand=taxonomy["brand"],
        vehicle_type_name="Monaco",
    )
    RolePermission.objects.filter(
        role=client_in.role, page__code="fleet.vehicle_type"
    ).update(can_delete=False)

    response = post(client_in, f"/fleet/vehicle-type/{vehicle_type.pk}/delete/", {})

    assert response.status_code == 403
    assert VehicleType.objects.count() == 1


# --- scope on the write -----------------------------------------------------------

def test_a_company_user_cannot_edit_another_companys_vehicle_type(client_in, world, taxonomy):
    theirs = VehicleType.objects.create(
        company=world["other"], category=taxonomy["their_category"], brand=taxonomy["their_brand"],
        vehicle_type_name="Their Monaco",
    )

    response = post(client_in, "/fleet/vehicle-type/save/", {
        "pk": theirs.pk, "category": taxonomy["category"].pk, "brand": taxonomy["brand"].pk,
        "vehicle_type_name": "Hijacked",
    })

    assert response.status_code == 404                    # not even acknowledged
    theirs.refresh_from_db()
    assert theirs.vehicle_type_name == "Their Monaco"


def test_a_company_user_cannot_delete_another_companys_vehicle_type(client_in, world, taxonomy):
    theirs = VehicleType.objects.create(
        company=world["other"], category=taxonomy["their_category"], brand=taxonomy["their_brand"],
        vehicle_type_name="Their Monaco",
    )

    response = post(client_in, f"/fleet/vehicle-type/{theirs.pk}/delete/", {})

    assert response.status_code == 404
    assert VehicleType.objects.filter(pk=theirs.pk).exists()


# =============================== UOM ===============================================
# --- create, update, delete ---------------------------------------------------

def test_create_a_uom(client_in, world):
    response = post(client_in, "/fleet/uom/save/", {"uom_code": "NO", "uom_name": "Number"})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["message"] == "UOM saved."

    uom = UOM.objects.get(pk=body["pk"])
    assert uom.company == world["company"]                 # from the user, not the payload
    assert uom.is_active is True
    assert uom.approval_status == ApprovalStatus.APPROVED


def test_update_a_uom(client_in, world):
    uom = UOM.objects.create(company=world["company"], uom_code="NO", uom_name="Number")

    post(client_in, "/fleet/uom/save/", {"pk": uom.pk, "uom_code": "NO", "uom_name": "Numbers"})

    uom.refresh_from_db()
    assert uom.uom_name == "Numbers"
    assert UOM.objects.count() == 1                        # updated, not duplicated


def test_delete_an_unused_uom(client_in, world):
    uom = UOM.objects.create(company=world["company"], uom_code="NO", uom_name="Number")

    response = post(client_in, f"/fleet/uom/{uom.pk}/delete/", {})

    assert response.json()["ok"] is True
    assert UOM.objects.count() == 0


def test_uom_description_is_optional(client_in, world):
    response = post(client_in, "/fleet/uom/save/", {"uom_code": "NO", "uom_name": "Number"})

    assert response.status_code == 200
    uom = UOM.objects.get(pk=response.json()["pk"])
    assert uom.description == ""


# --- validation -----------------------------------------------------------------

def test_a_duplicate_uom_code_is_reported_in_words(client_in, world):
    UOM.objects.create(company=world["company"], uom_code="NO", uom_name="Number")

    response = post(client_in, "/fleet/uom/save/", {"uom_code": "NO", "uom_name": "Numbers"})

    assert response.status_code == 400
    assert any("already exists" in error["message"] for error in response.json()["errors"])


def test_missing_required_uom_fields_are_reported_at_once(client_in):
    response = post(client_in, "/fleet/uom/save/", {"uom_code": "", "uom_name": ""})

    assert response.status_code == 400
    errors = response.json()["errors"]
    fields = {error["field"] for error in errors}

    assert len(errors) >= 2
    assert "UOM Code" in fields                            # labelled as the drawer labels it
    assert "uom_code" not in fields                          # never a column name


# --- permission on the write -----------------------------------------------------

@pytest.mark.parametrize("action,payload", [
    ("create", {"uom_code": "X", "uom_name": "X"}),
    ("update", {"pk": 0, "uom_code": "X", "uom_name": "X"}),
])
def test_a_role_without_the_uom_action_is_refused(client_in, world, action, payload):
    uom = UOM.objects.create(company=world["company"], uom_code="NO", uom_name="Number")
    if payload.get("pk") == 0:
        payload["pk"] = uom.pk
    RolePermission.objects.filter(
        role=client_in.role, page__code="fleet.uom"
    ).update(**{f"can_{action}": False})

    response = post(client_in, "/fleet/uom/save/", payload)

    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"


def test_a_role_without_uom_delete_is_refused(client_in, world):
    uom = UOM.objects.create(company=world["company"], uom_code="NO", uom_name="Number")
    RolePermission.objects.filter(
        role=client_in.role, page__code="fleet.uom"
    ).update(can_delete=False)

    response = post(client_in, f"/fleet/uom/{uom.pk}/delete/", {})

    assert response.status_code == 403
    assert UOM.objects.count() == 1


# --- scope on the write -----------------------------------------------------------

def test_a_company_user_cannot_edit_another_companys_uom(client_in, world):
    theirs = UOM.objects.create(company=world["other"], uom_code="NO", uom_name="Their Number")

    response = post(client_in, "/fleet/uom/save/",
                     {"pk": theirs.pk, "uom_code": "NO", "uom_name": "Hijacked"})

    assert response.status_code == 404                    # not even acknowledged
    theirs.refresh_from_db()
    assert theirs.uom_name == "Their Number"


def test_a_company_user_cannot_post_a_different_company_for_uom(client_in, world):
    response = post(client_in, "/fleet/uom/save/",
                     {"uom_code": "NO", "uom_name": "Number", "company": world["other"].pk})

    assert response.json()["ok"] is True
    uom = UOM.objects.get(pk=response.json()["pk"])
    assert uom.company == world["company"]                 # the payload was ignored


def test_a_company_user_cannot_delete_another_companys_uom(client_in, world):
    theirs = UOM.objects.create(company=world["other"], uom_code="NO", uom_name="Their Number")

    response = post(client_in, f"/fleet/uom/{theirs.pk}/delete/", {})

    assert response.status_code == 404
    assert UOM.objects.filter(pk=theirs.pk).exists()


def test_two_companies_may_use_the_same_uom_code(world):
    ours = UOM.objects.create(company=world["company"], uom_code="NO", uom_name="Ours")
    theirs = UOM.objects.create(company=world["other"], uom_code="NO", uom_name="Theirs")

    assert (ours.uom_code, theirs.uom_code) == ("NO", "NO")


def test_one_company_cannot_reuse_its_own_uom_code(world):
    from django.db import IntegrityError

    UOM.objects.create(company=world["company"], uom_code="NO", uom_name="First")
    with pytest.raises(IntegrityError):
        UOM.objects.create(company=world["company"], uom_code="NO", uom_name="Second")


# =============================== Asset Type =========================================
# --- create, update, delete ---------------------------------------------------

def test_create_an_asset_type(client_in, world):
    response = post(client_in, "/fleet/asset-type/save/", {
        "asset_type_name": "Vehicle", "asset_type_code": "VEH",
    })

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["message"] == "Asset Type saved."

    asset_type = AssetType.objects.get(pk=body["pk"])
    assert asset_type.company == world["company"]          # from the user, not the payload
    assert asset_type.is_active is True
    assert asset_type.approval_status == ApprovalStatus.APPROVED


def test_asset_type_code_is_optional(client_in, world):
    response = post(client_in, "/fleet/asset-type/save/", {"asset_type_name": "Vehicle"})

    assert response.status_code == 200
    asset_type = AssetType.objects.get(pk=response.json()["pk"])
    assert asset_type.asset_type_code == ""


def test_update_an_asset_type(client_in, world):
    asset_type = AssetType.objects.create(company=world["company"], asset_type_name="Vehicle")

    post(client_in, "/fleet/asset-type/save/", {"pk": asset_type.pk, "asset_type_name": "Battery"})

    asset_type.refresh_from_db()
    assert asset_type.asset_type_name == "Battery"
    assert AssetType.objects.count() == 1                  # updated, not duplicated


def test_delete_an_unused_asset_type(client_in, world):
    asset_type = AssetType.objects.create(company=world["company"], asset_type_name="Vehicle")

    response = post(client_in, f"/fleet/asset-type/{asset_type.pk}/delete/", {})

    assert response.json()["ok"] is True
    assert AssetType.objects.count() == 0


# --- validation -----------------------------------------------------------------

def test_missing_required_asset_type_fields_are_reported_at_once(client_in):
    response = post(client_in, "/fleet/asset-type/save/", {"asset_type_name": ""})

    assert response.status_code == 400
    errors = response.json()["errors"]
    fields = {error["field"] for error in errors}

    assert "Asset Type" in fields                          # labelled as the drawer labels it
    assert "asset_type_name" not in fields                  # never a column name


# --- permission on the write -----------------------------------------------------

@pytest.mark.parametrize("action,payload", [
    ("create", {"asset_type_name": "X"}),
    ("update", {"pk": 0, "asset_type_name": "X"}),
])
def test_a_role_without_the_asset_type_action_is_refused(client_in, world, action, payload):
    asset_type = AssetType.objects.create(company=world["company"], asset_type_name="Vehicle")
    if payload.get("pk") == 0:
        payload["pk"] = asset_type.pk
    RolePermission.objects.filter(
        role=client_in.role, page__code="fleet.asset_type"
    ).update(**{f"can_{action}": False})

    response = post(client_in, "/fleet/asset-type/save/", payload)

    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"


def test_a_role_without_asset_type_delete_is_refused(client_in, world):
    asset_type = AssetType.objects.create(company=world["company"], asset_type_name="Vehicle")
    RolePermission.objects.filter(
        role=client_in.role, page__code="fleet.asset_type"
    ).update(can_delete=False)

    response = post(client_in, f"/fleet/asset-type/{asset_type.pk}/delete/", {})

    assert response.status_code == 403
    assert AssetType.objects.count() == 1


# --- scope on the write -----------------------------------------------------------

def test_a_company_user_cannot_edit_another_companys_asset_type(client_in, world):
    theirs = AssetType.objects.create(company=world["other"], asset_type_name="Their Vehicle")

    response = post(client_in, "/fleet/asset-type/save/",
                     {"pk": theirs.pk, "asset_type_name": "Hijacked"})

    assert response.status_code == 404                    # not even acknowledged
    theirs.refresh_from_db()
    assert theirs.asset_type_name == "Their Vehicle"


def test_a_company_user_cannot_post_a_different_company_for_asset_type(client_in, world):
    response = post(client_in, "/fleet/asset-type/save/",
                     {"asset_type_name": "Vehicle", "company": world["other"].pk})

    assert response.json()["ok"] is True
    asset_type = AssetType.objects.get(pk=response.json()["pk"])
    assert asset_type.company == world["company"]          # the payload was ignored


def test_a_company_user_cannot_delete_another_companys_asset_type(client_in, world):
    theirs = AssetType.objects.create(company=world["other"], asset_type_name="Their Vehicle")

    response = post(client_in, f"/fleet/asset-type/{theirs.pk}/delete/", {})

    assert response.status_code == 404
    assert AssetType.objects.filter(pk=theirs.pk).exists()


# =============================== Asset ==============================================

@pytest.fixture
def asset_refs(world):
    """An asset type, brand, employee (custodian) and branch for each
    company -- so an asset has something real to point at, and the
    cross-company rejection tests have another company's rows to try."""
    from apps.company.models import Branch, BranchType, Location
    from apps.crew.models import Designation, Employee

    uae = Location.objects.create(
        country=world["company"].country, state=world["company"].state,
        short_code="CORN", name="Corniche",
    )

    def make(company, tag):
        designation = Designation.objects.create(
            company=company, code=f"MECH{tag}", title="Mechanic", rank_order=1
        )
        return {
            "asset_type": AssetType.objects.create(
                company=company, asset_type_name=f"Vehicle {tag}"
            ),
            "brand": Brand.objects.create(
                company=company, brand_code=f"BYK{tag}", brand_name=f"Byky {tag}"
            ),
            "employee": Employee.objects.create(
                company=company, employee_code=f"E{tag}", first_name="Anil", last_name="R",
                designation=designation,
            ),
            "branch": Branch.objects.create(
                company=company, location=uae, short_code=f"AUH{tag}", name=f"Station {tag}",
                branch_type=BranchType.STATION,
            ),
        }

    ours = make(world["company"], "1")
    theirs = make(world["other"], "2")
    return {"ours": ours, "theirs": theirs}


def test_create_an_asset(client_in, world, asset_refs):
    response = post(client_in, "/fleet/asset/save/", {
        "asset_code": "AST-001",
        "asset_type": asset_refs["ours"]["asset_type"].pk,
        "brand": asset_refs["ours"]["brand"].pk,
        "custodian": asset_refs["ours"]["employee"].pk,
        "branch": asset_refs["ours"]["branch"].pk,
        "serial_no": "SN-1", "manufacturer": "Byky Motors", "supplier": "ACME Supplies",
        "purchase_invoice_no": "INV-1", "warranty_from_date": "2026-01-01",
        "warranty_to_date": "2028-01-01",
    })

    assert response.status_code == 200, response.content
    body = response.json()
    assert body["ok"] is True
    assert body["message"] == "Asset saved."

    asset = Asset.objects.get(pk=body["pk"])
    assert asset.company == world["company"]                # from the user, not the payload
    assert asset.asset_type == asset_refs["ours"]["asset_type"]
    assert asset.brand == asset_refs["ours"]["brand"]
    assert asset.custodian == asset_refs["ours"]["employee"]
    assert asset.branch == asset_refs["ours"]["branch"]
    assert asset.is_active is True
    assert asset.approval_status == ApprovalStatus.APPROVED


def test_brand_custodian_and_branch_are_optional(client_in, world, asset_refs):
    response = post(client_in, "/fleet/asset/save/", {
        "asset_code": "AST-001", "asset_type": asset_refs["ours"]["asset_type"].pk,
    })

    assert response.status_code == 200, response.content
    asset = Asset.objects.get(pk=response.json()["pk"])
    assert asset.brand_id is None
    assert asset.custodian_id is None
    assert asset.branch_id is None


def test_update_an_asset(client_in, world, asset_refs):
    asset = Asset.objects.create(
        company=world["company"], asset_code="AST-001",
        asset_type=asset_refs["ours"]["asset_type"],
    )

    post(client_in, "/fleet/asset/save/", {
        "pk": asset.pk, "asset_code": "AST-001",
        "asset_type": asset_refs["ours"]["asset_type"].pk, "serial_no": "SN-2",
    })

    asset.refresh_from_db()
    assert asset.serial_no == "SN-2"
    assert Asset.objects.count() == 1                       # updated, not duplicated


def test_delete_an_unused_asset(client_in, world, asset_refs):
    asset = Asset.objects.create(
        company=world["company"], asset_code="AST-001",
        asset_type=asset_refs["ours"]["asset_type"],
    )

    response = post(client_in, f"/fleet/asset/{asset.pk}/delete/", {})

    assert response.json()["ok"] is True
    assert Asset.objects.count() == 0


# --- validation -----------------------------------------------------------------

def test_missing_required_asset_fields_are_reported_at_once(client_in):
    response = post(client_in, "/fleet/asset/save/", {"asset_code": ""})

    assert response.status_code == 400
    errors = response.json()["errors"]
    fields = {error["field"] for error in errors}

    assert "Asset Code" in fields                             # labelled as the drawer labels it
    assert "Asset type" in fields                             # Django's default FK field label
    assert "asset_code" not in fields                         # never a column name


def test_a_duplicate_asset_code_is_reported_in_words(client_in, world, asset_refs):
    Asset.objects.create(
        company=world["company"], asset_code="AST-001",
        asset_type=asset_refs["ours"]["asset_type"],
    )

    response = post(client_in, "/fleet/asset/save/", {
        "asset_code": "AST-001", "asset_type": asset_refs["ours"]["asset_type"].pk,
    })

    assert response.status_code == 400
    assert any("already exists" in error["message"] for error in response.json()["errors"])


def test_a_company_user_cannot_pick_another_companys_asset_type(client_in, world, asset_refs):
    response = post(client_in, "/fleet/asset/save/", {
        "asset_code": "AST-001", "asset_type": asset_refs["theirs"]["asset_type"].pk,
    })

    assert response.status_code == 400
    assert any("valid choice" in error["message"] for error in response.json()["errors"])
    assert Asset.objects.count() == 0


def test_a_company_user_cannot_pick_another_companys_brand_on_an_asset(client_in, world, asset_refs):
    response = post(client_in, "/fleet/asset/save/", {
        "asset_code": "AST-001", "asset_type": asset_refs["ours"]["asset_type"].pk,
        "brand": asset_refs["theirs"]["brand"].pk,
    })

    assert response.status_code == 400
    assert any("valid choice" in error["message"] for error in response.json()["errors"])
    assert Asset.objects.count() == 0


def test_a_company_user_cannot_pick_another_companys_employee_as_custodian(client_in, world, asset_refs):
    response = post(client_in, "/fleet/asset/save/", {
        "asset_code": "AST-001", "asset_type": asset_refs["ours"]["asset_type"].pk,
        "custodian": asset_refs["theirs"]["employee"].pk,
    })

    assert response.status_code == 400
    assert any("valid choice" in error["message"] for error in response.json()["errors"])
    assert Asset.objects.count() == 0


def test_a_company_user_cannot_pick_another_companys_branch_on_an_asset(client_in, world, asset_refs):
    response = post(client_in, "/fleet/asset/save/", {
        "asset_code": "AST-001", "asset_type": asset_refs["ours"]["asset_type"].pk,
        "branch": asset_refs["theirs"]["branch"].pk,
    })

    assert response.status_code == 400
    assert any("valid choice" in error["message"] for error in response.json()["errors"])
    assert Asset.objects.count() == 0


# --- permission on the write -----------------------------------------------------

@pytest.mark.parametrize("action", ["create", "update"])
def test_a_role_without_the_asset_action_is_refused(client_in, world, asset_refs, action):
    asset = Asset.objects.create(
        company=world["company"], asset_code="AST-001",
        asset_type=asset_refs["ours"]["asset_type"],
    )
    payload = {"asset_code": "X", "asset_type": asset_refs["ours"]["asset_type"].pk}
    if action == "update":
        payload["pk"] = asset.pk
    RolePermission.objects.filter(
        role=client_in.role, page__code="fleet.asset"
    ).update(**{f"can_{action}": False})

    response = post(client_in, "/fleet/asset/save/", payload)

    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"


def test_a_role_without_asset_delete_is_refused(client_in, world, asset_refs):
    asset = Asset.objects.create(
        company=world["company"], asset_code="AST-001",
        asset_type=asset_refs["ours"]["asset_type"],
    )
    RolePermission.objects.filter(
        role=client_in.role, page__code="fleet.asset"
    ).update(can_delete=False)

    response = post(client_in, f"/fleet/asset/{asset.pk}/delete/", {})

    assert response.status_code == 403
    assert Asset.objects.count() == 1


# --- scope on the write -----------------------------------------------------------

def test_a_company_user_cannot_edit_another_companys_asset(client_in, world, asset_refs):
    theirs = Asset.objects.create(
        company=world["other"], asset_code="AST-001",
        asset_type=asset_refs["theirs"]["asset_type"],
    )

    response = post(client_in, "/fleet/asset/save/", {
        "pk": theirs.pk, "asset_code": "HIJACKED",
        "asset_type": asset_refs["theirs"]["asset_type"].pk,
    })

    assert response.status_code == 404                      # not even acknowledged
    theirs.refresh_from_db()
    assert theirs.asset_code == "AST-001"


def test_a_company_user_cannot_delete_another_companys_asset(client_in, world, asset_refs):
    theirs = Asset.objects.create(
        company=world["other"], asset_code="AST-001",
        asset_type=asset_refs["theirs"]["asset_type"],
    )

    response = post(client_in, f"/fleet/asset/{theirs.pk}/delete/", {})

    assert response.status_code == 404
    assert Asset.objects.filter(pk=theirs.pk).exists()


def test_two_companies_may_use_the_same_asset_code(world, asset_refs):
    ours = Asset.objects.create(
        company=world["company"], asset_code="AST-001",
        asset_type=asset_refs["ours"]["asset_type"],
    )
    theirs = Asset.objects.create(
        company=world["other"], asset_code="AST-001",
        asset_type=asset_refs["theirs"]["asset_type"],
    )

    assert (ours.asset_code, theirs.asset_code) == ("AST-001", "AST-001")


def test_one_company_cannot_reuse_its_own_asset_code(world, asset_refs):
    from django.db import IntegrityError

    Asset.objects.create(
        company=world["company"], asset_code="AST-001",
        asset_type=asset_refs["ours"]["asset_type"],
    )
    with pytest.raises(IntegrityError):
        Asset.objects.create(
            company=world["company"], asset_code="AST-001",
            asset_type=asset_refs["ours"]["asset_type"],
        )


# =============================== Vehicle ============================================

@pytest.fixture
def vehicle_refs(world):
    """A vehicle type + UOM for each company -- so a vehicle has something
    real to point at, and the cross-company rejection tests have another
    company's rows to try."""
    def make(company, tag):
        category = Category.objects.create(
            company=company, category_code=f"BYKY{tag}", category_name=f"Byky {tag}"
        )
        brand = Brand.objects.create(
            company=company, brand_code=f"BYK{tag}", brand_name=f"Byky {tag}"
        )
        return {
            "vehicle_type": VehicleType.objects.create(
                company=company, category=category, brand=brand,
                vehicle_type_name=f"Monaco {tag}",
            ),
            "uom": UOM.objects.create(
                company=company, uom_code=f"NO{tag}", uom_name=f"Number {tag}"
            ),
        }

    return {"ours": make(world["company"], "1"), "theirs": make(world["other"], "2")}


def test_create_a_vehicle(client_in, world, vehicle_refs):
    # is_available explicit, matching how the real drawer always sends it
    # (byky-crud.js::collect() reads every checkbox's .checked state and
    # never omits the key) -- omitting it here would test an unrealistic
    # payload no browser client actually sends.
    response = post(client_in, "/fleet/vehicle/save/", {
        "vehicle_code": "V-001", "vehicle_name": "Monaco 1",
        "vehicle_type": vehicle_refs["ours"]["vehicle_type"].pk,
        "uom": vehicle_refs["ours"]["uom"].pk, "rfid_epc": "EPC-001",
        "is_available": True,
    })

    assert response.status_code == 200, response.content
    body = response.json()
    assert body["ok"] is True
    assert body["message"] == "Vehicle saved."

    vehicle = Vehicle.objects.get(pk=body["pk"])
    assert vehicle.company == world["company"]              # from the user, not the payload
    assert vehicle.vehicle_type == vehicle_refs["ours"]["vehicle_type"]
    assert vehicle.uom == vehicle_refs["ours"]["uom"]
    assert vehicle.is_active is True
    assert vehicle.is_available is True                     # BooleanField default
    assert vehicle.approval_status == ApprovalStatus.APPROVED


def test_update_a_vehicle(client_in, world, vehicle_refs):
    vehicle = Vehicle.objects.create(
        company=world["company"], vehicle_code="V-001", vehicle_name="Monaco 1",
        vehicle_type=vehicle_refs["ours"]["vehicle_type"], uom=vehicle_refs["ours"]["uom"],
        rfid_epc="EPC-001",
    )

    post(client_in, "/fleet/vehicle/save/", {
        "pk": vehicle.pk, "vehicle_code": "V-001", "vehicle_name": "Monaco 1 Refurbished",
        "vehicle_type": vehicle_refs["ours"]["vehicle_type"].pk,
        "uom": vehicle_refs["ours"]["uom"].pk, "rfid_epc": "EPC-001",
    })

    vehicle.refresh_from_db()
    assert vehicle.vehicle_name == "Monaco 1 Refurbished"
    assert Vehicle.objects.count() == 1                      # updated, not duplicated


def test_delete_an_unused_vehicle(client_in, world, vehicle_refs):
    vehicle = Vehicle.objects.create(
        company=world["company"], vehicle_code="V-001", vehicle_name="Monaco 1",
        vehicle_type=vehicle_refs["ours"]["vehicle_type"], uom=vehicle_refs["ours"]["uom"],
        rfid_epc="EPC-001",
    )

    response = post(client_in, f"/fleet/vehicle/{vehicle.pk}/delete/", {})

    assert response.json()["ok"] is True
    assert Vehicle.objects.count() == 0


def test_a_vehicle_can_be_marked_unavailable(client_in, world, vehicle_refs):
    response = post(client_in, "/fleet/vehicle/save/", {
        "vehicle_code": "V-001", "vehicle_name": "Monaco 1",
        "vehicle_type": vehicle_refs["ours"]["vehicle_type"].pk,
        "uom": vehicle_refs["ours"]["uom"].pk, "rfid_epc": "EPC-001",
        "is_available": False,
    })

    assert response.status_code == 200, response.content
    vehicle = Vehicle.objects.get(pk=response.json()["pk"])
    assert vehicle.is_available is False


# --- validation -----------------------------------------------------------------

def test_missing_required_vehicle_fields_are_reported_at_once(client_in):
    response = post(client_in, "/fleet/vehicle/save/", {"vehicle_code": "", "vehicle_name": ""})

    assert response.status_code == 400
    errors = response.json()["errors"]
    fields = {error["field"] for error in errors}

    assert "Vehicle Code" in fields                           # labelled as the drawer labels it
    assert "vehicle_code" not in fields                        # never a column name


def test_a_duplicate_vehicle_code_is_reported_in_words(client_in, world, vehicle_refs):
    Vehicle.objects.create(
        company=world["company"], vehicle_code="V-001", vehicle_name="Monaco 1",
        vehicle_type=vehicle_refs["ours"]["vehicle_type"], uom=vehicle_refs["ours"]["uom"],
        rfid_epc="EPC-001",
    )

    response = post(client_in, "/fleet/vehicle/save/", {
        "vehicle_code": "V-001", "vehicle_name": "Monaco 2",
        "vehicle_type": vehicle_refs["ours"]["vehicle_type"].pk,
        "uom": vehicle_refs["ours"]["uom"].pk, "rfid_epc": "EPC-002",
    })

    assert response.status_code == 400
    assert any("already exists" in error["message"] for error in response.json()["errors"])


def test_a_duplicate_rfid_epc_is_reported_in_words(client_in, world, vehicle_refs):
    Vehicle.objects.create(
        company=world["company"], vehicle_code="V-001", vehicle_name="Monaco 1",
        vehicle_type=vehicle_refs["ours"]["vehicle_type"], uom=vehicle_refs["ours"]["uom"],
        rfid_epc="EPC-001",
    )

    response = post(client_in, "/fleet/vehicle/save/", {
        "vehicle_code": "V-002", "vehicle_name": "Monaco 2",
        "vehicle_type": vehicle_refs["ours"]["vehicle_type"].pk,
        "uom": vehicle_refs["ours"]["uom"].pk, "rfid_epc": "EPC-001",
    })

    assert response.status_code == 400
    assert any("already exists" in error["message"] for error in response.json()["errors"])


def test_rfid_epc_is_optional_and_two_blanks_coexist(client_in, world, vehicle_refs):
    """rfid_epc's uniqueness constraint is conditional (~Q(rfid_epc="")) --
    two vehicles with no tag assigned yet must not collide with each other."""
    first = post(client_in, "/fleet/vehicle/save/", {
        "vehicle_code": "V-001", "vehicle_name": "Monaco 1",
        "vehicle_type": vehicle_refs["ours"]["vehicle_type"].pk,
        "uom": vehicle_refs["ours"]["uom"].pk,
    })
    second = post(client_in, "/fleet/vehicle/save/", {
        "vehicle_code": "V-002", "vehicle_name": "Monaco 2",
        "vehicle_type": vehicle_refs["ours"]["vehicle_type"].pk,
        "uom": vehicle_refs["ours"]["uom"].pk,
    })

    assert first.status_code == 200, first.content
    assert second.status_code == 200, second.content
    assert Vehicle.objects.filter(rfid_epc="").count() == 2


def test_a_company_user_cannot_pick_another_companys_vehicle_type(client_in, world, vehicle_refs):
    response = post(client_in, "/fleet/vehicle/save/", {
        "vehicle_code": "V-001", "vehicle_name": "Monaco 1",
        "vehicle_type": vehicle_refs["theirs"]["vehicle_type"].pk,
        "uom": vehicle_refs["ours"]["uom"].pk, "rfid_epc": "EPC-001",
    })

    assert response.status_code == 400
    assert any("valid choice" in error["message"] for error in response.json()["errors"])
    assert Vehicle.objects.count() == 0


def test_a_company_user_cannot_pick_another_companys_uom(client_in, world, vehicle_refs):
    response = post(client_in, "/fleet/vehicle/save/", {
        "vehicle_code": "V-001", "vehicle_name": "Monaco 1",
        "vehicle_type": vehicle_refs["ours"]["vehicle_type"].pk,
        "uom": vehicle_refs["theirs"]["uom"].pk, "rfid_epc": "EPC-001",
    })

    assert response.status_code == 400
    assert any("valid choice" in error["message"] for error in response.json()["errors"])
    assert Vehicle.objects.count() == 0


# --- permission on the write -----------------------------------------------------

@pytest.mark.parametrize("action", ["create", "update"])
def test_a_role_without_the_vehicle_action_is_refused(client_in, world, vehicle_refs, action):
    vehicle = Vehicle.objects.create(
        company=world["company"], vehicle_code="V-001", vehicle_name="Monaco 1",
        vehicle_type=vehicle_refs["ours"]["vehicle_type"], uom=vehicle_refs["ours"]["uom"],
        rfid_epc="EPC-001",
    )
    payload = {
        "vehicle_code": "X", "vehicle_name": "X",
        "vehicle_type": vehicle_refs["ours"]["vehicle_type"].pk,
        "uom": vehicle_refs["ours"]["uom"].pk, "rfid_epc": "EPC-999",
    }
    if action == "update":
        payload["pk"] = vehicle.pk
    RolePermission.objects.filter(
        role=client_in.role, page__code="fleet.vehicle"
    ).update(**{f"can_{action}": False})

    response = post(client_in, "/fleet/vehicle/save/", payload)

    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"


def test_a_role_without_vehicle_delete_is_refused(client_in, world, vehicle_refs):
    vehicle = Vehicle.objects.create(
        company=world["company"], vehicle_code="V-001", vehicle_name="Monaco 1",
        vehicle_type=vehicle_refs["ours"]["vehicle_type"], uom=vehicle_refs["ours"]["uom"],
        rfid_epc="EPC-001",
    )
    RolePermission.objects.filter(
        role=client_in.role, page__code="fleet.vehicle"
    ).update(can_delete=False)

    response = post(client_in, f"/fleet/vehicle/{vehicle.pk}/delete/", {})

    assert response.status_code == 403
    assert Vehicle.objects.count() == 1


# --- scope on the write -----------------------------------------------------------

def test_a_company_user_cannot_edit_another_companys_vehicle(client_in, world, vehicle_refs):
    theirs = Vehicle.objects.create(
        company=world["other"], vehicle_code="V-001", vehicle_name="Their Monaco",
        vehicle_type=vehicle_refs["theirs"]["vehicle_type"], uom=vehicle_refs["theirs"]["uom"],
        rfid_epc="EPC-001",
    )

    response = post(client_in, "/fleet/vehicle/save/", {
        "pk": theirs.pk, "vehicle_code": "HIJACKED", "vehicle_name": "Hijacked",
        "vehicle_type": vehicle_refs["theirs"]["vehicle_type"].pk,
        "uom": vehicle_refs["theirs"]["uom"].pk, "rfid_epc": "EPC-001",
    })

    assert response.status_code == 404                       # not even acknowledged
    theirs.refresh_from_db()
    assert theirs.vehicle_code == "V-001"


def test_a_company_user_cannot_delete_another_companys_vehicle(client_in, world, vehicle_refs):
    theirs = Vehicle.objects.create(
        company=world["other"], vehicle_code="V-001", vehicle_name="Their Monaco",
        vehicle_type=vehicle_refs["theirs"]["vehicle_type"], uom=vehicle_refs["theirs"]["uom"],
        rfid_epc="EPC-001",
    )

    response = post(client_in, f"/fleet/vehicle/{theirs.pk}/delete/", {})

    assert response.status_code == 404
    assert Vehicle.objects.filter(pk=theirs.pk).exists()


def test_two_companies_may_use_the_same_vehicle_code(world, vehicle_refs):
    ours = Vehicle.objects.create(
        company=world["company"], vehicle_code="V-001", vehicle_name="Ours",
        vehicle_type=vehicle_refs["ours"]["vehicle_type"], uom=vehicle_refs["ours"]["uom"],
        rfid_epc="EPC-001",
    )
    theirs = Vehicle.objects.create(
        company=world["other"], vehicle_code="V-001", vehicle_name="Theirs",
        vehicle_type=vehicle_refs["theirs"]["vehicle_type"], uom=vehicle_refs["theirs"]["uom"],
        rfid_epc="EPC-002",
    )

    assert (ours.vehicle_code, theirs.vehicle_code) == ("V-001", "V-001")


def test_one_company_cannot_reuse_its_own_vehicle_code(world, vehicle_refs):
    from django.db import IntegrityError

    Vehicle.objects.create(
        company=world["company"], vehicle_code="V-001", vehicle_name="First",
        vehicle_type=vehicle_refs["ours"]["vehicle_type"], uom=vehicle_refs["ours"]["uom"],
        rfid_epc="EPC-001",
    )
    with pytest.raises(IntegrityError):
        Vehicle.objects.create(
            company=world["company"], vehicle_code="V-001", vehicle_name="Second",
            vehicle_type=vehicle_refs["ours"]["vehicle_type"], uom=vehicle_refs["ours"]["uom"],
            rfid_epc="EPC-002",
        )
