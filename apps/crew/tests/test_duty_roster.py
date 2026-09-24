"""Duty Roster: the model's guarantees, the branch-shift prefill, the week
math, and saving one employee's week.

design/duty roster/duty-roster.md. Employees are not branch-mapped (client
decision, 20 Sep 2026) -- every test below picks a branch per day, never reads
Employee.branch for staffing.
"""

import datetime
import json

import pytest
from django.db import IntegrityError
from django.utils import timezone

from apps.company.models import (
    Branch, BranchType, BranchWorkingTime, Company, Country, Location, State, WeekDay,
)
from apps.crew import services
from apps.crew.models import DutyRoster, DutyRosterDayType, Employee, RosterCategory
from apps.portal.models import Role
from apps.portal.services import grant_all
from core.enums import Channel
from core.models import User

PASSWORD = "Byky#2026"


@pytest.fixture
def world(db):
    country = Country.objects.create(short_code="AE", name="United Arab Emirates")
    state = State.objects.create(country=country, short_code="AUH", name="Abu Dhabi")
    company = Company.objects.create(
        short_code="BYKY", name="BYKY", country=country, state=state,
        phone_number="+9710000000", email="ops@byky.test",
    )
    location = Location.objects.create(
        country=country, state=state, short_code="CORN", name="Corniche"
    )
    branch = Branch.objects.create(
        company=company, location=location, short_code="AUH01",
        name="Creek Park 1", branch_type=BranchType.STATION,
    )
    other_branch = Branch.objects.create(
        company=company, location=location, short_code="AUH02",
        name="Zabeel Park 1", branch_type=BranchType.STATION,
    )
    from apps.crew.models import Designation

    cashier = Designation.objects.create(
        company=company, code="CASH", title="Cashier", roster_category=RosterCategory.CASHIER,
    )
    labour = Designation.objects.create(
        company=company, code="LAB", title="Labour", roster_category=RosterCategory.LABOUR,
    )
    supervisor = Designation.objects.create(company=company, code="SUP", title="Supervisor")
    return {
        "company": company, "branch": branch, "other_branch": other_branch,
        "country": country, "state": state,
        "cashier": cashier, "labour": labour, "supervisor": supervisor,
    }


@pytest.fixture
def cashier(world):
    return Employee.objects.create(
        company=world["company"], employee_code="BYKY001", first_name="Rashed", last_name="K",
        designation=world["cashier"],
    )


@pytest.fixture
def supervisor(world):
    """Not roster-eligible: Supervisor is not Cashier or Labour."""
    return Employee.objects.create(
        company=world["company"], employee_code="BYKY002", first_name="Not", last_name="Eligible",
        designation=world["supervisor"],
    )


@pytest.fixture
def working_time(world):
    """Creek Park 1: every day 07:00-23:00, one shift -- Sunday also carries a
    Shift 2, so both branches (single-shift and split-shift) are covered."""
    for day in range(7):
        BranchWorkingTime.objects.create(
            branch=world["branch"], week_day=day, shift_number=1,
            start_time=datetime.time(7, 0), end_time=datetime.time(23, 0),
        )
    BranchWorkingTime.objects.create(
        branch=world["branch"], week_day=WeekDay.SUNDAY, shift_number=2,
        start_time=datetime.time(23, 0), end_time=datetime.time(3, 0),
    )


@pytest.fixture
def user(world):
    """A company-scoped user -- roster_payload/roster_eligible_employees scope
    every query to core.scoping.scoped_to, which sees nothing for user=None."""
    role = Role.objects.create(company=world["company"], name="Roster User")
    grant_all(role)
    return User.objects.create_user(
        "roster.user", PASSWORD, display_name="Roster User", company=world["company"], role=role,
        allowed_channels=[Channel.WEB],
    )


