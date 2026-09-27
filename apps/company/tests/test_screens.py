"""The Company screens with real rows in them, and the sidebar.

That every screen renders on an empty database, refuses a role without it and
sends a stranger to sign in is apps/portal/tests/test_every_screen.py.
"""

import pytest

from apps.company.models import Branch, BranchType, Company, Country, Department, Location, State
from apps.portal.models import Role, RolePermission
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
    # Not "/dashboard/": DashboardView carries no page_code (apps/portal/views.py)
    # -- it's deliberately ungated, not a Company screen, and its own contract
    # is covered by apps/portal/tests/test_login.py.
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
