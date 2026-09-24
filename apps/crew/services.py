"""Crew business rules.

Three things happen here rather than in a view, because each has to keep two or
more tables agreeing with each other.
"""

import datetime

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.crew.models import (
    AddressType,
    Attendance,
    BlockAction,
    Employee,
    EmployeeAddress,
    EmployeeBlockLog,
    EmployeeDesignation,
)

# A document inside this many days counts as needing attention.
EXPIRY_WARNING_DAYS = 30

DOCUMENTS = [
    ("visa", "Visa", "visa_expiry"),
    ("eid", "Emirates ID", "emirates_id_expiry"),
    ("labor", "Labour Card", "labour_card_expiry"),
    ("passport", "Passport", "passport_expiry"),
]


@transaction.atomic
def set_designation(employee, designation, on_date=None, branch=None, by=None):
    """Move someone to a designation, keeping the history straight.

    Closes the open posting, opens a new one, and updates the employee's current
    designation -- in one function, so the history can never disagree with the
    column every list screen reads.

    Does nothing when the designation has not changed, so saving an unrelated
    edit does not litter the history with identical rows.
    """
    on_date = on_date or timezone.localdate()
    current = employee.postings.filter(to_date__isnull=True).first()

    if current and current.designation_id == designation.pk:
        return current

    if current:
        # A posting that starts and ends on the same day is a correction, not a
        # period worked: replace it rather than leaving a zero-length row.
        if current.from_date == on_date:
            current.delete()
        else:
            current.to_date = on_date
            current.save(update_fields=["to_date", "modified_on"])

    posting = EmployeeDesignation.objects.create(
        employee=employee,
        designation=designation,
        branch=branch or employee.branch,
        from_date=on_date,
        created_by=by,
    )
    Employee.objects.filter(pk=employee.pk).update(designation=designation)
    employee.designation = designation
    return posting


@transaction.atomic
def set_blocked(employee, blocked, reason="", effective_date=None, remarks="", by=None):
    """Block or unblock someone.

    Writes the log, flips the flag, and -- when blocking -- ends every session
    they have open. design/03-login.md section 4 requires that: without it a
    blocked cashier keeps working until their next sign-in.
    """
    from apps.portal.auth import close_sessions_for_user
    from apps.portal.session_models import LogoutReason

    effective_date = effective_date or timezone.localdate()

    log = EmployeeBlockLog.objects.create(
        employee=employee,
        action=BlockAction.BLOCK if blocked else BlockAction.UNBLOCK,
        reason=reason,
        effective_date=effective_date,
        remarks=remarks,
        created_by=by,
    )

    employee.is_blocked = blocked
    employee.save(update_fields=["is_blocked", "modified_on"])

    if blocked:
        user = getattr(employee, "user", None)
        if user is not None:
            close_sessions_for_user(user, LogoutReason.EMPLOYEE_BLOCKED)

    return log


def save_address(employee, address_type, fields, by=None):
    """One current address per type: a new UAE address replaces the old one."""
    EmployeeAddress.objects.filter(
        employee=employee, address_type=address_type, is_current=True
    ).update(is_current=False)
    return EmployeeAddress.objects.create(
        employee=employee, address_type=address_type, created_by=by, **fields
    )


def document_expiry(employees, today=None):
    """How many staff have each document expired or expiring soon.

    Computed from the real expiry dates. The wireframe hashed the employee code
    into a bucket so its filters had something to do -- honest in a wireframe,
    a lie here.
    """
    today = today or timezone.localdate()
    soon = today + datetime.timedelta(days=EXPIRY_WARNING_DAYS)

    summary = []
    for key, label, field in DOCUMENTS:
        expired = attention = 0
        for employee in employees:
            detail = getattr(employee, "detail", None)
            expiry = getattr(detail, field, None) if detail else None
            if expiry is None:
                continue
            if expiry < today:
                expired += 1
            elif expiry <= soon:
                attention += 1
        summary.append({
            "key": key, "label": label,
            "expired": expired, "nearing": attention,
            "total": expired + attention,
        })
    return summary


