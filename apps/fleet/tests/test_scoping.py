"""A company user sees their company's Brand/Category rows and never picks
which company they belong to. A system user sees every company's rows and
must choose one in the drawer.

Mirrors apps/company/tests/test_scoping.py. This is the coverage the
company-field-in-the-drawer behavior needs but didn't have: the mechanism
(ScopedModelForm hiding/locking the field, theme.drawers.for_user dropping
it from the spec for a company user) was already built for Brand -- these
tests prove it, and prove Category inherits it for free.
"""

import pytest

from apps.company.models import Company, Country, State
from apps.fleet.models import UOM, Asset, AssetType, Brand, Category, VehicleType
from apps.portal.models import Role
from apps.portal.services import grant_all
from core.enums import Channel, UserScope
from core.models import User

PASSWORD = "Byky#2026"


@pytest.fixture
def two_companies(db):
    uae = Country.objects.create(short_code="AE", name="United Arab Emirates")
    kuwait = Country.objects.create(short_code="KW", name="Kuwait")
    abu_dhabi = State.objects.create(country=uae, short_code="AUH", name="Abu Dhabi")
    hawalli = State.objects.create(country=kuwait, short_code="HAW", name="Hawalli")

    byky = Company.objects.create(
        short_code="BYKY", name="BYKY", country=uae, state=abu_dhabi,
        phone_number="+9710000000", email="ops@byky.test",
    )
    byky_kw = Company.objects.create(
        short_code="BYKYKW", name="BYKY Kuwait", country=kuwait, state=hawalli,
        timezone="Asia/Kuwait", phone_number="+9650000000", email="ops@bykykw.test",
    )

    uae_brand = Brand.objects.create(company=byky, brand_code="BYK", brand_name="Byky UAE Brand")
    kw_brand = Brand.objects.create(company=byky_kw, brand_code="BYK", brand_name="Byky Kuwait Brand")
    uae_category = Category.objects.create(
        company=byky, category_code="BYKY", category_name="Byky UAE Category"
    )
    kw_category = Category.objects.create(
        company=byky_kw, category_code="BYKY", category_name="Byky Kuwait Category"
    )
    VehicleType.objects.create(
        company=byky, category=uae_category, brand=uae_brand, vehicle_type_name="Byky UAE Type"
    )
    VehicleType.objects.create(
        company=byky_kw, category=kw_category, brand=kw_brand, vehicle_type_name="Byky Kuwait Type"
    )
    UOM.objects.create(company=byky, uom_code="NO", uom_name="Byky UAE UOM")
    UOM.objects.create(company=byky_kw, uom_code="NO", uom_name="Byky Kuwait UOM")
    uae_asset_type = AssetType.objects.create(company=byky, asset_type_name="Byky UAE Asset Type")
    kw_asset_type = AssetType.objects.create(
        company=byky_kw, asset_type_name="Byky Kuwait Asset Type"
    )
    Asset.objects.create(
        company=byky, asset_code="BYKY-UAE-1", asset_type=uae_asset_type, brand=uae_brand,
    )
    Asset.objects.create(
        company=byky_kw, asset_code="BYKY-KW-1", asset_type=kw_asset_type, brand=kw_brand,
    )

    return {"uae": byky, "kuwait": byky_kw}


def _company_user(client, company, username):
    role = Role.objects.create(company=company, name=f"Admin {company.short_code}")
    grant_all(role)
    User.objects.create_user(
        username, PASSWORD, display_name=username, company=company, role=role,
        allowed_channels=[Channel.WEB],
    )
    client.post("/login/", {"username": username, "password": PASSWORD})
    return client


def _system_user(client, username):
    role = Role.objects.create(company=None, name="System Administrator")
    grant_all(role)
    User.objects.create_user(
        username, PASSWORD, display_name=username, scope=UserScope.SYSTEM, role=role,
        allowed_channels=[Channel.WEB],
    )
    client.post("/login/", {"username": username, "password": PASSWORD})
    return client


# --- company scope: rows ------------------------------------------------------

def test_a_company_user_never_sees_another_companys_brand(client, two_companies):
    _company_user(client, two_companies["uae"], "uae.admin")

    body = client.get("/fleet/brand/list/").content

    assert b"Byky UAE Brand" in body
    assert b"Byky Kuwait Brand" not in body