@pytest.fixture
def client_in(client, world):
    role = Role.objects.create(company=world["company"], name="Administrator")
    grant_all(role)
    User.objects.create_user(
        "sara.k", PASSWORD, display_name="Sara K", company=world["company"], role=role,
        allowed_channels=[Channel.WEB],
    )
    client.post("/login/", {"username": "sara.k", "password": PASSWORD})
    return client


def post(client, url, payload):
    return client.post(url, data=json.dumps(payload), content_type="application/json")


def working_day(employee, branch, date, start="08:00", end="17:00"):
    return DutyRoster.objects.create(
        employee=employee, date=date, day_type=DutyRosterDayType.WORKING, branch=branch,
        shift1_start=timezone.make_aware(datetime.datetime.combine(date, datetime.time(*map(int, start.split(":"))))),
        shift1_end=timezone.make_aware(datetime.datetime.combine(date, datetime.time(*map(int, end.split(":"))))),
    )


# -- The model's own guarantees ------------------------------------------------


def test_one_entry_per_employee_per_day(cashier, world):
    working_day(cashier, world["branch"], datetime.date(2026, 9, 21))
    with pytest.raises(IntegrityError):
        working_day(cashier, world["branch"], datetime.date(2026, 9, 21))


def test_a_working_day_needs_a_branch_and_shift_one(cashier, world):
    with pytest.raises(IntegrityError):
        DutyRoster.objects.create(
            employee=cashier, date=datetime.date(2026, 9, 21), day_type=DutyRosterDayType.WORKING,
        )


def test_a_non_working_day_carries_no_branch_or_shift(cashier, world):
    with pytest.raises(IntegrityError):
        DutyRoster.objects.create(
            employee=cashier, date=datetime.date(2026, 9, 21), day_type=DutyRosterDayType.WEEK_OFF,
            branch=world["branch"],
        )


def test_every_day_type_but_working_needs_nothing_else(cashier):
    for day_type in (DutyRosterDayType.WEEK_OFF, DutyRosterDayType.SICK_LEAVE, DutyRosterDayType.CASUAL_LEAVE):
        row = DutyRoster.objects.create(
            employee=cashier, date=datetime.date(2026, 9, 20 + list(DutyRosterDayType.values).index(day_type)),
            day_type=day_type,
        )
        assert row.branch is None and row.shift1_start is None


def test_shift1_must_end_after_it_starts(cashier, world):
    with pytest.raises(IntegrityError):
        DutyRoster.objects.create(
            employee=cashier, date=datetime.date(2026, 9, 21), day_type=DutyRosterDayType.WORKING,
            branch=world["branch"],
            shift1_start=timezone.make_aware(datetime.datetime(2026, 9, 21, 17, 0)),
            shift1_end=timezone.make_aware(datetime.datetime(2026, 9, 21, 8, 0)),
        )


def test_an_overnight_shift_saves_cleanly(cashier, world):
    """Both ends carry their own date, so an overnight shift is just an end on
    the next calendar day -- no flag needed (business-logic.md section 2.1)."""
    row = DutyRoster.objects.create(
        employee=cashier, date=datetime.date(2026, 9, 21), day_type=DutyRosterDayType.WORKING,
        branch=world["branch"],
        shift1_start=timezone.make_aware(datetime.datetime(2026, 9, 21, 18, 0)),
        shift1_end=timezone.make_aware(datetime.datetime(2026, 9, 22, 3, 0)),
    )
    assert row.shift1_end.date() > row.shift1_start.date()


def test_shift2_cannot_exist_without_shift1(cashier, world):
    with pytest.raises(IntegrityError):
        DutyRoster.objects.create(
            employee=cashier, date=datetime.date(2026, 9, 21), day_type=DutyRosterDayType.WORKING,
            branch=world["branch"],
            shift1_start=timezone.make_aware(datetime.datetime(2026, 9, 21, 8, 0)),
            shift1_end=timezone.make_aware(datetime.datetime(2026, 9, 21, 17, 0)),
            shift2_start=timezone.make_aware(datetime.datetime(2026, 9, 21, 18, 0)),
            # shift2_end left null -- both or neither.
        )


