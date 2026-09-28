"""Shared fixtures for the rental tests: two companies, a branch each (for the
device lookup API's session), a signed-in administrator, and a small factory
for customers."""

import json

import pytest

from apps.company.models import Branch, BranchType, Company, Country, Location, State
from apps.portal.models import Role
from apps.portal.services import grant_all
from apps.rental.models import Customer
from core.enums import Channel
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
    corniche = Location.objects.create(country=uae, state=abu_dhabi, short_code="CORN", name="Corniche")

    def branch(company, code, name):
        return Branch.objects.create(company=company, location=corniche, short_code=code, name=name,
                                     branch_type=BranchType.STATION)

    return {
        "company": byky, "other": other,
        "adc1": branch(byky, "ADC1", "Abu Dhabi Corniche 1"),
        "theirs": branch(other, "OTH1", "Their Station"),
    }


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


@pytest.fixture
def user(client_in):
    return User.objects.get(username="sara.k")


def post(client, url, payload):
    return client.post(url, data=json.dumps(payload), content_type="application/json")


def make_customer(world, *, mobile_no="501234567", **fields):
    values = {
        "company": world["company"], "customer_code": "CU001", "first_name": "Ahmed",
        "mobile_country_code": "+971", "mobile_no": mobile_no,
    }
    values.update(fields)
    return Customer.objects.create(**values)
