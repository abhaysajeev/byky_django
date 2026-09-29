"""Request and error logs: what is written, what is masked, who may read it."""

import datetime
import json
import queue
import re

import pytest
from django.utils import timezone

from apps.fare.tests import test_api as fare_api
from apps.monitoring import redact, writer
from apps.monitoring.models import ErrorLog, RequestLog
from apps.monitoring.services import purge
from apps.portal import privileges
from apps.portal.models import Role
from apps.portal.services import grant_all
from apps.portal.session_models import AppSession
from core.enums import Channel, UserScope
from core.models import User

# The signed-in till from the fare API tests: company, station, device, operator.
world, token, fresh_throttle, call = fare_api.world, fare_api.token, fare_api.fresh_throttle, fare_api.call
PASSWORD = fare_api.PASSWORD
LOGIN = "/api/v1/operator/auth/login"
FARES = "/api/v1/operator/fares"


def login(client, **extra):
    return call(client, LOGIN, {"username": "OPR001", "password": PASSWORD, "installation_id": "till-1", **extra})


# -- What a device call leaves behind ---------------------------------------------------------


def test_a_device_call_is_logged_with_secrets_masked(client, world):
    response = login(client)
    assert response.status_code == 200

    [log] = RequestLog.objects.all()
    assert str(log.pk) == response["X-Request-ID"]
    assert (log.app, log.method, log.path, log.status, log.code) == ("operator", "POST", LOGIN, 200, "ok")
    assert (log.installation_id, log.username, log.url_name) == ("till-1", "OPR001", "api-app-login")
    assert log.duration_ms >= 0 and log.created_at <= timezone.now()
    sent, answered = json.loads(log.request_body), json.loads(log.response_body)
    assert sent["request_data"]["password"] == redact.MASK
    assert sent["request_data"]["username"] == "OPR001"
    assert answered["data"]["tokens"] == redact.MASK                       # both tokens, masked
    assert PASSWORD not in log.request_body and response.json()["data"]["tokens"]["access"] not in log.response_body


def test_a_signed_in_call_records_the_device_branch_and_user(client, world, token):
    call(client, FARES, {"date": "2026-09-30"}, token=token)
    log = RequestLog.objects.get(path=FARES)
    user = User.objects.get(username__iexact="OPR001")
    assert (log.user_id, log.company_id, log.branch_id) == (user.pk, world["company"].pk, world["adc1"].pk)
    assert log.device_id is not None and log.status == 200


def test_behind_nginx_the_log_and_the_session_record_the_device_not_the_proxy(client, world, settings):
    settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK, "NUM_PROXIES": 1}
    client.defaults.update(REMOTE_ADDR="172.18.0.5", HTTP_X_FORWARDED_FOR="203.0.113.7")

    assert login(client).status_code == 200

    assert RequestLog.objects.get(path=LOGIN).ip == "203.0.113.7"
    assert AppSession.objects.get().ip_address == "203.0.113.7"


def test_a_refused_call_is_logged_with_its_code(client, world):
    call(client, FARES, {"date": "2026-09-30"})                              # no token
    assert RequestLog.objects.get(path=FARES).code == "not_authenticated"


def test_a_crash_is_one_request_row_and_one_error_row_linked_together(client, world, token, monkeypatch):
    from apps.fare import services

    def boom(*args, **kwargs):
        raise RuntimeError("fares exploded")
    monkeypatch.setattr(services, "device_fares", boom)

    response = call(client, FARES, {"date": "2026-09-30"}, token=token)
    assert response.status_code == 500 and response.json()["code"] == "server_error"

    log = RequestLog.objects.get(path=FARES)
    [error] = ErrorLog.objects.all()                                       # not also Django's "Internal Server Error"
    assert error.request_id == log.pk and str(log.pk) == response["X-Request-ID"]
    assert (error.source, error.app, error.path, error.level) == ("api", "operator", FARES, "ERROR")
    assert error.exception_type == "builtins.RuntimeError"
    assert "fares exploded" in error.traceback and "boom" in error.traceback
    assert error.username.upper() == "OPR001" and error.device_id == log.device_id