def expiry_state(expiry, today=None):
    """'expired', 'nearing' or '' for one date -- used per row."""
    if expiry is None:
        return ""
    today = today or timezone.localdate()
    if expiry < today:
        return "expired"
    if expiry <= today + datetime.timedelta(days=EXPIRY_WARNING_DAYS):
        return "nearing"
    return ""


def record_punch(employee, source, punched_at, company, **extra):
    """Write one attendance punch.

    Not reachable from the web -- punches come from the apps -- but the rule for
    business_date lives here so there is one implementation when they do.
    """
    from core.timezones import business_date_for

    return Attendance.objects.create(
        employee=employee,
        source=source,
        punched_at=punched_at,
        business_date=business_date_for(company, punched_at),
        **extra,
    )


ADDRESS_TYPES = AddressType


# -- Duty Roster (design/duty roster/duty-roster.md) --------------------------
#
# One employee's plan for one day: where, and on which shift. Employees are
# not branch-mapped (client decision, 20 Sep 2026) -- every roster day picks
# its own branch, because Cashier and Labour staff move between branches as
# staffing needs it. Shift times are never typed from nothing: they are read
# from the branch's own working time (branch_shift_defaults), the same table
# the Branch Working Time screen writes.
#
# Two features read against DutyRoster are deliberately not built here, by the
# client's own scoping for the Oct 2 demo -- see today_assignment's docstring
# and absent_today below.

from apps.company.models import Branch, BranchWorkingTime, WeekDay
from apps.company.services import week_day_of
from core.timezones import business_date_for, zone_for
from apps.crew.models import DutyRoster, DutyRosterDayType, RosterCategory
from core.scoping import scoped_to


class Invalid(Exception):
    """Every problem with a posted form at once, as {field, message} pairs, so
    a drawer or modal can list them together instead of one per save."""

    def __init__(self, errors):
        super().__init__("; ".join(e["message"] for e in errors))
        self.errors = errors


def roster_eligible_employees(user, *, search=""):
    """Employees the duty roster's pickers may offer: a Cashier or Labour
    designation, this user's own company. Every other designation is not a
    roster concept -- it never appears here, by client decision."""
    rows = scoped_to(
        Employee.objects.filter(designation__roster_category__in=RosterCategory.values)
        .select_related("designation"),
        user,
    )
    search = (search or "").strip()
    if search:
        rows = rows.filter(
            Q(first_name__icontains=search) | Q(last_name__icontains=search)
            | Q(employee_code__icontains=search)
        )
    return rows.order_by("first_name", "last_name")


def branch_shift_defaults(branch, on_date):
    """Shift 1 (and Shift 2, if the branch has one) for `branch` on the
    weekday `on_date` falls on -- read straight from BranchWorkingTime, the
    same table the Branch Working Time screen writes. Never shift_number >= 3:
    the roster only ever shows two.

    {"shift1": {"start": "08:00", "end": "17:00"} | None,
     "shift2": {...} | None}

    A branch with no working time at all (or none for that weekday) answers
    both None -- not an error. The admin still picks times by hand; this is a
    prefill, not a requirement.
    """
    rows = {
        row.shift_number: row
        for row in BranchWorkingTime.objects.filter(
            branch=branch, week_day=week_day_of(on_date), is_active=True, shift_number__in=(1, 2),
        )
    }
    return {
        "shift1": _shift_json(rows.get(1)),
        "shift2": _shift_json(rows.get(2)),
    }


def _shift_json(row):
    if row is None:
        return None
    return {"start": row.start_time.strftime("%H:%M"), "end": row.end_time.strftime("%H:%M")}


