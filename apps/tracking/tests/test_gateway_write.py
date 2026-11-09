"""The gateway in write mode: reads stored once, ACK only after the commit,
replays never refresh last-seen (design §3, §5; E8)."""

import asyncio
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg
import pytest
from django.db import connection
from django.utils import timezone

from apps.tracking import protocol as p
from apps.tracking.gateway import Config, Gateway
from apps.tracking.grants import grants_for
from apps.tracking.models import Calibration, Reader, ReadingKey, TagLastSeen
from apps.tracking.store import Store, dsn
from apps.tracking.tests.test_gateway_capture import DEVICE, approve

pytestmark = pytest.mark.django_db(transaction=True)

# The reader's clock: UAE time, no zone, and recent enough to be believed.
T0 = datetime.now(ZoneInfo("Asia/Dubai")).replace(tzinfo=None, microsecond=0) - timedelta(hours=1)


def upload(*reads):
    return p.build(p.TAG_DATA, DEVICE, b"".join(p.tag_record(r) for r in reads))


def read(tag="0A0B0C01", port=1, rssi=-40, at=T0, low=False):
    return p.TagRead(port, False, True, 32, tag, 0, low, rssi, at)


def readings():
    with connection.cursor() as cursor:
        cursor.execute("SELECT tag, port, rssi FROM tracking_reading ORDER BY tag, port, rssi")
        return cursor.fetchall()


async def _send(frames, store=None):
    store = store or Store(size=2)
    gateway = await Gateway(Config(port=0, enabled=True, mode="write"), store).start()
    replies = []
    reader, writer = await asyncio.open_connection("127.0.0.1", gateway.port)
    try:
        for frame in frames:
            writer.write(frame)
            await writer.drain()
            try:
                head = await asyncio.wait_for(reader.readexactly(4), 1.0)
                replies.append(head + await reader.readexactly(p.frame_size(head) - 4))
            except (TimeoutError, asyncio.IncompleteReadError):
                replies.append(None)
    finally:
        writer.close()
        await asyncio.sleep(0.05)
        await gateway.close()
        await store.close()
    return replies


def send(*frames, store=None):
    return asyncio.run(_send(list(frames), store))


def test_an_upload_is_stored_then_acked(world):
    approve(company=world["company"])
    acks = send(upload(read(port=1, rssi=-40), read(port=2, rssi=-35), read(tag="0A0B0C02", rssi=-50)))
    assert acks[0] is not None and acks[0][4:6] == b"\x80\x04"
    assert readings() == [("0A0B0C01", 1, -40), ("0A0B0C01", 2, -35), ("0A0B0C02", 1, -50)]
    seen = TagLastSeen.objects.get(tag="0A0B0C01")
    assert (seen.port, seen.rssi, seen.rssi_avg) == (2, -35, -35)      # the strongest antenna


def test_a_resent_upload_is_acked_but_stored_once(world):
    approve(company=world["company"])
    frame = upload(read(), read(tag="0A0B0C02"))
    acks = send(frame, frame, frame)
    assert all(a is not None for a in acks)
    assert len(readings()) == 2 and ReadingKey.objects.count() == 2


def test_a_read_repeated_inside_one_upload_is_kept_once(world):
    approve(company=world["company"])
    send(upload(read(), read()))
    assert len(readings()) == 1


def test_a_replayed_old_batch_does_not_refresh_last_seen(world):
    approve(company=world["company"])
    send(upload(read(at=T0, rssi=-40)))
    first = TagLastSeen.objects.get()
    # a batch the reader kept from 5 minutes earlier, never sent before
    send(upload(read(at=T0 - timedelta(minutes=5), rssi=-30)))
    after = TagLastSeen.objects.get()
    assert after.seen_at == first.seen_at and after.rssi == -40
    assert len(readings()) == 2                           # the read itself is kept


def test_ewma_moves_toward_new_reads(world):
    approve(company=world["company"])
    send(upload(read(at=T0, rssi=-40)), upload(read(at=T0 + timedelta(seconds=30), rssi=-60)))
    seen = TagLastSeen.objects.get()
    assert seen.rssi == -60 and seen.rssi_avg == pytest.approx(0.3 * -60 + 0.7 * -40)