# -- branch_shift_defaults: the Branch Working Time prefill --------------------


def test_shift_one_only_leaves_shift_two_empty(world, working_time):
    result = services.branch_shift_defaults(world["branch"], datetime.date(2026, 9, 22))  # Tuesday
    assert result == {"shift1": {"start": "07:00", "end": "23:00"}, "shift2": None}


def test_both_shifts_returned_when_both_exist(world, working_time):
    result = services.branch_shift_defaults(world["branch"], datetime.date(2026, 9, 20))  # Sunday
    assert result["shift1"] == {"start": "07:00", "end": "23:00"}
    assert result["shift2"] == {"start": "23:00", "end": "03:00"}


def test_a_branch_with_no_working_time_answers_none_not_an_error(world):
    result = services.branch_shift_defaults(world["other_branch"], datetime.date(2026, 9, 22))
    assert result == {"shift1": None, "shift2": None}


# -- week_dates / default_week: the same math as the mockup's buildWeeks ------


def test_week_boundaries_match_the_client_mockups_own_september_2026():
    """The mockup's WEEKS array for Sep 2026: week 1 is Tue 1-Sat 5."""
    weeks = services.week_dates_all(2026, 9)
    assert [d.day for d in weeks[0]] == [1, 2, 3, 4, 5]
    assert [d.day for d in weeks[1]] == [6, 7, 8, 9, 10, 11, 12]
    assert weeks[-1][-1] == datetime.date(2026, 9, 30)


def test_default_week_is_the_one_right_after_todays():
    assert services.week_index_for(datetime.date(2026, 9, 20)) == 4
    assert services.default_week(datetime.date(2026, 9, 20)) == (2026, 9, 5)


def test_default_week_spills_into_next_month_from_the_last_week():
    assert services.default_week(datetime.date(2026, 9, 30)) == (2026, 10, 1)


def test_default_week_spills_into_next_year_from_december():
    assert services.default_week(datetime.date(2026, 12, 31)) == (2027, 1, 1)


# -- save_week -------------------------------------------------------------------


def one_day(date, **extra):
    fields = {"date": date, "day_type": "week_off"}
    fields.update(extra)
    return fields


def test_saving_a_week_writes_one_row_per_working_day(cashier, world):
    from apps.company.scoping import branches_for
    from apps.crew.scoping import employees_for

    days = [
        one_day(datetime.date(2026, 9, 20), day_type="working", branch=world["branch"].pk,
                shift1_start="08:00", shift1_end="17:00"),
        one_day(datetime.date(2026, 9, 21), day_type="week_off"),
    ]
    written = services.save_week(cashier, days, user=None, branches_qs=Branch.objects.all())

    assert len(written) == 2
    row = DutyRoster.objects.get(employee=cashier, date=datetime.date(2026, 9, 20))
    assert row.branch == world["branch"]
    assert timezone.localtime(row.shift1_start).strftime("%H:%M") == "08:00"


def test_saving_twice_updates_rather_than_duplicating(cashier, world):
    days = [one_day(datetime.date(2026, 9, 20), day_type="working", branch=world["branch"].pk,
                    shift1_start="08:00", shift1_end="17:00")]
    services.save_week(cashier, days, user=None, branches_qs=Branch.objects.all())
    days[0]["shift1_start"] = "09:00"
    services.save_week(cashier, days, user=None, branches_qs=Branch.objects.all())

    assert DutyRoster.objects.filter(employee=cashier, date=datetime.date(2026, 9, 20)).count() == 1
    assert timezone.localtime(DutyRoster.objects.get(employee=cashier).shift1_start).strftime("%H:%M") == "09:00"


def test_a_working_day_with_no_branch_is_refused_by_name(cashier, world):
    days = [one_day(datetime.date(2026, 9, 20), day_type="working")]
    with pytest.raises(services.Invalid) as refused:
        services.save_week(cashier, days, user=None, branches_qs=Branch.objects.all())
    assert "Branch" in refused.value.errors[0]["field"]


