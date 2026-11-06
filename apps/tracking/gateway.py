"""The RFID gateway: one asyncio TCP server the stations' readers connect to
(design/07-tracking.md §3). It runs in its own container
(`manage.py run_rfid_gateway`), never inside the web or Celery processes.

Per connection, strictly one frame at a time:
    read 4 bytes -> the length -> read exactly the rest -> CRC -> handle -> reply
so a slow database slows that reader down instead of filling our memory, and
whatever we have not ACKed stays on the reader, which sends it again (E8).

Readers are allow-listed by device id (Reader.status):
  - unknown  -> stored as pending (at most TRACKING_MAX_PENDING), tag data not
                ACKed; register / login / heartbeat are answered, so it shows
                up on the Readers screen with a heartbeat;
  - blocked  -> the connection is closed;
  - approved + enabled -> tag data is stored, then ACKed.

Modes (TRACKING_MODE):
  capture -- every frame is stored as received (tracking_raw_frame), for the
             P0 field test: what the reader really sends and whether it moves
             on after our ACK;
  write   -- tag reads are stored (store.save_reads, slice 5).

The port is public (a Docker-published port bypasses ufw), so: frames over
4 KB or with a bad CRC close the connection, idle connections are closed,
and connections per IP and in total are capped.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from apps.tracking import protocol as p

log = logging.getLogger("apps.tracking.gateway")


@dataclass
class Config:
    host: str = "0.0.0.0"
    port: int = 5100
    health_port: int = 0           # 0 = no health server
    mode: str = "capture"
    enabled: bool = False
    server_ip: str = ""
    server_port: int = 0
    reader_tz: str = "Asia/Dubai"
    idle_seconds: float = 600
    max_per_ip: int = 5
    max_connections: int = 200


@dataclass
class Stats:
    started: float = field(default_factory=time.monotonic)
    connections: int = 0
    frames: int = 0
    bad_frames: int = 0
    last_frame_at: float | None = None
    db_errors: int = 0


class Gateway:
    def __init__(self, config, store, events=None):
        self.config = config
        self.store = store
        self.events = events or (lambda name, **fields: None)   # ops events (log-ingestion)
        self.stats = Stats()
        self._per_ip = {}
        self._server = None
        self._health = None
        self._raw_marks = {}         # device id -> last raw frame stored for a pending reader

    # -- server ---------------------------------------------------------------------

    async def start(self):
        if self.config.enabled:
            self._server = await asyncio.start_server(self._client, self.config.host, self.config.port)
        if self.config.health_port:
            self._health = await asyncio.start_server(self._health_client, "0.0.0.0", self.config.health_port)
        return self

    @property
    def port(self):
        return self._server.sockets[0].getsockname()[1] if self._server else None

    async def serve_forever(self):
        await self.start()
        log.info("rfid gateway: %s, mode %s, port %s", "enabled" if self.config.enabled else "DISABLED",
                 self.config.mode, self.config.port)
        await asyncio.Event().wait()

    async def close(self):
        for server in (self._server, self._health):
            if server:
                server.close()
                await server.wait_closed()

    # -- one reader -----------------------------------------------------------------

    def _now(self):
        return datetime.now(UTC)

    def _reader_clock(self):
        return datetime.now(ZoneInfo(self.config.reader_tz)).replace(tzinfo=None)

    async def _client(self, reader, writer):
        peer = writer.get_extra_info("peername")
        ip = peer[0] if peer else "?"
        if self.stats.connections >= self.config.max_connections or self._per_ip.get(ip, 0) >= self.config.max_per_ip:
            self.events("refused", ip=ip, reason="connection limit")
            writer.close()
            return
        self.stats.connections += 1
        self._per_ip[ip] = self._per_ip.get(ip, 0) + 1
        self.events("connect", ip=ip)
        device = ""
        try:
            while True:
                try:
                    head = await asyncio.wait_for(reader.readexactly(4), self.config.idle_seconds)
                    size = p.frame_size(head)
                    rest = await asyncio.wait_for(reader.readexactly(size - 4), 30)
                except p.FrameError as exc:
                    self._bad(ip, device, head, str(exc))
                    await self._save_raw_safe(device, ip, head, False, str(exc))
                    return
                frame = head + rest
                try:
                    p.check(frame)
                    parsed = p.parse(frame)
                except p.FrameError as exc:
                    self._bad(ip, device, frame, str(exc))
                    await self._save_raw_safe(device, ip, frame, False, str(exc))
                    return
                device = parsed.device_id
                self.stats.frames += 1
                self.stats.last_frame_at = time.monotonic()
                answer = await self._handle(parsed, frame, ip)
                if answer is None:            # blocked / refused
                    return
                if answer:
                    writer.write(answer)
                    await writer.drain()
        except (TimeoutError, asyncio.IncompleteReadError, ConnectionError):
            pass
        except Exception:                    # never let one reader take the server down
            log.exception("rfid gateway: connection from %s (%s) failed", ip, device)
        finally:
            self.stats.connections -= 1
            self._per_ip[ip] -= 1
            if not self._per_ip[ip]:
                del self._per_ip[ip]
            self.events("disconnect", ip=ip, device=device)
            writer.close()

    def _bad(self, ip, device, data, why):
        self.stats.bad_frames += 1
        self.events("bad_frame", ip=ip, device=device, reason=why)
        log.warning("rfid gateway: bad frame from %s (%s): %s", ip, device, why)

    async def _handle(self, frame, raw, ip):
        """-> the reply bytes, b"" for no reply, or None to close the connection."""
        now = self._now()
        firmware = ""
        if frame.command == p.HEARTBEAT:
            try:
                firmware = "%d.%d" % divmod(p.heartbeat(frame.payload).software, 256)
            except p.FrameError:
                pass
        try:
            status = await self.store.touch(frame.device_id, ip, frame.kind, now, firmware=firmware)
        except Exception as exc:
            self.stats.db_errors += 1
            self.events("db_error", device=frame.device_id, error=str(exc)[:200])
            log.warning("rfid gateway: touch failed for %s: %s", frame.device_id, exc)
            return None                      # no ACK; the reader reconnects and resends
        if status == "blocked":
            self.events("blocked", device=frame.device_id, ip=ip)
            return None
        accepted = status == "accepted"     # approved and switched on
        if self.config.mode == "capture" and (accepted or self._raw_due(frame.device_id)):
            if not await self._save_raw_safe(frame.device_id, ip, raw, True, frame.kind):
                return None
        if frame.command == p.TAG_DATA:
            if not accepted:
                return b""                   # pending / switched off: keep it on the reader
            if self.config.mode == "write":
                try:
                    await self.store.save_reads(frame, p.tag_reads(frame.payload), now)
                except p.FrameError as exc:
                    self._bad(ip, frame.device_id, raw, str(exc))
                    return None
                except Exception as exc:
                    self.stats.db_errors += 1
                    self.events("db_error", device=frame.device_id, error=str(exc)[:200])
                    log.warning("rfid gateway: save failed for %s: %s", frame.device_id, exc)
                    return None              # not committed -> not ACKed
        elif frame.command not in p.COMMANDS:
            return b""                       # unknown command: no reply (the legacy sent an empty one)
        return p.reply(frame, self._reader_clock(), server_ip=self.config.server_ip,
                       server_port=self.config.server_port)

    def _raw_due(self, device):
        """A pending reader's frames are stored at most once a minute, so an
        unknown device cannot fill the table."""
        now = time.monotonic()
        if now - self._raw_marks.get(device, -1e9) < 60:
            return False
        self._raw_marks[device] = now
        return True

    async def _save_raw_safe(self, device, ip, data, crc_ok, note):
        try:
            await self.store.save_raw(device, ip, self._now(), data, crc_ok, note)
            return True
        except Exception as exc:
            self.stats.db_errors += 1
            log.warning("rfid gateway: raw frame not stored: %s", exc)
            return False

    # -- health ---------------------------------------------------------------------

    async def _health_client(self, reader, writer):
        try:
            await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            ok = await self.store.ping()
            age = None if self.stats.last_frame_at is None else round(time.monotonic() - self.stats.last_frame_at)
            body = json.dumps({"enabled": self.config.enabled, "mode": self.config.mode, "db": ok,
                               "connections": self.stats.connections, "frames": self.stats.frames,
                               "bad_frames": self.stats.bad_frames, "db_errors": self.stats.db_errors,
                               "last_frame_seconds_ago": age}).encode()
            status = b"200 OK" if ok else b"503 Service Unavailable"
            writer.write(b"HTTP/1.1 " + status + b"\r\nContent-Type: application/json\r\nContent-Length: "
                         + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()
