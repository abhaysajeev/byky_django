"""Company rules that more than one caller needs.

Working time is read by the Branch Working Time screen and by the app update
check, which offers an "Any time" release only while the tablet's branch is
open (design/03-login.md section 9A.3a). Keeping the rule here is what stops
the two ever disagreeing about when a branch is open.

The legacy stored one window per branch per day (RmsWorkingHours, minutes from
midnight) and never crossed midnight -- 0 of its 560 rows do. "Open all day"
was 0-1439, which is 00:00-23:59 here.

A shift may run past midnight (owner, 26 Nov 2026): an end earlier than its
start ends the next day. It must be the day's last shift and end by the
company's business-day start (CompanyConfig.report_day_start, 04:00 at BYKY),
so attendance auto-close at the day start never cuts a shift still running.
"""

import datetime
import zoneinfo

from django.utils import timezone

from apps.company.models import UAE_WEEK, BranchWorkingTime, PaymentMode, WeekDay
from core.enums import ApprovalStatus

OPEN, CLOSED, NOT_SET = "open", "closed", "not_set"

MAX_SHIFTS = 4
SHIFTS = range(1, MAX_SHIFTS + 1)


def branch_details(branch):
    """`branch`'s own details, for the operator app -- station name/address/
    contact for the checkout screen and receipt header. Authenticated only
    (apps/company/api.py::BranchDetailsView): the branch always comes from
    the signed-in session, matching apps/devices/api.py::RegistrationView's
    own rule that a branch is never revealed before login."""
    return {
        "id": branch.pk,
        "code": branch.short_code,
        "name": branch.name,
        "branch_type": branch.branch_type,
        "is_hotel": branch.is_hotel,
        "accepts_app_payment": branch.accepts_app_payment,
        "address": branch.address,
        "latitude": branch.latitude,
        "longitude": branch.longitude,
        "contact_no": branch.contact_no,
    }


def _money_text(value):
    return f"{value:.2f}" if value is not None else None


def company_details(company, now=None):
    """Every field of `company` the web's Company screen holds, except the
    logo, for the operator app (owner, 13 Nov 2026). The company is the one
    the signed-in tablet is registered to (apps/company/api.py::
    CompanyDetailsView), never a value the app sends. Empty text stays "",
    an unset date stays None, so the shape never changes."""
    from core.timezones import utc_offset_string, zone_for

    zone = zone_for(company)
    changed = company.modified_on or company.created_on
    return {
        "id": company.pk,
        "code": company.short_code,
        "name": company.name,
        "timezone": company.timezone,
        "utc_offset": utc_offset_string(zone, now),
        "ceo_name": company.ceo_name,
        "dto_name": company.dto_name,
        "date_of_commissioning": company.date_of_commissioning.isoformat() if company.date_of_commissioning else None,
        "address": {
            "address": company.address,
            "city": company.city,
            "state_id": company.state_id,
            "state": company.state.name,
            "country_id": company.country_id,
            "country": company.country.name,
            "zip_code": company.zip_code,
        },
        "registration": {
            "incorporation_certificate_number": company.incorporation_certificate_number,
            "business_certificate_number": company.business_certificate_number,
            "income_tax_number": company.income_tax_number,
            "tin": company.tin,
            "cst": company.cst,
            "service_tax_number": company.service_tax_number,
        },
        "tax": {
            "tax_percentage": _money_text(company.tax_percentage),
            "tax_type": company.tax_type,
            "discount_type": company.discount_type,
        },
        "contact": {
            "contact_person": company.contact_person,
            "phone_number": company.phone_number,
            "fax_number": company.fax_number,
            "email": company.email,
            "website": company.website,
        },
        # moves whenever the company is saved on the web
        "version": changed.astimezone(zone).strftime("%Y-%m-%d %H:%M:%S") if changed else None,
    }


def device_payment_modes(company):
    """This company's active, approved payment modes, for the operator app's
    checkout screen. Legacy SfaPaymentMode: only 2 of its 5 rows are
    IsActive -- Cash On Delivery, Cheque and Creditor exist switched off, not
    deleted (apps/company/models.py::PaymentMode)."""
    return list(
        PaymentMode.objects
        .filter(company=company, is_active=True, approval_status=ApprovalStatus.APPROVED)
        .order_by("name")
        .values("id", "name")
    )


def local_now(company, at=None):
    """`at` (default now) as a wall-clock time in the company's timezone."""
    at = at or timezone.now()
    try:
        zone = zoneinfo.ZoneInfo(company.timezone)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        zone = zoneinfo.ZoneInfo("Asia/Dubai")
    return at.astimezone(zone)


def week_day_of(moment):
    """WeekDay value for a date or datetime -- Python's own, Monday is 0."""
    return moment.weekday()


def previous_week_day(day):
    """The WeekDay value before `day` (Monday's is Sunday)."""
    return (day - 1) % 7


def ends_next_day(start, end):
    """True for a shift that runs past midnight -- its end is before its start."""
    return end < start


def shift_covers(week_day, start, end, day, minute):
    """Whether a working-time shift (`week_day`, `start`, `end`) covers
    `minute` on weekday `day`. Start and end minutes both count. A shift past
    midnight covers its own day from `start` and the next day up to `end`."""
    if not ends_next_day(start, end):
        return week_day == day and start <= minute <= end
    return ((week_day == day and minute >= start)
            or (week_day == previous_week_day(day) and minute <= end))


