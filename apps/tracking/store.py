"""The gateway's database access: psycopg 3 async, its own few connections,
plain SQL on tracking tables only -- the role it runs as (tracking_writer,
deploy/tracking-role.sql) cannot touch anything else.

No Django ORM here: the gateway is an asyncio process, and this keeps every
statement it can run visible in one file.
"""

import asyncio

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


def dsn():
    """TRACKING_DB_URL, or the app's own database (dev, tests)."""
    if settings.TRACKING_DB_URL:
        return settings.TRACKING_DB_URL
    db = connections["default"].settings_dict
    return psycopg.conninfo.make_conninfo(dbname=db["NAME"], user=db["USER"], password=db["PASSWORD"],
                                          host=db["HOST"] or "localhost", port=db["PORT"] or 5432)


class Store:
    def __init__(self, conninfo=None, size=3, max_pending=50):
        self.conninfo = conninfo or dsn()
        self.size = size
        self.max_pending = max_pending
        self._pool = None
        self._made = 0
        self._lock = asyncio.Lock()

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
        (approved, switched off), "pending" or "blocked"."""
        args = {"device": device, "ip": ip, "now": now, "firmware": firmware or "",
                "heartbeat": kind in ("heartbeat", "login", "register"), "data": kind == "tag_data"}

        async def go(conn):
            row = await (await conn.execute(READER_STATUS, args)).fetchone()
            if row is None:
                pending = await (await conn.execute(
                    "SELECT count(*) FROM tracking_reader WHERE status = 'pending'")).fetchone()
                if pending[0] < self.max_pending:
                    await conn.execute(NEW_PENDING, args)
                return "pending"
            status, enabled = row
            if status == "approved":
                return "accepted" if enabled else "off"
            return status
        return await self.run(go)

    async def save_raw(self, device, ip, now, data, crc_ok, note):
        async def go(conn):
            await conn.execute(RAW, (device or "", ip or "", now, bytes(data), crc_ok, (note or "")[:200]))
        await self.run(go)

    async def save_reads(self, frame, reads, now):
        raise NotImplementedError("write mode arrives with slice 5")