def test_low_battery_is_kept(world):
    approve(company=world["company"])
    send(upload(read(low=True)))
    assert TagLastSeen.objects.get().low_battery


def test_a_failed_save_is_not_acked(world):
    approve(company=world["company"])

    class Broken(Store):
        async def save_reads(self, frame, reads, now):
            raise psycopg.OperationalError("database went away")

    assert send(upload(read()), store=Broken(size=1)) == [None]
    assert readings() == []


def test_after_a_failure_the_resend_is_stored_once(world):
    approve(company=world["company"])
    frame = upload(read())

    class OnceBroken(Store):
        failed = False

        async def save_reads(self, frame, reads, now):
            if not OnceBroken.failed:
                OnceBroken.failed = True
                raise psycopg.OperationalError("blip")
            return await super().save_reads(frame, reads, now)

    assert send(frame, store=OnceBroken(size=1)) == [None]     # connection closed, no ACK
    assert send(frame, store=OnceBroken(size=1))[0] is not None
    assert len(readings()) == 1


def test_the_writer_role_cannot_touch_business_tables(world):
    """deploy/tracking-role.sql's grants, on a throw-away role."""
    role = f"tw_test_{uuid.uuid4().hex[:8]}"
    with connection.cursor() as cursor:
        try:
            cursor.execute(f"CREATE ROLE {role} LOGIN PASSWORD 'x'")
        except Exception:
            pytest.skip("this database user may not create roles")
        cursor.execute(grants_for(role))
    try:
        info = psycopg.conninfo.conninfo_to_dict(dsn())
        info.update(user=role, password="x")
        with psycopg.connect(**info, autocommit=True) as conn:
            conn.execute("SELECT count(*) FROM tracking_reading_key")          # allowed
            for table in ("vehicle", '"order"', '"user"', "customer"):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    conn.execute(f"SELECT 1 FROM {table} LIMIT 1")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("DELETE FROM tracking_reader")
    finally:
        with connection.cursor() as cursor:
            cursor.execute(f"DROP OWNED BY {role}")
            cursor.execute(f"DROP ROLE {role}")


# -- the reader's clock is not to be trusted (E11) -----------------------------------------

def test_a_clock_in_2070_does_not_freeze_the_bike(world):
    approve(company=world["company"])
    send(upload(read(at=datetime(2070, 1, 1))))
    first = TagLastSeen.objects.get(tag="0A0B0C01")
    assert first.reader_time is None

    send(upload(read(rssi=-30)))

    assert TagLastSeen.objects.get(tag="0A0B0C01").seen_at > first.seen_at


def test_the_sent_clock_is_kept_on_the_reading_for_debugging(world):
    approve(company=world["company"])
    send(upload(read(at=datetime(2070, 1, 1))))
    with connection.cursor() as cursor:
        cursor.execute("SELECT extract(year FROM reader_time) FROM tracking_reading")
        assert int(cursor.fetchone()[0]) in (2069, 2070)


def test_a_backlog_too_old_is_stored_but_says_nothing_about_now(world):
    approve(company=world["company"])
    send(upload(read(at=T0 - timedelta(days=5))))
    assert len(readings()) == 1
    assert not TagLastSeen.objects.exists()


# -- the raw switch (owner, 9 Nov 2026) ----------------------------------------------------

def test_with_the_switch_off_only_last_seen_is_kept(world):
    approve(company=world["company"])
    Reader.objects.update(keep_raw=False)
    send(upload(read()))
    assert readings() == []
    assert TagLastSeen.objects.filter(tag="0A0B0C01").exists() and ReadingKey.objects.count() == 1


def test_an_open_calibration_keeps_raw_reads_whatever_the_switch(world):
    approve(company=world["company"])
    Reader.objects.update(keep_raw=False)
    Calibration.objects.create(reader=Reader.objects.get(), tags=["0A0B0C01"], started_at=timezone.now())
    send(upload(read()))
    assert len(readings()) == 1
