"""The gateway in capture mode, over real TCP, against the real test database
(transactional: the gateway writes on its own connections)."""

import asyncio
from datetime import datetime

import pytest

from apps.tracking import protocol as p
from apps.tracking.gateway import Config, Gateway
from apps.tracking.models import RawFrame, Reader, ReaderStatus
from apps.tracking.store import Store
from apps.tracking.tests.test_protocol import LEGACY_FRAME

pytestmark = pytest.mark.django_db(transaction=True)

DEVICE = "190619101016097"
WHEN = datetime(2026, 11, 6, 10, 0, 0)


def tag_frame(device=DEVICE, tag="0A0B0C01"):
    read = p.TagRead(1, False, True, 32, tag, 0, False, -40, WHEN)
    return p.build(p.TAG_DATA, device, p.tag_record(read))


def heartbeat_frame(device=DEVICE):
    return p.build(p.HEARTBEAT, device, bytes([1, 1, 0x11, 0x95, 2, 7]) + bytes([26, 11, 6, 10, 0, 0]))


async def _session(frames, *, enabled=True, mode="capture", read_replies=None, **config):
    """Start a gateway on a free port, send `frames`, collect what comes back."""
    store = Store(size=2)
    gateway = await Gateway(Config(port=0, enabled=enabled, mode=mode, **config), store).start()
    replies = []
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", gateway.port)
        for frame in frames:
            writer.write(frame)
            await writer.drain()
            try:
                head = await asyncio.wait_for(reader.readexactly(4), 1.0)
                replies.append(head + await reader.readexactly(p.frame_size(head) - 4))
            except (TimeoutError, asyncio.IncompleteReadError):
                replies.append(None)
        writer.close()
        await asyncio.sleep(0.05)
    finally:
        await gateway.close()
        await store.close()
    return replies


def run(frames, **kwargs):
    return asyncio.run(_session(frames, **kwargs))


def approve(device=DEVICE, enabled=True, company=None):
    from apps.company.models import Company
    company = company or Company.objects.first()
    Reader.objects.update_or_create(device_id=device, defaults={
        "keep_raw": True,
        "status": ReaderStatus.APPROVED, "enabled": enabled, "company": company})


def test_unknown_reader_becomes_pending_and_its_tag_data_is_not_acked():
    replies = run([heartbeat_frame(), tag_frame()])
    reader = Reader.objects.get(device_id=DEVICE)
    assert reader.status == ReaderStatus.PENDING and reader.firmware == "2.7"
    assert reader.last_heartbeat_at is not None
    assert replies[0] is not None and replies[0][4:6] == b"\x80\x03"   # heartbeat answered
    assert replies[1] is None                                          # tag data kept on the reader


def test_approved_reader_gets_an_exact_ack_and_the_frame_is_captured(world):
    approve(company=world["company"])
    frame = tag_frame()
    replies = run([frame])
    ack = replies[0]
    p.check(ack)
    assert ack[4:6] == b"\x80\x04" and ack[6:30] == frame[6:30] and ack[30] == 0
    raw = RawFrame.objects.get()
    assert bytes(raw.data) == frame and raw.crc_ok and raw.device_id == DEVICE and raw.note == "tag_data"


def test_the_legacy_frame_is_answered_like_the_legacy(world):
    approve("SIL_UHF_V171101", company=world["company"])
    ack = run([LEGACY_FRAME])[0]
    assert ack[4:30] == b"\x80\x04" + LEGACY_FRAME[6:30]


def test_switched_off_reader_is_not_acked(world):
    approve(enabled=False, company=world["company"])
    assert run([tag_frame()]) == [None]


def test_blocked_reader_is_closed(world):
    Reader.objects.create(device_id=DEVICE, status=ReaderStatus.BLOCKED, company=world["company"])
    assert run([heartbeat_frame()]) == [None]


