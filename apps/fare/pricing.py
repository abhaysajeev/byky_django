"""Fare pricing: the one place the precedence lives.

Pure Python, no ORM. It works on a FareSpec built either from saved rows
(services.spec_from_fare) or from a form that has not been saved yet
(payload.parse), so the screen's live checks, the Test fare dialog and, later,
the device's daily timetable all run the same code.

Which price applies at a date and minute -- fixed, never set by a user
(design/fares and offers/fare-schema.md section 1.1):

  1. a single date (always on the fare, never in a season) whose window holds
     the minute wins;
  2. otherwise one price list is used: the season covering the date, else the
     fare itself;
  3. in that list: a selected-days rule, then an every-day rule, then the
     list's own base price.

validate_spec -- mirrored by the database's constraints -- guarantees at most
one match at every step, so every minute of a valid fare has exactly one price.
Brute-force checked against design/fares and offers/fare_algorithm_check.py.

Times are minutes from midnight: a window is [start, end), and end 1440 is
midnight. Weekdays are company.WeekDay: Python's numbering, Monday is 0.
"""

import datetime
from dataclasses import dataclass
from decimal import Decimal

from apps.company.models import UAE_WEEK, WeekDay

MIDNIGHT = 1440
ONE_DAY = datetime.timedelta(days=1)

EVERY_DAY, SELECTED_DAYS, SINGLE_DATE = "every_day", "selected_days", "single_date"
BASE = "base"
KIND_LABELS = {EVERY_DAY: "Every day", SELECTED_DAYS: "Selected days", SINGLE_DATE: "Single date"}
# Within one price list the more specific kind wins.
LIST_ORDER = (SELECTED_DAYS, EVERY_DAY)

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
SHORT_DAYS = {day: day.label[:3] for day in WeekDay}


# -- The data -------------------------------------------------------------------


@dataclass(frozen=True)
class Price:
    """The five numbers that price a rental -- identical on the fare, a season
    and a rule, so the device runs one calculation whichever it lands on."""

    base_fare: Decimal
    grace_minutes: int
    concurrent_interval_minutes: int
    concurrent_fare: Decimal
    concurrent_grace_minutes: int


@dataclass(frozen=True)
class Rule:
    """A special price. `key` names it in messages and answers: "r12" for a
    saved row, anything else (e.g. "n3") for one the browser has just made."""

    key: str
    kind: str
    start: int
    end: int
    price: Price
    weekdays: frozenset = frozenset()
    on_date: datetime.date | None = None

    def applies_on(self, day):
        if self.kind == EVERY_DAY:
            return True
        if self.kind == SELECTED_DAYS:
            return day.weekday() in self.weekdays
        return self.on_date == day

    def holds(self, minute):
        return self.start <= minute < self.end


@dataclass(frozen=True)
class Season:
    key: str
    name: str
    start: datetime.date
    end: datetime.date
    base: Price
    rules: tuple = ()

    def covers(self, day):
        return self.start <= day <= self.end


@dataclass(frozen=True)
class FareSpec:
    valid_from: datetime.date
    valid_to: datetime.date
    package_minutes: int
    base: Price
    rules: tuple = ()          # the fare's own list, single dates included
    seasons: tuple = ()

    def season_on(self, day):
        for season in self.seasons:
            if season.covers(day):
                return season
        return None

    def singles_on(self, day):
        return [r for r in self.rules if r.kind == SINGLE_DATE and r.on_date == day]


@dataclass(frozen=True)
class Source:
    """Where a price came from: the tier, the rule (if any) and the season (if
    any). A base price has no rule; the fare's base has neither."""

    tier: str
    rule: Rule | None = None
    season: Season | None = None


@dataclass(frozen=True)
class Resolution:
    price: Price
    source: Source
    also: tuple = ()           # (Source, reason) -- matched, but not used


@dataclass(frozen=True)
class Segment:
    start: int
    end: int
    price: Price
    source: Source


