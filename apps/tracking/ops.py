"""Operational events of the gateway and engine -- connects, disconnects, bad
frames, database errors, cycle times -- to the central log server
(LOG_INGEST_URL), for watching only (owner: tracking data itself never goes
there, design §2).

Separate from apps/monitoring/shipper.py on purpose: that pipeline carries the
device-request logs and is not touched. Same rules though: its own bounded
queue and thread, put_nowait only, drops when full, never raises, off unless
LOG_INGEST_URL and LOG_INGEST_KEY are set. Every event is also a console log
line, so `docker logs` shows it either way.
"""

import json
import logging
import queue
import threading
import time
import urllib.request
from datetime import UTC, datetime

from django.conf import settings

from apps.monitoring.shipper import chunks

log = logging.getLogger("apps.tracking.ops")

QUEUE_SIZE = 2000
FLUSH_SECONDS = 5.0

_queue = None
_thread = None
_lock = threading.Lock()


def enabled():
    return bool(settings.LOG_INGEST_URL and settings.LOG_INGEST_KEY)


def document(name, fields):
    device = fields.get("device") or (f"ip:{fields['ip']}" if fields.get("ip") else "rfid-gateway")
    return {"device_id": f"rfid:{device}", "form_data": {},
            "log": {"source": "tracking", "event": name, "at": datetime.now(UTC).isoformat(),
                    "environment": settings.LOG_ENVIRONMENT or None, **fields}}


def event(name, **fields):
    """Record one event. Never raises, never blocks."""
    try:
        log.info("tracking %s %s", name, json.dumps(fields, default=str))
        if not enabled():
            return
        _start()
        _queue.put_nowait(document(name, fields))
    except queue.Full:
        pass                                  # the log server's copy has a gap; the console line exists
    except Exception:                         # noqa: BLE001 -- watching must never break tracking
        pass


def _start():
    global _queue, _thread
    if _thread is not None and _thread.is_alive():
        return
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _queue = queue.Queue(maxsize=QUEUE_SIZE)
        _thread = threading.Thread(target=_run, args=(_queue,), name="tracking-ops", daemon=True)
        _thread.start()


def _run(source):
    while True:
        time.sleep(FLUSH_SECONDS)
        batch = []
        while len(batch) < 500:
            try:
                batch.append(source.get_nowait())
            except queue.Empty:
                break
        for chunk in chunks(batch):
            try:
                body = json.dumps({"logs": chunk}, default=str).encode()
                request = urllib.request.Request(settings.LOG_INGEST_URL, data=body, method="POST", headers={
                    "Content-Type": "application/json", "X-API-Key": settings.LOG_INGEST_KEY})
                urllib.request.urlopen(request, timeout=settings.LOG_INGEST_TIMEOUT).close()
            except Exception:                 # noqa: BLE001 -- dropped, never retried forever
                pass