def week_dates(year, month, week_index):
    """The `week_index`-th Sun->Sat week of `year`-`month` (design/duty
    roster/business-logic.md section 2.4), as a list of 7 dates. The first and
    last week of a month are usually partial -- this returns only the days
    that fall inside the month, the same as the mockup's own buildWeeks()."""
    first = datetime.date(year, month, 1)
    if month == 12:
        next_month = datetime.date(year + 1, 1, 1)
    else:
        next_month = datetime.date(year, month + 1, 1)
    days_in_month = (next_month - first).days

    weeks = []
    day = 1
    while day <= days_in_month:
        start = day
        # Saturday is 5, Sunday 6: a week starting on Sunday runs 6 days on.
        until_saturday = (WeekDay.SATURDAY - week_day_of(datetime.date(year, month, day))) % 7
        end = min(day + until_saturday, days_in_month)
        weeks.append([datetime.date(year, month, d) for d in range(start, end + 1)])
        day = end + 1
    if not 1 <= week_index <= len(weeks):
        return []
    return weeks[week_index - 1]


def week_index_for(on_date):
    """Which of that month's Sun->Sat weeks `on_date` falls in -- the inverse
    of week_dates, used to find "today's week" before defaulting past it."""
    for index, days in enumerate(week_dates_all(on_date.year, on_date.month), start=1):
        if on_date in days:
            return index
    return 1


def week_dates_all(year, month):
    """Every week of the month, in order -- what week_dates(..., i) draws
    week i from."""
    weeks, index = [], 1
    while True:
        days = week_dates(year, month, index)
        if not days:
            return weeks
        weeks.append(days)
        index += 1


def default_week(today=None):
    """(year, month, week_index) for the week right after today's own --
    matching the mockup's own default (renderWeekOptions: WEEKS.length >= 2 ?
    '2' : '1') generalised to any month and any today."""
    today = today or timezone.localdate()
    this_week = week_index_for(today)
    total = len(week_dates_all(today.year, today.month))
    if this_week < total:
        return today.year, today.month, this_week + 1
    # Today's week is the month's last -- the "week after" spills into next
    # month's first week.
    if today.month == 12:
        return today.year + 1, 1, 1
    return today.year, today.month + 1, 1


@transaction.atomic
def save_week(employee, days, *, user, branches_qs):
    """Write one employee's week: `days` is
    [{"date", "day_type", "branch", "shift1_start", "shift1_end",
      "shift2_start", "shift2_end", "remarks"}, ...], `branch` an id or None,
    the four shift fields "HH:MM" strings or None. One transaction, every
    day's problems collected before anything is written -- a half-saved week
    is worse than none.
    """
    if employee.designation.roster_category not in RosterCategory.values:
        raise Invalid([{
            "field": "Employee",
            "message": f"{employee} is not a Cashier or Labour and cannot be rostered.",
        }])

    errors = []
    clean = []
    for entry in days:
        day_errors, fields = _check_day(entry, branches_qs=branches_qs)
        errors += day_errors
        if not day_errors:
            clean.append(fields)
    if errors:
        raise Invalid(errors)

    written = []
    for fields in clean:
        row, _ = DutyRoster.objects.update_or_create(
            employee=employee, date=fields["date"],
            defaults={**fields, "modified_by": user},
        )
        written.append(row)
    return written