def test_a_non_roster_designation_is_refused(supervisor, world):
    days = [one_day(datetime.date(2026, 9, 20))]
    with pytest.raises(services.Invalid, match="not a Cashier or Labour"):
        services.save_week(supervisor, days, user=None, branches_qs=Branch.objects.all())


def test_an_end_before_start_becomes_an_overnight_shift(cashier, world):
    """Both shift fields are plain HH:MM (loaded from BranchWorkingTime, which
    has no date of its own), so "end before start" reads as an overnight
    shift, not a typo -- business-logic.md section 2.1 does not restrict this
    to Shift 2."""
    days = [one_day("2026-09-20", day_type="working", branch=world["branch"].pk,
                    shift1_start="17:00", shift1_end="08:00")]

    written = services.save_week(cashier, days, user=None, branches_qs=Branch.objects.all())

    row = written[0]
    assert row.shift1_end.date() > row.shift1_start.date()
    assert timezone.localtime(row.shift1_end).strftime("%H:%M") == "08:00"


def test_a_missing_shift_time_is_refused_by_name(cashier, world):
    days = [one_day("2026-09-20", day_type="working", branch=world["branch"].pk,
                    shift1_start="08:00")]  # no shift1_end
    with pytest.raises(services.Invalid, match="Shift 1"):
        services.save_week(cashier, days, user=None, branches_qs=Branch.objects.all())


# -- today_assignment: what the employee login will call ------------------------


def test_today_assignment_reads_the_working_day(cashier, world):
    today = timezone.localdate()
    working_day(cashier, world["branch"], today)

    result = services.today_assignment(cashier)

    assert result["branch"] == world["branch"]
    assert result["role"] == "Cashier"


def test_today_assignment_is_none_off_duty(cashier):
    DutyRoster.objects.create(employee=cashier, date=timezone.localdate(), day_type=DutyRosterDayType.WEEK_OFF)
    assert services.today_assignment(cashier) is None


def test_today_assignment_is_none_with_no_roster_row(cashier):
    assert services.today_assignment(cashier) is None


# -- the screen and its write endpoints -------------------------------------------


def test_the_screen_renders_on_an_empty_roster(client_in):
    response = client_in.get("/crew/duty-roster/")
    assert response.status_code == 200
    assert b"No Cashier or Labour employees" in response.content


def test_the_screen_lists_eligible_employees_and_hides_the_ineligible(client_in, cashier, supervisor):
    response = client_in.get("/crew/duty-roster/")
    body = response.content.decode()
    assert cashier.full_name in body
    assert supervisor.employee_code not in body


def test_branch_shifts_endpoint_returns_the_real_working_time(client_in, world, working_time):
    response = client_in.get(
        "/crew/duty-roster/branch-shifts/",
        {"branch": world["branch"].pk, "date": "2026-09-22"},
    )
    assert response.json() == {"ok": True, "shift1": {"start": "07:00", "end": "23:00"}, "shift2": None}


def test_save_week_endpoint_writes_through(client_in, cashier, world):
    response = post(client_in, "/crew/duty-roster/save-week/", {
        "employee": cashier.pk,
        "days": [one_day("2026-09-20", day_type="working", branch=world["branch"].pk,
                          shift1_start="08:00", shift1_end="17:00")],
    })
    assert response.json()["ok"] is True
    assert DutyRoster.objects.filter(employee=cashier).count() == 1


def test_writes_need_permission(client, world, cashier):
    role = Role.objects.create(company=world["company"], name="Visitor")
    User.objects.create_user(
        "vis.itor", PASSWORD, display_name="Vis Itor", company=world["company"], role=role,
        allowed_channels=[Channel.WEB],
    )
    client.post("/login/", {"username": "vis.itor", "password": PASSWORD})

    assert post(client, "/crew/duty-roster/save-week/", {"employee": cashier.pk, "days": []}).status_code == 403
    assert client.get("/crew/duty-roster/branch-shifts/").status_code == 403


