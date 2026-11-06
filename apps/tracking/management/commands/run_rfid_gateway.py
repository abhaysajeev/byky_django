"""The RFID gateway (apps/tracking/gateway.py). Runs in its own container:

    python manage.py run_rfid_gateway

With TRACKING_ENABLED off it opens no reader port, only the health port, and
waits -- the container stays up and healthy and nothing reaches it.
"""

import asyncio
import logging

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.tracking import ops
from apps.tracking.gateway import Config, Gateway
from apps.tracking.store import Store


class Command(BaseCommand):
    help = "Run the RFID reader gateway (TCP)."

    def handle(self, *args, **options):
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
        config = Config(port=settings.TRACKING_PORT, health_port=settings.TRACKING_HEALTH_PORT,
                        mode=settings.TRACKING_MODE, enabled=settings.TRACKING_ENABLED,
                        server_ip=settings.TRACKING_SERVER_IP, server_port=settings.TRACKING_SERVER_PORT,
                        reader_tz=settings.TRACKING_READER_TZ, idle_seconds=settings.TRACKING_IDLE_SECONDS,
                        max_per_ip=settings.TRACKING_MAX_PER_IP, max_connections=settings.TRACKING_MAX_CONNECTIONS)
        store = Store(size=settings.TRACKING_DB_POOL, max_pending=settings.TRACKING_MAX_PENDING,
                      reader_tz=settings.TRACKING_READER_TZ)
        asyncio.run(Gateway(config, store, events=ops.event).serve_forever())
