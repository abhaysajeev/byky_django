"""Primary / Secondary cashiers and the past-day lock on the duty roster
(owner, 13 Nov 2026; apps/crew/cashiers.py, services._past_errors)."""

import datetime
import importlib
import uuid

import pytest
from django.utils import timezone

from apps.company.models import Branch, CompanyConfig
from apps.crew import duty, leave, services
from apps.crew.models import CashierType, DutyRoster, DutyRosterDayType, Employee, LeaveRequest
from apps.crew.tests import test_duty_roster as roster

world, user, client_in = roster.world, roster.user, roster.client_in

pytestmark = pytest.mark.django_db
P, S = CashierType.PRIMARY, CashierType.SECONDARY


@pytest.fixture
def staff(world):
    def make(code, name):
        return Employee.objects.create(company=world["company"], employee_code=code, first_name=name,
                                       designation=world["cashier"])
    return {"a": make("C1", "Asha"), "b": make("C2", "Bilal"), "c": make("C3", "Chen")}


def day_ahead(n=2):
    return timezone.localdate() + datetime.timedelta(days=n)


def branches(world):
    return Branch.objects.filter(company=world["company"])


def put(world, user, employee, on_date, *, branch=None, role="cashier", kind="", day_type="working"):
    entry = {"date": on_date.isoformat(), "day_type": day_type, "role": role, "cashier_type": kind,
             "branch": (branch or world["branch"]).pk, "shift1_start": "08:00", "shift1_end": "16:00"}
    return services.assign_day(employee, entry, branches_qs=branches(world), user=user)


def kinds(world, on_date, branch=None):
    rows = DutyRoster.objects.filter(branch=branch or world["branch"], date=on_date).exclude(cashier_type="")
    return {r.employee.first_name: r.cashier_type for r in rows.select_related("employee")}


# -- The rules ---------------------------------------------------------------------------------


def test_the_first_cashier_is_primary_and_the_next_secondary(world, user, staff):
    day = day_ahead()
    put(world, user, staff["a"], day)
    put(world, user, staff["b"], day)

    assert kinds(world, day) == {"Asha": P, "Bilal": S}


def test_a_secondary_needs_a_primary(world, user, staff):
    with pytest.raises(services.Invalid) as refused:
        put(world, user, staff["a"], day_ahead(), kind="secondary")

    assert "Set a primary cashier" in refused.value.errors[0]["message"]
    assert not DutyRoster.objects.exists()


def test_choosing_primary_makes_the_old_primary_secondary(world, user, staff):
    day = day_ahead()
    put(world, user, staff["a"], day)
    put(world, user, staff["b"], day, kind="primary")

    assert kinds(world, day) == {"Asha": S, "Bilal": P}


def test_a_primary_can_not_demote_itself_alone(world, user, staff):
    day = day_ahead()
    put(world, user, staff["a"], day)

    with pytest.raises(services.Invalid):
        put(world, user, staff["a"], day, kind="secondary")
    assert kinds(world, day) == {"Asha": P}


@pytest.mark.parametrize("change", ["remove", "labour", "week_off", "move"])
def test_when_the_primary_goes_the_earliest_secondary_takes_over(world, user, staff, change):
    day = day_ahead()
    put(world, user, staff["a"], day)
    put(world, user, staff["b"], day)
    put(world, user, staff["c"], day)

    if change == "remove":
        services.remove_day(staff["a"], day, user=user)
    elif change == "labour":
        put(world, user, staff["a"], day, role="labour")
    elif change == "week_off":
        services.assign_day(staff["a"], {"date": day.isoformat(), "day_type": "week_off"},
                            branches_qs=branches(world), user=user)
    else:
        put(world, user, staff["a"], day, branch=world["other_branch"])

    assert kinds(world, day) == {"Bilal": P, "Chen": S}


def test_a_cleared_week_day_hands_over_primary(world, user, staff):
    day = day_ahead()
    put(world, user, staff["a"], day)
    put(world, user, staff["b"], day)

    services.save_week(staff["a"], [{"date": day.isoformat(), "day_type": "not_set"}], user=user,
                       branches_qs=branches(world))

    assert kinds(world, day) == {"Bilal": P}


