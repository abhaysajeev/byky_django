"""Brute force: the device fare download, read back exactly as the app will.

Each round builds a random fare setup at one station and asks for the fares of
every date around it, with and without filters:

- company fares and station fares changing over on random days, with gaps;
  station fares for other stations only, overlapping ours on the same dates;
- nested windows (12-14 inside 08-20), nested seasons (Eid inside Summer),
  single dates inside seasons, selected days over every day;
- fares the station must never get: inactive, not approved, another company's,
  a vehicle type that is inactive, not approved, or in an inactive category.

Then, for every request:

1. exact fares -- the answer holds exactly the fares an independent filter over
   the generated setup expects: no more, no fewer;
2. shape and order -- no empty groups; the station's fares before the
   company's; seasons shortest first; special prices by kind, shortest window
   first; the same fares, unchanged, whatever the filters;
3. prices -- for the request date and the day after, at midnight and at every
   window start and end (the only minutes a price can change), the app's
   reading (apps/fare/api.py: first match at every level) gives the same fare,
   season, special price and five numbers as pricing.resolve on the fare the
   rules pick: the station's, else the company's.

The generator checks nesting with its own code, and pricing.validate_spec must
agree with it. Counters at the end prove every tricky case really occurred.
"""

import datetime
import random

import pytest
from collections import Counter
from decimal import Decimal

from apps.company.models import Branch, BranchType
from apps.fare import services
from apps.fare.models import Fare, FareLevel, RuleKind
from apps.fare.pricing import FareSpec, Price, Rule, Season, resolve, validate_spec
from apps.fare.tests import test_api
from apps.fare.tests.conftest import make_fare, make_rule, make_season
from apps.fleet.models import Brand, Category, VehicleType
from core.enums import ApprovalStatus

# The signed-in till from the HTTP tests, reused as fixtures here.
URL, call = test_api.URL, test_api.call
world, token, fresh_throttle = test_api.world, test_api.token, test_api.fresh_throttle

D = datetime.date
DAY = datetime.timedelta(days=1)
APPROVED, PENDING = ApprovalStatus.APPROVED, ApprovalStatus.PENDING
SPAN_START = D(2028, 2, 10)                 # the span crosses 29 Feb 2028
SPAN_END = SPAN_START + 39 * DAY
ROUNDS = 25
EVERY, DAYS, SINGLE = RuleKind.EVERY_DAY, RuleKind.SELECTED_DAYS, RuleKind.SINGLE_DATE
KIND_RANK = {SINGLE: 0, DAYS: 1, EVERY: 2}


# -- The app's reading, written from the API description ------------------------------------


def minute_of(hhmm):
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


def holds(entry, minute):
    return minute_of(entry["start"]) <= minute < minute_of(entry["end"])


def app_price(answer, vehicle_type_id, package, day, minute):
    """(fare_id, season_id, special_price_id, price) the app would charge, or None."""
    fares = []
    for vt in answer["vehicle_types"]:
        if vt["vehicle_type_id"] == vehicle_type_id:
            for pkg in vt["packages"]:
                if pkg["package_minutes"] == package:
                    fares = pkg["fares"]
    iso = day.isoformat()
    fare = next((f for f in fares if f["valid_from"] <= iso <= f["valid_to"]), None)       # 1
    if fare is None:
        return None
    for sp in fare["special_prices"]:                                                        # 2
        if sp["kind"] == SINGLE and sp["on_date"] == iso and holds(sp, minute):
            return fare["fare_id"], None, sp["id"], sp["price"]
    season = next((s for s in fare["seasons"] if s["start_date"] <= iso <= s["end_date"]), None)   # 3
    owner = season or fare
    for sp in owner["special_prices"]:                                                       # 4
        if sp["kind"] == SINGLE:
            continue
        if sp["kind"] == DAYS and day.weekday() not in sp["weekdays"]:
            continue
        if holds(sp, minute):
            return fare["fare_id"], season and season["id"], sp["id"], sp["price"]
    return fare["fare_id"], season and season["id"], None, owner["price"]


# -- The generator -----------------------------------------------------------------------------


