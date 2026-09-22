"""Fixtures shared by the device tests.

Mirrors apps/company/tests/test_screens.py: one company, one branch, and two
users -- one whose role may read everything, one whose role grants nothing.
"""

import pytest

from apps.company.models import Branch, BranchType, Company, Country, Location, State
from apps.portal.models import Role
from apps.portal.services import grant_all
from core.enums import Channel
from core.models import User

PASSWORD = "Byky#2026"


@pytest.fixture
def company(db):
    country = Country.objects.create(short_code="AE", name="United Arab Emirates")
    state = State.objects.create(country=country, short_code="AUH", name="Abu Dhabi")
    return Company.objects.create(
        short_code="BYKY", name="BYKY", country=country, state=state,
        phone_number="+9710000000", email="ops@byky.test",
    )


@pytest.fixture
def branch(company):
    country = Country.objects.get(short_code="AE")
    state = State.objects.get(short_code="AUH")
    location = Location.objects.create(
        country=country, state=state, short_code="CORN", name="Corniche"
    )
    return Branch.objects.create(
        company=company, location=location, short_code="AUH01", name="Corniche 1",
        branch_type=BranchType.STATION,
    )


@pytest.fixture
def other_branch(company):
    """A second station, for re-mapping and for the multi-device rule."""
    country = Country.objects.get(short_code="AE")
    state = State.objects.get(short_code="AUH")
    location = Location.objects.create(
        country=country, state=state, short_code="MARI", name="Marina"
    )
    return Branch.objects.create(
        company=company, location=location, short_code="AUH02", name="Marina 1",
        branch_type=BranchType.STATION,
    )


@pytest.fixture
def signed_in(client, company):
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
    role = Role.objects.create(company=company, name="Visitor")
    User.objects.create_user(
        "vis.itor", PASSWORD, display_name="Vis Itor", company=company, role=role,
        allowed_channels=[Channel.WEB],
    )
    client.post("/login/", {"username": "vis.itor", "password": PASSWORD})
    return client
