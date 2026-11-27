"""POST /api/v1/integrations/erpnext/<slug> -- what ERPNext's sender
(byky_app/rms_sync.py, send()) needs from RMS: 200 with a top-level `status`
of received / duplicate, 401 for a bad token, 413 for a body too large, and
every readable message kept, problems and all."""

import base64
import hashlib
import hmac
import json
import time
import uuid

import pytest
from django.core.management import call_command

from apps.integrations.erpnext import SLUGS
from apps.integrations.models import ErpnextMessage, ErpnextToken
from apps.integrations.services import hash_token
from apps.monitoring.models import RequestLog

TOKEN = "test-token-abc123"
BASE = "/api/v1/integrations/erpnext/"


@pytest.fixture
def token(db):
    return ErpnextToken.objects.create(label="erpnext-test", token_hash=hash_token(TOKEN))


def envelope(doctype="Branch", name="CREEK PARK 1", action="create", **changes):
    """A message as rms_sync.build_payload makes it."""
    body = {
        "event_id": str(uuid.uuid4()), "doctype": doctype, "name": name, "action": action,
        "modified": "2026-11-19 10:42:07.123456", "sent_at": "2026-11-19 10:42:09", "company": "BYKY",
        "data": {"name": name, "branch": name, "branch_code": "CP1", "warehouse": "Creek Park 1 - BYKY"},
    }
    body.update(changes)
    return body


def post(client, slug, body, *, token=TOKEN, raw=None, headers=None):
    data = raw if raw is not None else json.dumps(body)
    extra = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
    extra.update(headers or {})
    return client.post(BASE + slug, data=data, content_type="application/json",
                       HTTP_USER_AGENT="byky_app/1.0.0", **extra)


def test_a_message_is_kept_and_answered_received(client, token):
    body = envelope()

    response = post(client, "branch", body)

    assert response.status_code == 200
    reply = response.json()
    # Their sender reads the top-level status (rms_sync.send).
    assert reply["status"] == "received" and reply["code"] == "ok"
    message = ErpnextMessage.objects.get()
    assert reply["data"]["inbox_id"] == message.pk
    assert (message.event_id, message.slug, message.doctype, message.erp_name, message.action,
            message.erp_modified, message.company_name) == (
        body["event_id"], "branch", "Branch", "CREEK PARK 1", "create", "2026-11-19 10:42:07.123456", "BYKY")
    assert message.payload == body and message.raw_body == "" and message.problems == []
    assert message.token == token and message.user_agent == "byky_app/1.0.0" and message.source_ip
    token.refresh_from_db()
    assert token.last_used_at is not None


def test_the_same_event_id_again_is_a_duplicate(client, token):
    body = envelope()
    first = post(client, "branch", body).json()

    again = post(client, "branch", body)

    assert again.status_code == 200 and again.json()["status"] == "duplicate"
    assert again.json()["data"]["inbox_id"] == first["data"]["inbox_id"]
    assert ErpnextMessage.objects.count() == 1


@pytest.mark.parametrize("slug,doctype", sorted(SLUGS.items()))
def test_every_doctype_has_its_address(client, token, slug, doctype):
    response = post(client, slug, envelope(doctype=doctype))
    assert response.status_code == 200 and response.json()["status"] == "received"
    assert ErpnextMessage.objects.get().problems == []


def test_an_unknown_address_is_not_found(client, token):
    response = post(client, "customer", envelope())
    assert response.status_code == 404 and response.json()["code"] == "not_found"
    assert not ErpnextMessage.objects.exists()


@pytest.mark.parametrize("value", [None, "", "wrong-token", "Basic dGVzdA=="])
def test_without_a_good_token_nothing_is_kept(client, token, value):
    headers = {"HTTP_AUTHORIZATION": value} if value is not None else {}
    response = post(client, "branch", envelope(), token=None, headers=headers)
    assert response.status_code == 401 and response.json()["code"] == "not_authenticated"
    assert "status" not in response.json()
    assert not ErpnextMessage.objects.exists()


def test_a_revoked_token_is_refused(client, token):
    token.is_active = False
    token.save()
    assert post(client, "branch", envelope()).status_code == 401


def test_a_body_over_the_limit_is_413(client, token, settings):
    settings.ERPNEXT_MAX_BODY = 1024
    body = envelope(data={"description": "x" * 2000})
    response = post(client, "item", body)
    assert response.status_code == 413 and response.json()["code"] == "payload_too_large"
    assert not ErpnextMessage.objects.exists()


@pytest.mark.parametrize("raw,problem", [
    ("not json at all", "body is not valid JSON"),
    ("[1, 2, 3]", "body is not a JSON object"),
    ("", "body is empty"),
])
def test_an_unreadable_body_is_still_kept_with_its_problem(client, token, raw, problem):
    response = post(client, "branch", None, raw=raw)

    assert response.status_code == 200 and response.json()["status"] == "received"
    message = ErpnextMessage.objects.get()
    assert problem in message.problems and message.payload is None and message.raw_body == raw
    # The same body again is the same message, by a hash of address and body.
    assert post(client, "branch", None, raw=raw).json()["status"] == "duplicate"
    assert ErpnextMessage.objects.count() == 1


