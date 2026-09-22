"""Company rules that more than one caller needs.

Working time is read by the Branch Working Time screen and by the app update
check, which offers an "Any time" release only while the tablet's branch is
open (design/03-login.md section 9A.3a). Keeping the rule here is what stops
the two ever disagreeing about when a branch is open.

The legacy stored one window per branch per day (RmsWorkingHours, minutes from
midnight) and never crossed midnight -- 0 of its 560 rows do. "Open all day"
was 0-1439, which is 00:00-23:59 here.
"""

import datetime
import zoneinfo

from django.utils import timezone

from apps.company.models import BranchWorkingTime, WeekDay

OPEN, CLOSED, NOT_SET = "open", "closed", "not_set"

MAX_SHIFTS = 4
SHIFTS = range(1, MAX_SHIFTS + 1)


def local_now(company, at=None):
    """`at` (default now) as a wall-clock time in the company's timezone."""
    at = at or timezone.now()
    try:
        zone = zoneinfo.ZoneInfo(company.timezone)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        zone = zoneinfo.ZoneInfo("Asia/Dubai")
    return at.astimezone(zone)


def week_day_of(moment):
    """WeekDay value for a datetime: Sunday is 0 here, Monday is 0 in Python."""
    return moment.isoweekday() % 7


def branch_open_state(branch, at=None):
    """OPEN, CLOSED or NOT_SET for `branch` at `at` (default now).

    Open means the company-local time falls inside one of that day's shifts,
    start and end minutes both included -- so 00:00-23:59 is the whole day.
    A day with no shift is closed. A branch with no working time at all is
    NOT_SET: nobody has said when it opens, which is not the same as closed.
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
        if week_day == day and start <= minute <= end:
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
        }
        for row in BranchWorkingTime.objects.filter(branch=branch, is_active=True)
        .order_by("week_day", "shift_number")
    ]


def _time(text):
    try:
        return datetime.datetime.strptime(text, "%H:%M").time()
    except (TypeError, ValueError):
        return None


def validate_schedule(shifts):
    """(rows, errors) for a posted week.

    `rows` are dicts of week_day, shift_number, start_time, end_time -- only the
    shifts actually worked. `errors` are {"field", "message"} pairs naming the
    day and shift, e.g. "Monday shift 2". The rules:

      - a shift has both times or neither (neither = not worked)
      - end is after start; a shift never crosses midnight
      - shifts in a day are consecutive: shift 2 needs shift 1
      - each shift starts after the previous one ends
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
        if end <= start:
            errors.append({
                "field": label,
                "message": "End must be after start. A shift cannot run past midnight; "
                           "a full day is 00:00 to 23:59.",
            })
            continue
        if number in by_day.setdefault(day, {}):
            errors.append({"field": label, "message": "This shift is entered twice."})
            continue
        by_day[day][number] = (start, end)

    rows = []
    for day in sorted(by_day):
        shifts_of_day = by_day[day]
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
