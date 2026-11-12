"""Fake Marktrace readers, for development and the load test (design §9):

    python manage.py simulate_rfid_reader --readers 3 --tags 40 --every 30 --minutes 5
    python manage.py simulate_rfid_reader --host 127.0.0.1 --port 5100 --rate-x 10 --minutes 60

Each reader logs in, sends a heartbeat each --heartbeat seconds, and every
--every seconds uploads one read per tag per antenna that "hears" it. Like
the real reader (E8) it re-sends an upload until that upload is ACKed.
Tags are SIM00001.. per reader unless --tags-from names vehicles' tags.
At the end it prints what was sent, ACKed and re-sent.
"""

import asyncio
import random
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from django.core.management.base import BaseCommand

from apps.tracking import protocol as p


class Command(BaseCommand):
    help = "Simulate RFID readers against the gateway."

    def add_arguments(self, parser):
        parser.add_argument("--host", default="127.0.0.1")
        parser.add_argument("--port", type=int, default=5100)
        parser.add_argument("--readers", type=int, default=1)
        parser.add_argument("--first-device", default="990000000000001",
                            help="device id of the first simulated reader; the rest count up")
        parser.add_argument("--tags", type=int, default=30, help="tags per reader")
        parser.add_argument("--tags-from", default="", help="comma-separated real tags (one reader)")
        parser.add_argument("--every", type=float, default=30, help="seconds between uploads")
        parser.add_argument("--heartbeat", type=float, default=60)
        parser.add_argument("--rate-x", type=float, default=1, help="multiply the upload rate")
        parser.add_argument("--minutes", type=float, default=5)
        parser.add_argument("--tz", default="Asia/Dubai")

    def handle(self, *args, **o):
        self.totals = {"uploads": 0, "acked": 0, "resent": 0, "reads": 0, "failures": 0}
        asyncio.run(self._run(o))
        self.stdout.write("simulator: " + ", ".join(f"{k} {v}" for k, v in self.totals.items()))

    async def _run(self, o):
        first = int(o["first_device"])
        deadline = time.monotonic() + o["minutes"] * 60
        given = [t.strip().upper().zfill(8) for t in o["tags_from"].split(",") if t.strip()]
        tasks = []
        for n in range(o["readers"]):
            tags = given if given and n == 0 else [f"{0x51000000 + n * 0x10000 + i:08X}" for i in range(o["tags"])]
            tasks.append(self._reader(str(first + n), tags, o, deadline))
        await asyncio.gather(*tasks)

    async def _reader(self, device, tags, o, deadline):
        tz = ZoneInfo(o["tz"])
        rng = random.Random(device)
        # each tag sits nearest one antenna; the others hear it fainter, or not at all
        home = {t: (rng.randint(1, 4), rng.randint(-55, -25)) for t in tags}
        every = o["every"] / o["rate_x"]
        next_hb = 0
        pending = None                            # an upload not ACKed yet: kept across reconnects, sent first
        while time.monotonic() < deadline:
            try:
                reader, writer = await asyncio.open_connection(o["host"], o["port"])
            except OSError:
                self.totals["failures"] += 1
                await asyncio.sleep(5)
                continue
            try:
                await self._exchange(reader, writer, p.build(p.LOGIN, device, b""))
                while time.monotonic() < deadline:
                    if time.monotonic() >= next_hb:
                        now = datetime.now(tz)
                        hb = bytes([1, 1, 0x11, 0x95, 2, 7]) + bytes([now.year - 2000, now.month, now.day,
                                                                        now.hour, now.minute, now.second])
                        await self._exchange(reader, writer, p.build(p.HEARTBEAT, device, hb))
                        next_hb = time.monotonic() + o["heartbeat"]
                    if pending is None:
                        now = datetime.now(tz).replace(tzinfo=None, microsecond=0)
                        records = []
                        for tag, (port, rssi) in home.items():
                            for ant in (1, 2, 3, 4):
                                if ant == port or rng.random() < 0.3:
                                    level = rssi - (0 if ant == port else rng.randint(6, 15)) + rng.randint(-3, 3)
                                    records.append(p.tag_record(p.TagRead(ant, False, True, 32, tag, 0, False,
                                                                          max(-90, level), now)))
                        # a reader sends at most ~4 KB per frame: split into several uploads
                        chunk = (p.MAX_FRAME - 64) // 21
                        pending = [p.build(p.TAG_DATA, device, b"".join(records[i:i + chunk]))
                                   for i in range(0, len(records), chunk)]
                        self.totals["reads"] += len(records)
                    else:
                        self.totals["resent"] += len(pending)
                    while pending:
                        self.totals["uploads"] += 1
                        ack = await self._exchange(reader, writer, pending[0])
                        if ack is None:
                            raise ConnectionError("no ACK")
                        self.totals["acked"] += 1
                        pending.pop(0)
                    pending = None
                    await asyncio.sleep(every)
            except (ConnectionError, OSError, asyncio.IncompleteReadError, TimeoutError):
                self.totals["failures"] += 1
                await asyncio.sleep(2)
            finally:
                writer.close()

    async def _exchange(self, reader, writer, frame):
        writer.write(frame)
        await writer.drain()
        try:
            head = await asyncio.wait_for(reader.readexactly(4), 10)
            reply = head + await asyncio.wait_for(reader.readexactly(p.frame_size(head) - 4), 10)
            p.check(reply)
            return reply
        except TimeoutError:
            return None
