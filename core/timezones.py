"""Time, per company.

Everything is stored in UTC. A company carries its own timezone as an IANA name,
and that is the only place a timezone is decided (design/00-findings.md §7).

Two jobs live here so neither is written twice:

* which day a moment counts as, for a company (`business_date_for`);
* which UTC range a day covers, for a query (`business_day_range`).

Never convert a stored timestamp inside SQL to compare it -- that discards the
index. Convert the boundaries once, here, and compare against stored UTC.
"""

import datetime
import zoneinfo

from django.utils import timezone

DEFAULT_TIMEZONE = "Asia/Dubai"


def zone_for(company):
    """The company's timezone, or the default when it has none."""
    name = getattr(company, "timezone", None) or DEFAULT_TIMEZONE
    try:
        return zoneinfo.ZoneInfo(name)
    except zoneinfo.ZoneInfoNotFoundError:
        return zoneinfo.ZoneInfo(DEFAULT_TIMEZONE)


def business_date_for(company, moment=None):
    """Which day `moment` counts as for this company.

    Written once, when the record is created, and never recomputed: if a
    company's timezone is corrected later, past records keep the day they were
    actually transacted on (design/00-findings.md §8).
    """
    moment = moment or timezone.now()
    if timezone.is_naive(moment):
        moment = timezone.make_aware(moment, datetime.UTC)
    return moment.astimezone(zone_for(company)).date()


def utc_offset_string(zone, moment=None):
    """`+04:00`-style UTC offset for `zone` at `moment` (default: now)."""
    moment = moment or timezone.now()
    offset = moment.astimezone(zone).utcoffset()
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    hh, mm = divmod(abs(total_minutes), 60)
    return f"{sign}{hh:02d}:{mm:02d}"


def business_day_range(company, day):
    """The UTC window covering one company-local day, as (start, end).

    `end` is exclusive, so a query is `punched_at__gte=start, punched_at__lt=end`
    and midnight belongs to exactly one day.
    """
    zone = zone_for(company)
    start_local = datetime.datetime.combine(day, datetime.time.min, tzinfo=zone)
    end_local = start_local + datetime.timedelta(days=1)
    return start_local.astimezone(datetime.UTC), end_local.astimezone(datetime.UTC)