def test_a_company_user_never_sees_another_companys_category(client, two_companies):
    _company_user(client, two_companies["uae"], "uae.admin")

    body = client.get("/fleet/category/list/").content

    assert b"Byky UAE Category" in body
    assert b"Byky Kuwait Category" not in body


# --- system scope: rows --------------------------------------------------------

def test_a_system_user_sees_every_companys_brand(client, two_companies):
    _system_user(client, "platform.admin")

    body = client.get("/fleet/brand/list/").content

    assert b"Byky UAE Brand" in body
    assert b"Byky Kuwait Brand" in body


def test_a_system_user_sees_every_companys_category(client, two_companies):
    _system_user(client, "platform.admin")

    body = client.get("/fleet/category/list/").content

    assert b"Byky UAE Category" in body
    assert b"Byky Kuwait Category" in body


def test_a_system_user_still_needs_the_permission_granted(client, two_companies):
    """Scope decides which rows; the role still decides which screens.
    System scope is not a bypass around RolePermission."""
    _system_user(client, "platform.admin")
    Role.objects.filter(company=None).first().permissions.filter(
        page__code="fleet.brand"
    ).update(can_read=False)

    assert client.get("/fleet/brand/list/").status_code == 403


# --- the drawers follow the same rule -------------------------------------------

def test_a_company_user_is_not_asked_which_company_for_a_brand(client, two_companies):
    _company_user(client, two_companies["uae"], "uae.admin")

    body = client.get("/fleet/brand/list/").content

    assert b'data-field="brand_code"' in body
    assert b'data-field="company"' not in body


def test_a_system_user_must_choose_a_company_for_a_brand(client, two_companies):
    _system_user(client, "platform.admin")

    body = client.get("/fleet/brand/list/").content

    assert b'data-field="company"' in body


def test_a_company_user_is_not_asked_which_company_for_a_category(client, two_companies):
    _company_user(client, two_companies["uae"], "uae.admin")

    body = client.get("/fleet/category/list/").content

    assert b'data-field="category_code"' in body
    assert b'data-field="company"' not in body


def test_a_system_user_must_choose_a_company_for_a_category(client, two_companies):
    _system_user(client, "platform.admin")

    body = client.get("/fleet/category/list/").content

    assert b'data-field="company"' in body


# --- Vehicle Type: rows ------------------------------------------------------

def test_a_company_user_never_sees_another_companys_vehicle_type(client, two_companies):
    _company_user(client, two_companies["uae"], "uae.admin")

    body = client.get("/fleet/vehicle-type/list/").content

    assert b"Byky UAE Type" in body
    assert b"Byky Kuwait Type" not in body


def test_a_system_user_sees_every_companys_vehicle_type(client, two_companies):
    _system_user(client, "platform.admin")

    body = client.get("/fleet/vehicle-type/list/").content

    assert b"Byky UAE Type" in body
    assert b"Byky Kuwait Type" in body


# --- Vehicle Type: the drawer's company field follows the same rule ------------

def test_a_company_user_is_not_asked_which_company_for_a_vehicle_type(client, two_companies):
    _company_user(client, two_companies["uae"], "uae.admin")

    body = client.get("/fleet/vehicle-type/list/").content

    assert b'data-field="vehicle_type_name"' in body
    assert b'data-field="company"' not in body


def test_a_system_user_must_choose_a_company_for_a_vehicle_type(client, two_companies):
    _system_user(client, "platform.admin")

    body = client.get("/fleet/vehicle-type/list/").content

    assert b'data-field="company"' in body


# --- Vehicle Type: the category/brand dropdowns are scoped too ------------------
# Unlike company (special-cased in ScopedModelForm), category and brand are
# plain cross-referencing FKs -- apps/fleet/forms.py::VehicleTypeForm scopes
# their querysets by hand. These prove that scoping actually reaches the
# rendered drawer, not just the form's server-side validation.

def test_a_company_users_vehicle_type_drawer_only_offers_their_own_categories(client, two_companies):
    _company_user(client, two_companies["uae"], "uae.admin")

    body = client.get("/fleet/vehicle-type/list/").content

    assert b"Byky UAE Category" in body
    assert b"Byky Kuwait Category" not in body


def test_a_company_users_vehicle_type_drawer_only_offers_their_own_brands(client, two_companies):
    _company_user(client, two_companies["uae"], "uae.admin")

    body = client.get("/fleet/vehicle-type/list/").content

    assert b"Byky UAE Brand" in body
    assert b"Byky Kuwait Brand" not in body


# --- UOM: rows ------------------------------------------------------

