"""The reader protocol against the one real frame we have (the legacy
listener's comment, ServerSocket.cs:693) and against frames we build."""

import random
from datetime import datetime

import pytest

from apps.tracking import protocol as p

# 116 bytes, a tag-data upload from reader SIL_UHF_V171101, 4 Nov 2017.
LEGACY_FRAME = bytes.fromhex(
    "55aa007000040000118e0001000053494c5f5548465f56313731313031008b0100114120170732eaa6000000e4110b04"
    "0838038b01001141201707365c30000000d6110b040838038b0100114120170737aae1000000dd110b040838038b0100"
    "114120170736ace0000000e9110b040838036625"
)
WHEN = datetime(2026, 11, 6, 14, 30, 5)


def test_crc_of_the_legacy_frame():
    assert p.crc16(LEGACY_FRAME[2:-2]) == 0x6625
    p.check(LEGACY_FRAME)


def test_crc_known_answer():
    # CRC-16/CCITT-FALSE check value
    assert p.crc16(b"123456789") == 0x29B1


def test_the_legacy_frame_decodes():
    assert p.frame_size(LEGACY_FRAME[:4]) == 116
    frame = p.parse(LEGACY_FRAME)
    assert frame.kind == "tag_data"
    assert frame.device_id == "SIL_UHF_V171101"
    assert frame.head == LEGACY_FRAME[6:30]
    reads = p.tag_reads(frame.payload)
    assert [r.tag for r in reads] == ["170732EA", "1707365C", "170737AA", "170736AC"]
    assert [r.rssi for r in reads] == [-28, -42, -35, -23]
    first = reads[0]
    assert (first.port, first.staying, first.entry, first.low_battery, first.address) == (1, True, False, False, 0)
    assert first.reader_time == datetime(2017, 11, 4, 8, 56, 3)


def test_reply_matches_the_legacy_layout():
    frame = p.parse(LEGACY_FRAME)
    out = p.reply(frame, WHEN)
    # 55 AA | len | 80 04 | bytes 6-29 echoed | 00 | time | CRC
    assert out[:2] == p.HEADER
    assert out[4:6] == b"\x80\x04"
    assert out[6:30] == LEGACY_FRAME[6:30]
    assert out[30] == 0
    assert out[31:37] == bytes([26, 11, 6, 14, 30, 5])
    assert len(out) == p.frame_size(out[:4])
    p.check(out)


def test_register_reply_carries_ip_and_little_endian_port():
    frame = p.parse(p.build(p.REGISTER, "190619101016097", b""))
    out = p.reply(frame, WHEN, server_ip="97.74.81.23", server_port=5100)
    p.check(out)
    body = out[4:-2]
    ip = body[2 + 24 + 1 + 6:][:32]
    assert ip.rstrip(b"\0") == b"97.74.81.23" and len(ip) == 32
    assert body[-2:] == bytes([5100 & 0xFF, 5100 >> 8])


def test_build_round_trips_tag_records():
    reads = [p.TagRead(port=port, entry=port == 2, staying=port != 2, antenna_type=32, tag=f"0A0B0C0{port}",
                       address=0, low_battery=port == 3, rssi=-30 - port, reader_time=WHEN) for port in (1, 2, 3, 4)]
    frame = p.build(p.TAG_DATA, "190619101016097", b"".join(p.tag_record(r) for r in reads))
    p.check(frame)
    assert p.tag_reads(p.parse(frame).payload) == reads


def test_heartbeat_decodes():
    payload = bytes([1, 0x23, 0x11, 0x95, 0x02, 0x07]) + bytes([26, 11, 6, 9, 0, 0])
    hb = p.heartbeat(p.parse(p.build(p.HEARTBEAT, "190619101016097", payload)).payload)
    assert (hb.gprs, hb.port, hb.config_crc, hb.software) == (1, 3, 0x1195, 0x0207)
    assert hb.reader_time == datetime(2026, 11, 6, 9, 0, 0)


def test_a_reader_clock_that_is_not_a_date_gives_none():
    bad = p.TagRead(1, False, True, 32, "00000001", 0, False, -40, WHEN)
    record = bytearray(p.tag_record(bad))
    record[-4] = 0              # day 0
    assert p.tag_reads(bytes(record))[0].reader_time is None


@pytest.mark.parametrize("seed", range(20))
def test_any_chunking_gives_the_same_frames(seed):
    rng = random.Random(seed)
    other = p.build(p.HEARTBEAT, "190619101016097", bytes(12))
    stream = LEGACY_FRAME + other + LEGACY_FRAME
    frames, buffer = [], b""
    i = 0
    while i < len(stream):
        step = rng.randint(1, 40)
        buffer += stream[i:i + step]
        i += step
        got, buffer = p.split(buffer)
        frames += got
    assert frames == [LEGACY_FRAME, other, LEGACY_FRAME] and buffer == b""


def test_every_split_point_works():
    for cut in range(1, len(LEGACY_FRAME)):
        got, rest = p.split(LEGACY_FRAME[:cut])
        assert got == [] and rest == LEGACY_FRAME[:cut]
        got, rest = p.split(rest + LEGACY_FRAME[cut:])
        assert got == [LEGACY_FRAME] and rest == b""


def test_bad_crc_is_refused():
    broken = bytearray(LEGACY_FRAME)
    broken[40] ^= 0xFF
    with pytest.raises(p.FrameError, match="CRC"):
        p.check(bytes(broken))


@pytest.mark.parametrize("head", [b"\x00\x00\x00\x70", b"\x55\xaa\x00\x00", b"\x55\xaa\xff\xff"])
def test_bad_header_or_length_is_refused(head):
    with pytest.raises(p.FrameError):
        p.frame_size(head)


def test_a_cut_short_record_is_refused():
    payload = p.parse(LEGACY_FRAME).payload
    with pytest.raises(p.FrameError):
        p.tag_reads(payload[:-3])


def test_other_tlv_types_are_skipped():
    payload = bytes([0x12, 0x34, 0, 3, 9, 9, 9]) + p.parse(LEGACY_FRAME).payload
    assert len(p.tag_reads(payload)) == 4
