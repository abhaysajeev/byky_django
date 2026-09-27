"""Writing log rows without making anyone wait for them.

A request only puts a dict on an in-memory queue -- microseconds -- and a
background thread turns the queue into batched INSERTs: one bulk_create per
second or per MONITORING_BATCH_SIZE rows, whichever comes first. This is the
QueueHandler/QueueListener shape from Python's logging cookbook, and what
DRF-API-Logger does; there is no Celery or Redis in this project to hand it to.

- One thread per process, started on first use -- after gunicorn forks, so each
  worker gets its own (a thread does not survive fork).
- The queue is bounded. If the database stalls and it fills, new records are
  dropped and counted rather than growing memory or blocking requests.
- Rows still queued at shutdown are flushed by an atexit hook.
- The thread also purges expired rows now and then (services.purge), under a
  Postgres advisory lock so only one worker does it at a time.
- MONITORING_SYNC=True (tests) writes inline instead.

Nothing here may raise into a request. A failure is reported once a minute on
the console (the "apps.monitoring" logger, which the database handler ignores,
so a broken log table cannot feed itself).
"""

import atexit
import logging
import os
import queue
import threading
import time

from django.conf import settings
from django.db import close_old_connections, transaction

log = logging.getLogger("apps.monitoring")

REQUEST, ERROR = "request", "error"

_lock = threading.Lock()
_queue = None
_thread = None
_pid = None
_dropped = 0
_last_warning = 0.0
_last_purge = 0.0


def enqueue(kind, fields):
    """Hand one row to the writer. Never raises, never blocks."""
    try:
        if settings.MONITORING_SYNC:
            _write([(kind, fields)])
            return
        _ensure_started()
        _queue.put_nowait((kind, fields))
    except queue.Full:
        _note_drop()
    except Exception:                       # noqa: BLE001 -- logging must never break a request
        _warn("could not queue a log row")


def _ensure_started():
    global _queue, _thread, _pid
    if _thread is not None and _pid == os.getpid() and _thread.is_alive():
        return
    with _lock:
        if _thread is not None and _pid == os.getpid() and _thread.is_alive():
            return
        _queue = queue.Queue(maxsize=settings.MONITORING_QUEUE_SIZE)
        _pid = os.getpid()
        _thread = threading.Thread(target=_run, name="log-writer", daemon=True)
        _thread.start()


def _run():
    while True:
        batch = _take_batch(timeout=settings.MONITORING_FLUSH_SECONDS)
        if batch:
            _write(batch)
        _maybe_purge()


def _take_batch(timeout):
    try:
        batch = [_queue.get(timeout=timeout)]
    except queue.Empty:
        return []
    while len(batch) < settings.MONITORING_BATCH_SIZE:
        try:
            batch.append(_queue.get_nowait())
        except queue.Empty:
            break
    return batch


def _write(batch):
    from apps.monitoring.models import ErrorLog, RequestLog

    try:
        if threading.current_thread() is _thread:
            # The writer's own connection: a dropped one is replaced, not
            # retried forever. Never another thread's (flush() runs on the
            # main thread at exit).
            close_old_connections()
        requests = [RequestLog(**row) for row in _finished(fields for kind, fields in batch if kind == REQUEST)]
        errors = [ErrorLog(**fields) for kind, fields in batch if kind == ERROR]
        # Its own savepoint: inline (tests) a failed write must not break the
        # surrounding transaction.
        with transaction.atomic():
            if requests:
                RequestLog.objects.bulk_create(requests)
            if errors:
                ErrorLog.objects.bulk_create(errors)
    except Exception:                       # noqa: BLE001
        _warn(f"could not write {len(batch)} log row(s)")


def _finished(rows):
    """Request rows with their bodies parsed, masked and cut (middleware.finish)
    -- here, off the request thread. A row that cannot be finished is dropped
    alone, not with its batch."""
    from apps.monitoring.middleware import finish

    done = []
    for fields in rows:
        try:
            done.append(finish(dict(fields)))
        except Exception:                   # noqa: BLE001
            _warn("could not finish a request log row")
    return done


def _maybe_purge():
    global _last_purge
    now = time.monotonic()
    if now - _last_purge < settings.MONITORING_PURGE_HOURS * 3600:
        return
    _last_purge = now
    try:
        from apps.monitoring.services import purge

        purge(lock=True)
    except Exception:                       # noqa: BLE001
        _warn("could not purge expired log rows")


def _note_drop():
    global _dropped
    _dropped += 1
    _warn(f"log queue full: {_dropped} row(s) dropped so far")


def _warn(message):
    """At most one console line a minute, whatever the failure."""
    global _last_warning
    now = time.monotonic()
    if now - _last_warning >= 60:
        _last_warning = now
        log.warning(message, exc_info=True)


def flush():
    """Write whatever is queued, now. At exit, and for tests of the thread."""
    if _queue is None or _pid != os.getpid():
        return
    batch = []
    while True:
        try:
            batch.append(_queue.get_nowait())
        except queue.Empty:
            break
    for start in range(0, len(batch), settings.MONITORING_BATCH_SIZE):
        _write(batch[start:start + settings.MONITORING_BATCH_SIZE])


atexit.register(flush)