@dataclass(frozen=True)
class Issue:
    """One problem, in the shape the message modal lists: `field` leads the
    line, `key` says which row it belongs to ("" for the fare itself)."""

    key: str
    field: str
    message: str

    def as_dict(self):
        return {"key": self.key, "field": self.field, "message": self.message}


# -- Words ------------------------------------------------------------------------


def hhmm(minute):
    return "24:00" if minute >= MIDNIGHT else f"{minute // 60:02d}:{minute % 60:02d}"


def date_label(day):
    return f"{day.day} {MONTHS[day.month - 1]} {day.year}"


def window_label(rule):
    if rule.start == 0 and rule.end == MIDNIGHT:
        return "All day"
    return f"{hhmm(rule.start)} – {hhmm(rule.end)}"


def days_label(weekdays):
    """Listed in the UAE week's order, Sunday first -- the order screens use."""
    return ", ".join(SHORT_DAYS[day] for day in UAE_WEEK if day in weekdays)


def when_label(rule):
    if rule.kind == EVERY_DAY:
        return "Every day"
    if rule.kind == SELECTED_DAYS:
        return days_label(rule.weekdays)
    return date_label(rule.on_date) if rule.on_date else "Single date"


def rule_label(rule):
    return f"{when_label(rule)} · {window_label(rule)}"


def source_label(source):
    if source.rule is None:
        return f"{source.season.name} season · base fare" if source.season else "Base fare"
    rule = source.rule
    # "Every day · 16:00 – 20:00" already names its kind; the others do not.
    text = rule_label(rule) if rule.kind == EVERY_DAY else f"{KIND_LABELS[rule.kind]} · {rule_label(rule)}"
    return f"{source.season.name} season · {text}" if source.season else text


# -- Validation --------------------------------------------------------------------


def _price_issues(price, key, where):
    issues = []
    if price.base_fare < 0:
        issues.append(Issue(key, where, "Basic fare cannot be negative."))
    if price.concurrent_fare < 0:
        issues.append(Issue(key, where, "Concurrent fare cannot be negative."))
    if price.concurrent_interval_minutes < 1:
        issues.append(Issue(key, where, "Concurrent interval must be at least 1 minute."))
    if price.grace_minutes < 0 or price.concurrent_grace_minutes < 0:
        issues.append(Issue(key, where, "Grace periods cannot be negative."))
    return issues


def _overlaps(a, b):
    """Two rules of one kind in one list that can meet at the same minute."""
    if a.kind != b.kind or not (a.start < b.end and b.start < a.end):
        return False
    if a.kind == EVERY_DAY:
        return True
    if a.kind == SELECTED_DAYS:
        return bool(a.weekdays & b.weekdays)
    return a.on_date == b.on_date


def _list_issues(spec, rules, season):
    issues = []
    owner = f"{season.name} · " if season else ""
    for index, rule in enumerate(rules):
        where = f"{owner}{rule_label(rule)}"
        if not (0 <= rule.start < MIDNIGHT and 0 < rule.end <= MIDNIGHT) or rule.end <= rule.start:
            issues.append(Issue(rule.key, where,
                                '"To" must be later than "From". Use 00:00 in "To" for midnight; '
                                "a price past midnight needs a second rule on the next day."))
        if rule.kind == SELECTED_DAYS and not rule.weekdays:
            issues.append(Issue(rule.key, where, "Select at least one day."))
        if rule.kind == SINGLE_DATE:
            if season is not None:
                issues.append(Issue(rule.key, where,
                                    "Single dates are added on the fare, not inside a season. "
                                    "They apply inside seasons too."))
            elif rule.on_date is None:
                issues.append(Issue(rule.key, where, "Pick the date."))
            elif not spec.valid_from <= rule.on_date <= spec.valid_to:
                issues.append(Issue(rule.key, where,
                                    f"The date must be inside the fare validity "
                                    f"({date_label(spec.valid_from)} – {date_label(spec.valid_to)})."))
        issues.extend(_price_issues(rule.price, rule.key, where))
        for other in rules[index + 1:]:
            if _overlaps(rule, other):
                issues.append(Issue(
                    other.key, f"{owner}{rule_label(other)}",
                    f"Overlaps {rule_label(rule)}. Two {KIND_LABELS[rule.kind].lower()} "
                    "prices cannot cover the same time.",
                ))
    return issues


