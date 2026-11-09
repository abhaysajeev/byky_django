"""The gateway's database access: psycopg 3 async, its own few connections,
plain SQL on tracking tables only -- the role it runs as (tracking_writer,
deploy/tracking-role.sql) cannot touch anything else.

No Django ORM here: the gateway is an asyncio process, and this keeps every
statement it can run visible in one file.
"""

import asyncio
import time
from datetime import timedelta
from typing import NamedTuple
from zoneinfo import ZoneInfo

import psycopg
from django.conf import settings
from django.db import connections

READER_STATUS = """
UPDATE tracking_reader SET
    last_ip = %(ip)s,
    first_seen_at = COALESCE(first_seen_at, %(now)s),
    last_heartbeat_at = CASE WHEN %(heartbeat)s THEN %(now)s ELSE last_heartbeat_at END,
    last_data_at = CASE WHEN %(data)s THEN %(now)s ELSE last_data_at END,
    firmware = CASE WHEN %(firmware)s <> '' THEN %(firmware)s ELSE firmware END
WHERE device_id = %(device)s
RETURNING status, enabled
"""

NEW_PENDING = """
INSERT INTO tracking_reader (device_id, name, status, enabled, firmware, last_ip, first_seen_at,
                             last_heartbeat_at, last_data_at, created_on)
VALUES (%(device)s, '', 'pending', false, %(firmware)s, %(ip)s, %(now)s,
        CASE WHEN %(heartbeat)s THEN %(now)s::timestamptz END, CASE WHEN %(data)s THEN %(now)s::timestamptz END, %(now)s)
ON CONFLICT (device_id) DO NOTHING
"""

RAW = """
INSERT INTO tracking_raw_frame (device_id, peer, received_at, data, crc_ok, note)
VALUES (%s, %s, %s, %s, %s, %s)
"""


# The reader of an upload, and whether its raw reads are kept (owner, 9 Nov 2026).
READER_FOR_UPLOAD = """
SELECT r.id, r.keep_raw OR EXISTS (
    SELECT 1 FROM tracking_calibration c WHERE c.reader_id = r.id AND c.status = 'open')
FROM tracking_reader r WHERE r.device_id = %s
"""

# touch(): a device we had no row for until now (treated as pending).
NEW_DEVICE = "new"

# Which reads of an upload are new: the dedupe key decides (E8: readers re-send).
NEW_KEYS = """
INSERT INTO tracking_reading_key (reader_id, port, tag, reader_time, received_at)
SELECT %(r)s, k.port, k.tag, k.reader_time, %(now)s
FROM unnest(%(ports)s::smallint[], %(tags)s::varchar[], %(times)s::timestamptz[]) AS k(port, tag, reader_time)
ON CONFLICT DO NOTHING
RETURNING port, tag, reader_time
"""

INSERT_READS = """
INSERT INTO tracking_reading (reader_id, port, tag, reader_time, received_at, rssi, entry, staying, low_battery)
SELECT %(r)s, x.port, x.tag, x.reader_time, %(now)s, x.rssi, x.entry, x.staying, x.low_battery
FROM unnest(%(ports)s::smallint[], %(tags)s::varchar[], %(times)s::timestamptz[], %(rssi)s::smallint[],
            %(entry)s::boolean[], %(staying)s::boolean[], %(lowbat)s::boolean[])
     AS x(port, tag, reader_time, rssi, entry, staying, low_battery)
"""

# Last seen moves only for new reads, and not for a batch older than what we
# have by more than 2 minutes (a late or replayed upload -- design §5).
LAST_SEEN = """
INSERT INTO tracking_tag_last_seen AS t (tag, reader_id, port, seen_at, reader_time, rssi, rssi_avg, low_battery)
SELECT x.tag, %(r)s, x.port, %(now)s, x.reader_time, x.rssi, x.rssi, x.low_battery
FROM unnest(%(tags)s::varchar[], %(ports)s::smallint[], %(times)s::timestamptz[], %(rssi)s::smallint[],
            %(lowbat)s::boolean[]) AS x(tag, port, reader_time, rssi, low_battery)
ON CONFLICT (tag) DO UPDATE SET
    reader_id = EXCLUDED.reader_id, port = EXCLUDED.port, seen_at = EXCLUDED.seen_at,
    reader_time = EXCLUDED.reader_time, rssi = EXCLUDED.rssi, low_battery = EXCLUDED.low_battery,
    rssi_avg = CASE WHEN t.reader_id = EXCLUDED.reader_id
                    THEN %(alpha)s * EXCLUDED.rssi + (1 - %(alpha)s) * t.rssi_avg
                    ELSE EXCLUDED.rssi END
WHERE EXCLUDED.reader_time IS NULL OR t.reader_time IS NULL
   OR EXCLUDED.reader_time >= t.reader_time - interval '2 minutes'
"""


