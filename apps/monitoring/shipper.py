"""Copying device-request logs to the central log server, off every hot path.

The request_log table here keeps 30 days for the system administrator; the log
server (the log-ingestion project, LOG_INGEST_URL) keeps its own copy for its
own dashboards. This module feeds it without costing a device call anything:

  request thread   -> writer queue           (writer.py, unchanged: microseconds)
  writer thread    -> Postgres, then offer()  (a put_nowait per row, never waits)
  shipper thread   -> POST /ingest/batch      (here: names looked up, bodies
                                               parsed, batches sized, sent)

- Its own thread and its own bounded queue, per process -- a slow or dead log
  server can stall only this thread, never the writer or a request.
- The queue is bounded (LOG_INGEST_QUEUE_SIZE). Full means new rows are
  dropped and counted: the log server's copy has a gap, request_log does not.
- A batch that fails is retried once, then dropped. Nothing is ever retried
  forever, and nothing here raises into its caller.
- A batch is at most 100 logs and LOG_INGEST_BATCH_BYTES, so it passes the
  log server's own limits.
- Off unless LOG_INGEST_URL and LOG_INGEST_KEY are both set.
"""

import json
import logging
import os
import queue
import re
import threading
import time
import urllib.error
import urllib.request
import uuid

from django.conf import settings
from django.db import close_old_connections

log = logging.getLogger("apps.monitoring")

MAX_LOGS_PER_BATCH = 100            # the log server's limit
RETRY_DELAY_SECONDS = 2.0
MAX_ORDER_NOS = 50                  # a list reply (orders/changes) cannot make a huge field

# The log server's "Order no" filter reads log.order_nos (owner, 24 Nov 2026).
# Many order calls name the order only by its id, so ids are looked up too.
ORDER_NO_KEYS = ("order_no",)
ORDER_ID_KEYS = ("order_id", "order_sync_id")
# Where the request's own sync_id IS the order's id.
ORDER_SYNC_ID_URLS = ("api-app-order-create", "api-app-order-detail")
_ORDER_NO_TEXT = re.compile(r'"order_no"\s*:\s*"([^"]{1,30})"')
_ORDER_ID_TEXT = re.compile(r'"order_(?:sync_)?id"\s*:\s*"([0-9a-fA-F-]{36})"')

_lock = threading.Lock()
_queue = None
_thread = None
_pid = None
_dropped = 0
_last_warning = 0.0


def enabled():
    return bool(settings.LOG_INGEST_URL and settings.LOG_INGEST_KEY)


def offer(rows):
    """Hand saved RequestLog rows over for sending. Never raises, never blocks."""
    if not rows or not enabled():
        return
    try:
        _ensure_started()
        for row in rows:
            _queue.put_nowait(_fields(row))
    except queue.Full:
        _note_drop()
    except Exception:                       # noqa: BLE001 -- shipping must never break the writer
        _warn("could not queue logs for the log server")


def _fields(row):
    """Plain values from a saved row -- read now, on the writer thread, so the
    shipper never touches the model instance."""
    return {
        "request_id": str(row.pk), "at": row.created_at.isoformat(), "app": row.app, "method": row.method,
        "path": row.path, "url_name": row.url_name, "status": row.status, "code": row.code,
        "duration_ms": row.duration_ms, "ip": row.ip, "user_agent": row.user_agent,
        "installation_id": row.installation_id, "device_id": row.device_id, "user_id": row.user_id,
        "username": row.username, "company_id": row.company_id, "branch_id": row.branch_id,
        "request_body": row.request_body, "response_body": row.response_body,
        "request_truncated": row.request_truncated, "response_truncated": row.response_truncated,
    }


def _ensure_started():
    global _queue, _thread, _pid
    if _thread is not None and _pid == os.getpid() and _thread.is_alive():
        return
    with _lock:
        if _thread is not None and _pid == os.getpid() and _thread.is_alive():
            return
        _queue = queue.Queue(maxsize=settings.LOG_INGEST_QUEUE_SIZE)
        _pid = os.getpid()
        # The thread keeps its own queue: it never picks up a later one.
        _thread = threading.Thread(target=_run, args=(_queue,), name="log-shipper", daemon=True)
        _thread.start()


def _run(source):
    while True:
        batch = _take(source, timeout=1.0)
        if batch:
            ship(batch)


def _take(source, timeout):
    try:
        batch = [source.get(timeout=timeout)]
    except queue.Empty:
        return []
    while len(batch) < MAX_LOGS_PER_BATCH * 5:
        try:
            batch.append(source.get_nowait())
        except queue.Empty:
            break
    return batch


def ship(batch):
    """Send queued rows: look up names once, build the documents, post them in
    size-capped batches. Never raises."""
    try:
        if threading.current_thread() is _thread:
            close_old_connections()
        refs = [order_refs(fields) for fields in batch]
        numbers = _order_numbers(refs)
        documents = [document(fields, names, order_nos(ref, numbers))
                     for fields, names, ref in zip(batch, _names(batch), refs, strict=True)]
        for chunk in chunks(documents):
            _send(chunk)
    except Exception:                       # noqa: BLE001
        _warn(f"could not ship {len(batch)} log(s)")