def _check_day(entry, *, branches_qs):
    """(errors, fields) for one posted day. Mirrors DutyRoster's own check
    constraints, said as a sentence naming the date, not a 500."""
    errors = []
    on_date = entry.get("date")
    if isinstance(on_date, str):
        try:
            on_date = datetime.date.fromisoformat(on_date)
        except ValueError:
            on_date = None
    if not isinstance(on_date, datetime.date):
        return [{"field": "Date", "message": "Every day needs a date."}], {}
    label = on_date.strftime("%d %b")

    day_type = entry.get("day_type")
    if day_type not in DutyRosterDayType.values:
        errors.append({"field": label, "message": "Choose a day type."})
        return errors, {}

    fields = {"date": on_date, "day_type": day_type}

    if day_type != DutyRosterDayType.WORKING:
        fields.update(branch=None, shift1_start=None, shift1_end=None,
                      shift2_start=None, shift2_end=None,
                      remarks=(entry.get("remarks") or "")[:200])
        return errors, fields

    branch = branches_qs.filter(pk=entry.get("branch")).select_related("company").first()
    if branch is None:
        errors.append({"field": f"{label} — Branch", "message": "Choose an active station of this company."})
        return errors, {}
    # The branch's own company, not Django's global default -- the same
    # convention company.services.local_now uses, so a shift typed as "08:00"
    # means 08:00 in that company's timezone, not the server's.
    zone = zone_for(branch.company)

    # _combine_end rolls the end onto the next calendar day whenever it is not
    # after the start -- both shift inputs are plain HH:MM (loaded from
    # BranchWorkingTime, which has no date of its own), so "end before start"
    # is read as an overnight shift, not a typo, matching
    # business-logic.md section 2.1's own example generically (it does not
    # say only Shift 2 may cross midnight). The result is always after the
    # start once both times parse -- there is no "must end after start" case
    # left for these two fields to fail.
    shift1_start = _combine(on_date, entry.get("shift1_start"), zone)
    shift1_end = _combine_end(on_date, entry.get("shift1_start"), entry.get("shift1_end"), zone)
    if shift1_start is None or shift1_end is None:
        errors.append({"field": f"{label} — Shift 1", "message": "Enter Shift 1's start and end."})

    shift2_start = shift2_end = None
    has_shift2 = entry.get("shift2_start") or entry.get("shift2_end")
    if has_shift2:
        shift2_start = _combine(on_date, entry.get("shift2_start"), zone)
        shift2_end = _combine_end(on_date, entry.get("shift2_start"), entry.get("shift2_end"), zone)
        if shift2_start is None or shift2_end is None:
            errors.append({"field": f"{label} — Shift 2", "message": "Enter both of Shift 2's times, or remove it."})

    if errors:
        return errors, {}

    fields.update(
        branch=branch, shift1_start=shift1_start, shift1_end=shift1_end,
        shift2_start=shift2_start, shift2_end=shift2_end,
        remarks=(entry.get("remarks") or "")[:200],
    )
    return errors, fields


def _combine(on_date, hhmm, zone):
    """A "HH:MM" string on `on_date`, in `zone` -- or None."""
    if not hhmm:
        return None
    try:
        hour, minute = (int(p) for p in str(hhmm).split(":"))
    except (TypeError, ValueError):
        return None
    return datetime.datetime.combine(on_date, datetime.time(hour, minute), tzinfo=zone)


def _combine_end(on_date, start_hhmm, end_hhmm, zone):
    """The end time, rolled onto the next day when it is not after the start
    -- an overnight shift, exactly as the mockup's own inputs allow (both ends
    are full date+time; this is where a plain HH:MM pair becomes one)."""
    start = _combine(on_date, start_hhmm, zone)
    end = _combine(on_date, end_hhmm, zone)
    if start is None or end is None:
        return None
    if end <= start:
        end += datetime.timedelta(days=1)
    return end


def _hhmm(dt, zone):
    """The other half of _combine: a stored DateTimeField (always UTC once
    saved -- USE_TZ=True) back to the "HH:MM" the branch's own clock would
    show. `dt.strftime()` alone prints the raw UTC value, which is Dubai's
    07:00 read back as "03:00" -- every roster screen was showing that until
    this was added, since none of them converted before formatting."""
    if dt is None:
        return None
    return timezone.localtime(dt, zone).strftime("%H:%M")


