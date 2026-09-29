"""The copy of each device request sent to the central log server
(apps/monitoring/shipper.py): what it holds, and that it can never slow or
break a device call."""

import json
import queue
import threading
import time

import pytest

from apps.monitoring import shipper
from apps.monitoring.models import RequestLog
from apps.monitoring.tests import test_monitoring as monitoring

world, token, fresh_throttle = monitoring.world, monitoring.token, monitoring.fresh_throttle
login = monitoring.login


@pytest.fixture
def log_server(settings, monkeypatch):
    """Shipping switched on; every POST lands in `sent` instead of the network."""
    settings.LOG_INGEST_URL = "https://logs.test/ingest/batch"
    settings.LOG_INGEST_KEY = "k" * 64
    sent = []
    monkeypatch.setattr(shipper, "_post", lambda body: sent.append(json.loads(body)))
    return sent


@pytest.fixture
def inline(monkeypatch):
    """Ship on the calling thread, so a test can read the result straight away."""
    monkeypatch.setattr(shipper, "offer", lambda rows: shipper.ship([shipper._fields(r) for r in rows]))


@pytest.fixture(autouse=True)
def fresh_shipper(monkeypatch):
    for name, value in (("_queue", None), ("_thread", None), ("_pid", None), ("_dropped", 0)):
        monkeypatch.setattr(shipper, name, value)


def test_nothing_is_sent_unless_configured(client, world, settings, monkeypatch):
    settings.LOG_INGEST_URL = ""
    monkeypatch.setattr(shipper, "_ensure_started", lambda: pytest.fail("no thread without a URL"))
    assert login(client).status_code == 200
    assert RequestLog.objects.count() == 1                     # the local log is unaffected


def test_a_device_call_reaches_the_log_server_as_one_document(client, world, log_server, inline):
    response = login(client)

    [batch] = log_server
    [doc] = batch["logs"]
    assert doc["device_id"] == "till-1"
    assert doc["form_data"]["request_data"]["password"] == "***"          # masked here, never sent in clear
    record = doc["log"]
    assert record["request_id"] == response["X-Request-ID"]
    assert (record["app"], record["method"], record["path"]) == ("operator", "POST", monitoring.LOGIN)
    assert (record["status"], record["code"], record["username"]) == (200, "ok", "OPR001")
    assert isinstance(record["device_no"], int)
    assert record["response"]["data"]["tokens"] == "***"
    assert "request_body" not in record and "response_body" not in record


def test_a_signed_in_call_carries_the_station_and_company_names(client, world, token, log_server, inline):
    monitoring.call(client, monitoring.FARES, {"date": "2026-09-30"}, token=token)

    [record] = [d["log"] for batch in log_server for d in batch["logs"] if d["log"]["path"] == monitoring.FARES]
    assert record["branch"] == world["adc1"].name and record["company"] == world["company"].name
    assert record["username"].upper() == "OPR001" and record["status"] == 200


def test_a_dead_log_server_never_slows_a_device_call(client, world, settings, monkeypatch):
    settings.LOG_INGEST_URL, settings.LOG_INGEST_KEY = "https://logs.test/ingest/batch", "k" * 64
    reached = threading.Event()

    def hang(body):
        reached.set()
        time.sleep(3)                                          # a log server that never answers in time
    monkeypatch.setattr(shipper, "_post", hang)
    monkeypatch.setattr(shipper, "_names", lambda batch: [{} for _ in batch])

    started = time.monotonic()
    assert login(client).status_code == 200
    assert login(client).status_code == 200
    elapsed = time.monotonic() - started

    assert reached.wait(5)                                     # the shipper did try to send...
    assert elapsed < 1.0, f"two device calls took {elapsed:.2f}s"   # ...on its own thread


def test_a_full_queue_drops_rows_and_never_raises(world, log_server, monkeypatch):
    monkeypatch.setattr(shipper, "_ensure_started", lambda: None)
    monkeypatch.setattr(shipper, "_queue", queue.Queue(maxsize=1))
    rows = [RequestLog(created_at=monitoring.timezone.now(), method="POST", path=f"/x/{n}", status=200,
                       duration_ms=1) for n in range(3)]

    shipper.offer(rows)                                        # no exception, no wait

    assert shipper._queue.qsize() == 1 and shipper._dropped >= 1


def test_a_failed_batch_is_tried_twice_then_dropped(log_server, monkeypatch):
    attempts = []

    def refuse(body):
        attempts.append(1)
        raise OSError("connection refused")
    monkeypatch.setattr(shipper, "_post", refuse)
    monkeypatch.setattr(shipper, "RETRY_DELAY_SECONDS", 0)

    shipper._send([{"device_id": "d", "log": {}}])             # returns, does not raise

    assert len(attempts) == 2 and shipper._dropped == 1


def test_batches_stay_within_the_log_servers_limits(settings):
    small = [{"device_id": "d", "log": {"n": n}} for n in range(250)]
    assert [len(c) for c in shipper.chunks(small)] == [100, 100, 50]

    settings.LOG_INGEST_BATCH_BYTES = 10_000
    big = [{"device_id": "d", "log": {"response": "x" * 4_000}} for _ in range(5)]
    sizes = [len(json.dumps({"logs": c}).encode()) for c in shipper.chunks(big)]
    assert len(sizes) == 3 and all(size <= 10_000 for size in sizes)


@pytest.mark.parametrize(("stored", "sent"), [
    ("", {}),
    ('{"a": 1}', {"a": 1}),
    ("not json", {"text": "not json"}),
    ("[1, 2]", {"value": [1, 2]}),
])
def test_a_stored_body_is_sent_as_an_object(stored, sent):
    assert shipper._body(stored) == sent
