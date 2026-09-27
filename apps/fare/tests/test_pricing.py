"""apps.fare.pricing -- the precedence, checked two ways.

Fixed cases walk the example fare from the prototype (design/fares and offers/
fare-prototype.html), renumbered to Monday=0. Then a seeded brute force
compares every date of hundreds of random fares, at every minute a price can
change, against an independent reference: nested-or-apart validity, innermost
wins, and a check
that the covering windows really do form a chain.
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


def test_two_every_day_prices_cannot_partly_overlap():
    spec = with_rules(rule("a", EVERY_DAY, "16:00", "20:00", 70), rule("b", EVERY_DAY, "18:00", "22:00", 80))
    assert messages(spec) == [("b", "Partly overlaps Every day · 16:00 – 20:00. Two every day prices can sit "
                                    "one inside the other, or apart, but cannot partly overlap.")]


def test_identical_windows_are_a_tie_and_refused():
    spec = with_rules(rule("a", EVERY_DAY, "16:00", "20:00", 70), rule("b", EVERY_DAY, "16:00", "20:00", 80))
    assert messages(spec) == [("b", "Same time as Every day · 16:00 – 20:00. Two every day prices cannot "
                                    "cover exactly the same time.")]


def test_a_window_inside_another_is_allowed_and_the_innermost_wins():
    spec = with_rules(rule("day", EVERY_DAY, "08:00", "20:00", 70), rule("lunch", EVERY_DAY, "12:00", "14:00", 90))
    assert messages(spec) == []
    at = {t: pricing.resolve(spec, D(2026, 3, 4), hm(t)).source.rule.key for t in ("09:00", "12:00", "13:59", "14:00")}
    assert at == {"09:00": "day", "12:00": "lunch", "13:59": "lunch", "14:00": "day"}
    also = pricing.resolve(spec, D(2026, 3, 4), hm("13:00")).also
    assert [(s.rule.key, reason) for s, reason in also] == [("day", "a narrower window inside it applies")]


# The examples discussed with the client: A 06-10, B 07-09 inside A.
def test_the_worked_examples():
    a, b = rule("A", EVERY_DAY, "06:00", "10:00", 1), rule("B", EVERY_DAY, "07:00", "09:00", 2)
    assert messages(with_rules(a, b)) == []
    # C 08-10: inside A, but crosses B (07-09) -> refused.
    assert [k for k, _ in messages(with_rules(a, b, rule("C", EVERY_DAY, "08:00", "10:00", 3)))] == ["C"]
    # D 07:30-08:30 inside B inside A, and E 05-20 around all of them -> fine.
    d, e = rule("D", EVERY_DAY, "07:30", "08:30", 4), rule("E", EVERY_DAY, "05:00", "20:00", 5)
    spec = with_rules(a, b, d, e)
    assert messages(spec) == []
    who = {t: pricing.resolve(spec, D(2026, 3, 4), hm(t)).source.rule.key
           for t in ("05:00", "06:30", "07:15", "08:00", "08:45", "09:30", "12:00")}
    assert who == {"05:00": "E", "06:30": "A", "07:15": "B", "08:00": "D", "08:45": "B", "09:30": "A", "12:00": "E"}
    # 06-10 on Sat,Sun and 08-12 on Sun only: crossing on the shared Sunday.
    assert messages(with_rules(rule("x", SELECTED_DAYS, "06:00", "10:00", 1, days=(SAT, SUN)),
                               rule("y", SELECTED_DAYS, "08:00", "12:00", 1, days=(SUN,))))[0][0] == "y"
    # The same windows on days that never meet: fine.
    assert messages(with_rules(rule("x", SELECTED_DAYS, "06:00", "10:00", 1, days=(SAT,)),
                               rule("y", SELECTED_DAYS, "08:00", "12:00", 1, days=(SUN,)))) == []
    # Two single dates on one day nest the same way.
    on = D(2026, 12, 2)
    nd = with_rules(rule("n1", SINGLE_DATE, "10:00", "22:00", 100, on=on),
                    rule("n2", SINGLE_DATE, "18:00", "20:00", 150, on=on))
    assert messages(nd) == [] and pricing.resolve(nd, on, hm("19:00")).source.rule.key == "n2"


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


def test_seasons_cannot_partly_overlap_or_leave_the_validity():
    a = Season("s1", "Spring", D(2026, 4, 1), D(2026, 4, 30), price(55))
    b = Season("s2", "Ramadan", D(2026, 4, 30), D(2026, 5, 20), price(60))
    c = Season("s3", "Winter", D(2026, 12, 1), D(2027, 1, 31), price(60))
    d = Season("s4", "Spring again", D(2026, 4, 1), D(2026, 4, 30), price(56))
    found = messages(with_rules(seasons=(a, b, c, d)))
    assert ("s2", "Partly overlaps Spring. A season can sit inside another, or apart, "
                  "but cannot partly overlap it.") in found
    assert ("s4", "Same dates as Spring. Two seasons cannot cover exactly the same dates.") in found
    assert any(k == "s3" and "inside the fare validity" in m for k, m in found)


def test_a_season_inside_a_season_uses_its_own_prices():
    summer = Season("su", "Summer", D(2026, 2, 1), D(2026, 9, 30), price(60),
                    (rule("su1", EVERY_DAY, "16:00", "20:00", 75),))
    eid = Season("eid", "Eid", D(2026, 4, 10), D(2026, 5, 15), price(90))
    spec = with_rules(rule("f1", EVERY_DAY, "16:00", "20:00", 70), seasons=(summer, eid))
    assert messages(spec) == []
    assert pricing.resolve(spec, D(2026, 3, 1), hm("17:00")).source.rule.key == "su1"
    inside = pricing.resolve(spec, D(2026, 4, 20), hm("17:00"))
    assert (inside.price.base_fare, inside.source.season.key, inside.source.rule) == (90, "eid", None)
    assert pricing.resolve(spec, D(2026, 5, 16), hm("17:00")).source.rule.key == "su1"
    assert pricing.resolve(spec, D(2026, 10, 1), hm("17:00")).source.rule.key == "f1"
    assert pricing.never_applies(spec) == {}
    # A season rule whose days all fall in the inner season never applies.
    short = Season("su", "Summer", D(2026, 4, 1), D(2026, 5, 31), price(60),
                   (rule("sat", SELECTED_DAYS, "10:00", "12:00", 70, days=(SAT,)),))
    inner = Season("x", "May", D(2026, 4, 4), D(2026, 5, 31), price(60))   # 4 Apr is the first Saturday
    assert pricing.never_applies(with_rules(seasons=(short, inner))) == {
        "sat": "Every day it could apply is inside a narrower season, which uses its own prices.",
    }


def test_money_and_intervals_are_checked_everywhere():
    bad = Price(Decimal("-1"), 5, 0, Decimal("1"), 0)
    spec = FareSpec(D(2026, 1, 1), D(2026, 12, 31), 0, bad, (), ())
    assert {m for _, m in messages(spec)} >= {
        "Package time must be 1 to 1440 minutes.", "Basic fare cannot be negative.",
        "Concurrent interval must be 1 to 1440 minutes.",
    }
    long = FareSpec(D(2026, 1, 1), D(2026, 12, 31), 1441, Price(Decimal("1"), 1441, 1441, Decimal("1"), 0), (), ())
    assert {m for _, m in messages(long)} == {
        "Package time must be 1 to 1440 minutes.", "Concurrent interval must be 1 to 1440 minutes.",
        "Grace periods must be 0 to 1440 minutes.",
    }


# -- Never applies ------------------------------------------------------------------


def test_a_season_over_the_whole_fare_hides_the_fares_own_rules():
    whole = Season("s", "All year", D(2026, 1, 1), D(2026, 12, 31), price(60))
    spec = with_rules(rule("a", EVERY_DAY, "16:00", "20:00", 70), seasons=(whole,))
    assert pricing.never_applies(spec) == {
        "a": "Every day it could apply is inside a season, which uses its own prices.",
    }


def test_a_window_filled_by_narrower_ones_never_applies():
    spec = with_rules(rule("out", EVERY_DAY, "08:00", "12:00", 70),
                      rule("in1", EVERY_DAY, "08:00", "10:00", 71), rule("in2", EVERY_DAY, "10:00", "12:00", 72))
    assert list(pricing.never_applies(spec)) == ["out"]
    on = D(2026, 12, 2)
    singles = with_rules(rule("d1", SINGLE_DATE, "10:00", "12:00", 1, on=on),
                         rule("d2", SINGLE_DATE, "10:00", "11:00", 2, on=on),
                         rule("d3", SINGLE_DATE, "11:00", "12:00", 3, on=on))
    assert list(pricing.never_applies(singles)) == ["d1"]


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


def ref_nested_or_apart(a0, a1, b0, b1, closed=False):
    """Spans [a0, a1) and [b0, b1) -- or [a0, a1] with closed -- are apart, or
    one strictly inside the other. Identical is not allowed."""
    apart = (a1 < b0 or b1 < a0) if closed else (a1 <= b0 or b1 <= a0)
    if apart:
        return True
    if (a0, a1) == (b0, b1):
        return False
    return (a0 <= b0 and b1 <= a1) or (b0 <= a0 and a1 <= b1)


def ref_valid(spec):
    def list_ok(rules):
        for i, a in enumerate(rules):
            if not a.start < a.end:
                return False
            if a.kind == SELECTED_DAYS and not a.weekdays:
                return False
            for b in rules[i + 1:]:
                if a.kind != b.kind:
                    continue
                shared = a.kind == EVERY_DAY or (a.kind == SELECTED_DAYS and a.weekdays & b.weekdays) \
                    or (a.kind == SINGLE_DATE and a.on_date == b.on_date)
                if shared and not ref_nested_or_apart(a.start, a.end, b.start, b.end):
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
            if not ref_nested_or_apart(s.start, s.end, o.start, o.end, closed=True):
                return False
    return True


def ref_innermost(found, span):
    """Every candidate with the smallest span. Also checks the chain the
    validation promises: each covering candidate lies inside the next."""
    if not found:
        return []
    found = sorted(found, key=lambda x: span(x)[1] - span(x)[0])
    for inner, outer in zip(found, found[1:], strict=False):
        (i0, i1), (o0, o1) = span(inner), span(outer)
        assert o0 <= i0 and i1 <= o1 and (i0, i1) != (o0, o1), "covering windows must form a chain"
    shortest = span(found[0])[1] - span(found[0])[0]
    return [x for x in found if span(x)[1] - span(x)[0] == shortest]


def ref_resolve(spec, d, m):
    window = lambda r: (r.start, r.end)                                  # noqa: E731
    singles = [x for x in spec.rules if x.kind == SINGLE_DATE and x.on_date == d and x.start <= m < x.end]
    if singles:
        return ref_innermost(singles, window)
    ss = ref_innermost([s for s in spec.seasons if s.start <= d <= s.end],
                       lambda s: (s.start.toordinal(), s.end.toordinal()))
    rules = ss[0].rules if ss else spec.rules
    days = [r for r in rules if r.kind == SELECTED_DAYS and d.weekday() in r.weekdays and r.start <= m < r.end]
    if days:
        return ref_innermost(days, window)
    every = [r for r in rules if r.kind == EVERY_DAY and r.start <= m < r.end]
    return ref_innermost(every, window) or [None]


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
    for i in range(random.randint(0, 3)):
        s = v0 + datetime.timedelta(days=random.randint(0, span))
        e = min(v1, s + datetime.timedelta(days=random.randint(0, 20)))
        seasons.append(Season(f"s{n}-{i}", f"S{i}", s, e, price(random.randint(1, 99)),
                              tuple(random_rule(f"s{n}-{i}-{j}") for j in range(random.randint(0, 3)))))
    return FareSpec(v0, v1, 30, price(50), tuple(rules), tuple(seasons))


def edges(spec):
    """Every minute where a price can change: midnight and each window's start
    and end. The price is constant from one edge to the next, so checking the
    edges checks every minute of the day."""
    windows = [*spec.rules, *(r for s in spec.seasons for r in s.rules)]
    return sorted({0} | {m for w in windows for m in (w.start, w.end) if m < MIDNIGHT})


def test_brute_force_matches_the_reference():
    random.seed(7)
    valid = nested = 0
    for n in range(1200):
        spec = random_spec(n)
        assert (pricing.validate_spec(spec) == []) == ref_valid(spec), n
        if not ref_valid(spec):
            continue
        valid += 1
        won = set()
        minutes = edges(spec)
        day = spec.valid_from
        while day <= spec.valid_to:
            for m in minutes:
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
            for m in minutes:
                at = next(s for s in segments if s.start <= m < s.end)
                assert at.price == pricing.resolve(spec, day, m).price
            day += datetime.timedelta(days=1)
        every = {r.key for r in spec.rules} | {r.key for s in spec.seasons for r in s.rules}
        assert set(pricing.never_applies(spec)) == every - won, n
        nested += any(a.kind == b.kind == EVERY_DAY and a.start < b.end and b.start < a.end
                      for rules in [spec.rules, *(s.rules for s in spec.seasons)]
                      for i, a in enumerate(rules) for b in rules[i + 1:])
    assert valid > 300 and nested > 100            # plenty of fares really do nest