# A reader's clock is only believed within these limits (E11: readers have
# reported 2070). Ahead of the server: as good as no clock. Older than the limit
# (a long-held backlog): stored, but it says nothing about where the bike is now.
CLOCK_AHEAD = timedelta(minutes=10)
CLOCK_TOO_OLD = timedelta(days=3)
BAD_CLOCK_EVENT_SECONDS = 3600
EWMA_ALPHA = 0.3                   # calibration replays the same smoothing


class Row(NamedTuple):
    port: int
    tag: str
    sent: object          # the reader's clock, as sent -- what tracking_reading keeps
    clock: object         # the clock we believe, or None
    rssi: int
    entry: bool
    staying: bool
    low_battery: bool


def dsn():
    """TRACKING_DB_URL, or the app's own database (dev, tests)."""
    if settings.TRACKING_DB_URL:
        return settings.TRACKING_DB_URL
    db = connections["default"].settings_dict
    return psycopg.conninfo.make_conninfo(dbname=db["NAME"], user=db["USER"], password=db["PASSWORD"],
                                          host=db["HOST"] or "localhost", port=db["PORT"] or 5432)


class Store:
    def __init__(self, conninfo=None, size=3, max_pending=50, reader_tz="Asia/Dubai", ewma_alpha=EWMA_ALPHA, events=None):
        self.conninfo = conninfo or dsn()
        self.reader_tz = reader_tz
        self.ewma_alpha = ewma_alpha
        self.size = size
        self.max_pending = max_pending
        self._pool = None
        self._made = 0
        self._lock = asyncio.Lock()
        self.events = events or (lambda name, **fields: None)
        self._clock_warned = {}

    # -- a tiny pool: `size` connections, handed out one at a time --------------------

    async def _get(self):
        async with self._lock:
            if self._pool is None:
                self._pool = asyncio.Queue()
            if self._pool.empty() and self._made < self.size:
                self._made += 1
                try:
                    return await psycopg.AsyncConnection.connect(self.conninfo, autocommit=True,
                                                                  application_name="rfid-gateway")
                except Exception:
                    self._made -= 1
                    raise
        return await self._pool.get()

    async def _put(self, conn, broken=False):
        if broken or conn.closed:
            self._made -= 1
            try:
                await conn.close()
            except Exception:
                pass
            return
        self._pool.put_nowait(conn)

    async def run(self, fn):
        """fn(conn) on a pooled connection; a failed connection is dropped, not reused."""
        conn = await self._get()
        try:
            result = await fn(conn)
        except (psycopg.OperationalError, psycopg.InterfaceError):
            await self._put(conn, broken=True)
            raise
        except Exception:
            if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
                await conn.rollback()
            await self._put(conn)
            raise
        await self._put(conn)
        return result

    async def close(self):
        if self._pool:
            while not self._pool.empty():
                await (self._pool.get_nowait()).close()

    # -- what the gateway asks ----------------------------------------------------------

    async def ping(self):
        async def go(conn):
            await conn.execute("SELECT 1")
            return True
        try:
            return await self.run(go)
        except Exception:
            return False

    async def touch(self, device, ip, kind, now, *, firmware=""):
        """Note that `device` was heard -> "accepted" (approved and on), "off"
        (approved, switched off), "pending", "new" (not known until this frame) or "blocked"."""
        args = {"device": device, "ip": ip, "now": now, "firmware": firmware or "",
                "heartbeat": kind in ("heartbeat", "login", "register"), "data": kind == "tag_data"}

        async def go(conn):
            row = await (await conn.execute(READER_STATUS, args)).fetchone()
            if row is None:
                pending = await (await conn.execute(
                    "SELECT count(*) FROM tracking_reader WHERE status = 'pending'")).fetchone()
                if pending[0] < self.max_pending:
                    await conn.execute(NEW_PENDING, args)
                return NEW_DEVICE
            status, enabled = row
            if status == "approved":
                return "accepted" if enabled else "off"
            return status
        return await self.run(go)

    def _warn_clock(self, device, count):
        """At most once an hour per reader."""
        at = time.monotonic()
        if at - self._clock_warned.get(device, -1e9) >= BAD_CLOCK_EVENT_SECONDS:
            self._clock_warned[device] = at
            self.events("reader_clock_ahead", device=device, reads=count)

    async def save_raw(self, device, ip, now, data, crc_ok, note):
        async def go(conn):
            await conn.execute(RAW, (device or "", ip or "", now, bytes(data), crc_ok, (note or "")[:200]))
        await self.run(go)

    async def save_reads(self, frame, reads, now):
        """Store one upload in ONE transaction; returns how many reads were new.
        The gateway ACKs only after this returns -- if it raises, nothing was
        committed and the reader sends the batch again (design §3)."""
        tz = ZoneInfo(self.reader_tz)
        rows = []
        ahead = 0
        for r in reads:
            sent = r.reader_time.replace(tzinfo=tz) if r.reader_time else None
            clock = sent
            if sent is not None and sent > now + CLOCK_AHEAD:
                clock, ahead = None, ahead + 1
            rows.append(Row(r.port, r.tag, sent, clock, r.rssi, r.entry, r.staying, r.low_battery))
        if ahead:
            self._warn_clock(frame.device_id, ahead)

        async def go(conn):
            async with conn.transaction():
                found = await (await conn.execute(READER_FOR_UPLOAD, [frame.device_id])).fetchone()
                if found is None:
                    return 0
                reader_id, keep_raw = found
                keyed = [row for row in rows if row.clock is not None]
                new_keys = set()
                if keyed:
                    cur = await conn.execute(NEW_KEYS, {
                        "r": reader_id, "now": now, "ports": [r.port for r in keyed],
                        "tags": [r.tag for r in keyed], "times": [r.clock for r in keyed]})
                    new_keys = {(port, tag, at) for port, tag, at in await cur.fetchall()}
                # a read with no usable reader clock cannot be deduped; it counts as new
                # (and a read repeated inside one upload is kept once)
                fresh, taken = [], set()
                for r in rows:
                    key = (r.port, r.tag, r.clock)
                    if r.clock is None or (key in new_keys and key not in taken):
                        fresh.append(r)
                        taken.add(key)
                if not fresh:
                    return 0
                if keep_raw:
                    await conn.execute(INSERT_READS, {
                        "r": reader_id, "now": now, "ports": [r.port for r in fresh], "tags": [r.tag for r in fresh],
                        "times": [r.sent for r in fresh], "rssi": [r.rssi for r in fresh],
                        "entry": [r.entry for r in fresh], "staying": [r.staying for r in fresh],
                        "lowbat": [r.low_battery for r in fresh]})
                # per tag, the strongest new read stands for the batch (the strongest
                # antenna is the presence feature, design §6)
                best = {}
                too_old = now - CLOCK_TOO_OLD
                for r in fresh:
                    if r.clock is not None and r.clock < too_old:
                        continue
                    if r.tag not in best or r.rssi > best[r.tag].rssi:
                        best[r.tag] = r
                # in tag order, so two readers hearing the same tags cannot deadlock
                b = [best[tag] for tag in sorted(best)]
                if b:
                    await conn.execute(LAST_SEEN, {
                        "r": reader_id, "now": now, "tags": [r.tag for r in b], "ports": [r.port for r in b],
                        "times": [r.clock for r in b], "rssi": [r.rssi for r in b],
                        "lowbat": [r.low_battery for r in b], "alpha": self.ewma_alpha})
                return len(fresh)
        return await self.run(go)