def validate_spec(spec):
    """Every problem at once. Mirrors the database's CHECK and EXCLUDE
    constraints, plus the checks that span tables (dates inside the validity)."""
    issues = []
    if spec.valid_to < spec.valid_from:
        issues.append(Issue("", "Valid to", "The fare ends before it starts."))
    if spec.package_minutes < 1:
        issues.append(Issue("", "Package time", "Package time must be at least 1 minute."))
    issues.extend(_price_issues(spec.base, "", "Base fare"))
    issues.extend(_list_issues(spec, spec.rules, None))

    for index, season in enumerate(spec.seasons):
        name = season.name.strip() or "Season"
        if not season.name.strip():
            issues.append(Issue(season.key, name, "Name the season."))
        if season.end < season.start:
            issues.append(Issue(season.key, name, "The season ends before it starts."))
        elif not (spec.valid_from <= season.start and season.end <= spec.valid_to):
            issues.append(Issue(season.key, name,
                                f"The season must be inside the fare validity "
                                f"({date_label(spec.valid_from)} – {date_label(spec.valid_to)})."))
        issues.extend(_price_issues(season.base, season.key, f"{name} · base fare"))
        issues.extend(_list_issues(spec, season.rules, season))
        for other in spec.seasons[index + 1:]:
            if season.start <= other.end and other.start <= season.end:
                issues.append(Issue(other.key, other.name or "Season",
                                    f"Shares dates with {name}. A date can belong to one season only."))
    return issues


# -- Which price applies ---------------------------------------------------------------


def resolve(spec, day, minute):
    """The price at `day` / `minute`, where it came from, and what else matched
    but was not used. None outside the validity: no fare that day."""
    if not spec.valid_from <= day <= spec.valid_to:
        return None
    season = spec.season_on(day)
    found = []   # (Source, used, reason if not used)

    for rule in spec.singles_on(day):
        if rule.holds(minute):
            found.append(Source(SINGLE_DATE, rule))

    lists = [(season, season.rules)] if season else []
    lists.append((None, spec.rules))
    for owner, rules in lists:
        for kind in LIST_ORDER:
            for rule in rules:
                if rule.kind == kind and rule.applies_on(day) and rule.holds(minute):
                    found.append(Source(kind, rule, owner))

    active = season  # the list in force on this date
    winner = None
    for source in found:
        if source.tier == SINGLE_DATE or source.season is active:
            winner = source
            break
    also = []
    for source in found:
        if source is winner:
            continue
        if source.tier != SINGLE_DATE and source.season is not active:
            also.append((source, f"not used inside {season.name}"))
        else:
            also.append((source, "a more specific price applies"))

    if winner is None:
        base = season.base if season else spec.base
        winner = Source(BASE, None, season)
        return Resolution(base, winner, tuple(also))
    return Resolution(winner.rule.price, winner, tuple(also))


def day_schedule(spec, day, merge="source"):
    """The whole of `day` as back-to-back segments from 00:00 to 24:00 --
    what the device will download, and what the Test fare timeline draws.

    merge="source" joins neighbours only when the same rule priced them (the
    screen wants to show where each block came from); merge="price" joins
    neighbours whose five numbers are equal (the device only needs prices).
    Empty outside the validity.
    """
    if not spec.valid_from <= day <= spec.valid_to:
        return []
    season = spec.season_on(day)
    edges = {0, MIDNIGHT}
    candidates = list(spec.singles_on(day)) + [
        r for r in (season.rules if season else spec.rules) if r.kind != SINGLE_DATE and r.applies_on(day)
    ]
    for rule in candidates:
        edges.update((rule.start, rule.end))
    points = sorted(e for e in edges if 0 <= e <= MIDNIGHT)

    segments = []
    for start, end in zip(points, points[1:], strict=False):
        found = resolve(spec, day, start)
        last = segments[-1] if segments else None
        same = last and (last.price == found.price if merge == "price" else last.source == found.source)
        if same:
            segments[-1] = Segment(last.start, end, last.price, last.source)
        else:
            segments.append(Segment(start, end, found.price, found.source))
    return segments


