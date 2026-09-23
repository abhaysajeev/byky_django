"""Every fleet screen renders, refuses, and survives an empty database.

Same shape as apps/company/tests/test_screens.py.
"""

import pytest

from apps.company.models import Company, Country, State
from apps.fleet.models import UOM, Brand, Category, VehicleType
from apps.portal.models import Page, Role, RolePermission
from apps.portal.services import grant_all
from core.enums import Channel
from core.models import User

PASSWORD = "Byky#2026"

SCREENS = [
    ("/fleet/brand/list/", "fleet.brand"),
    ("/fleet/category/list/", "fleet.category"),
    ("/fleet/vehicle-type/list/", "fleet.vehicle_type"),
    ("/fleet/uom/list/", "fleet.uom"),
]


@pytest.fixture
def company(db):
    country = Country.objects.create(short_code="AE", name="United Arab Emirates")
    state = State.objects.create(country=country, short_code="AUH", name="Abu Dhabi")
    return Company.objects.create(
        short_code="BYKY", name="BYKY", country=country, state=state,
        phone_number="+9710000000", email="ops@byky.test",
    )


@pytest.fixture
def signed_in(client, company):
    """A user whose role may read everything."""
    role = Role.objects.create(company=company, name="Administrator")
    grant_all(role)
    User.objects.create_user(
        "sara.k", PASSWORD, display_name="Sara K", company=company, role=role,
        allowed_channels=[Channel.WEB],
    )
    client.post("/login/", {"username": "sara.k", "password": PASSWORD})
    return client


@pytest.fixture
def no_permissions(client, company):
    """A user with a role that grants nothing."""
    role = Role.objects.create(company=company, name="Visitor")
    User.objects.create_user(
        "vis.itor", PASSWORD, display_name="Vis Itor", company=company, role=role,
        allowed_channels=[Channel.WEB],
    )
    client.post("/login/", {"username": "vis.itor", "password": PASSWORD})
    return client


@pytest.mark.parametrize("url,page_code", SCREENS)
def test_a_screen_renders_on_an_empty_database(signed_in, url, page_code):
    response = signed_in.get(url)

    assert response.status_code == 200
    assert b"byky-sidebar" in response.content
    assert page_code


@pytest.mark.parametrize("url,page_code", SCREENS)
def test_a_screen_refuses_a_role_without_permission(no_permissions, url, page_code):
    assert no_permissions.get(url).status_code == 403


@pytest.mark.parametrize("url,page_code", SCREENS)
def test_a_screen_sends_a_stranger_to_sign_in(client, db, url, page_code):
    response = client.get(url)

    assert response.status_code == 302
    assert response.url.startswith("/login/")


@pytest.mark.parametrize("url,page_code", SCREENS)
def test_every_screen_has_a_page_row(db, url, page_code):
    """A screen with no Page row cannot be permission-checked and never appears
    in the sidebar."""
    page = Page.objects.get(code=page_code)

    assert page.is_active
    assert "read" in page.actions
    assert "web" in page.channels


def test_screens_render_with_data(signed_in, company):
    Brand.objects.create(
        company=company, brand_code="BYK", brand_name="Byky", manufacturer="Byky Motors",
    )

    response = signed_in.get("/fleet/brand/list/")

    assert response.status_code == 200
    assert b"Byky" in response.content
    assert b"BYK" in response.content


def test_the_category_screen_renders_with_data(signed_in, company):
    Category.objects.create(company=company, category_code="BYKY", category_name="Byky")

    response = signed_in.get("/fleet/category/list/")

    assert response.status_code == 200
    assert b"Byky" in response.content
    assert b"BYKY" in response.content


def test_the_sidebar_lists_brand_once_granted(signed_in):
    assert b">Brand<" in signed_in.get("/dashboard/").content


def test_the_sidebar_hides_brand_when_permission_is_revoked(signed_in, company):
    role = Role.objects.get(name="Administrator")
    RolePermission.objects.filter(role=role, page__code="fleet.brand").update(can_read=False)

    body = signed_in.get("/dashboard/").content

    assert b">Brand<" not in body


def test_the_sidebar_lists_category_once_granted(signed_in):
    # "Category" alone collides with the dashboard's own "Revenue by
    # Category" chart and a table column header -- match the sidebar's own
    # menu-label markup instead (theme/templates/sidebar/menu_link_template.html).
    assert b'class="menu-label">Category</span>' in signed_in.get("/dashboard/").content


def test_the_sidebar_hides_category_when_permission_is_revoked(signed_in, company):
    role = Role.objects.get(name="Administrator")
    RolePermission.objects.filter(role=role, page__code="fleet.category").update(can_read=False)

    body = signed_in.get("/dashboard/").content

    assert b'class="menu-label">Category</span>' not in body


def test_the_vehicle_type_screen_renders_with_data(signed_in, company):
    category = Category.objects.create(company=company, category_code="BYKY", category_name="Byky")
    brand = Brand.objects.create(company=company, brand_code="BYK", brand_name="Byky")
    VehicleType.objects.create(
        company=company, category=category, brand=brand, vehicle_type_name="Monaco",
    )

    response = signed_in.get("/fleet/vehicle-type/list/")

    assert response.status_code == 200
    assert b"Monaco" in response.content
    assert b"Byky" in response.content            # category and brand names both render


def test_the_sidebar_lists_vehicle_type_once_granted(signed_in):
    assert b'class="menu-label">Vehicle Type</span>' in signed_in.get("/dashboard/").content


def test_the_sidebar_hides_vehicle_type_when_permission_is_revoked(signed_in, company):
    role = Role.objects.get(name="Administrator")
    RolePermission.objects.filter(role=role, page__code="fleet.vehicle_type").update(can_read=False)

    body = signed_in.get("/dashboard/").content

    assert b'class="menu-label">Vehicle Type</span>' not in body


def test_the_uom_screen_renders_with_data(signed_in, company):
    UOM.objects.create(company=company, uom_code="NO", uom_name="Number")

    response = signed_in.get("/fleet/uom/list/")

    assert response.status_code == 200
    assert b"Number" in response.content
    assert b"NO" in response.content


def test_the_sidebar_lists_uom_once_granted(signed_in):
    assert b'class="menu-label">UOM</span>' in signed_in.get("/dashboard/").content


def test_the_sidebar_hides_uom_when_permission_is_revoked(signed_in, company):
    role = Role.objects.get(name="Administrator")
    RolePermission.objects.filter(role=role, page__code="fleet.uom").update(can_read=False)

    body = signed_in.get("/dashboard/").content

    assert b'class="menu-label">UOM</span>' not in body
