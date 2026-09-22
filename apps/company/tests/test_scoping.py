"""A company user sees their company. A system user sees every company.

The documented way shared-database multi-tenancy leaks is a query that forgets
its filter, so these run against every Company screen rather than a sample.
"""

import pytest

from apps.company.models import Branch, BranchType, Company, Country, Department, Location, State
from apps.portal.models import Role
from apps.portal.services import grant_all
from core.enums import Channel, UserScope
from core.models import User

PASSWORD = "Byky#2026"

LIST_SCREENS = [
    "/company/company/list/",
    "/company/country-state/list/",
    "/company/location/list/",
    "/company/department/list/",
    "/company/branch/list/",
    "/dashboard/",
]


@pytest.fixture
def two_companies(db):
    """BYKY in the UAE and BYKY Kuwait, each with one branch."""
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

    corniche = Location.objects.create(
        country=uae, state=abu_dhabi, short_code="CORN", name="Corniche"
    )
    salmiya = Location.objects.create(
        country=kuwait, state=hawalli, short_code="SALM", name="Salmiya"
    )
    Branch.objects.create(
        company=byky, location=corniche, short_code="AUH01", name="Corniche Station",
        branch_type=BranchType.STATION, latitude="24.466700", longitude="54.366700",
    )
    Branch.objects.create(
        company=byky_kw, location=salmiya, short_code="KW01", name="Salmiya Station",
        branch_type=BranchType.STATION, latitude="29.333300", longitude="48.066700",
    )
    Department.objects.create(company=byky, short_code="OPS", name="Operations")
    Department.objects.create(company=byky_kw, short_code="OPS", name="Operations")
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


# --- the same department name in two companies -------------------------------

def test_two_companies_can_each_have_an_operations_department(two_companies):
    assert Department.objects.filter(name="Operations").count() == 2


# --- company scope ------------------------------------------------------------

@pytest.mark.parametrize("url", LIST_SCREENS)
def test_a_company_user_never_sees_another_companys_rows(client, two_companies, url):
    _company_user(client, two_companies["uae"], "uae.admin")

    body = client.get(url).content

    assert b"Salmiya" not in body
    assert b"BYKY Kuwait" not in body


def test_a_company_user_sees_their_own_rows(client, two_companies):
    _company_user(client, two_companies["uae"], "uae.admin")

    assert b"Corniche Station" in client.get("/company/branch/list/").content
    assert b"Operations" in client.get("/company/department/list/").content


def test_countries_are_derived_from_where_the_company_operates(client, two_companies):
    """Country has no company column -- it is shared reference data -- so a
    company user sees the ones it actually reaches."""
    _company_user(client, two_companies["uae"], "uae.admin")

    body = client.get("/company/country-state/list/").content

    assert b"United Arab Emirates" in body
    assert b"Kuwait" not in body


# --- system scope -------------------------------------------------------------

def test_a_system_user_sees_every_company(client, two_companies):
    _system_user(client, "platform.admin")

    branches = client.get("/company/branch/list/").content
    countries = client.get("/company/country-state/list/").content

    assert b"Corniche Station" in branches
    assert b"Salmiya Station" in branches
    assert b"United Arab Emirates" in countries
    assert b"Kuwait" in countries


def test_a_system_user_still_obeys_permissions(client, two_companies):
    """Scope decides which rows; the role still decides which actions."""
    _system_user(client, "platform.admin")
    Role.objects.filter(company=None).first().permissions.filter(
        page__code="company.branch"
    ).update(can_read=False)

    assert client.get("/company/branch/list/").status_code == 403


# --- failing closed -----------------------------------------------------------

def test_a_company_user_with_no_company_sees_nothing(client, two_companies):
    """A misconfigured account must show an empty screen, never every company.

    The database constraint stops this state being reached normally; the filter
    refuses it anyway, because a blank field must not grant power.
    """
    from core.scoping import scoped_to

    broken = User(username="broken", scope=UserScope.COMPANY, company=None)

    assert scoped_to(Branch.objects.all(), broken).count() == 0
    assert scoped_to(Branch.objects.all(), None).count() == 0


def test_the_database_refuses_a_company_user_without_a_company(two_companies):
    from django.db import IntegrityError

    with pytest.raises(IntegrityError):
        User.objects.create_user(
            "no.company", PASSWORD, display_name="No Company",
            scope=UserScope.COMPANY, company=None,
        )


def test_the_database_refuses_a_system_user_that_keeps_a_company(two_companies):
    from django.db import IntegrityError

    with pytest.raises(IntegrityError):
        User.objects.create_user(
            "both", PASSWORD, display_name="Both",
            scope=UserScope.SYSTEM, company=two_companies["uae"],
        )


# --- the drawers follow the same rule ----------------------------------------

def test_a_company_user_is_not_asked_which_company(client, two_companies):
    """Their company is theirs. Offering the choice would be noise, and a way
    to write into someone else's data."""
    _company_user(client, two_companies["uae"], "uae.admin")

    body = client.get("/company/department/list/").content

    assert b'data-field="code"' in body
    assert b'data-field="company"' not in body


def test_a_system_user_must_choose_a_company(client, two_companies):
    _system_user(client, "platform.admin")

    body = client.get("/company/department/list/").content

    assert b'data-field="company"' in body


def test_only_a_system_user_may_add_a_company(client, two_companies):
    _company_user(client, two_companies["uae"], "uae.admin")
    assert b'data-scr-open="company:add"' not in client.get("/company/company/list/").content

    client.get("/logout/")
    _system_user(client, "platform.admin")
    assert b'data-scr-open="company:add"' in client.get("/company/company/list/").content