# -- roster_payload / roster_counts: the one consolidated payload every tab reads --


def test_roster_payload_matrix_only_has_real_rows(cashier, world, user):
    today = timezone.localdate()
    working_day(cashier, world["branch"], today)
    payload = services.roster_payload(user, today.year, today.month)

    matrix = payload["matrix"][cashier.employee_code]
    assert matrix[today.isoformat()]["status"] == "Working"
    assert matrix[today.isoformat()]["branch"] == world["branch"].name
    # No entry at all for a day nobody set -- never a default "Working" fill.
    other_day = (today.replace(day=1)).isoformat()
    if other_day != today.isoformat():
        assert other_day not in matrix


def test_roster_payload_employees_carry_no_branch_field(cashier, world, user):
    payload = services.roster_payload(user, 2026, 9)
    emp = payload["employees"][0]
    assert emp["emp_no"] == cashier.employee_code
    assert "branch" not in emp


def test_roster_payload_excludes_non_roster_designations(cashier, supervisor, world, user):
    payload = services.roster_payload(user, 2026, 9)
    codes = [e["emp_no"] for e in payload["employees"]]
    assert cashier.employee_code in codes
    assert supervisor.employee_code not in codes


def test_roster_payload_matrix_shows_the_branch_local_time_not_utc(cashier, world, user):
    """Regression: the matrix used to print row.shift1_start.strftime()
    directly -- the raw UTC value a USE_TZ=True field is always stored and
    read back as -- instead of converting to the branch's own zone first.
    Dubai is UTC+4, so an 08:00-17:00 shift silently became "04:00"-"13:00"
    on every screen that reads this matrix (By Branch, Branch Roster,
    Employee View)."""
    today = timezone.localdate()
    working_day(cashier, world["branch"], today, start="08:00", end="17:00")
    payload = services.roster_payload(user, today.year, today.month)
    rec = payload["matrix"][cashier.employee_code][today.isoformat()]
    assert rec["shift1_start"] == "08:00"
    assert rec["shift1_end"] == "17:00"


def test_roster_counts_reads_todays_row_only(cashier, world, user):
    today = timezone.localdate()
    working_day(cashier, world["branch"], today)
    payload = services.roster_payload(user, today.year, today.month)
    counts = services.roster_counts(payload["employees"], payload["matrix"], payload["today"])
    assert counts == {"total": 1, "on_duty": 1, "week_off": 0, "sick_leave": 0}


def test_roster_counts_ignores_a_day_with_no_row(cashier, user):
    payload = services.roster_payload(user, 2026, 9)
    counts = services.roster_counts(payload["employees"], payload["matrix"], payload["today"])
    assert counts["total"] == 1 and counts["on_duty"] == 0


# -- assign_day / remove_day: Edit Day and Add to Day's round trip -----------------


def test_assign_day_creates_a_working_row(cashier, world):
    row = services.assign_day(
        cashier, {"date": "2026-09-20", "day_type": "working", "branch": world["branch"].pk,
                  "shift1_start": "08:00", "shift1_end": "17:00"},
        branches_qs=Branch.objects.all(), user=None,
    )
    assert row.branch == world["branch"]
    assert DutyRoster.objects.filter(employee=cashier, date=datetime.date(2026, 9, 20)).count() == 1


def test_assign_day_updates_rather_than_duplicates(cashier, world):
    services.assign_day(
        cashier, {"date": "2026-09-20", "day_type": "working", "branch": world["branch"].pk,
                  "shift1_start": "08:00", "shift1_end": "17:00"},
        branches_qs=Branch.objects.all(), user=None,
    )
    services.assign_day(
        cashier, {"date": "2026-09-20", "day_type": "week_off"},
        branches_qs=Branch.objects.all(), user=None,
    )
    row = DutyRoster.objects.get(employee=cashier, date=datetime.date(2026, 9, 20))
    assert row.day_type == "week_off" and row.branch is None