def today_assignment(employee, at=None):
    """What this employee's app should show as "today's assignment": the
    branch, shift(s) and role from today's DutyRoster row, or None when there
    is none or the day is not Working.

    This is the function design/device-login.md's employee-login response is
    built to call. One seam is left for a feature that does not exist yet:

        # TODO once Leave Request exists: an *approved* leave request for
        # this employee/date should override whatever this function returns
        # with "on leave, <type>" -- checked here, before the DutyRoster
        # lookup below, so today_assignment stays the one place login reads.

    Until then, a day typed directly as Sick/Casual Leave on the roster itself
    already answers None here (only Working carries an assignment) -- the
    Leave Request table changes *how* a leave day is decided, not what this
    function returns for one.
    """
    today = (at or timezone.now())
    if timezone.is_aware(today):
        today = timezone.localtime(today).date()
    row = (
        DutyRoster.objects
        .filter(employee=employee, date=today, day_type=DutyRosterDayType.WORKING)
        .select_related("branch")
        .first()
    )
    if row is None:
        return None
    return {
        "branch": row.branch,
        "shift1": {"start": row.shift1_start, "end": row.shift1_end},
        "shift2": (
            {"start": row.shift2_start, "end": row.shift2_end}
            if row.shift2_start else None
        ),
        "role": employee.designation.title,
    }


def absent_today(employee):
    """Not built. Design/duty roster/business-logic.md section 7.3: a Working
    day with no matching Attendance row should read as Absent. Deferred with
    Attendance's own write path (the QR scan endpoint), by client decision for
    the Oct 2 demo. Its shape, once built:

        Attendance.objects.filter(
            employee=employee, business_date=today,
        ).exists()

    compared against today_assignment(employee) is not None.
    """
    raise NotImplementedError("Attendance is not built yet -- design/duty roster/duty-roster.md")


# -- The screen's three tabs -----------------------------------------------
#
# One consolidated payload per page load, the way the client's own mockup did
# it (State Wise / Branch Roster / Employee View all read one matrix rather
# than each tab re-querying) -- except this one is real: a day with no
# DutyRoster row is simply absent from the matrix, never filled in, unlike the
# wireframe's deterministic hash which always had an answer for every day.


def roster_payload(user, year, month):
    """Everything the three tabs draw from, for one month:

        {"employees": [...], "matrix": {emp_no: {iso_date: {...}}},
         "weeks": [{"index", "label", "days": [iso, ...]}, ...],
         "current_week_index", "today", "branches": [...]}

    `matrix[emp_no][date]` exists only for a date that has a DutyRoster row --
    absence means "not set", not "Working" or any other default.
    """
    today = timezone.localdate()
    employees = list(
        roster_eligible_employees(user).select_related("designation", "company")
    )
    weeks = week_dates_all(year, month)
    if not weeks:
        weeks = week_dates_all(today.year, today.month)
        year, month = today.year, today.month

    rows = (
        DutyRoster.objects
        .filter(employee__in=employees, date__range=(weeks[0][0], weeks[-1][-1]))
        .select_related("branch__company")
    )
    matrix = {}
    for row in rows:
        # The branch's own zone, not Django's global default -- the same
        # convention _check_day saves with (see _hhmm), so a shift typed as
        # "08:00" reads back as "08:00", not shifted by the UTC offset.
        zone = zone_for(row.branch.company) if row.branch_id else None
        matrix.setdefault(row.employee.employee_code, {})[row.date.isoformat()] = {
            "status": row.get_day_type_display(),
            "branch": row.branch.name if row.branch_id else None,
            "shift1_start": _hhmm(row.shift1_start, zone),
            "shift1_end": _hhmm(row.shift1_end, zone),
            "shift2_start": _hhmm(row.shift2_start, zone),
            "shift2_end": _hhmm(row.shift2_end, zone),
        }

    current_week_index = 0
    if today.year == year and today.month == month:
        current_week_index = week_index_for(today) - 1

    branch_ids = {r.branch_id for r in rows if r.branch_id}
    from apps.company.scoping import branches_for
    branches = list(branches_for(user).filter(pk__in=branch_ids).order_by("name"))

    return {
        # No "branch" field: unlike the wireframe's employees (each with a
        # fixed home branch), ours have none for staffing purposes -- which
        # branch someone is working is a fact about one day, read from the
        # matrix, never about the person (duty-roster.md, "employees are not
        # branch-mapped").
        "employees": [
            {"id": e.pk, "emp_no": e.employee_code, "name": e.full_name,
             "role": e.designation.get_roster_category_display(), "designation": e.designation.title}
            for e in employees
        ],
        "matrix": matrix,
        "weeks": [
            {"index": i, "label": f"Week {i} · {days[0]:%d %b} – {days[-1]:%d %b}", "days": [d.isoformat() for d in days]}
            for i, days in enumerate(weeks, start=1)
        ],
        "current_week_index": current_week_index,
        "today": today.isoformat(),
        "branches": [{"id": b.pk, "name": b.name} for b in branches],
    }


