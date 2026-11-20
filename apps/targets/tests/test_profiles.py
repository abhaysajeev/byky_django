# ruff: noqa: F811  -- pytest fixtures imported from test_targets
"""Target Profiles (design/11-target-profiles.md): periods per type, the
annual split, the duplicate rule, generation, amount changes, overrides,
station removal, and overlapping profiles counting the same orders."""

import datetime
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction

from apps.company.models import Branch, BranchType, CompanyConfig
from apps.targets import periods as P
from apps.targets import services
from apps.targets.models import (
    PeriodType,
    Target,
    TargetDay,
    TargetKind,
    TargetProfile,
    TargetProfileBranch,
)
from apps.targets.tests.test_targets import NOW, local, sale, staff, two_decimals  # noqa: F401

pytestmark = pytest.mark.django_db
D = Decimal
date = datetime.date


def make_profile(world, staff, name, period_type, starts_on, ends_on, amounts, metric=TargetKind.SALES,  # noqa: F811
                 is_active=True):
    profile = TargetProfile.objects.create(company=world["company"], name=name, metric=metric,
                                           period_type=period_type, starts_on=starts_on, ends_on=ends_on,
                                           is_active=is_active, created_by=staff, modified_by=staff)
    for branch, amount in amounts:
        TargetProfileBranch.objects.create(profile=profile, branch=branch, amount=D(amount))
    return profile


@pytest.fixture
def creek(world):
    return Branch.objects.create(company=world["company"], location=world["adc1"].location, short_code="CRK",
                                 name="Creek Park", branch_type=BranchType.STATION)


# -- Periods ---------------------------------------------------------------------------------


def test_each_type_makes_its_periods():
    assert P.periods(PeriodType.DAILY, date(2026, 12, 5), date(2026, 12, 7)) == [
        (date(2026, 12, 5), date(2026, 12, 5)), (date(2026, 12, 6), date(2026, 12, 6)),
        (date(2026, 12, 7), date(2026, 12, 7))]
    assert P.periods(PeriodType.MONTHLY, date(2026, 11, 14), date(2027, 1, 3)) == [
        (date(2026, 11, 1), date(2026, 11, 30)), (date(2026, 12, 1), date(2026, 12, 31)),
        (date(2027, 1, 1), date(2027, 1, 31))]
    assert P.periods(PeriodType.CUSTOM, date(2026, 12, 1), date(2026, 12, 10)) == [
        (date(2026, 12, 1), date(2026, 12, 10))]


def test_an_annual_profile_follows_the_financial_year():
    assert P.periods(PeriodType.ANNUAL, date(2027, 5, 1), date(2027, 5, 1), start_month=4) == [
        (date(2027, 4, 1), date(2028, 3, 31))]
    assert P.periods(PeriodType.ANNUAL, date(2027, 2, 1), date(2027, 5, 1), start_month=4) == [
        (date(2026, 4, 1), date(2027, 3, 31)), (date(2027, 4, 1), date(2028, 3, 31))]
    assert P.period_label(PeriodType.ANNUAL, date(2027, 4, 1), date(2028, 3, 31)) == \
        "FY 2027–28 (Apr 2027 – Mar 2028)"
    assert P.period_label(PeriodType.ANNUAL, date(2027, 1, 1), date(2027, 12, 31)).startswith("FY 2027 ")


def test_labels():
    assert P.period_label(PeriodType.DAILY, date(2026, 12, 5), date(2026, 12, 5)) == "5 Dec 2026"
    assert P.period_label(PeriodType.MONTHLY, date(2026, 12, 1), date(2026, 12, 31)) == "December 2026"
    assert P.period_label(PeriodType.CUSTOM, date(2026, 12, 1), date(2026, 12, 10)) == "1–10 Dec 2026"


def test_default_weights_follow_the_days_and_total_100():
    leap = P.default_weights(date(2028, 1, 1))
    assert sum(leap.values()) == 100 and leap[2] > P.default_weights(date(2027, 1, 1))[2]
    assert P.weights_valid(leap)
    assert not P.weights_valid({**leap, 1: leap[1] + 1})


def test_the_annual_plan_uses_the_month_weights_and_adds_up():
    weights = dict.fromkeys(range(1, 13), D("5"))
    weights[12] = D("45")                                       # a heavy last month
    plan = P.plan(PeriodType.ANNUAL, D("120000"), date(2027, 1, 1), date(2027, 12, 31), 2, weights)

    assert sum(plan.values()) == D("120000")
    assert sum(v for d, v in plan.items() if d.month == 12) == D("54000")
    assert sum(v for d, v in plan.items() if d.month == 1) == D("6000")
    assert len(plan) == 365


# -- Generation and the duplicate rule --------------------------------------------------------


