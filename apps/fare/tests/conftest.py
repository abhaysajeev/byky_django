"""Shared fixtures for the fare tests: two companies, branches, vehicle types,
a signed-in administrator, and small factories for fares and rules."""

import datetime
import json
from decimal import Decimal

import pytest

from apps.company.models import Branch, BranchType, Company, Country, Location, State
from apps.fare.models import Fare, FareBranch, FareLevel, FareRule, FareSeason, RuleKind
from apps.fleet.models import Brand, Category, VehicleType
from apps.portal.models import Role
from apps.portal.services import grant_all
from core.enums import Channel
from core.models import User

PASSWORD = "Byky#2026"
D = datetime.date


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

    def vehicle_type(company, name, tax="5.00"):
        category = Category.objects.get_or_create(
            company=company, category_code="BYKY", defaults={"category_name": "Byky"})[0]
        brand = Brand.objects.get_or_create(company=company, brand_code="BYK", defaults={"brand_name": "Byky"})[0]
        return VehicleType.objects.create(company=company, category=category, brand=brand,
                                          vehicle_type_name=name, tax_percentage=Decimal(tax))

    return {
        "company": byky, "other": other,
        "adc1": branch(byky, "ADC1", "Abu Dhabi Corniche 1"),
        "adc2": branch(byky, "ADC2", "Abu Dhabi Corniche 2"),
        "family": branch(byky, "FAM", "Family Park"),
        "theirs": branch(other, "OTH1", "Their Station"),
        "monaco": vehicle_type(byky, "Monaco"),
        "berg": vehicle_type(byky, "Berg"),
        "their_type": vehicle_type(other, "Their Kart"),
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


PRICE = {"base_fare": Decimal("50.00"), "grace_minutes": 5, "concurrent_interval_minutes": 10,
         "concurrent_fare": Decimal("10.00"), "concurrent_grace_minutes": 0}


def make_fare(world, *, branches=(), **fields):
    values = {"company": world["company"], "vehicle_type": world["monaco"], "level": FareLevel.COMPANY,
              "valid_from": D(2026, 1, 1), "valid_to": D(2026, 12, 31), "package_minutes": 30, **PRICE}
    if branches:
        values["level"] = FareLevel.BRANCH
    values.update(fields)
    fare = Fare.objects.create(**values)
    for branch in branches:
        FareBranch.objects.create(fare=fare, branch=branch)
    return fare


def make_season(fare, start, end, name="Spring", **fields):
    return FareSeason.objects.create(fare=fare, name=name, start_date=start, end_date=end, **{**PRICE, **fields})


def make_rule(fare, kind=RuleKind.EVERY_DAY, start=960, end=1200, *, season=None, weekdays=(), on_date=None, **fields):
    return FareRule.objects.create(fare=fare, season=season, kind=kind, start_minute=start, end_minute=end,
                                   weekdays=list(weekdays), on_date=on_date, **{**PRICE, **fields})