# -- Rules that can never win --------------------------------------------------------------


def _subtract(free, cut_start, cut_end):
    out = []
    for start, end in free:
        if cut_end <= start or cut_start >= end:
            out.append((start, end))
            continue
        if start < cut_start:
            out.append((start, cut_start))
        if cut_end < end:
            out.append((cut_end, end))
    return out


def _stretches(spec):
    """The validity cut into runs of dates that share one season (or none)."""
    seasons = sorted(
        (s for s in spec.seasons if s.end >= spec.valid_from and s.start <= spec.valid_to),
        key=lambda s: s.start,
    )
    runs, cursor = [], spec.valid_from
    for season in seasons:
        start, end = max(season.start, spec.valid_from), min(season.end, spec.valid_to)
        if start > cursor:
            runs.append((cursor, start - ONE_DAY, None))
        if end >= max(start, cursor):
            runs.append((max(start, cursor), end, season))
        cursor = max(cursor, end + ONE_DAY)
    if cursor <= spec.valid_to:
        runs.append((cursor, spec.valid_to, None))
    return runs


def _day_classes(spec):
    """Every distinct kind of date in the validity, each standing for at least
    one real date: (season or None, weekday, single-date windows that day).

    Whether a rule can win on a date depends only on those three, so checking
    each class once is the same as checking every date -- and stays small
    however long the validity runs.
    """
    specials = {r.on_date for r in spec.rules
                if r.kind == SINGLE_DATE and r.on_date and spec.valid_from <= r.on_date <= spec.valid_to}
    classes = set()
    for day in specials:
        windows = tuple(sorted((r.start, r.end) for r in spec.singles_on(day)))
        classes.add((spec.season_on(day), day.weekday(), windows))
    for start, end, season in _stretches(spec):
        weekdays, day = set(), start
        while day <= end and len(weekdays) < 7:
            if day not in specials:
                weekdays.add(day.weekday())
            day += ONE_DAY
        classes.update((season, weekday, ()) for weekday in weekdays)
    return classes


def never_applies(spec):
    """{rule key: reason} for every rule that wins at no minute of any date.
    Saved all the same -- the client may be preparing it on purpose -- but it
    will never be charged, and the screen says so. Single dates always win in
    their window, so they never appear here."""
    classes = _day_classes(spec)
    owners = [(None, spec.rules)] + [(s, s.rules) for s in spec.seasons]
    reasons = {}
    for owner, rules in owners:
        for rule in rules:
            if rule.kind == SINGLE_DATE:
                continue
            applicable = in_list = 0
            wins = False
            for season, weekday, windows in classes:
                if rule.kind == SELECTED_DAYS and weekday not in rule.weekdays:
                    continue
                applicable += 1
                if season is not owner:
                    continue
                in_list += 1
                free = [(rule.start, rule.end)]
                for cut in windows:
                    free = _subtract(free, *cut)
                if rule.kind == EVERY_DAY:
                    for other in rules:
                        if other.kind == SELECTED_DAYS and weekday in other.weekdays:
                            free = _subtract(free, other.start, other.end)
                if free:
                    wins = True
                    break
            if wins:
                continue
            if owner is None and applicable and not in_list:
                reasons[rule.key] = "Every day it could apply is inside a season, which uses its own prices."
            elif not in_list:
                reasons[rule.key] = ("None of its days fall inside this season." if owner
                                     else "None of its days fall inside the fare validity.")
            else:
                reasons[rule.key] = "Always covered by a more specific price (a single date or a selected-days rule)."
    return reasons


def single_date_notes(spec):
    """{rule key: note} for single dates that fall inside a season -- they
    still win there, which is worth saying next to the row."""
    notes = {}
    for rule in spec.rules:
        if rule.kind == SINGLE_DATE and rule.on_date:
            season = spec.season_on(rule.on_date)
            if season:
                notes[rule.key] = f"Inside {season.name}. Takes priority there."
    return notes
