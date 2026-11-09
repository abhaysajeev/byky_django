"""The presence engine's loop: its own container (`tracking-engine`), not Celery
(design §2, D7). Off unless TRACKING_ENABLED. In capture mode only the
housekeeping runs: with no readings being stored, every vehicle would look
unseen, but partitions must still be kept ahead for the switch to write mode.
"""

import pathlib
import signal
import threading
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.utils import timezone

from apps.tracking import engine, ops, upkeep

CYCLE_SECONDS = 30
UPKEEP_SECONDS = 3600
ALIVE_FILE = pathlib.Path("/tmp/tracking-engine.alive")


class Command(BaseCommand):
    help = "Run the antenna tracking presence engine."

    def handle(self, *args, **options):
        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        last_upkeep, last_day = None, None
        while not stop.is_set():
            ALIVE_FILE.touch()
            if settings.TRACKING_ENABLED:
                close_old_connections()
                last_upkeep = self._hourly(last_upkeep)      # first, so a partition exists before anything else
                if settings.TRACKING_MODE == "write":
                    engine.tick()
                    last_day = self._daily(last_day)
            stop.wait(CYCLE_SECONDS)

    def _hourly(self, last):
        """Housekeeping must never stop presence, so a failure is only logged."""
        if last is not None and time.monotonic() - last < UPKEEP_SECONDS:
            return last
        try:
            upkeep.hourly()
            return time.monotonic()
        except Exception as failure:          # noqa: BLE001
            ops.event("engine_upkeep_failed", error=f"{type(failure).__name__}: {failure}"[:200])
            return last

    def _daily(self, last):
        today = timezone.localdate()
        if today == last:
            return last
        try:
            upkeep.daily()
            return today
        except Exception as failure:          # noqa: BLE001
            ops.event("engine_upkeep_failed", error=f"{type(failure).__name__}: {failure}"[:200])
            return last
