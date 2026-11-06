"""tracking_reading's daily partitions (apps/tracking/partitions.py)."""

from datetime import UTC, date, datetime, timedelta

import pytest
from django.db import connection
from django.utils import timezone

from apps.tracking import partitions

pytestmark = pytest.mark.django_db


def _insert(cursor, at):
    cursor.execute(
        "INSERT INTO tracking_reading (reader_id, port, tag, reader_time, received_at, rssi, entry, staying, low_battery)"
        " VALUES (1, 1, '0A0B0C0D', NULL, %s, -40, false, true, false)", [at])


def _home(cursor, at):
    cursor.execute("SELECT tableoid::regclass::text FROM tracking_reading WHERE received_at = %s", [at])
    return cursor.fetchone()[0]


def test_migration_makes_today_and_three_days_ahead():
    today = timezone.now().date()
    with connection.cursor() as cursor:
        have = partitions.existing(cursor)
    assert {today + timedelta(days=n) for n in range(4)} <= have


def test_a_row_lands_in_its_utc_day():
    today = timezone.now().date()
    at = datetime(today.year, today.month, today.day, 23, 59, 59, tzinfo=UTC)
    with connection.cursor() as cursor:
        _insert(cursor, at)
        assert _home(cursor, at) == partitions.name_for(today)


def test_out_of_range_rows_land_in_default():
    at = datetime(2001, 1, 1, tzinfo=UTC)
    with connection.cursor() as cursor:
        _insert(cursor, at)
        assert _home(cursor, at) == partitions.DEFAULT
        assert partitions.default_rows(cursor) == 1


def test_ensure_is_idempotent_and_makes_missing_days():
    start = date(2030, 1, 1)
    with connection.cursor() as cursor:
        assert partitions.ensure(cursor, start, ahead=2) == [start, start + timedelta(1), start + timedelta(2)]
        assert partitions.ensure(cursor, start, ahead=2) == []


def test_drop_older_keeps_the_retention():
    start = date(2030, 2, 1)
    with connection.cursor() as cursor:
        partitions.ensure(cursor, start, ahead=5)
        today = start + timedelta(days=5)
        dropped = partitions.drop_older(cursor, today, keep_days=3)
        have = partitions.existing(cursor)
    assert dropped[-1] == today - timedelta(days=3)
    assert {today, today - timedelta(1), today - timedelta(2)} <= have
    assert today - timedelta(3) not in have