def test_assign_day_require_new_refuses_an_already_mapped_employee(cashier, world):
    """Add to Day's guard: it introduces someone new to a day, it does not
    silently replace whoever the day already had (Edit Day does that)."""
    working_day(cashier, world["branch"], datetime.date(2026, 9, 20), start="08:00", end="17:00")
    with pytest.raises(services.Invalid, match="already has a Working entry"):
        services.assign_day(
            cashier, {"date": "2026-09-20", "day_type": "working", "branch": world["other_branch"].pk,
                      "shift1_start": "09:00", "shift1_end": "18:00"},
            branches_qs=Branch.objects.all(), user=None, require_new=True,
        )
    # Refused, not silently overwritten.
    row = DutyRoster.objects.get(employee=cashier, date=datetime.date(2026, 9, 20))
    assert row.branch == world["branch"]


def test_assign_day_require_new_allows_a_first_assignment(cashier, world):
    row = services.assign_day(
        cashier, {"date": "2026-09-20", "day_type": "working", "branch": world["branch"].pk,
                  "shift1_start": "08:00", "shift1_end": "17:00"},
        branches_qs=Branch.objects.all(), user=None, require_new=True,
    )
    assert row.branch == world["branch"]


def test_assign_day_without_require_new_still_overwrites(cashier, world):
    """Edit Day's own call keeps its old behaviour -- require_new defaults
    False, so changing what a day already says still works from there."""
    working_day(cashier, world["branch"], datetime.date(2026, 9, 20))
    services.assign_day(
        cashier, {"date": "2026-09-20", "day_type": "week_off"},
        branches_qs=Branch.objects.all(), user=None,
    )
    row = DutyRoster.objects.get(employee=cashier, date=datetime.date(2026, 9, 20))
    assert row.day_type == "week_off"


def test_remove_day_deletes_the_row(cashier, world):
    working_day(cashier, world["branch"], datetime.date(2026, 9, 20))
    removed = services.remove_day(cashier, "2026-09-20", user=None)
    assert removed is True
    assert not DutyRoster.objects.filter(employee=cashier, date=datetime.date(2026, 9, 20)).exists()


def test_remove_day_on_an_unset_day_reports_nothing_removed(cashier):
    assert services.remove_day(cashier, "2026-09-20", user=None) is False


# -- import_rows: Bulk Import's Apply, one row judged at a time -------------------


def test_import_rows_mixed_batch(cashier, world):
    rows = [
        {"employee_code": cashier.employee_code, "date": "2026-09-20", "day_type": "working",
         "branch": world["branch"].pk, "shift1_start": "08:00", "shift1_end": "17:00"},
        {"employee_code": "NOPE", "date": "2026-09-20", "day_type": "working"},
        {"employee_code": cashier.employee_code, "date": "2026-09-21", "day_type": "working"},  # no branch
    ]
    written, results = services.import_rows(
        rows, user=None, employees_qs=Employee.objects.all(), branches_qs=Branch.objects.all(),
    )
    assert written == 1
    assert results[0]["ok"] is True
    assert results[1]["ok"] is False and "No Cashier or Labour" in results[1]["message"]
    assert results[2]["ok"] is False
    assert DutyRoster.objects.filter(employee=cashier).count() == 1


# -- the three write endpoints -----------------------------------------------------


def test_assign_day_endpoint_writes_through(client_in, cashier, world):
    response = post(client_in, "/crew/duty-roster/assign-day/", {
        "employee": cashier.pk, "date": "2026-09-20", "day_type": "working",
        "branch": world["branch"].pk, "shift1_start": "08:00", "shift1_end": "17:00",
    })
    assert response.json()["ok"] is True
    assert DutyRoster.objects.filter(employee=cashier, date=datetime.date(2026, 9, 20)).exists()


