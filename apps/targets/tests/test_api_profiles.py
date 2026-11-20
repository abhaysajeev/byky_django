# ruff: noqa: F811  -- pytest fixtures imported from the other test modules
"""Target Profiles on the device APIs (design/11-target-profiles.md §7):
added fields only -- the operator's `profiles`, the manager's profile_id,
targets/profiles and targets/target."""

import datetime
from decimal import Decimal

import pytest

from apps.crew.tests.test_attendance import (  # noqa: F401
    fresh_throttle,
    manager,
    operator,
    staff_app,
    world,
)
from apps.fare.tests.test_api import call
from apps.targets import services
from apps.targets.models import PeriodType, TargetProfile, TargetProfileBranch
from apps.targets.tests.test_api import (  # noqa: F401
    BRANCH,
    BRANCHES,
    OPERATOR,
    STATES,
    at,
    boss,
    clock,
    data,
    sale,
    target,
)

D = Decimal
PROFILES, TARGET = "/api/v1/manager/targets/profiles", "/api/v1/manager/targets/target"


def special(world, boss, day=10, amount="500"):
    when = datetime.date(2026, 11, day)
    profile = TargetProfile.objects.create(company=world["company"], name="Gate special", metric="sales",
                                           period_type=PeriodType.DAILY, starts_on=when, ends_on=when,
                                           is_active=True)
    TargetProfileBranch.objects.create(profile=profile, branch=world["auh1"], amount=D(amount))
    services.sync_profile(profile, boss, now=at(11, 1))
    return profile


def test_the_operator_gets_every_profile_active_today(client, world, operator, boss, sale, clock):
    target(world, boss, world["auh1"], "3000")
    special(world, boss)
    sale(at(11, 10, 9), "600")

    got = data(call(client, OPERATOR, {}, token=operator))
    assert got["sales"] is not None                                    # the old card is unchanged
    names = [(p["name"], p["type"]) for p in got["profiles"]]
    assert names == [("Monthly sales", "monthly"), ("Gate special", "daily")]
    day = got["profiles"][1]
    assert (D(day["target"]), D(day["achieved"]), day["met"], day["status"], day["today"]) == (
        D(500), D(600), True, "met", None)
    assert day["period"] == {"from_date": "2026-11-10", "to_date": "2026-11-10", "label": "10 Nov 2026"}
    assert got["profiles"][0]["today"] is not None


def test_the_manager_lists_profiles_and_reads_one(client, world, manager, boss, sale, clock):
    target(world, boss, world["auh1"], "3000")
    profile = special(world, boss)
    sale(at(11, 10, 9), "200")

    listed = data(call(client, PROFILES, {"year": 2026, "month": 11}, token=manager))["profiles"]
    assert [(p["name"], p["branch_count"]) for p in listed] == [("Monthly sales", 1), ("Gate special", 1)]

    states = data(call(client, STATES, {"profile_id": profile.pk, "date": "2026-11-10"}, token=manager))
    assert states["profile"]["name"] == "Gate special" and states["period"]["label"] == "10 Nov 2026"
    assert (D(states["total"]["target"]), D(states["total"]["pending"])) == (D(500), D(300))

    month = data(call(client, BRANCH, {"year": 2026, "month": 11, "branch_id": world["auh1"].pk}, token=manager))
    assert month["profile"]["name"] == "Monthly sales" and D(month["target"]) == 3000   # still the monthly one

    branches = data(call(client, BRANCHES, {"state_id": states["states"][0]["state_id"], "profile_id": profile.pk,
                                            "date": "2026-11-10"}, token=manager))
    one = data(call(client, TARGET, {"target_id": branches["branches"][0]["target_id"]}, token=manager))
    assert (one["card"], one["days"], one["today"], D(one["pending"])) == (True, [], None, D(300))

    unknown = call(client, STATES, {"profile_id": 999999}, token=manager)
    assert (unknown.status_code, unknown.json()["code"]) == (404, "no_profile")
    missing = call(client, TARGET, {"target_id": 999999}, token=manager)
    assert (missing.status_code, missing.json()["code"]) == (404, "no_target")


@pytest.mark.django_db
def test_the_new_paths_are_in_the_api_docs(client):
    from drf_spectacular.generators import SchemaGenerator

    paths = SchemaGenerator().get_schema(request=None, public=True)["paths"]
    assert any(p.endswith("/targets/profiles") for p in paths) and any(p.endswith("/targets/target") for p in paths)
