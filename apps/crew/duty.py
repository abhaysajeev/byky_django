"""The duty roster as the apps read it -- read-only; rosters are written on
the web (apps/crew/services.py, the Duty Roster screen).

* `my_roster` -- the employee app's My Duty Roster and Previous Duty Rosters
  (employee app SRS sections 5-6): the signed-in employee's own days.
* `team_roster` -- the team's, by the Team Attendance hierarchy
  (services.team_scope_of): the manager app sees the whole company; in the
  employee app the designation's Team Scope decides, a Branch Supervisor
  seeing the stations they supervise (owner, 15 Oct 2026).

Only cashiers and labour are rostered. A supervisor sees everyone who works
at least one day of the range at their stations, with every day of the range
-- days at other stations (named), week off and leave too. The whole-company
view also lists the cashiers and labour with nothing rostered: their days
read "not set" (status null).
"""

import datetime

from django.db.models import Count, Q
from django.utils import timezone

from apps.crew import services
from apps.crew.models import DutyRoster, DutyRosterDayType, Employee, RosterCategory
from core.timezones import business_date_for, zone_for

ROSTER_MAX_DAYS = 31
PAGE_SIZE = 50
UPCOMING_DAYS = 7          # My Duty Roster: today and the six days after


def dates(from_date, to_date):
    return [from_date + datetime.timedelta(days=i) for i in range((to_date - from_date).days + 1)]


def current_week(company, at=None):
    """The company's current roster week -- the same week the web screen and
    the login reply show (services.week_dates_all / week_index_for)."""
    today = business_date_for(company, at or timezone.now())
    weeks = services.week_dates_all(today.year, today.month)
    index = services.week_index_for(today) - 1
    return weeks[index] if 0 <= index < len(weeks) else [today]


def upcoming(company, at=None):
    today = business_date_for(company, at or timezone.now())
    return today, today + datetime.timedelta(days=UPCOMING_DAYS - 1)


def _ends_next_day(start, end, zone):
    if start is None or end is None:
        return None
    return timezone.localtime(end, zone).date() > timezone.localtime(start, zone).date()


def day_json(row, day, zone):
    """One roster day, the same shape everywhere (login, duty/my, duty/team).
    No row: status null -- not set, never a defaulted Working."""
    if row is None:
        return {
            "date": day.isoformat(), "status": None, "branch_id": None, "branch": None, "branch_code": None,
            "state": None, "shift1_start": None, "shift1_end": None, "shift1_ends_next_day": None,
            "shift2_start": None, "shift2_end": None, "shift2_ends_next_day": None,
            "leave_type": None, "leave_reference": None,
        }
    branch = row.branch if row.branch_id else None
    # The station's own clock (services._hhmm): a Dubai 07:00 shift reads 07:00.
    zone = zone_for(branch.company) if branch else zone
    return {
        "date": day.isoformat(),
        "status": row.get_day_type_display(),
        # The station's database id, for the app's QR code.
        "branch_id": row.branch_id,
        "branch": branch.name if branch else None,
        # The QR code carries this as employee_branch_code (attendance/mark).
        "branch_code": branch.short_code if branch else None,
        "state": branch.location.state.name if branch else None,
        "shift1_start": services._hhmm(row.shift1_start, zone),
        "shift1_end": services._hhmm(row.shift1_end, zone),
        "shift1_ends_next_day": _ends_next_day(row.shift1_start, row.shift1_end, zone),
        "shift2_start": services._hhmm(row.shift2_start, zone),
        "shift2_end": services._hhmm(row.shift2_end, zone),
        "shift2_ends_next_day": _ends_next_day(row.shift2_start, row.shift2_end, zone),
        **services._leave_fields(row),
    }


def _rows(employee_ids, from_date, to_date):
    return (DutyRoster.objects
            .filter(employee_id__in=employee_ids, date__range=(from_date, to_date))
            .select_related("branch__company", "branch__location__state", "leave_request__leave_type"))


def days_by_employee(employee_ids, from_date, to_date, zone):
    """{employee id: [day_json for every date of the range]}."""
    held = {(row.employee_id, row.date): row for row in _rows(employee_ids, from_date, to_date)}
    span = dates(from_date, to_date)
    return {pk: [day_json(held.get((pk, day)), day, zone) for day in span] for pk in employee_ids}


def my_roster(employee, from_date, to_date):
    zone = zone_for(employee.company)
    return days_by_employee([employee.pk], from_date, to_date, zone)[employee.pk]


def team_roster(company, branch_ids, from_date, to_date, *, state_id=None, branch_id=None, category="",
                search=""):
    """(employees queryset, counts) -- who the team view lists, by name, and
    how many of their days in the range hold each status.

    `branch_ids` is the caller's scope (None: the whole company). With no
    station scope and no station / emirate filter, every active cashier and
    labour is listed, rostered or not; otherwise those with a Working day in
    the range at a station in scope.
    """
    ids = services._narrowed(company, branch_ids, state_id, branch_id)
    staff = Employee.objects.filter(company=company, is_active=True,
                                    designation__roster_category__in=RosterCategory.values)
    if category:
        staff = staff.filter(designation__roster_category=category)
    if ids is not None:
        working_here = DutyRoster.objects.filter(
            date__range=(from_date, to_date), day_type=DutyRosterDayType.WORKING, branch_id__in=ids,
        ).values("employee_id")
        staff = staff.filter(pk__in=working_here)
    text = (search or "").strip()
    if text:
        staff = staff.filter(Q(employee_code__icontains=text) | Q(first_name__icontains=text)
                             | Q(middle_name__icontains=text) | Q(last_name__icontains=text))
    staff = staff.select_related("designation").order_by("first_name", "last_name", "employee_code")

    by_type = dict(
        DutyRoster.objects.filter(employee__in=staff, date__range=(from_date, to_date))
        .values_list("day_type").annotate(n=Count("pk")).values_list("day_type", "n")
    )
    counts = {day_type: by_type.get(day_type, 0) for day_type in DutyRosterDayType.values}
    counts["not_set"] = staff.count() * len(dates(from_date, to_date)) - sum(by_type.values())
    return staff, counts