def random_price(rng):
    return {"base_fare": Decimal(f"{rng.randint(1, 99)}.00"), "grace_minutes": rng.choice([0, 5, 10]),
            "concurrent_interval_minutes": rng.choice([5, 10, 15]),
            "concurrent_fare": Decimal(f"{rng.randint(1, 30)}.00"), "concurrent_grace_minutes": rng.choice([0, 2])}


def nested_or_apart(a0, a1, b0, b1):
    """[a0, a1) and [b0, b1): apart, or one strictly inside the other."""
    if a1 <= b0 or b1 <= a0:
        return True
    return (a0, a1) != (b0, b1) and ((a0 <= b0 and b1 <= a1) or (b0 <= a0 and a1 <= b1))


def rule_fits(new, rules):
    for r in rules:
        if r["kind"] != new["kind"]:
            continue
        shared = (new["kind"] == EVERY or (new["kind"] == DAYS and set(r["weekdays"]) & set(new["weekdays"]))
                  or (new["kind"] == SINGLE and r["on_date"] == new["on_date"]))
        if shared and not nested_or_apart(r["start"], r["end"], new["start"], new["end"]):
            return False
    return True


def random_window(rng, inside=None):
    lo, hi = inside if inside else (0, 1440)
    if hi - lo < 60:
        return None
    start = rng.randrange(lo, hi - 30, 30)
    end = rng.randrange(start + 30, hi + 1, 30)
    return None if inside and (start, end) == inside else (start, end)


def random_rules(rng, kinds, count, dates=None):
    rules = []
    for _ in range(count):
        kind = rng.choice(kinds)
        same = [r for r in rules if r["kind"] == kind]
        parent = rng.choice(same) if same and rng.random() < 0.5 else None      # try to nest
        window = random_window(rng, (parent["start"], parent["end"]) if parent else None)
        if window is None:
            continue
        new = {"kind": kind, "start": window[0], "end": window[1], "weekdays": [], "on_date": None,
               "price": random_price(rng)}
        if kind == DAYS:
            new["weekdays"] = (sorted(set(parent["weekdays"]) | {rng.randrange(7)}) if parent
                               else sorted(rng.sample(range(7), rng.randint(1, 4))))
        if kind == SINGLE:
            new["on_date"] = parent["on_date"] if parent else rng.choice(dates)
        if rule_fits(new, rules):
            rules.append(new)
    return rules


def random_seasons(rng, valid_from, valid_to):
    seasons, length = [], (valid_to - valid_from).days
    for index in range(rng.randint(0, 3)):
        parent = rng.choice(seasons) if seasons and rng.random() < 0.5 else None
        lo, hi = (parent["start"], parent["end"]) if parent else (valid_from, valid_to)
        span = (hi - lo).days
        start = lo + rng.randint(0, span) * DAY
        end = min(hi, start + rng.randint(0, max(0, min(span, length))) * DAY)
        if any(not (end < s["start"] or s["end"] < start
                    or ((s["start"], s["end"]) != (start, end)
                        and ((s["start"] <= start and end <= s["end"]) or (start <= s["start"] and s["end"] <= end))))
               for s in seasons):
            continue
        seasons.append({"name": f"S{index}", "start": start, "end": end, "price": random_price(rng),
                        "rules": random_rules(rng, [EVERY, DAYS], rng.randint(0, 3))})
    return seasons


def spec_of(fare, seasons, rules):
    """The FareSpec of a saved setup, built from the generator's own data."""
    def rule(r):
        return Rule(f"r{r['pk']}", r["kind"], r["start"], r["end"], Price(**r["price"]),
                    frozenset(r["weekdays"]), r["on_date"])
    return FareSpec(fare["valid_from"], fare["valid_to"], fare["package"], Price(**fare["price"]),
                    tuple(rule(r) for r in rules),
                    tuple(Season(f"s{s['pk']}", s["name"], s["start"], s["end"], Price(**s["price"]),
                                 tuple(rule(r) for r in s["rules"])) for s in seasons))


