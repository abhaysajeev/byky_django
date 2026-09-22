"""Every Company screen renders, refuses, and survives an empty database.

The last one matters most for this pass: the screens are ported markup wired to
real queries, so "no rows yet" has to be a working state, not a crash.
"""

import pytest

from apps.company.models import Branch, BranchType, Company, Country, Department, Location, State
from apps.portal.models import Page, Role, RolePermission
from apps.portal.services import grant_all
from core.enums import Channel
from core.models import User

PASSWORD = "Byky#2026"

SCREENS = [
    ("/company/company/list/", "company.company"),
    ("/company/country-state/list/", "company.country_state"),
    ("/company/location/list/", "company.location"),
    ("/company/department/list/", "company.department"),
    ("/company/branch/list/", "company.branch"),
    ("/company/branch-working-time/edit/", "company.branch_working_time"),
    ("/dashboard/", "general.dashboard"),
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
    assert b"byky-sidebar" in response.content          # the shell rendered
    assert page_code  # the screen is registered; see the next test


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
    """The same screens, once the masters carry rows."""
    country = Country.objects.get(short_code="AE")
    state = State.objects.get(short_code="AUH")
    location = Location.objects.create(
        country=country, state=state, short_code="CORN", name="Corniche"
    )
    department = Department.objects.create(company=company, short_code="OPS", name="Operations")
    branch = Branch.objects.create(
        company=company, location=location, short_code="AUH01", name="Corniche 1",
        branch_type=BranchType.STATION, station_number="S-01",
        latitude="24.466700", longitude="54.366700",
    )
    branch.departments.add(department)

    for url, _ in SCREENS:
        response = signed_in.get(url)
        assert response.status_code == 200, url

    assert b"Corniche 1" in signed_in.get("/company/branch/list/").content


def test_the_sidebar_lists_only_what_the_role_may_read(signed_in, company):
    role = Role.objects.get(name="Administrator")
    RolePermission.objects.filter(role=role, page__code="company.branch").update(can_read=False)

    body = signed_in.get("/dashboard/").content

    assert b"Country &amp; State" in body
    assert b">Branch<" not in body
