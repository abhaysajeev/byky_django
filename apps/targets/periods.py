"""The periods a target profile makes, and how a period's amount is planned
over its days (design/11-target-profiles.md §4).

  daily     one period per day of the range
  monthly   one per calendar month the range touches (the range is whole months)
  custom    the range itself, once
  annual    one per financial year the range touches (whole financial years);
            the year's amount is shared by month weights, then evenly per day

The financial year starts in CompanyConfig.financial_year_start (a month).
"""

import calendar
import datetime
from decimal import ROUND_DOWN, Decimal

from apps.targets.models import PeriodType

ZERO = Decimal("0")
HUNDRED = Decimal("100")
ONE_DAY = datetime.timedelta(days=1)


# -- Calendar helpers ---------------------------------------------------------------------


def month_bounds(year, month):
    """(first, last) day of a calendar month."""
    return datetime.date(year, month, 1), datetime.date(year, month, calendar.monthrange(year, month)[1])


def add_months(day, months):
    """The first of the month `months` after `day`'s month."""
    index = day.year * 12 + day.month - 1 + months
    return datetime.date(index // 12, index % 12 + 1, 1)


def days_of(first, last):
    return [first + datetime.timedelta(days=n) for n in range((last - first).days + 1)]


def fy_start_month(company):
    """The month (1-12) the company's financial year starts in; January when unset."""
    from apps.company.models import CompanyConfig

    config = CompanyConfig.objects.filter(company=company).only("financial_year_start").first()
    return config.financial_year_start if config else 1


def financial_year(day, start_month):
    """(first, last) day of the financial year containing `day`."""
    year = day.year if day.month >= start_month else day.year - 1
    first = datetime.date(year, start_month, 1)
    return first, add_months(first, 12) - ONE_DAY


def fy_label(first, last):
    """'FY 2027' for a calendar year, 'FY 2027–28' when it spans two."""
    if first.year == last.year:
        return f"FY {first.year}"
    return f"FY {first.year}–{last.year % 100:02d}"


# -- A profile's range and periods --------------------------------------------------------


def normalise_range(period_type, starts_on, ends_on, start_month=1):
    """The range a profile actually covers: whole months for Monthly, whole
    financial years for Annual; Daily and Custom as given."""
    if period_type == PeriodType.MONTHLY:
        return starts_on.replace(day=1), month_bounds(ends_on.year, ends_on.month)[1]
    if period_type == PeriodType.ANNUAL:
        return financial_year(starts_on, start_month)[0], financial_year(ends_on, start_month)[1]
    return starts_on, ends_on


def periods(period_type, starts_on, ends_on, start_month=1):
    """[(start, end)] the profile's targets cover, in order."""
    first, last = normalise_range(period_type, starts_on, ends_on, start_month)
    if period_type == PeriodType.DAILY:
        return [(d, d) for d in days_of(first, last)]
    if period_type == PeriodType.CUSTOM:
        return [(first, last)]
    out, cursor = [], first
    step = 12 if period_type == PeriodType.ANNUAL else 1
    while cursor <= last:
        nxt = add_months(cursor, step)
        out.append((cursor, nxt - ONE_DAY))
        cursor = nxt
    return out


def profile_periods(profile):
    return periods(profile.period_type, profile.starts_on, profile.ends_on, fy_start_month(profile.company))


def period_containing(profile, day):
    """The (start, end) of the profile's period that contains `day`, or None."""
    for start, end in profile_periods(profile):
        if start <= day <= end:
            return start, end
    return None


def period_label(period_type, start, end):
    if period_type == PeriodType.DAILY or start == end:
        return f"{start.day} {start:%b %Y}"
    if period_type == PeriodType.MONTHLY:
        return f"{start:%B %Y}"
    if period_type == PeriodType.ANNUAL:
        return f"{fy_label(start, end)} ({start:%b %Y} – {end:%b %Y})"
    if start.year == end.year and start.month == end.month:
        return f"{start.day}–{end.day} {start:%b %Y}"
    if start.year == end.year:
        return f"{start.day} {start:%b} – {end.day} {end:%b %Y}"
    return f"{start.day} {start:%b %Y} – {end.day} {end:%b %Y}"


def target_label(target):
    return period_label(target.period_type, target.period_start, target.period_end)


def range_label(profile):
    """The whole profile, for lists: '5 Dec 2026', 'Oct – Dec 2026', 'FY 2027'."""
    first, last = profile.starts_on, profile.ends_on
    if profile.period_type == PeriodType.MONTHLY:
        if (first.year, first.month) == (last.year, last.month):
            return f"{first:%B %Y}"
        return f"{first:%b %Y} – {last:%b %Y}" if first.year != last.year else f"{first:%b} – {last:%b %Y}"
    if profile.period_type == PeriodType.ANNUAL:
        fy = financial_year(first, first.month)
        if last <= fy[1]:
            return fy_label(first, last)
        return f"{fy_label(*fy)} – {fy_label(*financial_year(last, first.month))}"
    return period_label(PeriodType.CUSTOM, first, last)


# -- Planning a period over its days ------------------------------------------------------


def _step(places):
    return Decimal(1).scaleb(-places)


def split_even(amount, days, places):
    """{day: amount}: `amount` over `days` evenly, rounded down to the money
    places, the remainder on the last day -- so the days add up exactly."""
    if not days:
        return {}
    amount = max(amount, ZERO)
    each = (amount / len(days)).quantize(_step(places), rounding=ROUND_DOWN)
    split = dict.fromkeys(days, each)
    split[days[-1]] = amount - each * (len(days) - 1)
    return split


def months_of(first, last):
    """[(month_first, month_last)] of the calendar months in first..last, clipped."""
    out, cursor = [], first.replace(day=1)
    while cursor <= last:
        end = add_months(cursor, 1) - ONE_DAY
        out.append((max(cursor, first), min(end, last)))
        cursor = add_months(cursor, 1)
    return out


def default_weights(fy_first):
    """{month_index: weight%} proportional to the days in each month of the
    financial year starting `fy_first`; rounded to 3 places, totalling 100."""
    months = months_of(fy_first, add_months(fy_first, 12) - ONE_DAY)
    total = sum((end - start).days + 1 for start, end in months)
    out, used = {}, ZERO
    for index, (start, end) in enumerate(months, start=1):
        share = (Decimal((end - start).days + 1) * HUNDRED / total).quantize(Decimal("0.001"), rounding=ROUND_DOWN)
        out[index] = share
        used += share
    out[12] += HUNDRED - used
    return out


def weights_valid(weights):
    """12 weights, none negative, totalling exactly 100."""
    return (sorted(weights) == list(range(1, 13)) and all(w >= 0 for w in weights.values())
            and sum(weights.values(), ZERO) == HUNDRED)


def plan(period_type, amount, start, end, places, weights=None):
    """{day: planned amount} for one period; the days add up to `amount` exactly.

    Annual: amount x weight per month (the remainder on the last month), then
    evenly within each month (the remainder on its last day)."""
    if period_type != PeriodType.ANNUAL:
        return split_even(amount, days_of(start, end), places)
    weights = weights or default_weights(start)
    months = months_of(start, end)
    step = _step(places)
    shares, used = [], ZERO
    for index, _ in enumerate(months, start=1):
        share = (amount * weights.get(index, ZERO) / HUNDRED).quantize(step, rounding=ROUND_DOWN)
        shares.append(share)
        used += share
    shares[-1] += amount - used
    out = {}
    for (first, last), share in zip(months, shares, strict=True):
        out.update(split_even(share, days_of(first, last), places))
    return out