def save(world, rng, *, vehicle_type, package, level, branches, valid_from, valid_to,
         is_active=True, approval=APPROVED, company=None):
    """Create one random fare in the database and return its record."""
    price = random_price(rng)
    dates = [valid_from + i * DAY for i in range((valid_to - valid_from).days + 1)]
    rules = random_rules(rng, [EVERY, DAYS, SINGLE], rng.randint(0, 6), dates)
    seasons = random_seasons(rng, valid_from, valid_to)
    fare = make_fare(world, company=company or world["company"], vehicle_type=vehicle_type,
                     package_minutes=package, valid_from=valid_from, valid_to=valid_to, is_active=is_active,
                     approval_status=approval, branches=branches if level == FareLevel.BRANCH else (),
                     **price)
    for s in seasons:
        row = make_season(fare, s["start"], s["end"], name=s["name"], **s["price"])
        s["pk"] = row.pk
        for r in s["rules"]:
            r["pk"] = make_rule(fare, r["kind"], r["start"], r["end"], season=row,
                                weekdays=r["weekdays"], **r["price"]).pk
    for r in rules:
        r["pk"] = make_rule(fare, r["kind"], r["start"], r["end"], weekdays=r["weekdays"],
                            on_date=r["on_date"], **r["price"]).pk
    record = {"pk": fare.pk, "vehicle_type": vehicle_type.pk, "package": package, "level": level,
              "branches": {b.pk for b in branches}, "valid_from": valid_from, "valid_to": valid_to,
              "active": is_active, "approved": approval == APPROVED,
              "company": (company or world["company"]).pk, "price": price}
    record["spec"] = spec_of({"valid_from": valid_from, "valid_to": valid_to, "package": package,
                              "price": price}, seasons, rules)
    assert validate_spec(record["spec"]) == [], "the generator and pricing disagree on what is valid"
    return record


def partition(rng):
    """Random back-to-back date ranges, with gaps, covering the span and a little around it."""
    cursor, ranges = SPAN_START - rng.randint(0, 4) * DAY, []
    while cursor <= SPAN_END + 3 * DAY:
        cursor += rng.choice([0, 0, 0, 1, 3]) * DAY
        end = cursor + rng.randint(0, 12) * DAY
        ranges.append((cursor, end))
        cursor = end + DAY
    return ranges


def build(world, rng, types):
    here, there, family = world["adc1"], world["adc2"], world["family"]
    records = []
    for vehicle_type, packages in types["usable"]:
        for package in packages:
            # Company fares: one live fare per date at most; some not approved,
            # some inactive, some dates with none.
            for start, end in partition(rng):
                roll = rng.random()
                if roll < 0.12:
                    continue
                records.append(save(world, rng, vehicle_type=vehicle_type, package=package,
                                    level=FareLevel.COMPANY, branches=(), valid_from=start, valid_to=end,
                                    is_active=roll > 0.22, approval=PENDING if 0.22 < roll < 0.3 else APPROVED))
            # Station fares for this station (sometimes shared with Family Park) ...
            for start, end in partition(rng):
                if rng.random() < 0.45:
                    continue
                shared = [here, family] if rng.random() < 0.4 else [here]
                records.append(save(world, rng, vehicle_type=vehicle_type, package=package,
                                    level=FareLevel.BRANCH, branches=shared, valid_from=start, valid_to=end,
                                    approval=PENDING if rng.random() < 0.08 else APPROVED))
            # ... and for another station only, on overlapping dates: never ours.
            for start, end in partition(rng):
                if rng.random() < 0.5:
                    records.append(save(world, rng, vehicle_type=vehicle_type, package=package,
                                        level=FareLevel.BRANCH, branches=[there], valid_from=start, valid_to=end))
            # An inactive company fare over everything: inactive fares may overlap.
            records.append(save(world, rng, vehicle_type=vehicle_type, package=package, level=FareLevel.COMPANY,
                                branches=(), valid_from=SPAN_START, valid_to=SPAN_END, is_active=False))
    # Fares of vehicle types the station cannot use, and another company's.
    for vehicle_type in types["unusable"]:
        records.append(save(world, rng, vehicle_type=vehicle_type, package=30, level=FareLevel.COMPANY,
                            branches=(), valid_from=SPAN_START - 5 * DAY, valid_to=SPAN_END + 5 * DAY))
    records.append(save(world, rng, vehicle_type=world["their_type"], package=30, level=FareLevel.COMPANY,
                        branches=(), valid_from=SPAN_START - 5 * DAY, valid_to=SPAN_END + 5 * DAY,
                        company=world["other"]))
    return records


