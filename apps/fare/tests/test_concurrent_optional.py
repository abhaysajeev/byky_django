"""The concurrent interval, fare and grace are optional (owner, 23 Oct 2026).

Left blank they are stored and sent to the tablet as null -- the tablet decides
what that means. A direct-rent vehicle type has no concurrent pricing at all:
whatever the browser posts, the fare stores null.
"""

import pytest
from django.db import IntegrityError, transaction

from apps.fare import payload, services
from apps.fare.models import Fare
from apps.fare.services import Invalid
from apps.fare.tests.conftest import make_fare, make_rule, make_season
from apps.fare.tests.test_services import data, every, price

BLANK = {"concurrent_interval_minutes": "", "concurrent_fare": "", "concurrent_grace_minutes": ""}
CONCURRENT = tuple(BLANK)


def blank(fare):
    return {**price(fare), **BLANK}


def concurrent(row):
    return tuple(getattr(row, name) for name in CONCURRENT)


def every_row(fare):
    return [fare, *fare.seasons.all(), *fare.rules.all()]


def test_a_fare_saves_with_the_concurrent_fields_blank(world, user):
    rule = {**every("r", "08:00", "20:00", 60), "price": blank(60)}
    season = {"key": "s", "name": "Summer", "start_date": "2026-06-01", "end_date": "2026-08-31",
              "base": blank(55), "rules": []}
    fare, _ = services.save_fare(user, data(world, base=blank(50), rules=[rule], seasons=[season]))
    assert [concurrent(row) for row in every_row(fare)] == [(None, None, None)] * 3


def test_some_may_be_set_and_others_left_blank(world, user):
    fare, _ = services.save_fare(user, data(world, base={**blank(50), "concurrent_fare": "7.5"}))
    assert concurrent(fare) == (None, 7.5, None)


@pytest.mark.parametrize("field,value", [
    ("concurrent_interval_minutes", "0"), ("concurrent_interval_minutes", "1441"),
    ("concurrent_grace_minutes", "1441"), ("concurrent_grace_minutes", "-1"),
    ("concurrent_fare", "-1"), ("concurrent_fare", "abc"),
])
def test_a_value_that_is_given_is_still_checked(field, value):
    parsed = payload.parse({"vehicle_type": 1, "level": "company", "valid_from": "2026-01-01",
                            "valid_to": "2026-12-31", "package_minutes": 30,
                            "base": {**blank(50), field: value}})
    assert parsed.errors


def test_basic_fare_and_grace_stay_required(world, user):
    with pytest.raises(Invalid):
        services.save_fare(user, data(world, base={**blank(50), "base_fare": ""}))
    with pytest.raises(Invalid):
        services.save_fare(user, data(world, base={**blank(50), "grace_minutes": ""}))


def test_a_direct_rent_type_stores_no_concurrent_values(world, user):
    world["monaco"].is_direct_rent = True
    world["monaco"].save()
    season = {"key": "s", "name": "Summer", "start_date": "2026-06-01", "end_date": "2026-08-31",
              "base": price(55), "rules": []}
    fare, _ = services.save_fare(user, data(world, rules=[every("r", "08:00", "20:00", 60)], seasons=[season]))
    assert [concurrent(row) for row in every_row(fare)] == [(None, None, None)] * 3
    assert fare.base_fare == 50                                    # the rest is kept


def test_blanks_round_trip_through_the_form(world, user):
    fare, _ = services.save_fare(user, data(world, base=blank(50)))
    sent = payload.serialise(services.with_children(Fare.objects.filter(pk=fare.pk)).get())
    assert {k: sent["base"][k] for k in CONCURRENT} == BLANK
    parsed = payload.parse(sent)
    assert parsed.errors == []
    assert concurrent(parsed.spec.base) == (None, None, None)


def test_the_database_allows_null_but_still_refuses_bad_values(world):
    fare = make_fare(world, concurrent_interval_minutes=None, concurrent_fare=None, concurrent_grace_minutes=None)
    make_season(fare, fare.valid_from, fare.valid_to, concurrent_fare=None)
    make_rule(fare, concurrent_interval_minutes=None)
    with pytest.raises(IntegrityError), transaction.atomic():
        make_fare(world, concurrent_interval_minutes=0)


def test_the_screen_marks_direct_rent_types(client_in, world):
    world["berg"].is_direct_rent = True
    world["berg"].save()
    html = client_in.get("/fare/add/").content.decode()
    assert 'data-direct-rent="1">Berg' in html and 'data-direct-rent="0">Monaco' in html