def _names(batch):
    """Device number, company and station names for a batch: three queries,
    whatever its size. A lookup failure only leaves the names out."""
    try:
        from django.db.models import Q

        from apps.company.models import Branch, Company
        from apps.devices.models import Device

        device_ids = {f["device_id"] for f in batch if f["device_id"]}
        installs = {f["installation_id"] for f in batch if f["installation_id"]}
        devices = list(Device.objects.filter(Q(pk__in=device_ids) | Q(installation_id__in=installs))
                       .values_list("pk", "installation_id", "device_registration_id"))
        by_id = {pk: number for pk, _, number in devices}
        by_install = {install: number for _, install, number in devices}
        companies = dict(Company.objects.filter(pk__in={f["company_id"] for f in batch if f["company_id"]})
                         .values_list("pk", "name"))
        branches = dict(Branch.objects.filter(pk__in={f["branch_id"] for f in batch if f["branch_id"]})
                        .values_list("pk", "name"))
    except Exception:                       # noqa: BLE001
        return [{} for _ in batch]
    return [{
        "device_no": by_id.get(f["device_id"]) or by_install.get(f["installation_id"]),
        "company": companies.get(f["company_id"], ""),
        "branch": branches.get(f["branch_id"], ""),
    } for f in batch]


def _body(text):
    """A stored (already masked) body as JSON when it is JSON, else as text."""
    if not text:
        return {}
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return {"text": text}
    return value if isinstance(value, dict) else {"value": value}


def _uuid(value):
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def _walk(node, numbers, ids):
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ORDER_NO_KEYS and isinstance(value, str) and value.strip():
                numbers.add(value.strip())
            elif key in ORDER_ID_KEYS and _uuid(value):
                ids.add(_uuid(value))
            else:
                _walk(value, numbers, ids)
    elif isinstance(node, list):
        for value in node:
            _walk(value, numbers, ids)


def order_refs(fields):
    """(order numbers, order ids) one log names, from its request and reply.
    A body cut short in the log is not JSON: the text that was kept is
    searched instead."""
    numbers, ids = set(), set()
    for key, cut in (("request_body", "request_truncated"), ("response_body", "response_truncated")):
        text = fields.get(key) or ""
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            parsed = None
        if parsed is None or fields.get(cut):
            numbers.update(_ORDER_NO_TEXT.findall(text))
            ids.update(filter(None, (_uuid(v) for v in _ORDER_ID_TEXT.findall(text))))
        if parsed is not None:
            _walk(parsed, numbers, ids)
            if key == "request_body" and fields.get("url_name") in ORDER_SYNC_ID_URLS and isinstance(parsed, dict):
                data = parsed.get("request_data")
                sync_id = _uuid(data.get("sync_id")) if isinstance(data, dict) else None
                if sync_id:
                    ids.add(sync_id)
    return numbers, ids


def _order_numbers(refs):
    """{order id: order number} for every id a batch names: one query. A
    lookup failure only leaves those numbers out."""
    ids = set().union(*(found for _, found in refs)) if refs else set()
    if not ids:
        return {}
    try:
        from apps.rental.models import Order

        return {str(pk): number for pk, number in Order.objects.filter(pk__in=ids).values_list("pk", "order_no")}
    except Exception:                       # noqa: BLE001
        return {}


def order_nos(ref, numbers):
    """The sorted order numbers of one log, at most MAX_ORDER_NOS."""
    direct, ids = ref
    found = set(direct) | {numbers[i] for i in ids if i in numbers}
    return sorted(found)[:MAX_ORDER_NOS]


def document(fields, names, orders=None):
    """One log for the log server: the device as `device_id`, the request body
    as `form_data`, everything else (response included) under `log` -- with
    `environment`, the copy that sent it (qa / dev; None when unset), so one
    viewer can tell the two deployments on the VPS apart, and `order_nos`, the
    orders the call was about (the viewer's Order no filter)."""
    record = {k: v for k, v in fields.items() if k not in ("request_body", "response_body")}
    record.update(names)
    record["response"] = _body(fields["response_body"])
    record["environment"] = settings.LOG_ENVIRONMENT or None
    record["order_nos"] = list(orders or [])
    return {
        "device_id": fields["installation_id"] or (f"ip:{fields['ip']}" if fields["ip"] else "unknown"),
        "form_data": _body(fields["request_body"]),
        "log": record,
    }


def chunks(documents):
    """Batches of at most 100 documents and LOG_INGEST_BATCH_BYTES of JSON."""
    limit = settings.LOG_INGEST_BATCH_BYTES
    chunk, size = [], 2
    for doc in documents:
        doc_size = len(json.dumps(doc, default=str).encode()) + 1
        if chunk and (len(chunk) >= MAX_LOGS_PER_BATCH or size + doc_size > limit):
            yield chunk
            chunk, size = [], 2
        chunk.append(doc)
        size += doc_size
    if chunk:
        yield chunk


def _send(chunk):
    body = json.dumps({"logs": chunk}, default=str).encode()
    for attempt in (1, 2):
        try:
            _post(body)
            return
        except Exception:                   # noqa: BLE001
            if attempt == 1:
                time.sleep(RETRY_DELAY_SECONDS)
    _note_drop(len(chunk), "the log server did not take a batch")


def _post(body):
    request = urllib.request.Request(
        settings.LOG_INGEST_URL, data=body, method="POST",
        headers={"Content-Type": "application/json", "X-API-Key": settings.LOG_INGEST_KEY},
    )
    with urllib.request.urlopen(request, timeout=settings.LOG_INGEST_TIMEOUT) as response:
        if response.status >= 300:
            raise urllib.error.HTTPError(settings.LOG_INGEST_URL, response.status, "rejected", None, None)


def _note_drop(count=1, why="log shipping queue full"):
    global _dropped
    _dropped += count
    _warn(f"{why}: {_dropped} log(s) not sent to the log server so far")


def _warn(message):
    """At most one console line a minute, whatever the failure."""
    global _last_warning
    now = time.monotonic()
    if now - _last_warning >= 60:
        _last_warning = now
        log.warning(message, exc_info=True)
