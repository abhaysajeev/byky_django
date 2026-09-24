"""apps.fare.pricing -- the precedence, checked two ways.

Fixed cases walk the example fare from the prototype (design/fares and offers/
fare-prototype.html), renumbered to Monday=0. Then a seeded brute force
compares every date and half hour of hundreds of random fares against an
independent reference copied from design/fares and offers/fare_algorithm_check.py.
No database needed.
"""

import datetime
import random
import time
from decimal import Decimal

import pytest

from apps.company.models import WeekDay
from apps.fare import pricing
from apps.fare.pricing import (
    EVERY_DAY,
    MIDNIGHT,
    SELECTED_DAYS,
    SINGLE_DATE,
    FareSpec,
    Price,
    Rule,
    Season,
)

D = datetime.date


def price(fare, conc=10):
    return Price(Decimal(fare), 5, 10, Decimal(conc), 0)


def hm(text):
    h, m = text.split(":")
    return int(h) * 60 + int(m)


def rule(key, kind, start, end, fare, days=(), on=None):
    return Rule(key, kind, hm(start), hm(end) if end != "24:00" else MIDNIGHT, price(fare),
                frozenset(days), on)


SAT, SUN = WeekDay.SATURDAY, WeekDay.SUNDAY
SPRING = Season("s1", "Spring", D(2026, 4, 10), D(2026, 5, 15), price(55), (
    rule("r11", EVERY_DAY, "16:00", "20:00", 90),
    rule("r12", SELECTED_DAYS, "14:00", "22:00", 85, days=(SAT, SUN)),
))
EXAMPLE = FareSpec(D(2026, 1, 1), D(2026, 12, 31), 30, price(50), (
    rule("r1", EVERY_DAY, "16:00", "20:00", 70),
    rule("r2", SELECTED_DAYS, "14:00", "22:00", 80, days=(SAT, SUN)),
    rule("r3", SINGLE_DATE, "12:00", "22:00", 100, on=D(2026, 12, 2)),
    rule("r4", SINGLE_DATE, "12:00", "18:00", 120, on=D(2026, 4, 25)),
), (SPRING,))


# -- The example fare ------------------------------------------------------------


@pytest.mark.parametrize("day,at,fare,source", [
    (D(2026, 9, 23), "17:30", 70, "r1"),          # Wednesday evening
    (D(2026, 9, 23), "10:00", 50, None),          # base
    (D(2026, 9, 26), "17:30", 80, "r2"),          # Saturday beats every day
    (D(2026, 4, 22), "17:30", 90, "r11"),         # Spring: the season's own evening
    (D(2026, 4, 22), "10:00", 55, None),          # Spring base
    (D(2026, 4, 25), "15:00", 120, "r4"),         # single date inside Spring wins
    (D(2026, 4, 25), "19:00", 85, "r12"),         # rest of that Saturday: Spring's weekend
    (D(2026, 12, 2), "18:00", 100, "r3"),         # National Day
    (D(2026, 12, 2), "23:00", 50, None),
    (D(2026, 12, 31), "23:59", 50, None),         # last minute of the fare
    (D(2026, 5, 15), "17:00", 90, "r11"),         # last day of Spring
    (D(2026, 5, 16), "17:00", 80, "r2"),          # first day after it (a Saturday)
])
def test_the_example_fare(day, at, fare, source):
    found = pricing.resolve(EXAMPLE, day, hm(at))
    assert found.price.base_fare == Decimal(fare)
    assert (found.source.rule.key if found.source.rule else None) == source


def test_no_fare_outside_the_validity():
    assert pricing.resolve(EXAMPLE, D(2027, 1, 5), hm("10:00")) is None
    assert pricing.day_schedule(EXAMPLE, D(2027, 1, 5)) == []


def test_what_else_matched_is_reported_with_the_reason():
    found = pricing.resolve(EXAMPLE, D(2026, 4, 25), hm("19:00"))
    reasons = {source.rule.key: reason for source, reason in found.also}
    assert reasons == {
        "r11": "a more specific price applies",       # Spring's every day, beaten by its Saturday
        "r1": "not used inside Spring",
        "r2": "not used inside Spring",
    }


def test_the_day_is_one_gap_free_timeline():
    segments = pricing.day_schedule(EXAMPLE, D(2026, 4, 25), merge="price")
    assert [(pricing.hhmm(s.start), pricing.hhmm(s.end), s.price.base_fare) for s in segments] == [
        ("00:00", "12:00", 55), ("12:00", "18:00", 120), ("18:00", "22:00", 85), ("22:00", "24:00", 55),
    ]


def test_labels_read_in_the_uae_week():
    assert pricing.days_label({SAT, SUN, WeekDay.MONDAY}) == "Sun, Mon, Sat"
    assert pricing.window_label(rule("x", EVERY_DAY, "00:00", "24:00", 1)) == "All day"
    assert pricing.rule_label(rule("x", EVERY_DAY, "20:00", "24:00", 1)) == "Every day · 20:00 – 24:00"


