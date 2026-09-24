"""The database's own guard: every CHECK and EXCLUDE on the fare tables, and
the composite foreign keys that keep branch links and seasons honest.

These hold even if apps.fare.services is bypassed -- that is their job.
"""

from decimal import Decimal

import pytest
from django.db import IntegrityError, connection, transaction

from apps.company.models import WeekDay
from apps.fare.models import Fare, FareBranch, FareRule, RuleKind
from apps.fare.tests.conftest import D, make_fare, make_rule, make_season

SAT, SUN, MON = WeekDay.SATURDAY, WeekDay.SUNDAY, WeekDay.MONDAY


def refused(fn, constraint=None):
    with pytest.raises(IntegrityError) as caught, transaction.atomic():
        fn()
    if constraint:
        assert caught.value.__cause__.diag.constraint_name == constraint


# -- fare ------------------------------------------------------------------------


def test_fare_dates_and_numbers(world):
    refused(lambda: make_fare(world, valid_to=D(2025, 12, 31)), "fare_valid_dates")
    refused(lambda: make_fare(world, package_minutes=0), "fare_package_positive")
    refused(lambda: make_fare(world, base_fare=Decimal("-1")), "fare_money_not_negative")
    refused(lambda: make_fare(world, concurrent_interval_minutes=0), "fare_interval_positive")


def test_two_live_company_fares_cannot_overlap(world):
    make_fare(world)
    refused(lambda: make_fare(world, valid_from=D(2026, 12, 31), valid_to=D(2027, 6, 30)),
            "fare_company_no_overlap")


def test_company_fares_that_do_not_collide(world):
    make_fare(world)
    make_fare(world, valid_from=D(2027, 1, 1), valid_to=D(2027, 12, 31))       # after it
    make_fare(world, package_minutes=60)                                         # another package
    make_fare(world, vehicle_type=world["berg"])                                 # another vehicle type
    make_fare(world, is_active=False)                                            # inactive
    make_fare(world, company=world["other"], vehicle_type=world["their_type"])   # another company
    assert Fare.objects.count() == 6


# -- fare_branch -------------------------------------------------------------------


def test_two_live_branch_fares_cannot_share_a_branch(world):
    make_fare(world, branches=[world["adc1"], world["adc2"]])
    refused(lambda: make_fare(world, branches=[world["adc2"], world["family"]]), "fare_branch_no_overlap")


def test_a_branch_fare_and_a_company_fare_may_overlap(world):
    make_fare(world)
    make_fare(world, branches=[world["adc1"]])
    assert Fare.objects.count() == 2


def test_the_copies_follow_the_fare(world):
    fare = make_fare(world, branches=[world["adc1"]])
    Fare.objects.filter(pk=fare.pk).update(valid_to=D(2026, 6, 30), is_active=False)
    link = FareBranch.objects.get(fare=fare)
    assert (link.valid_to, link.is_active) == (D(2026, 6, 30), False)


def test_a_copy_cannot_disagree_with_its_fare(world):
    fare = make_fare(world, branches=[world["adc1"]])
    refused(lambda: FareBranch.objects.filter(fare=fare).update(valid_to=D(2027, 1, 1)),
            "fare_branch_fare_copy_fk")


def test_a_company_fare_cannot_have_branch_links(world):
    fare = make_fare(world)
    refused(lambda: FareBranch.objects.create(fare=fare, branch=world["adc1"]))


def test_reactivating_into_a_clash_is_refused(world):
    make_fare(world, branches=[world["adc1"]])
    idle = make_fare(world, branches=[world["adc1"]], is_active=False)
    refused(lambda: Fare.objects.filter(pk=idle.pk).update(is_active=True), "fare_branch_no_overlap")


# -- fare_season ---------------------------------------------------------------------


def test_seasons_of_one_fare_cannot_share_a_date(world):
    fare = make_fare(world)
    make_season(fare, D(2026, 4, 1), D(2026, 4, 30))
    refused(lambda: make_season(fare, D(2026, 4, 30), D(2026, 5, 10), name="Late"), "fare_season_no_overlap")
    refused(lambda: make_season(fare, D(2026, 6, 2), D(2026, 6, 1), name="Backwards"), "fare_season_dates")
    other = make_fare(world, vehicle_type=world["berg"])
    make_season(other, D(2026, 4, 1), D(2026, 4, 30))                           # another fare: fine


# -- fare_rule --------------------------------------------------------------------------


