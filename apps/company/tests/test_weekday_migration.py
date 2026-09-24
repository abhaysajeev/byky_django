"""0008_weekday_monday_zero moves saved working times from Sunday=0 to
Python's Monday=0, and back.

A Sunday and a Monday row on the same branch and shift are the case a single
in-place UPDATE would trip over: PostgreSQL checks the (branch, week_day,
shift_number) unique constraint row by row, so Monday 1 -> 0 would collide
with Sunday still at 0. The migration parks the values first; this proves it.

Runs inside the test's own transaction -- PostgreSQL rolls schema changes back
too -- so unapplying 0008 (and whatever depends on it, e.g. the fare tables)
never leaks into the next test, and no flush wipes the pages portal's
migrations seed.
"""

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from apps.company.models import BranchWorkingTime, WeekDay
from apps.company.tests.test_crud import world  # noqa: F401 -- fixture
from apps.company.tests.test_working_time import shift, station  # noqa: F401 -- fixture

BEFORE = [("company", "0007_branch_code_per_company")]


def migrate(target=None):
    """To `target`, or with no target forward to every app's latest."""
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(target or executor.loader.graph.leaf_nodes())


def days_by_shift(branch):
    return dict(
        BranchWorkingTime.objects.filter(branch=branch).values_list("start_time", "week_day")
    )


@pytest.mark.django_db
def test_every_day_moves_both_ways_without_colliding(station):
    # One row per weekday, shift 1, each with its own start hour so it can be
    # told apart after the move: Monday 01:00, Tuesday 02:00 ... Sunday 07:00.
    for day in WeekDay:
        shift(station, day, 1, f"{day.value + 1:02d}:00", "23:00")
    after = days_by_shift(station)
    with connection.cursor() as cursor:
        # Fire the pending deferred foreign-key checks now: Postgres refuses to
        # alter a table that still has them queued in this transaction.
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")

    migrate(BEFORE)
    before = days_by_shift(station)
    for start, new_value in after.items():
        assert before[start] == (new_value + 1) % 7      # Sunday back to 0, Monday to 1 ...

    migrate()
    assert days_by_shift(station) == after