def test_a_company_user_never_sees_another_companys_uom(client, two_companies):
    _company_user(client, two_companies["uae"], "uae.admin")

    body = client.get("/fleet/uom/list/").content

    assert b"Byky UAE UOM" in body
    assert b"Byky Kuwait UOM" not in body


def test_a_system_user_sees_every_companys_uom(client, two_companies):
    _system_user(client, "platform.admin")

    body = client.get("/fleet/uom/list/").content

    assert b"Byky UAE UOM" in body
    assert b"Byky Kuwait UOM" in body


# --- UOM: the drawer follows the same rule -------------------------------------

def test_a_company_user_is_not_asked_which_company_for_a_uom(client, two_companies):
    _company_user(client, two_companies["uae"], "uae.admin")

    body = client.get("/fleet/uom/list/").content

    assert b'data-field="uom_code"' in body
    assert b'data-field="company"' not in body


def test_a_system_user_must_choose_a_company_for_a_uom(client, two_companies):
    _system_user(client, "platform.admin")

    body = client.get("/fleet/uom/list/").content

    assert b'data-field="company"' in body


# --- Asset Type: rows ------------------------------------------------------

def test_a_company_user_never_sees_another_companys_asset_type(client, two_companies):
    _company_user(client, two_companies["uae"], "uae.admin")

    body = client.get("/fleet/asset-type/list/").content

    assert b"Byky UAE Asset Type" in body
    assert b"Byky Kuwait Asset Type" not in body


def test_a_system_user_sees_every_companys_asset_type(client, two_companies):
    _system_user(client, "platform.admin")

    body = client.get("/fleet/asset-type/list/").content

    assert b"Byky UAE Asset Type" in body
    assert b"Byky Kuwait Asset Type" in body


# --- Asset Type: the drawer follows the same rule -------------------------------

def test_a_company_user_is_not_asked_which_company_for_an_asset_type(client, two_companies):
    _company_user(client, two_companies["uae"], "uae.admin")

    body = client.get("/fleet/asset-type/list/").content

    assert b'data-field="asset_type_name"' in body
    assert b'data-field="company"' not in body


def test_a_system_user_must_choose_a_company_for_an_asset_type(client, two_companies):
    _system_user(client, "platform.admin")

    body = client.get("/fleet/asset-type/list/").content

    assert b'data-field="company"' in body


# --- Asset: rows ------------------------------------------------------

def test_a_company_user_never_sees_another_companys_asset(client, two_companies):
    _company_user(client, two_companies["uae"], "uae.admin")

    body = client.get("/fleet/asset/list/").content

    assert b"BYKY-UAE-1" in body
    assert b"BYKY-KW-1" not in body


def test_a_system_user_sees_every_companys_asset(client, two_companies):
    _system_user(client, "platform.admin")

    body = client.get("/fleet/asset/list/").content

    assert b"BYKY-UAE-1" in body
    assert b"BYKY-KW-1" in body


# --- Asset: the drawer follows the same rule -------------------------------

def test_a_company_user_is_not_asked_which_company_for_an_asset(client, two_companies):
    _company_user(client, two_companies["uae"], "uae.admin")

    body = client.get("/fleet/asset/list/").content

    assert b'data-field="asset_code"' in body
    assert b'data-field="company"' not in body


def test_a_system_user_must_choose_a_company_for_an_asset(client, two_companies):
    _system_user(client, "platform.admin")

    body = client.get("/fleet/asset/list/").content

    assert b'data-field="company"' in body


# --- Asset: the asset_type/brand dropdowns are scoped too ---------------------
# custodian/branch reuse apps.crew.scoping.employees_for /
# apps.company.scoping.branches_for directly, already proven scoped
# elsewhere; asset_type/brand are this module's own, proven here the same
# way Vehicle Type proved category/brand.

def test_a_company_users_asset_drawer_only_offers_their_own_asset_types(client, two_companies):
    _company_user(client, two_companies["uae"], "uae.admin")

    body = client.get("/fleet/asset/list/").content

    assert b"Byky UAE Asset Type" in body
    assert b"Byky Kuwait Asset Type" not in body


def test_a_company_users_asset_drawer_only_offers_their_own_brands(client, two_companies):
    _company_user(client, two_companies["uae"], "uae.admin")

    body = client.get("/fleet/asset/list/").content

    assert b"Byky UAE Brand" in body
    assert b"Byky Kuwait Brand" not in body