def test_activating_makes_every_period_with_its_days(world, staff, creek):
    profile = make_profile(world, staff, "Dec specials", PeriodType.DAILY, date(2026, 12, 5), date(2026, 12, 6),
                           [(world["adc1"], "5000"), (creek, "3000")])
    made, changed, refused = services.sync_profile(profile, staff, now=NOW)

    assert (made, changed, refused) == (4, 0, [])
    target = Target.objects.get(profile=profile, branch=world["adc1"], period_start=date(2026, 12, 5))
    assert target.period_end == target.period_start and target.period_type == PeriodType.DAILY
    assert list(target.days.values_list("planned_amount", flat=True)) == [D("5000")]
    assert services.sync_profile(profile, staff, now=NOW) == (0, 0, [])          # idempotent


def test_a_draft_profile_makes_nothing(world, staff):
    profile = make_profile(world, staff, "Later", PeriodType.MONTHLY, date(2026, 12, 1), date(2026, 12, 31),
                           [(world["adc1"], "1000")], is_active=False)
    assert services.sync_profile(profile, staff, now=NOW) == (0, 0, [])
    assert not Target.objects.exists()


def test_different_types_may_overlap_but_the_same_type_and_metric_may_not(world, staff):
    monthly = make_profile(world, staff, "Dec", PeriodType.MONTHLY, date(2026, 12, 1), date(2026, 12, 31),
                           [(world["adc1"], "30000")])
    special = make_profile(world, staff, "5 Dec", PeriodType.DAILY, date(2026, 12, 5), date(2026, 12, 5),
                           [(world["adc1"], "5000")])
    customers = make_profile(world, staff, "Dec customers", PeriodType.MONTHLY, date(2026, 12, 1),
                             date(2026, 12, 31), [(world["adc1"], "300")], metric=TargetKind.CUSTOMERS)
    again = make_profile(world, staff, "Dec again", PeriodType.MONTHLY, date(2026, 12, 1), date(2026, 12, 31),
                         [(world["adc1"], "1")])

    assert services.sync_profile(monthly, staff, now=NOW)[0] == 1
    assert services.sync_profile(special, staff, now=NOW)[0] == 1
    assert services.sync_profile(customers, staff, now=NOW)[0] == 1
    made, _, refused = services.sync_profile(again, staff, now=NOW)
    assert made == 0
    assert refused[0].branch == world["adc1"] and "“Dec”" in refused[0].reason


def test_the_database_refuses_a_duplicate_too(world, staff):
    one = make_profile(world, staff, "A", PeriodType.CUSTOM, date(2026, 12, 1), date(2026, 12, 10), [])
    two = make_profile(world, staff, "B", PeriodType.CUSTOM, date(2026, 12, 8), date(2026, 12, 20), [])
    common = {"company": world["company"], "branch": world["adc1"], "kind": "sales", "period_type": "custom",
              "amount": D("1")}
    Target.objects.create(profile=one, period_start=date(2026, 12, 1), period_end=date(2026, 12, 10), **common)
    with pytest.raises(IntegrityError), transaction.atomic():
        Target.objects.create(profile=two, period_start=date(2026, 12, 8), period_end=date(2026, 12, 20), **common)


def test_ended_periods_are_filled_only_the_first_time(world, staff):
    profile = make_profile(world, staff, "Autumn", PeriodType.MONTHLY, date(2026, 10, 1), date(2026, 12, 31),
                           [(world["adc1"], "1000")])
    assert services.sync_profile(profile, staff, now=NOW)[0] == 3           # set up late: October too

    profile.ends_on = date(2027, 1, 31)
    profile.save()
    TargetProfileBranch.objects.create(profile=profile, branch=Branch.objects.create(
        company=world["company"], location=world["adc1"].location, short_code="NEW", name="New Gate",
        branch_type=BranchType.STATION), amount=D("500"))
    made, _, _ = services.sync_profile(profile, staff, now=NOW)
    # adc1 gets January; the new station Nov-Jan, never the ended October
    assert made == 1 + 3
    assert not Target.objects.filter(branch__name="New Gate", period_start=date(2026, 10, 1)).exists()


# -- Amount changes, overrides, removal ---------------------------------------------------------


def test_an_amount_change_moves_open_periods_only_and_keeps_an_edited_one(world, staff):
    profile = make_profile(world, staff, "Monthly", PeriodType.MONTHLY, date(2026, 10, 1), date(2026, 12, 31),
                           [(world["adc1"], "3000")])
    services.sync_profile(profile, staff, now=NOW)
    december = Target.objects.get(profile=profile, period_start=date(2026, 12, 1))
    services.update_target(december, D("9000"), "Eid", staff, now=NOW)             # edited on its own
    assert Target.objects.get(pk=december.pk).amount_overridden

    assignment = profile.assignments.get()
    assignment.amount = D("6000")
    assignment.save()
    _, changed, _ = services.sync_profile(profile, staff, now=NOW)

    amounts = dict(Target.objects.filter(profile=profile).values_list("period_start", "amount"))
    assert changed == 1
    assert amounts == {date(2026, 10, 1): D("3000"), date(2026, 11, 1): D("6000"), date(2026, 12, 1): D("9000")}
    november = Target.objects.get(profile=profile, period_start=date(2026, 11, 1))
    assert sum(november.days.values_list("planned_amount", flat=True)) == D("6000")
    assert november.days.get(date=date(2026, 11, 1)).planned_amount == D("100")     # past days keep their plan