def roster_counts(employees, matrix, today_iso):
    """Total / on duty / week-off / sick leave, for the KPI tiles -- real
    counts over today's rows only (business-logic.md section 5)."""
    total = on_duty = week_off = sick_leave = 0
    for e in employees:
        rec = (matrix.get(e["emp_no"]) or {}).get(today_iso)
        total += 1
        if not rec:
            continue
        if rec["status"] == DutyRosterDayType.WORKING.label:
            on_duty += 1
        elif rec["status"] == DutyRosterDayType.WEEK_OFF.label:
            week_off += 1
        elif rec["status"] == DutyRosterDayType.SICK_LEAVE.label:
            sick_leave += 1
    return {"total": total, "on_duty": on_duty, "week_off": week_off, "sick_leave": sick_leave}


def assign_day(employee, entry, *, branches_qs, user, require_new=False):
    """Single-day upsert -- Add to Day and Edit Day both write through this.
    `entry` is one day dict, the same shape save_week's `days` list holds.

    `require_new=True` (Add to Day only) refuses an employee who already has
    a row that day instead of silently overwriting it -- Add to Day's job is
    introducing someone new to a day; changing what a day already says for
    someone is Edit Day's. Without this, re-picking an already-rostered
    employee from the Employee combo replaced their existing branch/shift
    with no warning that anything had been there.
    """
    if employee.designation.roster_category not in RosterCategory.values:
        raise Invalid([{"field": "Employee", "message": f"{employee} is not a Cashier or Labour."}])
    errors, fields = _check_day(entry, branches_qs=branches_qs)
    if errors:
        raise Invalid(errors)
    if require_new:
        existing = (
            DutyRoster.objects.filter(employee=employee, date=fields["date"])
            .select_related("branch").first()
        )
        if existing is not None:
            where = f" at {existing.branch.name}" if existing.branch_id else ""
            raise Invalid([{
                "field": "Employee",
                "message": f"{employee} already has a {existing.get_day_type_display()} "
                           f"entry for {fields['date']:%d %b}{where}. Edit that day instead "
                           f"of adding a new one.",
            }])
    row, _ = DutyRoster.objects.update_or_create(
        employee=employee, date=fields["date"], defaults={**fields, "modified_by": user},
    )
    return row


def remove_day(employee, on_date, *, user):
    """Branch Roster's chip ✕: the day goes back to unset, not Week Off --
    the wireframe's own chip-remove leaves no day type behind either."""
    if isinstance(on_date, str):
        on_date = datetime.date.fromisoformat(on_date)
    deleted, _ = DutyRoster.objects.filter(employee=employee, date=on_date).delete()
    return deleted > 0