def branch_open_state(branch, at=None):
    """OPEN, CLOSED or NOT_SET for `branch` at `at` (default now).

    Open means the company-local time falls inside one of the shifts,
    start and end minutes both included -- so 00:00-23:59 is the whole day;
    yesterday's shift past midnight counts until its end. A day with no shift
    is closed. A branch with no working time at all is NOT_SET: nobody has
    said when it opens, which is not the same as closed.
    """
    rows = list(
        BranchWorkingTime.objects
        .filter(branch_id=branch.pk, is_active=True)
        .values_list("week_day", "start_time", "end_time")
    )
    if not rows:
        return NOT_SET

    now = local_now(branch.company, at)
    day = week_day_of(now)
    minute = now.time().replace(second=0, microsecond=0)
    for week_day, start, end in rows:
        if shift_covers(week_day, start, end, day, minute):
            return OPEN
    return CLOSED


def schedule_for(branch):
    """The branch's saved shifts, as the screen sends and receives them."""
    return [
        {
            "week_day": row.week_day,
            "shift_number": row.shift_number,
            "start": row.start_time.strftime("%H:%M"),
            "end": row.end_time.strftime("%H:%M"),
            "ends_next_day": ends_next_day(row.start_time, row.end_time),
        }
        for row in BranchWorkingTime.objects.filter(branch=branch, is_active=True)
        .order_by("week_day", "shift_number")
    ]


def _time(text):
    try:
        return datetime.datetime.strptime(text, "%H:%M").time()
    except (TypeError, ValueError):
        return None


def validate_schedule(shifts, day_start=datetime.time.min):
    """(rows, errors) for a posted week.

    `rows` are dicts of week_day, shift_number, start_time, end_time -- only the
    shifts actually worked. `errors` are {"field", "message"} pairs naming the
    day and shift, e.g. "Monday shift 2". `day_start` is the company's
    business-day start (core.timezones.day_start_for). The rules:

      - a shift has both times or neither (neither = not worked)
      - start and end differ; an end before the start runs past midnight
      - a shift past midnight is the day's last and ends by `day_start`
        (owner, 26 Nov 2026) -- no day start saved means midnight, so none can
      - shifts in a day are consecutive: shift 2 needs shift 1
      - each shift starts after the previous one ends, and a day's first
        shift after the previous day's shift past midnight ends
    """
    if not isinstance(shifts, list):
        return [], [{"field": "Schedule", "message": "Send the week as a list of shifts."}]

    errors = []
    by_day = {}
    for shift in shifts:
        if not isinstance(shift, dict):
            errors.append({"field": "Schedule", "message": "A shift could not be read."})
            continue
        try:
            day = WeekDay(int(shift.get("week_day")))
            number = int(shift.get("shift_number"))
        except (TypeError, ValueError):
            errors.append({"field": "Schedule", "message": "A shift has an unknown day or number."})
            continue
        if number not in SHIFTS:
            errors.append({"field": "Schedule", "message": "A shift has an unknown day or number."})
            continue

        label = f"{day.label} shift {number}"
        start_text = str(shift.get("start") or "").strip()
        end_text = str(shift.get("end") or "").strip()
        if not start_text and not end_text:
            continue                                  # not worked
        if not start_text or not end_text:
            errors.append({"field": label, "message": "Enter both a start and an end time."})
            continue
        start, end = _time(start_text), _time(end_text)
        if start is None or end is None:
            errors.append({"field": label, "message": "Use a 24-hour time such as 08:30."})
            continue
        if end == start:
            errors.append({
                "field": label,
                "message": "Start and end cannot be the same; a full day is 00:00 to 23:59.",
            })
            continue
        if ends_next_day(start, end) and end > day_start:
            errors.append({
                "field": label,
                "message": f"A shift past midnight must end by {day_start.strftime('%H:%M')}.",
            })
            continue
        if number in by_day.setdefault(day, {}):
            errors.append({"field": label, "message": "This shift is entered twice."})
            continue
        by_day[day][number] = (start, end)

    rows = []
    for day in sorted(by_day, key=UAE_WEEK.index):       # errors read in screen order
        shifts_of_day = by_day[day]
        overnight = [n for n, (s, e) in shifts_of_day.items() if ends_next_day(s, e)]
        if overnight and min(overnight) < max(shifts_of_day):
            errors.append({
                "field": f"{day.label} shift {min(overnight)}",
                "message": "Only the last shift of a day can run past midnight.",
            })
        # Yesterday's shift past midnight: today's first shift starts after it ends.
        yesterday = WeekDay(previous_week_day(day))
        carried = [(n, e) for n, (s, e) in by_day.get(yesterday, {}).items() if ends_next_day(s, e)]
        if carried and 1 in shifts_of_day:
            carried_number, carried_end = carried[0]
            if shifts_of_day[1][0] <= carried_end:
                errors.append({
                    "field": f"{day.label} shift 1",
                    "message": f"Must start after {yesterday.label} shift {carried_number} ends "
                               f"({carried_end.strftime('%H:%M')}).",
                })
        previous_end = None
        for number in SHIFTS:
            if number not in shifts_of_day:
                later = [n for n in shifts_of_day if n > number]
                if later:
                    errors.append({
                        "field": f"{day.label} shift {min(later)}",
                        "message": f"Fill shift {number} first; shifts are added in order.",
                    })
                break
            start, end = shifts_of_day[number]
            if previous_end is not None and start <= previous_end:
                errors.append({
                    "field": f"{day.label} shift {number}",
                    "message": f"Must start after shift {number - 1} ends "
                               f"({previous_end.strftime('%H:%M')}).",
                })
            previous_end = end
            rows.append({
                "week_day": day.value, "shift_number": number,
                "start_time": start, "end_time": end,
            })

    return rows, errors
