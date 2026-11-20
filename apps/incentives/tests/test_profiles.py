# ruff: noqa: F811  -- pytest fixtures imported from the other test modules
"""Incentives per target profile (design/11-target-profiles.md P6-P7): a
scheme pays on one profile, a run is a station's period of it, overlapping
profiles give separate runs, and each run's money lands in its payout month."""

import datetime
from decimal import Decimal

import pytest

from apps.company.models import CompanyConfig
from apps.incentives import services
from apps.incentives.models import AppliesTo, Payout, Scheme
from apps.incentives.services import IncentiveError
from apps.incentives.tests.test_calculate import (  # noqa: F401
    AFTER,
    NOV,
    example,
    monthly_sales,
    people,
    scheme,
)
from apps.targets import services as targets
from apps.targets.models import (
    PeriodType,
    ProfileStatus,
    Target,
    TargetProfile,
    TargetProfileBranch,
)
from apps.targets.tests.test_targets import local, sale, staff, two_decimals  # noqa: F401

pytestmark = pytest.mark.django_db
D = Decimal
SIXTH = datetime.date(2026, 11, 6)


def special_profile(world, staff, payout=Payout.PERIOD_END_MONTH, amount="300"):
    """A one-day special on 6 Nov at ADC1, with a flat 10 a person scheme."""
    profile = TargetProfile.objects.create(company=world["company"], name="6 Nov special", metric="sales",
                                           period_type=PeriodType.DAILY, starts_on=SIXTH, ends_on=SIXTH,
                                           status=ProfileStatus.ACTIVE)
    TargetProfileBranch.objects.create(profile=profile, branch=world["adc1"], amount=D(amount))
    targets.sync_profile(profile, staff, now=local(1))
    flat = Scheme.objects.create(company=world["company"], profile=profile, payout=payout, name="Special",
                                 effective_from=SIXTH, is_active=True)
    flat.components.create(position=1, name="Flat", applies_to=AppliesTo.EVERYONE, formula="10")
    return profile


def test_two_profiles_on_one_station_give_two_runs(world, staff, example, sale):
    special = special_profile(world, staff)
    sale(local(6), "400")

    monthly = services.calculate(world["adc1"], NOV, staff, now=AFTER)
    day = services.calculate(world["adc1"], SIXTH, staff, now=AFTER, profile=special)

    assert monthly.profile == monthly_sales(world) and day.profile == special
    assert (monthly.achieved, monthly.work_days) == (D("5400"), 4)           # the same orders count for both
    assert (day.period_start, day.period_end, day.achieved, day.work_days) == (SIXTH, SIXTH, D("400"), 1)
    assert day.total == D("40.00")                                            # 4 people rostered on the 6th
    assert day.payout_month == monthly.payout_month == NOV


def test_a_profile_not_met_is_refused_by_its_own_figure(world, staff, example, sale):
    special = special_profile(world, staff, amount="1000")
    sale(local(6), "400")
    with pytest.raises(IncentiveError, match="did not meet"):
        services.calculate(world["adc1"], SIXTH, staff, now=AFTER, profile=special)
    # ...while the monthly target, met, still pays
    assert services.calculate(world["adc1"], NOV, staff, now=AFTER).total > 0


def test_the_financial_year_end_payout(world, staff):
    CompanyConfig.objects.create(company=world["company"], financial_year_start=4)
    special = special_profile(world, staff, payout=Payout.FINANCIAL_YEAR_END)
    target = Target.objects.get(profile=special)
    scheme = special.schemes.get()

    assert services.payout_month(scheme, target) == datetime.date(2027, 3, 1)
    scheme.payout = Payout.PERIOD_END_MONTH
    assert services.payout_month(scheme, target) == NOV


def test_by_employee_groups_by_payout_month_one_line_per_profile(world, staff, example, sale):
    special = special_profile(world, staff)
    sale(local(6), "400")
    services.calculate(world["adc1"], NOV, staff, now=AFTER)
    services.calculate(world["adc1"], SIXTH, staff, now=AFTER, profile=special)

    anas = next(r for r in services.employee_totals(world["company"], [world["adc1"]], NOV)
                if r["employee"].first_name == "Anas")
    assert [(s["profile"].name, s["days"]) for s in anas["stations"]] == [("6 Nov special", 1), ("Monthly sales", 3)]
    assert anas["draft"] == D("13.50") * 3 + D("10.00")               # basis 5,400 / 4 days -> 13.50 a day
    assert services.employee_totals(world["company"], [world["adc1"]], datetime.date(2026, 12, 1)) == []


def test_days_in_period_and_the_old_name_agree():
    values = services.month_values(D("100"), D("50"), 2, SIXTH, SIXTH)
    assert values["days_in_period"] == values["days_in_month"] == D("1")
    november = services.month_values(D("100"), D("50"), 2, NOV)
    assert november["days_in_period"] == D("30")


def test_eligibility_uses_the_real_working_days(world, staff, example):
    scheme = monthly_sales(world).schemes.get()
    scheme.pays_when = "work_days >= 4"                          # 4 working days in the example
    scheme.save()
    row = services.eligibility(world["adc1"], NOV, now=AFTER)
    assert row["can_calculate"], row["reason"]


def test_a_daily_profile_is_calculated_only_after_its_day(world, staff, example, sale):
    special = special_profile(world, staff)
    sale(local(6), "400")
    during = datetime.datetime(2026, 11, 6, 12, 0, tzinfo=AFTER.tzinfo)
    with pytest.raises(IncentiveError, match="not over"):
        services.calculate(world["adc1"], SIXTH, staff, now=during, profile=special)
