"""Operational events of the gateway and engine -- connects, disconnects, bad
frames, database errors, cycle times -- as console log lines only, so
`docker logs` shows them.

Not sent to the central log server (owner, 12 Nov 2026): a reader that keeps
reconnecting filled it, and tracking data never went there anyway.
"""

import json
import logging

log = logging.getLogger("apps.tracking.ops")


def event(name, **fields):
    """Record one event as a log line. Never raises, never blocks."""
    try:
        log.info("tracking %s %s", name, json.dumps(fields, default=str))
    except Exception:                         # noqa: BLE001 -- watching must never break tracking
        pass