@pytest.mark.parametrize("changes,problem", [
    ({"event_id": None}, "event_id is missing"),
    ({"event_id": "x" * 65}, "event_id is longer than 64 characters"),
    ({"doctype": "Item"}, "doctype is 'Item'; this address is for 'Branch'"),
    ({"action": "delete"}, "action 'delete' is not one of create, update, deactivate"),
    ({"name": ""}, "name is missing"),
    ({"data": "oops"}, "data is not an object"),
    ({"modified": None}, "modified is missing"),
    ({"name": 42}, "name is not text"),
])
def test_a_faulty_envelope_is_kept_with_its_problems(client, token, changes, problem):
    response = post(client, "branch", envelope(**changes))
    assert response.status_code == 200 and response.json()["status"] == "received"
    assert problem in ErpnextMessage.objects.get().problems


def test_a_missing_data_key_is_a_problem(client, token):
    body = envelope()
    del body["data"]
    post(client, "branch", body)
    assert "data is missing" in ErpnextMessage.objects.get().problems


def test_the_ip_allow_list(client, token, settings):
    settings.ERPNEXT_ALLOWED_IPS = ["10.9.8.0/24"]
    assert post(client, "branch", envelope()).status_code == 401
    settings.ERPNEXT_ALLOWED_IPS = ["127.0.0.1"]
    assert post(client, "branch", envelope()).status_code == 200


def _signed(body, secret, timestamp=None):
    timestamp = str(int(time.time()) if timestamp is None else timestamp)
    digest = hmac.new(secret.encode(), f"{timestamp}.{body}".encode(), hashlib.sha256).digest()
    return {"HTTP_X_BYKY_TIMESTAMP": timestamp, "HTTP_X_BYKY_SIGNATURE": base64.b64encode(digest).decode()}


def test_a_signature_is_checked_when_a_secret_is_set(client, token, settings):
    settings.ERPNEXT_SIGNING_SECRET = "shh"
    raw = json.dumps(envelope())

    assert post(client, "branch", None, raw=raw).json()["code"] == "bad_signature"
    assert post(client, "branch", None, raw=raw, headers=_signed(raw, "wrong")).status_code == 401
    assert post(client, "branch", None, raw=raw, headers=_signed(raw, "shh", time.time() - 600)).status_code == 401
    assert not ErpnextMessage.objects.exists()
    response = post(client, "branch", None, raw=raw, headers=_signed(raw, "shh"))
    assert response.status_code == 200 and response.json()["status"] == "received"


def test_without_a_secret_signatures_are_not_checked(client, token):
    raw = json.dumps(envelope())
    assert post(client, "branch", None, raw=raw, headers=_signed(raw, "anything")).status_code == 200


def test_the_request_log_keeps_branch_bodies_but_not_employee_ones(client, token):
    post(client, "branch", envelope())
    post(client, "employee", envelope(doctype="Employee", name="HR-EMP-0001",
                                      data={"employee_name": "Asha K", "cell_number": "971500000000"}))

    logs = {log.path: log for log in RequestLog.objects.all()}
    branch = logs[BASE + "branch"]
    assert "CREEK PARK 1" in branch.request_body and branch.app == "integrations"
    employee = logs[BASE + "employee"]
    assert "Asha" not in employee.request_body and "971500000000" not in employee.request_body
    assert "redacted" in employee.request_body
    assert TOKEN not in branch.request_body and TOKEN not in employee.request_body
    # The inbox keeps the employee message whole -- it is where it is reviewed.
    assert ErpnextMessage.objects.get(doctype="Employee").payload["data"]["employee_name"] == "Asha K"


def test_the_token_command_shows_a_token_once_and_keeps_only_its_hash(db, capsys):
    call_command("erpnext_token", "create", "erpnext-dev")
    printed = capsys.readouterr().out.strip().splitlines()[-1]
    stored = ErpnextToken.objects.get(label="erpnext-dev")
    assert stored.token_hash == hash_token(printed) and printed not in stored.token_hash

    call_command("erpnext_token", "list")
    assert printed not in capsys.readouterr().out

    call_command("erpnext_token", "revoke", "erpnext-dev")
    stored.refresh_from_db()
    assert not stored.is_active


def test_the_token_command_refuses_a_duplicate_label(db):
    from django.core.management.base import CommandError
    call_command("erpnext_token", "create", "erpnext-dev")
    with pytest.raises(CommandError):
        call_command("erpnext_token", "create", "erpnext-dev")
    with pytest.raises(CommandError):
        call_command("erpnext_token", "revoke", "nope")


def test_a_command_made_token_is_accepted(client, db, capsys):
    call_command("erpnext_token", "create", "erpnext-dev")
    printed = capsys.readouterr().out.strip().splitlines()[-1]
    assert post(client, "brand", envelope(doctype="Brand", name="BYKY"), token=printed).status_code == 200