def test_the_example_is_valid_and_nothing_is_hidden():
    assert pricing.validate_spec(EXAMPLE) == []
    assert pricing.never_applies(EXAMPLE) == {}
    assert pricing.single_date_notes(EXAMPLE) == {"r4": "Inside Spring. Takes priority there."}


# -- Validation -------------------------------------------------------------------


def messages(spec):
    return [(i.key, i.message) for i in pricing.validate_spec(spec)]


def with_rules(*rules, seasons=()):
    return FareSpec(D(2026, 1, 1), D(2026, 12, 31), 30, price(50), rules, seasons)


def test_two_every_day_prices_cannot_share_a_minute():
    spec = with_rules(rule("a", EVERY_DAY, "16:00", "20:00", 70), rule("b", EVERY_DAY, "18:00", "22:00", 80))
    assert messages(spec) == [("b", "Overlaps Every day · 16:00 – 20:00. Two every day prices cannot cover the same time.")]


def test_back_to_back_windows_do_not_overlap():
    assert messages(with_rules(rule("a", EVERY_DAY, "09:00", "10:00", 1), rule("b", EVERY_DAY, "10:00", "11:00", 1))) == []


def test_selected_days_overlap_only_on_a_shared_day():
    ok = with_rules(rule("a", SELECTED_DAYS, "10:00", "12:00", 1, days=(SAT,)),
                    rule("b", SELECTED_DAYS, "10:00", "12:00", 1, days=(SUN,)))
    clash = with_rules(rule("a", SELECTED_DAYS, "10:00", "12:00", 1, days=(SAT, SUN)),
                       rule("b", SELECTED_DAYS, "11:00", "13:00", 1, days=(SUN,)))
    assert messages(ok) == []
    assert [k for k, _ in messages(clash)] == ["b"]


def test_different_kinds_may_overlap():
    assert messages(with_rules(rule("a", EVERY_DAY, "10:00", "20:00", 1),
                               rule("b", SELECTED_DAYS, "12:00", "14:00", 1, days=(SAT,)),
                               rule("c", SINGLE_DATE, "00:00", "24:00", 1, on=D(2026, 6, 6)))) == []


def test_windows_cannot_run_backwards_or_past_midnight():
    assert "past midnight" in messages(with_rules(rule("a", EVERY_DAY, "22:00", "02:00", 1)))[0][1]


def test_a_single_date_must_be_inside_the_validity_and_not_in_a_season():
    outside = with_rules(rule("a", SINGLE_DATE, "10:00", "12:00", 1, on=D(2027, 1, 1)))
    assert "inside the fare validity" in messages(outside)[0][1]
    season = Season("s", "Spring", D(2026, 4, 1), D(2026, 4, 30), price(55),
                    (rule("b", SINGLE_DATE, "10:00", "12:00", 1, on=D(2026, 4, 5)),))
    assert "added on the fare" in messages(with_rules(seasons=(season,)))[0][1]


def test_seasons_cannot_share_a_date_or_leave_the_validity():
    a = Season("s1", "Spring", D(2026, 4, 1), D(2026, 4, 30), price(55))
    b = Season("s2", "Ramadan", D(2026, 4, 30), D(2026, 5, 20), price(60))
    c = Season("s3", "Winter", D(2026, 12, 1), D(2027, 1, 31), price(60))
    found = messages(with_rules(seasons=(a, b, c)))
    assert ("s2", "Shares dates with Spring. A date can belong to one season only.") in found
    assert any(k == "s3" and "inside the fare validity" in m for k, m in found)


def test_money_and_intervals_are_checked_everywhere():
    bad = Price(Decimal("-1"), 5, 0, Decimal("1"), 0)
    spec = FareSpec(D(2026, 1, 1), D(2026, 12, 31), 0, bad, (), ())
    assert {m for _, m in messages(spec)} >= {
        "Package time must be at least 1 minute.", "Basic fare cannot be negative.",
        "Concurrent interval must be at least 1 minute.",
    }


# -- Never applies ------------------------------------------------------------------


def test_a_season_over_the_whole_fare_hides_the_fares_own_rules():
    whole = Season("s", "All year", D(2026, 1, 1), D(2026, 12, 31), price(60))
    spec = with_rules(rule("a", EVERY_DAY, "16:00", "20:00", 70), seasons=(whole,))
    assert pricing.never_applies(spec) == {
        "a": "Every day it could apply is inside a season, which uses its own prices.",
    }


def test_every_day_hidden_by_an_all_week_selected_days_rule():
    spec = with_rules(rule("a", EVERY_DAY, "16:00", "20:00", 70),
                      rule("b", SELECTED_DAYS, "00:00", "24:00", 65, days=tuple(WeekDay)))
    assert list(pricing.never_applies(spec)) == ["a"]


def test_days_that_never_occur_in_a_short_season():
    three_days = Season("s", "Short", D(2026, 6, 2), D(2026, 6, 4), price(60),   # Tue-Thu
                        (rule("a", SELECTED_DAYS, "10:00", "12:00", 70, days=(SAT,)),))
    assert pricing.never_applies(with_rules(seasons=(three_days,))) == {
        "a": "None of its days fall inside this season.",
    }