def test_a_big_body_is_cut_and_marked(client, world, settings):
    settings.MONITORING_BODY_LIMIT = 300
    login(client, note="x" * 1000)
    log = RequestLog.objects.get(path=LOGIN)
    assert log.request_truncated and len(log.request_body.encode()) <= 300


def test_a_broken_log_table_never_breaks_the_call(client, world, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("log table gone")
    monkeypatch.setattr(RequestLog.objects, "bulk_create", broken)
    assert login(client).status_code == 200


def test_web_screens_are_not_request_logged(client, world):
    client.get("/login/")
    assert not RequestLog.objects.exists()


# -- Error logs outside the device API ---------------------------------------------------------


@pytest.fixture
def system_user(client, db):
    role = Role.objects.create(company=None, name="System Administrator")
    grant_all(role)
    User.objects.create_user("platform.admin", PASSWORD, display_name="Platform", scope=UserScope.SYSTEM,
                             role=role, allowed_channels=[Channel.WEB])
    client.post("/login/", {"username": "platform.admin", "password": PASSWORD})
    return client


def test_a_web_crash_is_logged_with_the_signed_in_user(system_user, monkeypatch):
    from apps.monitoring.views import RequestListView

    def boom(self, **kwargs):
        raise ValueError("screen exploded")
    monkeypatch.setattr(RequestListView, "get_context_data", boom)
    system_user.raise_request_exception = False

    assert system_user.get("/monitoring/requests/").status_code == 500
    [error] = ErrorLog.objects.all()
    assert (error.source, error.path, error.username) == ("web", "/monitoring/requests/", "platform.admin")
    assert error.exception_type == "builtins.ValueError" and "screen exploded" in error.traceback


# -- Masking --------------------------------------------------------------------------------------


def test_secrets_are_masked_at_any_depth_and_nothing_else_is():
    body = {"request_data": {"password": "p", "new_password": "q", "installation_id": "till-1",
                             "device_mapping": {"pin": "1234", "branch": 3}},
            "data": {"tokens": {"access": "a", "refresh": "r"}, "items": [{"refresh_token": "t", "code": "ok"}]}}
    text, cut = redact.body_text(json.dumps(body))
    stored = json.loads(text)
    assert not cut
    assert stored["request_data"] == {"password": "***", "new_password": "***", "installation_id": "till-1",
                                      "device_mapping": {"pin": "***", "branch": 3}}
    assert stored["data"] == {"tokens": "***", "items": [{"refresh_token": "***", "code": "ok"}]}


def test_a_body_that_is_not_json_is_still_masked():
    text, _ = redact.body_text(b'{"username": "OPR001", "password": "hunter2", "otp": 1234, oops')
    assert "hunter2" not in text and "1234" not in text and "OPR001" in text


# -- The background writer -----------------------------------------------------------------------


def test_queued_rows_are_written_in_one_batch(world, settings, monkeypatch):
    settings.MONITORING_SYNC = False
    monkeypatch.setattr(writer, "_ensure_started", lambda: None)
    monkeypatch.setattr(writer, "_queue", queue.Queue())
    monkeypatch.setattr(writer, "_pid", __import__("os").getpid())
    now = timezone.now()
    for n in range(3):
        writer.enqueue(writer.REQUEST, {"created_at": now, "method": "POST", "path": f"/api/v1/x/{n}",
                                        "status": 200, "duration_ms": 1})
    writer.enqueue(writer.ERROR, {"created_at": now, "level": "ERROR", "logger": "x", "message": "m",
                                  "source": "other"})
    assert not RequestLog.objects.exists()                                   # only queued so far
    writer.flush()
    assert RequestLog.objects.count() == 3 and ErrorLog.objects.count() == 1


def test_a_full_queue_drops_rows_instead_of_blocking(settings, monkeypatch):
    settings.MONITORING_SYNC = False
    monkeypatch.setattr(writer, "_ensure_started", lambda: None)
    monkeypatch.setattr(writer, "_queue", queue.Queue(maxsize=1))
    monkeypatch.setattr(writer, "_dropped", 0)
    writer.enqueue(writer.REQUEST, {"n": 1})
    writer.enqueue(writer.REQUEST, {"n": 2})                                  # returns at once
    assert writer._dropped == 1 and writer._queue.qsize() == 1


def test_expired_rows_are_purged_and_the_rest_kept(db, settings):
    now = timezone.now()
    old_request, old_error = now - datetime.timedelta(days=31), now - datetime.timedelta(days=91)
    for when in (old_request, now):
        RequestLog.objects.create(created_at=when, method="POST", path="/api/v1/x", status=200, duration_ms=1)
    for when in (old_error, now - datetime.timedelta(days=60)):
        ErrorLog.objects.create(created_at=when, level="ERROR", logger="x", message="m", source="other")
    assert purge(lock=True) == {"RequestLog": 1, "ErrorLog": 1}
    assert RequestLog.objects.count() == 1 and ErrorLog.objects.count() == 1


# -- Who may read them -----------------------------------------------------------------------------


SCREENS = ["/monitoring/requests/", "/monitoring/errors/"]


def test_a_company_user_is_refused_even_with_every_box_ticked(client, world):
    role = Role.objects.create(company=world["company"], name="Everything")
    grant_all(role)                                                          # includes the log pages
    User.objects.create_user("boss", PASSWORD, display_name="Boss", company=world["company"], role=role,
                             allowed_channels=[Channel.WEB])
    client.post("/login/", {"username": "boss", "password": PASSWORD})
    log = RequestLog.objects.create(created_at=timezone.now(), method="POST", path="/api/v1/x", status=200,
                                    duration_ms=1)
    error = ErrorLog.objects.create(created_at=timezone.now(), level="ERROR", logger="x", message="m",
                                    source="other")
    for url in SCREENS + [f"/monitoring/requests/{log.pk}/", f"/monitoring/errors/{error.pk}/"]:
        assert client.get(url).status_code == 403, url
    sidebar = client.get("/dashboard/").content.decode()
    assert "Device Requests" not in sidebar and "Error Logs" not in sidebar
    user = User.objects.get(username__iexact="boss")
    assert privileges.matrix_context(user, "monitoring")["permissions"] == []


def test_a_system_user_reads_filters_and_opens_both_logs(client, world, system_user):
    login(client)                                                            # one 200 ...
    call(client, FARES, {"date": "2026-09-30"})                              # ... and one 401
    listed = client.get("/monitoring/requests/").content.decode()
    assert "Device Requests" in listed and LOGIN in listed and FARES in listed
    failed = client.get("/monitoring/requests/?status=4xx&app=operator").content.decode()
    assert FARES in failed and LOGIN not in failed
    by_device = client.get("/monitoring/requests/?device=till-1").content.decode()
    assert LOGIN in by_device and FARES not in by_device
    for text in ("till-1", "fares", "not_authenticated"):                    # the one search box
        found = client.get(f"/monitoring/requests/?q={text}").content.decode()
        assert (FARES in found) != (text == "till-1") and (LOGIN in found) == (text == "till-1"), text
    tomorrow = (timezone.localdate() + datetime.timedelta(days=1)).isoformat()
    assert LOGIN not in client.get(f"/monitoring/requests/?from={tomorrow}").content.decode()

    log = RequestLog.objects.get(path=LOGIN)
    detail = client.get(f"/monitoring/requests/{log.pk}/").content.decode()
    assert "&quot;password&quot;: &quot;***&quot;" in detail and PASSWORD not in detail

    error = ErrorLog.objects.create(created_at=timezone.now(), level="ERROR", logger="apps.fare",
                                    message="it broke", source="api", request_id=log.pk, path=LOGIN)
    errors = client.get("/monitoring/errors/?q=broke").content.decode()
    assert "it broke" in errors
    assert f"/monitoring/requests/{log.pk}/" in client.get(f"/monitoring/errors/{error.pk}/").content.decode()
    assert "it broke" in client.get(f"/monitoring/requests/{log.pk}/").content.decode()
    sidebar = client.get("/dashboard/").content.decode()
    assert "Device Requests" in sidebar and "Error Logs" in sidebar
    # A detail screen keeps its list's sidebar entry marked.
    marked = re.search(r'byky-menu-item active">\s*<a href="([^"]+)"', detail)
    assert marked and marked.group(1) == "/monitoring/requests/"