def vehicle_types(world):
    company = world["company"]
    brand = Brand.objects.get(company=company)
    category = Category.objects.get(company=company)
    retired_category = Category.objects.create(company=company, category_code="OLD", category_name="Old",
                                               is_active=False, approval_status=APPROVED)

    def make(name, **fields):
        values = {"company": company, "category": category, "brand": brand, "vehicle_type_name": name,
                  "vehicle_type_code": name[:3].upper(), "approval_status": APPROVED, **fields}
        return VehicleType.objects.create(**values)

    return {
        "usable": [(world["monaco"], [30, 60]), (world["berg"], [30, 45])],
        "unusable": [make("Pending", approval_status=PENDING), make("Retired", is_active=False),
                     make("Oldie", category=retired_category)],
    }


# -- The expectations -------------------------------------------------------------------------


def expected_ids(records, usable_ids, here, day, vehicle_type_id=None, package=None):
    return {
        r["pk"] for r in records
        if r["active"] and r["approved"] and r["vehicle_type"] in usable_ids and r["company"] == here.company_id
        and (r["level"] == FareLevel.COMPANY or here.pk in r["branches"])
        and r["valid_from"] <= day + DAY and r["valid_to"] >= day
        and (vehicle_type_id is None or r["vehicle_type"] == vehicle_type_id)
        and (package is None or r["package"] == package)
    }


def rule_pk(key):
    return int(key[1:]) if key else None


def expected_fare(records, usable_ids, here, vehicle_type_id, package, day, seen):
    """The fare the rules pick for a day: this station's, else the company's."""
    live = [r for r in records
            if r["active"] and r["approved"] and r["vehicle_type"] == vehicle_type_id and r["package"] == package
            and r["vehicle_type"] in usable_ids and r["company"] == here.company_id
            and r["valid_from"] <= day <= r["valid_to"]]
    own = [r for r in live if r["level"] == FareLevel.BRANCH and here.pk in r["branches"]]
    company = [r for r in live if r["level"] == FareLevel.COMPANY]
    assert len(own) <= 1 and len(company) <= 1, "the database let two live fares overlap"
    if own and company:
        seen["station fare over company fare"] += 1
    if not (own or company):
        seen["no fare that day"] += 1
    return (own or company or [None])[0]


def expected_price(fare, day, minute, seen):
    """pricing.resolve on that fare, in the app's terms."""
    if fare is None:
        return None
    found = resolve(fare["spec"], day, minute)
    source = found.source
    if any(reason == "a narrower window inside it applies" for _, reason in found.also):
        seen["narrower window inside another won"] += 1
    if sum(x.covers(day) for x in fare["spec"].seasons) >= 2:
        seen["date inside nested seasons"] += 1
    if source.tier == SINGLE and fare["spec"].season_on(day):
        seen["single date inside a season"] += 1
    if source.tier == DAYS and any(x.rule and x.rule.kind == EVERY for x, _ in found.also):
        seen["selected days over every day"] += 1
    price = found.price
    return fare["pk"], rule_pk(source.season.key if source.season else ""), \
        rule_pk(source.rule.key if source.rule else ""), {
            "base_fare": str(price.base_fare), "grace_minutes": price.grace_minutes,
            "concurrent_interval_minutes": price.concurrent_interval_minutes,
            "concurrent_fare": str(price.concurrent_fare), "concurrent_grace_minutes": price.concurrent_grace_minutes,
        }


def check_order(answer, levels):
    for vt in answer["vehicle_types"]:
        assert vt["packages"], "an empty vehicle type"
        for pkg in vt["packages"]:
            assert pkg["fares"], "an empty package"
            ranks = [0 if levels[f["fare_id"]] == FareLevel.BRANCH else 1 for f in pkg["fares"]]
            assert ranks == sorted(ranks), "a company fare before the station's"
            for fare in pkg["fares"]:
                lengths = [(D.fromisoformat(s["end_date"]) - D.fromisoformat(s["start_date"])).days
                           for s in fare["seasons"]]
                assert lengths == sorted(lengths), "seasons not shortest first"
                for owner in [fare, *fare["seasons"]]:
                    keys = [(KIND_RANK[r["kind"]], minute_of(r["end"]) - minute_of(r["start"]))
                            for r in owner["special_prices"]]
                    assert keys == sorted(keys), "special prices out of order"


