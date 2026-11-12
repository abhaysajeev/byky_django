"""The Marktrace reader's TCP protocol: framing, CRC, decoding, replies.

Pure functions, no I/O -- the gateway (apps/tracking/gateway.py) reads bytes
off the socket and hands them here.

Every layout below is taken from the legacy listener,
source/legacy-aspnet/ERP/RMS.Socket/ServerSocket.cs, and checked against the
real tag-data frame kept in its comment at line 693 (design/07-tracking.md
§1.1): the CRC of that frame verifies, and it decodes to four tag records.

    0-1    55 AA                     header
    2-3    length, big-endian        the frame is length + 4 bytes
    4-5    command                   0x0008 register, 0x0001 login,
                                     0x0003 heartbeat, 0x0004 tag data
    6-9    message sequence number   } echoed back in every reply
    10-11  protocol version          } (GetReplyFor*)
    12-13  security flag             }
    14-29  device id                 16 bytes ASCII, NUL padded
    30..   payload
    n-2    CRC-16/CCITT-FALSE, big-endian, over bytes [2 : n-2]

Not reproduced from the legacy (design §1.1, F1-F3):
  - one TCP read was taken as one frame; here the stream is split by length;
  - an unrecognised frame still got an (empty) reply; here it gets none;
  - the reply time was the server's clock shifted by -90 min; here the caller
    passes the time to send (UAE time, gateway.py).
"""

from dataclasses import dataclass
from datetime import datetime

HEADER = b"\x55\xaa"
HEAD_SIZE = 30                # header .. device id
MIN_FRAME = HEAD_SIZE + 2     # + CRC, no payload
MAX_FRAME = 4096              # a reader's largest batch is far below this (design §2)

REGISTER = 0x0008
LOGIN = 0x0001
HEARTBEAT = 0x0003
TAG_DATA = 0x0004
COMMANDS = {REGISTER: "register", LOGIN: "login", HEARTBEAT: "heartbeat", TAG_DATA: "tag_data"}

TAG_TLV = 0x8B01              # the type of a tag record (sample frame)
TAG_BODY = 17                 # its TLV length: the record is 4 + 17 = 21 bytes


class FrameError(ValueError):
    """The byte stream is not a valid frame. The connection is closed: once the
    framing is lost, nothing after it can be trusted."""


# -- CRC (ERP.Utils/CRCLib.cs: table-driven CCITT, poly 0x1021, init 0xFFFF) ----------

def _table():
    table = []
    for i in range(256):
        c = i << 8
        for _ in range(8):
            c = ((c << 1) ^ 0x1021) & 0xFFFF if c & 0x8000 else (c << 1) & 0xFFFF
        table.append(c)
    return tuple(table)


_TABLE = _table()


def crc16(data):
    crc = 0xFFFF
    for b in data:
        crc = ((crc << 8) & 0xFFFF) ^ _TABLE[((crc >> 8) ^ b) & 0xFF]
    return crc


# -- framing --------------------------------------------------------------------------

def frame_size(head):
    """The full size of the frame whose first 4 bytes are `head`."""
    if head[:2] != HEADER:
        raise FrameError(f"bad header {head[:2].hex()}")
    size = ((head[2] << 8) | head[3]) + 4
    if size < MIN_FRAME or size > MAX_FRAME:
        raise FrameError(f"bad length {size}")
    return size


def check(frame):
    """`frame` (exactly one, as sized by frame_size) has a valid CRC."""
    want = (frame[-2] << 8) | frame[-1]
    got = crc16(frame[2:-2])
    if got != want:
        raise FrameError(f"bad CRC {got:04x} != {want:04x}")


def split(buffer):
    """Whole frames at the start of `buffer` -> (frames, rest). For tests and the
    simulator; the gateway reads frame by frame with readexactly."""
    frames = []
    view = bytes(buffer)
    while len(view) >= 4:
        size = frame_size(view[:4])
        if len(view) < size:
            break
        frame, view = view[:size], view[size:]
        check(frame)
        frames.append(frame)
    return frames, view


# -- decoding -------------------------------------------------------------------------

@dataclass(frozen=True)
class Frame:
    command: int
    device_id: str            # "190619101016089"
    head: bytes               # bytes 6-29, echoed back in the reply
    payload: bytes

    @property
    def kind(self):
        return COMMANDS.get(self.command, "unknown")


@dataclass(frozen=True)
class TagRead:
    port: int                 # antenna 1-4
    entry: bool               # bit 7 of the antenna byte
    staying: bool             # bit 6
    antenna_type: int
    tag: str                  # 8 upper-case hex characters
    address: int              # 2 bytes; 0 in every record seen so far (design §6 R6)
    low_battery: bool         # 0x01 = low
    rssi: int                 # signed dBm (ServerSocket.cs:907, ToSByte)
    reader_time: datetime | None   # as the reader sent it; None if not a real date