def test_rule_windows(world):
    fare = make_fare(world)
    refused(lambda: make_rule(fare, start=600, end=600), "fare_rule_window")
    refused(lambda: make_rule(fare, start=1320, end=120), "fare_rule_window")
    refused(lambda: make_rule(fare, start=0, end=1441), "fare_rule_window")
    make_rule(fare, start=1200, end=1440)                                       # until midnight


def test_rule_fields_match_their_kind(world):
    fare = make_fare(world)
    season = make_season(fare, D(2026, 4, 1), D(2026, 4, 30))
    refused(lambda: make_rule(fare, RuleKind.EVERY_DAY, weekdays=[SAT]), "fare_rule_kind_fields")
    refused(lambda: make_rule(fare, RuleKind.SELECTED_DAYS), "fare_rule_kind_fields")
    refused(lambda: make_rule(fare, RuleKind.SINGLE_DATE), "fare_rule_kind_fields")
    refused(lambda: make_rule(fare, RuleKind.SINGLE_DATE, on_date=D(2026, 4, 5), season=season),
            "fare_rule_kind_fields")
    refused(lambda: make_rule(fare, RuleKind.SELECTED_DAYS, weekdays=[7]), "fare_rule_weekday_values")


def test_every_day_rules_in_one_list_cannot_overlap(world):
    fare = make_fare(world)
    season = make_season(fare, D(2026, 4, 1), D(2026, 4, 30))
    make_rule(fare, start=960, end=1200)
    refused(lambda: make_rule(fare, start=1080, end=1320), "fare_rule_every_day_fare")
    make_rule(fare, start=1200, end=1320)                                       # back to back
    make_rule(fare, start=960, end=1200, season=season)                          # the season's own list
    refused(lambda: make_rule(fare, start=1000, end=1100, season=season), "fare_rule_every_day_season")


def test_selected_days_overlap_only_on_a_shared_day(world):
    fare = make_fare(world)
    make_rule(fare, RuleKind.SELECTED_DAYS, 840, 1320, weekdays=[SAT, SUN])
    make_rule(fare, RuleKind.SELECTED_DAYS, 840, 1320, weekdays=[MON])
    refused(lambda: make_rule(fare, RuleKind.SELECTED_DAYS, 900, 960, weekdays=[SUN]), "fare_rule_days_fare")


def test_single_dates_overlap_only_on_the_same_date(world):
    fare = make_fare(world)
    make_rule(fare, RuleKind.SINGLE_DATE, 720, 1320, on_date=D(2026, 12, 2))
    make_rule(fare, RuleKind.SINGLE_DATE, 720, 1320, on_date=D(2026, 12, 3))
    refused(lambda: make_rule(fare, RuleKind.SINGLE_DATE, 600, 780, on_date=D(2026, 12, 2)),
            "fare_rule_single_date")


def test_different_kinds_may_overlap(world):
    fare = make_fare(world)
    make_rule(fare, RuleKind.EVERY_DAY, 960, 1200)
    make_rule(fare, RuleKind.SELECTED_DAYS, 840, 1320, weekdays=[SAT])
    make_rule(fare, RuleKind.SINGLE_DATE, 0, 1440, on_date=D(2026, 12, 2))
    assert FareRule.objects.count() == 3


def test_a_rules_season_belongs_to_its_own_fare(world):
    fare = make_fare(world)
    other = make_fare(world, vehicle_type=world["berg"])
    theirs = make_season(other, D(2026, 4, 1), D(2026, 4, 30))
    refused(lambda: make_rule(fare, season=theirs), "fare_rule_season_same_fare_fk")


def test_deleting_a_fare_takes_its_rows_with_it(world):
    fare = make_fare(world, branches=[world["adc1"]])
    season = make_season(fare, D(2026, 4, 1), D(2026, 4, 30))
    make_rule(fare, season=season)
    make_rule(fare, RuleKind.SINGLE_DATE, on_date=D(2026, 12, 2))
    fare.delete()
    assert not FareBranch.objects.exists() and not FareRule.objects.exists()


# -- Deferral: what services.save_fare relies on ------------------------------------------


def test_swapping_two_windows_passes_when_deferred(world):
    fare = make_fare(world)
    a = make_rule(fare, start=600, end=720)
    b = make_rule(fare, start=720, end=840)
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS fare_rule_every_day_fare DEFERRED")
        FareRule.objects.filter(pk=a.pk).update(start_minute=720, end_minute=840)
        FareRule.objects.filter(pk=b.pk).update(start_minute=600, end_minute=720)
    assert FareRule.objects.get(pk=a.pk).start_minute == 720
