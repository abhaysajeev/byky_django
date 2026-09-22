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
from apps.fleet.models import Brand, Category
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

    Brand.objects.create(company=byky, brand_code="BYK", brand_name="Byky UAE Brand")
    Brand.objects.create(company=byky_kw, brand_code="BYK", brand_name="Byky Kuwait Brand")
    Category.objects.create(company=byky, category_code="BYKY", category_name="Byky UAE Category")
    Category.objects.create(company=byky_kw, category_code="BYKY", category_name="Byky Kuwait Category")

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