def fares_in(answer):
    return {f["fare_id"]: f for vt in answer["vehicle_types"] for p in vt["packages"] for f in p["fares"]}


# -- The test ---------------------------------------------------------------------------------


@pytest.mark.exhaustive
def test_every_request_gets_exactly_the_right_fares_and_prices(client, world, token):
    world["family"] = Branch.objects.create(company=world["company"], location=world["adc1"].location,
                                            short_code="FAM", name="Family Park", branch_type=BranchType.STATION)
    types = vehicle_types(world)
    usable_ids = {vt.pk for vt, _ in types["usable"]}
    combos = [(vt.pk, package) for vt, packages in types["usable"] for package in packages]
    here = world["adc1"]
    seen = Counter()

    for round_no in range(ROUNDS):
        rng = random.Random(round_no)
        Fare.objects.all().delete()
        records = build(world, rng, types)
        levels = {r["pk"]: r["level"] for r in records}
        # A price can only change where a window starts or ends: checking
        # those minutes (and midnight) checks every minute of the day, and
        # catches a start or end read the wrong side of the boundary.
        edges = {}
        for r in records:
            windows = [*r["spec"].rules, *(x for s in r["spec"].seasons for x in s.rules)]
            edges.setdefault((r["vehicle_type"], r["package"]), {0}).update(
                m for w in windows for m in (w.start, w.end) if m < 1440)

        day = SPAN_START - 2 * DAY
        while day <= SPAN_END + 2 * DAY:
            answer = services.device_fares(here, day)
            assert answer["date"] == day.isoformat()
            got = fares_in(answer)
            # 1. exactly the right fares
            assert set(got) == expected_ids(records, usable_ids, here, day), (round_no, day)
            if any(D.fromisoformat(f["valid_from"]) == day + DAY for f in got.values()):
                seen["fare that starts the next day sent"] += 1
            # 2. shape and order
            check_order(answer, levels)
            # 2b. filters narrow, never change (every fourth date: the
            # filters only drop whole groups, so this is plenty)
            vt_id, package = rng.choice(combos)
            filter_sets = ({"vehicle_type_id": vt_id}, {"package_minutes": package},
                           {"vehicle_type_id": vt_id, "package_minutes": package})
            for filters in filter_sets if (day - SPAN_START).days % 4 == 0 else ():
                narrowed = services.device_fares(here, day, **filters)
                part = fares_in(narrowed)
                assert set(part) == expected_ids(records, usable_ids, here, day,
                                                 filters.get("vehicle_type_id"), filters.get("package_minutes"))
                assert all(part[pk] == got[pk] for pk in part)
                check_order(narrowed, levels)
            # 3. prices: the app's reading equals pricing.resolve, today and after midnight
            for combo in combos:
                minutes = sorted(edges.get(combo, {0}))
                for when in (day, day + DAY):
                    fare = expected_fare(records, usable_ids, here, *combo, when, seen)
                    for minute in minutes:
                        app = app_price(answer, *combo, when, minute)
                        assert app == expected_price(fare, when, minute, seen), (round_no, day, combo, when, minute)
                        seen["prices compared"] += 1
            day += DAY

        # The HTTP answer is the service's answer, as JSON.
        probe = SPAN_START + rng.randint(0, 39) * DAY
        body = call(client, URL, {"date": probe.isoformat()}, token=token).json()
        assert body["code"] == "ok" and body["data"] == services.device_fares(here, probe)

    # Every tricky case really happened, many times over.
    for case in ("station fare over company fare", "no fare that day", "narrower window inside another won",
                 "date inside nested seasons", "single date inside a season", "selected days over every day",
                 "fare that starts the next day sent"):
        assert seen[case] >= 20, (case, seen)
    assert seen["prices compared"] > 40_000, seen
    print(dict(seen))