def test_removing_a_station_keeps_its_history(world, staff):
    profile = make_profile(world, staff, "Monthly", PeriodType.MONTHLY, date(2026, 11, 1), date(2026, 12, 31),
                           [(world["adc1"], "3000")])
    services.sync_profile(profile, staff, now=NOW)
    services.remove_branch(profile, world["adc1"], now=NOW)

    assert list(Target.objects.filter(profile=profile).values_list("period_start", flat=True)) == [
        date(2026, 11, 1)]
    assert not profile.assignments.get().active
    assert services.sync_profile(profile, staff, now=NOW)[0] == 0                   # not brought back


def test_a_profile_can_be_deleted_only_once_no_branch_is_mapped(world, staff):
    running = make_profile(world, staff, "Now", PeriodType.CUSTOM, date(2026, 11, 1), date(2026, 11, 20),
                           [(world["adc1"], "1000")])
    services.sync_profile(running, staff, now=NOW)

    assert "mapped to 1 branch" in services.delete_refusal(running)
    assert services.profile_locked_fields(running) == {"metric", "period_type"}
    services.set_branches(running, [], staff, now=NOW)                  # unmap it
    assert services.can_delete_profile(running)
    services.delete_profile(running)
    assert not Target.objects.exists() and not TargetProfile.objects.filter(name="Now").exists()


def test_the_annual_profile_uses_its_weights(world, staff):
    CompanyConfig.objects.create(company=world["company"], financial_year_start=4)
    profile = make_profile(world, staff, "FY", PeriodType.ANNUAL, date(2027, 4, 1), date(2028, 3, 31),
                           [(world["adc1"], "120000")])
    weights = dict.fromkeys(range(1, 13), D("5"))
    weights[9] = D("45")                                         # December: the 9th month from April
    services.set_weights(profile, weights)
    services.sync_profile(profile, staff, now=NOW)

    target = Target.objects.get(profile=profile)
    assert (target.period_start, target.period_end) == (date(2027, 4, 1), date(2028, 3, 31))
    december = TargetDay.objects.filter(target=target, date__month=12)
    assert sum(december.values_list("planned_amount", flat=True)) == D("54000")
    with pytest.raises(services.TargetError):
        services.set_weights(profile, {**weights, 9: D("46")})


# -- Overlapping profiles count the same orders ---------------------------------------------


def test_the_same_orders_count_for_every_profile_covering_them(world, staff, sale):
    monthly = make_profile(world, staff, "Nov", PeriodType.MONTHLY, date(2026, 11, 1), date(2026, 11, 30),
                           [(world["adc1"], "30000")])
    special = make_profile(world, staff, "8 Nov", PeriodType.DAILY, date(2026, 11, 8), date(2026, 11, 8),
                           [(world["adc1"], "500")])
    services.sync_profile(monthly, staff, now=NOW)
    services.sync_profile(special, staff, now=NOW)
    sale(local(8), "700")
    sale(local(9), "100")

    on_the_day = Target.objects.get(profile=special)
    card = services.month_view(on_the_day, now=NOW)
    assert card["card"] and card["figures"].achieved == D("700") and card["status"]["label"] == "Met"
    assert services.month_view(Target.objects.get(profile=monthly), now=NOW)["figures"].achieved == D("800")

    rows = services.station_targets(world["adc1"], date(2026, 11, 10), now=NOW)
    assert [r["target"].profile.name for r in rows] == ["Nov", "8 Nov"]


def test_customer_counts_use_each_targets_own_window(world, staff, sale):
    month = make_profile(world, staff, "Nov customers", PeriodType.MONTHLY, date(2026, 11, 1), date(2026, 11, 30),
                         [(world["adc1"], "50")], metric=TargetKind.CUSTOMERS)
    week = make_profile(world, staff, "Week customers", PeriodType.CUSTOM, date(2026, 11, 9), date(2026, 11, 10),
                        [(world["adc1"], "5")], metric=TargetKind.CUSTOMERS)
    services.sync_profile(month, staff, now=NOW)
    services.sync_profile(week, staff, now=NOW)
    sale(local(2), "10")                                         # the same customer every time
    sale(local(9), "10")

    found = services.achieved_for(list(Target.objects.all()), date(2026, 11, 10))
    by_profile = {Target.objects.get(pk=pk).profile.name: v for pk, v in found.items()}
    assert by_profile == {"Nov customers": D("1"), "Week customers": D("1")}


def test_create_targets_puts_a_month_on_the_monthly_profile(world, staff):
    saved, _ = services.create_targets(world["company"], [world["adc1"]], 2026, 11, D("30000"), "", staff, now=NOW)
    target = saved[0]
    assert target.profile.name == "Monthly sales" and target.period_type == PeriodType.MONTHLY
    assert target.profile.assignments.get().amount == D("30000")