def test_assign_day_endpoint_echoes_the_branch_local_time_not_utc(client_in, cashier, world):
    """Same regression as roster_payload's: the response's "day" block must
    echo back what was typed, not the raw UTC the field is stored in."""
    response = post(client_in, "/crew/duty-roster/assign-day/", {
        "employee": cashier.pk, "date": "2026-09-20", "day_type": "working",
        "branch": world["branch"].pk, "shift1_start": "08:00", "shift1_end": "17:00",
    })
    day = response.json()["day"]
    assert day["shift1_start"] == "08:00"
    assert day["shift1_end"] == "17:00"


def test_assign_day_endpoint_require_new_refuses_a_double_booking(client_in, cashier, world):
    working_day(cashier, world["branch"], datetime.date(2026, 9, 20))
    response = post(client_in, "/crew/duty-roster/assign-day/", {
        "employee": cashier.pk, "date": "2026-09-20", "day_type": "working",
        "branch": world["other_branch"].pk, "shift1_start": "09:00", "shift1_end": "18:00",
        "require_new": True,
    })
    body = response.json()
    assert response.status_code == 400
    assert body["ok"] is False
    assert "already has a Working entry" in body["errors"][0]["message"]
    assert DutyRoster.objects.get(employee=cashier, date=datetime.date(2026, 9, 20)).branch == world["branch"]


def test_remove_day_endpoint_deletes(client_in, cashier, world):
    working_day(cashier, world["branch"], datetime.date(2026, 9, 20))
    response = post(client_in, "/crew/duty-roster/remove-day/", {
        "employee": cashier.pk, "date": "2026-09-20",
    })
    assert response.json()["ok"] is True
    assert not DutyRoster.objects.filter(employee=cashier, date=datetime.date(2026, 9, 20)).exists()


def test_import_endpoint_reports_per_row_results(client_in, cashier, world):
    response = post(client_in, "/crew/duty-roster/import/", {
        "rows": [
            {"employee_code": cashier.employee_code, "date": "2026-09-20", "day_type": "working",
             "branch": world["branch"].pk, "shift1_start": "08:00", "shift1_end": "17:00"},
            {"employee_code": "NOPE", "date": "2026-09-20", "day_type": "working"},
        ],
    })
    body = response.json()
    assert body["ok"] is True and body["written"] == 1 and body["failed"] == 1
    assert body["results"][0]["ok"] is True
    assert body["results"][1]["ok"] is False


def test_new_endpoints_need_permission(client, world, cashier):
    role = Role.objects.create(company=world["company"], name="Visitor")
    User.objects.create_user(
        "vis.itor2", PASSWORD, display_name="Vis Itor", company=world["company"], role=role,
        allowed_channels=[Channel.WEB],
    )
    client.post("/login/", {"username": "vis.itor2", "password": PASSWORD})

    assert post(client, "/crew/duty-roster/assign-day/", {"employee": cashier.pk}).status_code == 403
    assert post(client, "/crew/duty-roster/remove-day/", {"employee": cashier.pk}).status_code == 403
    assert post(client, "/crew/duty-roster/import/", {"rows": []}).status_code == 403


# -- the rebuilt screen: 3 tabs, KPI tiles, Bulk Import ----------------------------


def test_the_screen_renders_its_three_tabs_and_kpi_tiles(client_in, cashier, world):
    working_day(cashier, world["branch"], timezone.localdate())
    response = client_in.get("/crew/duty-roster/")
    body = response.content.decode()
    assert response.status_code == 200
    for tab in ("By Branch", "Branch Roster", "Employee View"):
        assert tab in body
    assert "Roster-eligible staff" in body and "On duty today" in body
    assert "Bulk Import (Excel)" in body
    assert "roster-branches-data" in body


def test_the_screen_renders_with_zero_data(client_in):
    response = client_in.get("/crew/duty-roster/")
    assert response.status_code == 200
    assert b"No Cashier or Labour employees" in response.content
