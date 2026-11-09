"""The presence engine's loop: its own container (`tracking-engine`), not Celery
(design §2, D7). Off unless TRACKING_ENABLED, and idle in capture mode: with no
readings being stored, every vehicle would look unseen.
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
        last_upkeep, last_day = 0.0, None
        while not stop.is_set():
            ALIVE_FILE.touch()
            if settings.TRACKING_ENABLED and settings.TRACKING_MODE == "write":
                close_old_connections()
                engine.tick()
                last_upkeep, last_day = self._upkeep(last_upkeep, last_day)
            stop.wait(CYCLE_SECONDS)

    def _upkeep(self, last_upkeep, last_day):
        """Housekeeping must never stop presence, so a failure is only logged."""
        try:
            if time.monotonic() - last_upkeep >= UPKEEP_SECONDS or last_upkeep == 0.0:
                upkeep.hourly()
                last_upkeep = time.monotonic()
            today = timezone.localdate()
            if today != last_day:
                upkeep.daily()
                last_day = today
        except Exception as failure:          # noqa: BLE001
            ops.event("engine_upkeep_failed", error=f"{type(failure).__name__}: {failure}"[:200])
        return last_upkeep, last_day