def import_rows(rows, *, user, employees_qs, branches_qs):
    """Bulk Import's Apply: `rows` are
    [{"employee_code", "date", "day_type", "branch", "shift1_start",
      "shift1_end", "shift2_start", "shift2_end"}, ...] -- the mockup's own
    column shape, `employee_code` the join key. Every row is judged on its
    own: one bad row does not block the rest, and the caller gets back which
    rows landed and which didn't, never a silent partial apply.

    Returns (written_count, row_results) where row_results is
    [{"row", "employee_code", "ok", "message"}, ...] in the order given.
    """
    by_code = {
        e.employee_code: e
        for e in employees_qs.filter(designation__roster_category__in=RosterCategory.values)
        .select_related("designation")
    }
    written = 0
    results = []
    for i, entry in enumerate(rows, start=1):
        code = str(entry.get("employee_code") or "").strip()
        employee = by_code.get(code)
        if employee is None:
            results.append({"row": i, "employee_code": code, "ok": False,
                            "message": "No Cashier or Labour employee with that code."})
            continue
        errors, fields = _check_day(entry, branches_qs=branches_qs)
        if errors:
            results.append({"row": i, "employee_code": code, "ok": False,
                            "message": errors[0]["message"]})
            continue
        DutyRoster.objects.update_or_create(
            employee=employee, date=fields["date"], defaults={**fields, "modified_by": user},
        )
        written += 1
        results.append({"row": i, "employee_code": code, "ok": True, "message": "Saved."})
    return written, results


# -- Employee App: what its login response returns --------------------------
#
# design/device-login.md section 4. Read-only, one employee at a time -- no
# relation to roster_payload's multi-employee admin-screen matrix above.


def employee_profile(employee):
    """Everything the app's profile screen shows. Every field comes straight
    off Employee/Designation -- nothing here is invented for the app."""
    designation = employee.designation
    return {
        "employee_code": employee.employee_code,
        "full_name": employee.full_name,
        "first_name": employee.first_name,
        "middle_name": employee.middle_name,
        "last_name": employee.last_name,
        "designation": designation.title,
        "role": designation.get_roster_category_display() if designation.roster_category else None,
        "branch": employee.branch.name if employee.branch_id else None,
        "mobile": employee.mobile,
        "email": employee.email,
        "photo_url": employee.photo.url if employee.photo else None,
        "company": employee.company.name,
    }


def current_week_roster(employee, *, at=None):
    """This employee's own current Sun-Sat week -- week_dates_all/
    week_index_for, the same pair roster_payload uses for the admin screen,
    just for one employee instead of every roster-eligible one.

    A day with no DutyRoster row comes back with status=None, never a
    defaulted "Working" -- the same rule roster_payload's matrix follows.
    Shift times go through _hhmm/zone_for, not row.shift1_start.strftime()
    directly -- that exact shortcut was the bug fixed earlier for the admin
    screen (a Dubai 07:00 shift printing back as "03:00", the raw UTC the
    field is stored in); new code reading the same DateTimeFields would
    reintroduce it without this.
    """
    company = employee.company
    today = business_date_for(company, at or timezone.now())
    weeks = week_dates_all(today.year, today.month)
    index = week_index_for(today) - 1
    days = weeks[index] if 0 <= index < len(weeks) else []

    rows = {
        row.date: row
        for row in DutyRoster.objects.filter(employee=employee, date__in=days)
        .select_related("branch__company")
    }

    out = []
    for day in days:
        row = rows.get(day)
        if row is None:
            out.append({
                "date": day.isoformat(), "status": None, "branch": None,
                "shift1_start": None, "shift1_end": None,
                "shift2_start": None, "shift2_end": None,
            })
            continue
        zone = zone_for(row.branch.company) if row.branch_id else zone_for(company)
        out.append({
            "date": day.isoformat(),
            "status": row.get_day_type_display(),
            "branch": row.branch.name if row.branch_id else None,
            "shift1_start": _hhmm(row.shift1_start, zone),
            "shift1_end": _hhmm(row.shift1_end, zone),
            "shift2_start": _hhmm(row.shift2_start, zone),
            "shift2_end": _hhmm(row.shift2_end, zone),
        })

    return {
        "week_start": days[0].isoformat() if days else None,
        "week_end": days[-1].isoformat() if days else None,
        "days": out,
    }
