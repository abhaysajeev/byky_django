"""Daily partitions of tracking_reading (design §4): made ahead, dropped after
the retention -- dropping a partition is instant and leaves no bloat, where a
DELETE of millions of rows is neither (TigerData, Crunchy pg_partman).

Partition days are UTC days: tracking_reading_p20261106 holds received_at in
[2026-11-06 00:00 UTC, 2026-11-07 00:00 UTC). A row outside every partition
lands in tracking_reading_default, which should stay empty (the engine warns).
"""

import re
from datetime import date, timedelta

PARENT = "tracking_reading"
DEFAULT = "tracking_reading_default"
_NAME = re.compile(r"^tracking_reading_p(\d{8})$")

CREATE_PARENT = f"""
CREATE TABLE IF NOT EXISTS {PARENT} (
    reader_id   bigint      NOT NULL,
    port        smallint    NOT NULL,
    tag         varchar(8)  NOT NULL,
    reader_time timestamptz NULL,
    received_at timestamptz NOT NULL,
    rssi        smallint    NOT NULL,
    entry       boolean     NOT NULL,
    staying     boolean     NOT NULL,
    low_battery boolean     NOT NULL
) PARTITION BY RANGE (received_at);
CREATE INDEX IF NOT EXISTS tracking_reading_tag_at ON {PARENT} (tag, received_at DESC);
CREATE INDEX IF NOT EXISTS tracking_reading_reader_at ON {PARENT} (reader_id, received_at DESC);
CREATE TABLE IF NOT EXISTS {DEFAULT} PARTITION OF {PARENT} DEFAULT;
"""

DROP_PARENT = f"DROP TABLE IF EXISTS {PARENT} CASCADE;"


def name_for(day):
    return f"tracking_reading_p{day:%Y%m%d}"


def existing(cursor):
    """The days that have a partition."""
    cursor.execute(
        "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
        "JOIN pg_class p ON p.oid = i.inhparent WHERE p.relname = %s", [PARENT])
    days = set()
    for (name,) in cursor.fetchall():
        m = _NAME.match(name)
        if m:
            s = m.group(1)
            days.add(date(int(s[:4]), int(s[4:6]), int(s[6:])))
    return days


def repair_default(cursor):
    """Move rows that landed in the default partition into their own day's.

    `CREATE TABLE ... PARTITION OF` refuses while the default holds rows for
    that day, which would stop every partition being made from then on: a
    capture period longer than the days made ahead could cause exactly that.
    Returns the days moved."""
    cursor.execute(f"LOCK TABLE {DEFAULT} IN ACCESS EXCLUSIVE MODE")
    cursor.execute(f"SELECT DISTINCT (received_at AT TIME ZONE 'UTC')::date FROM {DEFAULT} ORDER BY 1")
    days = [row[0] for row in cursor.fetchall()]
    have = existing(cursor)
    for day in days:
        name = name_for(day)
        low, high = f"{day:%Y-%m-%d} 00:00+00", f"{day + timedelta(days=1):%Y-%m-%d} 00:00+00"
        if day not in have:
            cursor.execute(f"CREATE TABLE {name} (LIKE {PARENT} INCLUDING DEFAULTS INCLUDING CONSTRAINTS)")
        cursor.execute(
            f"WITH moved AS (DELETE FROM {DEFAULT} WHERE received_at >= %s AND received_at < %s RETURNING *) "
            f"INSERT INTO {name} SELECT * FROM moved", [low, high])
        if day not in have:
            cursor.execute(f"ALTER TABLE {PARENT} ATTACH PARTITION {name} FOR VALUES FROM ('{low}') TO ('{high}')")
    return days


def ensure(cursor, today, ahead=3):
    """Partitions for today .. today + ahead (UTC days). Returns the ones made."""
    have = existing(cursor)
    made = []
    for n in range(ahead + 1):
        day = today + timedelta(days=n)
        if day in have:
            continue
        cursor.execute(
            f"CREATE TABLE IF NOT EXISTS {name_for(day)} PARTITION OF {PARENT} "
            f"FOR VALUES FROM ('{day:%Y-%m-%d} 00:00+00') TO ('{day + timedelta(days=1):%Y-%m-%d} 00:00+00')")
        made.append(day)
    return made


def drop_older(cursor, today, keep_days):
    """Drop partitions entirely older than `keep_days` (today counts as one).
    Returns the days dropped."""
    cutoff = today - timedelta(days=keep_days - 1)
    dropped = []
    for day in sorted(existing(cursor)):
        if day < cutoff:
            cursor.execute(f"DROP TABLE IF EXISTS {name_for(day)}")
            dropped.append(day)
    return dropped


def default_rows(cursor):
    cursor.execute(f"SELECT count(*) FROM {DEFAULT}")
    return cursor.fetchone()[0]