def test_bad_crc_closes_and_is_kept_as_a_bad_frame(world):
    approve(company=world["company"])
    broken = bytearray(tag_frame())
    broken[35] ^= 0xFF
    replies = run([heartbeat_frame(), bytes(broken)])
    assert replies[0] is not None and replies[1] is None
    raw = RawFrame.objects.get(crc_ok=False)
    assert "CRC" in raw.note


def test_a_bad_first_frame_from_an_unknown_sender_stores_nothing(world):
    approve(company=world["company"])
    broken = bytearray(tag_frame())
    broken[35] ^= 0xFF
    assert run([bytes(broken)]) == [None]
    assert not RawFrame.objects.exists()


def test_garbage_header_closes_without_reading_further():
    assert run([b"GET / HTTP/1.1\r\n\r\n"]) == [None]
    assert not Reader.objects.exists()


def test_frames_split_and_merged_on_the_wire(world):
    approve(company=world["company"])

    async def go():
        store = Store(size=2)
        gateway = await Gateway(Config(port=0, enabled=True), store).start()
        reader, writer = await asyncio.open_connection("127.0.0.1", gateway.port)
        stream = tag_frame(tag="00000001") + tag_frame(tag="00000002")
        for i in range(0, len(stream), 7):            # dribbled 7 bytes at a time
            writer.write(stream[i:i + 7])
            await writer.drain()
            await asyncio.sleep(0.005)
        got = []
        for _ in range(2):
            head = await asyncio.wait_for(reader.readexactly(4), 2)
            got.append(head + await reader.readexactly(p.frame_size(head) - 4))
        writer.close()
        await gateway.close()
        await store.close()
        return got

    acks = asyncio.run(go())
    assert [a[4:6] for a in acks] == [b"\x80\x04", b"\x80\x04"]
    assert RawFrame.objects.count() == 2


def test_disabled_gateway_opens_no_port():
    async def go():
        gateway = await Gateway(Config(port=0, enabled=False), Store(size=1)).start()
        return gateway.port
    assert asyncio.run(go()) is None


def test_connection_limit_per_ip(world):
    approve(company=world["company"])

    async def go():
        store = Store(size=2)
        gateway = await Gateway(Config(port=0, enabled=True, max_per_ip=1), store).start()
        first = await asyncio.open_connection("127.0.0.1", gateway.port)
        await asyncio.sleep(0.05)
        second_r, second_w = await asyncio.open_connection("127.0.0.1", gateway.port)
        closed = await asyncio.wait_for(second_r.read(1), 1) == b""
        first[1].close()
        second_w.close()
        await gateway.close()
        await store.close()
        return closed
    assert asyncio.run(go())


def test_health_reports_the_database_and_counters():
    async def go():
        store = Store(size=1)
        gateway = await Gateway(Config(port=0, enabled=False, health_port=0), store).start()
        # health on a free port: start it by hand on port 0
        server = await asyncio.start_server(gateway._health_client, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        await writer.drain()
        body = await reader.read()
        server.close()
        await store.close()
        return body
    body = asyncio.run(go())
    assert body.startswith(b"HTTP/1.1 200 OK")
    assert b'"db": true' in body and b'"enabled": false' in body


def test_tracking_reader_command_approves_assigns_and_switches(world):
    from django.core.management import call_command

    from apps.tracking.models import Antenna, ReaderAssignment
    world["adc1"].refresh_from_db()
    call_command("tracking_reader", "approve", DEVICE, company="BYKY", branch="ADC1")
    reader = Reader.objects.get(device_id=DEVICE)
    assert reader.status == ReaderStatus.APPROVED and reader.enabled and reader.company == world["company"]
    assert ReaderAssignment.objects.get(reader=reader, to_at__isnull=True).branch == world["adc1"]
    assert Antenna.objects.filter(reader=reader).count() == 4
    call_command("tracking_reader", "off", DEVICE)
    reader.refresh_from_db()
    assert not reader.enabled