def test_a_very_long_validity_stays_fast():
    spec = FareSpec(D(2026, 1, 1), D(9999, 12, 31), 30, price(50),
                    (rule("a", EVERY_DAY, "16:00", "20:00", 70),), (SPRING,))
    began = time.perf_counter()
    assert pricing.never_applies(spec) == {}
    assert time.perf_counter() - began < 1


# -- Brute force against the reference ------------------------------------------------
# The reference below is design/fares and offers/fare_algorithm_check.py, kept
# independent of the code under test on purpose.


def ref_valid(spec):
    def list_ok(rules):
        for i, a in enumerate(rules):
            if not a.start < a.end:
                return False
            if a.kind == SELECTED_DAYS and not a.weekdays:
                return False
            for b in rules[i + 1:]:
                if a.kind != b.kind or not (a.start < b.end and b.start < a.end):
                    continue
                if a.kind == EVERY_DAY or (a.kind == SELECTED_DAYS and a.weekdays & b.weekdays) \
                        or (a.kind == SINGLE_DATE and a.on_date == b.on_date):
                    return False
        return True

    if not list_ok(spec.rules):
        return False
    for x in spec.rules:
        if x.kind == SINGLE_DATE and not spec.valid_from <= x.on_date <= spec.valid_to:
            return False
    for i, s in enumerate(spec.seasons):
        if not (spec.valid_from <= s.start <= s.end <= spec.valid_to) or not list_ok(s.rules):
            return False
        for o in spec.seasons[i + 1:]:
            if s.start <= o.end and o.start <= s.end:
                return False
    return True


def ref_resolve(spec, d, m):
    singles = [x for x in spec.rules if x.kind == SINGLE_DATE and x.on_date == d and x.start <= m < x.end]
    if singles:
        return singles
    ss = [s for s in spec.seasons if s.start <= d <= s.end]
    rules = ss[0].rules if ss else spec.rules
    days = [r for r in rules if r.kind == SELECTED_DAYS and d.weekday() in r.weekdays and r.start <= m < r.end]
    if days:
        return days
    every = [r for r in rules if r.kind == EVERY_DAY and r.start <= m < r.end]
    return every or [None]


def random_window():
    a = random.choice(range(0, MIDNIGHT, 60))
    return a, random.choice(range(a + 60, MIDNIGHT + 1, 60))


def random_rule(key):
    a, b = random_window()
    if random.random() < .5:
        return Rule(key, EVERY_DAY, a, b, price(random.randint(1, 99)))
    return Rule(key, SELECTED_DAYS, a, b, price(random.randint(1, 99)),
                frozenset(random.sample(range(7), random.randint(1, 4))))


def random_spec(n):
    v0 = D(2028, 2, 20)                       # spans 29 Feb 2028
    v1 = v0 + datetime.timedelta(days=random.randint(10, 40))
    span = (v1 - v0).days
    rules = [random_rule(f"f{n}-{i}") for i in range(random.randint(0, 4))]
    for i in range(random.randint(0, 3)):
        a, b = random_window()
        rules.append(Rule(f"d{n}-{i}", SINGLE_DATE, a, b, price(random.randint(1, 99)),
                          frozenset(), v0 + datetime.timedelta(days=random.randint(0, span))))
    seasons = []
    for i in range(random.randint(0, 2)):
        s = v0 + datetime.timedelta(days=random.randint(0, span))
        e = min(v1, s + datetime.timedelta(days=random.randint(0, 12)))
        seasons.append(Season(f"s{n}-{i}", f"S{i}", s, e, price(random.randint(1, 99)),
                              tuple(random_rule(f"s{n}-{i}-{j}") for j in range(random.randint(0, 3)))))
    return FareSpec(v0, v1, 30, price(50), tuple(rules), tuple(seasons))


def test_brute_force_matches_the_reference():
    random.seed(7)
    valid = 0
    for n in range(1200):
        spec = random_spec(n)
        assert (pricing.validate_spec(spec) == []) == ref_valid(spec), n
        if not ref_valid(spec):
            continue
        valid += 1
        won = set()
        day = spec.valid_from
        while day <= spec.valid_to:
            for m in range(0, MIDNIGHT, 30):
                expected = ref_resolve(spec, day, m)
                assert len(expected) == 1                       # never ambiguous
                found = pricing.resolve(spec, day, m)
                if expected[0] is None:
                    assert found.source.rule is None
                else:
                    assert found.source.rule is expected[0]
                    won.add(expected[0].key)
            segments = pricing.day_schedule(spec, day, merge="price")
            assert segments[0].start == 0 and segments[-1].end == MIDNIGHT
            assert all(a.end == b.start and a.price != b.price for a, b in zip(segments, segments[1:], strict=False))
            for m in range(0, MIDNIGHT, 15):
                at = next(s for s in segments if s.start <= m < s.end)
                assert at.price == pricing.resolve(spec, day, m).price
            day += datetime.timedelta(days=1)
        hidden = {r.key for r in spec.rules if r.kind != SINGLE_DATE} \
            | {r.key for s in spec.seasons for r in s.rules}
        assert set(pricing.never_applies(spec)) == hidden - won, n
    assert valid > 300