@dataclass(frozen=True)
class Heartbeat:
    gprs: int
    port: int                 # reporting antenna, low nibble
    config_crc: int
    software: int             # e.g. 0x0207 = V2.7
    reader_time: datetime | None


def _time(six):
    try:
        return datetime(2000 + six[0], six[1], six[2], six[3], six[4], six[5])
    except ValueError:
        return None


def parse(frame):
    """A checked frame -> Frame."""
    if len(frame) < MIN_FRAME:
        raise FrameError("frame too short")
    device = frame[14:30].split(b"\0", 1)[0]
    try:
        device_id = device.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise FrameError("device id is not ASCII") from exc
    return Frame(command=(frame[4] << 8) | frame[5], device_id=device_id,
                 head=bytes(frame[6:30]), payload=bytes(frame[HEAD_SIZE:-2]))


def tag_reads(payload):
    """The tag records of a tag-data payload. A TLV of another type is skipped by
    its length; a record cut short raises -- the CRC passed, so the reader
    really sent it, and we would rather see it than guess."""
    reads = []
    i = 0
    while i < len(payload):
        if len(payload) - i < 4:
            raise FrameError(f"{len(payload) - i} stray bytes after the records")
        kind = (payload[i] << 8) | payload[i + 1]
        size = (payload[i + 2] << 8) | payload[i + 3]
        body = payload[i + 4:i + 4 + size]
        if len(body) < size:
            raise FrameError("tag record cut short")
        i += 4 + size
        if kind != TAG_TLV:
            continue
        if size < TAG_BODY:
            raise FrameError(f"tag record of {size} bytes")
        ant = body[0]
        rssi = body[10] - 256 if body[10] > 127 else body[10]
        reads.append(TagRead(port=ant & 0x0F, entry=bool(ant & 0x80), staying=bool(ant & 0x40),
                             antenna_type=body[1], tag=body[2:6].hex().upper(),
                             address=(body[7] << 8) | body[8], low_battery=body[9] == 0x01,
                             rssi=rssi, reader_time=_time(body[11:17])))
    return reads


def heartbeat(payload):
    if len(payload) < 12:
        raise FrameError("heartbeat too short")
    return Heartbeat(gprs=payload[0], port=payload[1] & 0x0F, config_crc=(payload[2] << 8) | payload[3],
                     software=(payload[4] << 8) | payload[5], reader_time=_time(payload[6:12]))


# -- replies (ServerSocket.cs GetReplyFor*, OnDataReceived) ---------------------------

def _wrap(body):
    length = len(body) + 2
    inner = bytes([length >> 8, length & 0xFF]) + body
    crc = crc16(inner)
    return HEADER + inner + bytes([crc >> 8, crc & 0xFF])


def _time_bytes(when):
    return bytes([when.year - 2000, when.month, when.day, when.hour, when.minute, when.second])


def reply(frame, when, *, server_ip="", server_port=0):
    """The reply to `frame`: command | 0x80, the echoed bytes 6-29, status 00
    (success / no operation), the time, and -- for a register -- the server's
    IP (ASCII, 32 bytes) and port (little-endian, as the legacy sent it)."""
    body = bytes([0x80 | (frame.command >> 8), frame.command & 0xFF]) + frame.head + b"\x00" + _time_bytes(when)
    if frame.command == REGISTER:
        ip = server_ip.encode("ascii")[:32]
        body += ip + b"\0" * (32 - len(ip)) + bytes([server_port & 0xFF, (server_port >> 8) & 0xFF])
    return _wrap(body)


def build(command, device_id, payload, *, head=b"\0" * 8):
    """A reader's frame -- for tests and the simulator."""
    dev = device_id.encode("ascii")[:16]
    inner_wo_len = bytes([command >> 8, command & 0xFF]) + head + dev + b"\0" * (16 - len(dev)) + payload
    length = len(inner_wo_len) + 2
    inner = bytes([length >> 8, length & 0xFF]) + inner_wo_len
    crc = crc16(inner)
    return HEADER + inner + bytes([crc >> 8, crc & 0xFF])


def tag_record(read):
    """One 21-byte tag record -- for tests and the simulator."""
    ant = (read.port & 0x0F) | (0x80 if read.entry else 0) | (0x40 if read.staying else 0)
    body = (bytes([ant, read.antenna_type]) + bytes.fromhex(read.tag) + b"\0"
            + bytes([read.address >> 8, read.address & 0xFF, 1 if read.low_battery else 0, read.rssi & 0xFF])
            + _time_bytes(read.reader_time))
    return bytes([TAG_TLV >> 8, TAG_TLV & 0xFF, 0, len(body)]) + body