def test_leave_on_a_primary_hands_over_and_converts_a_past_day(world, user, staff):
    past = timezone.localdate() - datetime.timedelta(days=1)
    for emp, kind in ((staff["a"], P), (staff["b"], S)):
        DutyRoster.objects.create(employee=emp, date=past, day_type=DutyRosterDayType.WORKING, role="cashier",
                                  cashier_type=kind, branch=world["branch"],
                                  shift1_start=timezone.now(), shift1_end=timezone.now() + datetime.timedelta(hours=8))
    request = LeaveRequest.objects.create(
        id=uuid.uuid4(), company=world["company"], employee=staff["a"], leave_type=_leave_type(world),
        from_date=past, to_date=past, days=1, reason="Sick", applied_at=timezone.now())

    leave._mark_roster(request, user)

    assert DutyRoster.objects.get(employee=staff["a"], date=past).day_type == DutyRosterDayType.ON_LEAVE
    assert kinds(world, past) == {"Bilal": P}


def _leave_type(world):
    from apps.crew.models import LeaveType

    return LeaveType.objects.create(company=world["company"], code="SL", name="Sick Leave")


def test_import_takes_primaries_first_whatever_the_row_order(world, user, staff):
    day = day_ahead()
    row = {"date": day.isoformat(), "day_type": "working", "role": "Cashier", "branch": world["branch"].pk,
           "shift1_start": "08:00", "shift1_end": "16:00"}
    written, results = services.import_rows(
        [{**row, "employee_code": "C2", "cashier_type": "Secondary"},
         {**row, "employee_code": "C1", "cashier_type": "Primary"}],
        user=user, employees_qs=Employee.objects.all(), branches_qs=branches(world))

    assert written == 2 and [r["row"] for r in results] == [1, 2]
    assert kinds(world, day) == {"Asha": P, "Bilal": S}


def test_the_database_allows_one_primary_per_branch_day(world, user, staff):
    day = day_ahead()
    put(world, user, staff["a"], day)

    from django.db import IntegrityError, transaction
    with pytest.raises(IntegrityError), transaction.atomic():
        DutyRoster.objects.create(employee=staff["b"], date=day, day_type=DutyRosterDayType.WORKING,
                                  role="cashier", cashier_type=P, branch=world["branch"],
                                  shift1_start=timezone.now(), shift1_end=timezone.now() + datetime.timedelta(hours=1))


def test_the_apps_see_the_cashier_type(world, user, staff):
    day = day_ahead()
    row = put(world, user, staff["a"], day)

    assert duty.day_json(row, day, None)["cashier_type"] == "primary"
    assert duty.day_json(None, day, None)["cashier_type"] is None


# -- The migration -----------------------------------------------------------------------------


def test_the_migration_makes_the_first_cashier_primary(world, staff):
    from django.apps import apps as live_apps

    day = day_ahead()
    for emp in (staff["a"], staff["b"]):
        DutyRoster.objects.create(employee=emp, date=day, day_type=DutyRosterDayType.WORKING, role="",
                                  branch=world["branch"], shift1_start=timezone.now(),
                                  shift1_end=timezone.now() + datetime.timedelta(hours=1))

    migration = importlib.import_module("apps.crew.migrations.0009_duty_roster_cashier_type")
    migration.fill(live_apps, None)

    assert kinds(world, day) == {"Asha": P, "Bilal": S}


# -- The past-day lock -------------------------------------------------------------------------


def test_a_past_day_is_read_only_on_the_web(world, user, staff):
    yesterday = timezone.localdate() - datetime.timedelta(days=1)

    with pytest.raises(services.Invalid) as refused:
        put(world, user, staff["a"], yesterday)
    assert "has passed" in refused.value.errors[0]["message"]
    with pytest.raises(services.Invalid):
        services.remove_day(staff["a"], yesterday, user=user)
    with pytest.raises(services.Invalid):
        services.save_week(staff["a"], [{"date": yesterday.isoformat(), "day_type": "week_off"}], user=user,
                           branches_qs=branches(world))
    _, results = services.import_rows(
        [{"employee_code": "C1", "date": yesterday.isoformat(), "day_type": "week_off"}],
        user=user, employees_qs=Employee.objects.all(), branches_qs=branches(world))
    assert results[0]["ok"] is False and "has passed" in results[0]["message"]


def test_today_is_still_editable(world, user, staff):
    assert put(world, user, staff["a"], timezone.localdate()).cashier_type == P


def test_the_business_day_start_decides_what_today_is(world):
    CompanyConfig.objects.create(company=world["company"], report_day_start=datetime.time(4, 0))
    zone = datetime.timezone(datetime.timedelta(hours=4))
    at_two = datetime.datetime(2026, 11, 14, 2, 0, tzinfo=zone)
    at_five = datetime.datetime(2026, 11, 14, 5, 0, tzinfo=zone)

    assert services.roster_today(world["company"], at_two) == datetime.date(2026, 11, 13)
    assert services.roster_today(world["company"], at_five) == datetime.date(2026, 11, 14)
