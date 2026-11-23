"""Receiving an ERPNext message (design/10-erpnext-integration.md §4, §17).

The rules come from ERPNext's own sender (`byky_app/rms_sync.py`, send()):
- 200 with a top-level `status` of `received` or `duplicate` is success;
- 401 stops it and alerts their admin; 413 stops it;
- 429 / 5xx / timeouts are retried; any other reply is a permanent failure.
So a message RMS can read at all is always kept and answered 200 -- what is
wrong with it is noted in `problems` for the inbox, never refused.
"""

import base64
import hashlib
import hmac
import ipaddress
import json
import time

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.integrations.erpnext import ACTIONS, SLUGS
from apps.integrations.models import ErpnextMessage, ErpnextToken

SIGNATURE_MAX_AGE = 300          # seconds a signed request stays valid


def hash_token(token):
    """SHA-256 hex of a token -- what ErpnextToken keeps (apps/portal/jwt.py does the same)."""
    return hashlib.sha256(token.encode()).hexdigest()


def token_for(authorization):
    """The active ErpnextToken named by an `Authorization: Bearer <token>`
    header, or None. Looked up by its hash, so the token itself is never
    compared or stored."""
    scheme, _, token = (authorization or "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return None
    return ErpnextToken.objects.filter(token_hash=hash_token(token), is_active=True).first()


def ip_allowed(ip):
    """ERPNEXT_ALLOWED_IPS: addresses or networks (CIDR); empty allows any."""
    allowed = getattr(settings, "ERPNEXT_ALLOWED_IPS", [])
    if not allowed:
        return True
    if not ip:
        return False
    address = ipaddress.ip_address(ip)
    for entry in allowed:
        try:
            if address in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            continue
    return False


def signature_ok(timestamp, signature, raw, *, now=None):
    """ERPNEXT_SIGNING_SECRET set: X-Byky-Signature must be base64 HMAC-SHA256
    over "<timestamp>.<body>" (rms_sync._headers) and the timestamp no older
    or newer than five minutes. Unset: not checked."""
    secret = getattr(settings, "ERPNEXT_SIGNING_SECRET", "")
    if not secret:
        return True
    if not timestamp or not signature or not timestamp.isdigit():
        return False
    now = time.time() if now is None else now
    if abs(now - int(timestamp)) > SIGNATURE_MAX_AGE:
        return False
    digest = hmac.new(secret.encode(), timestamp.encode() + b"." + raw, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), signature)


def _text(value, limit, label, problems):
    """A string column from the envelope: anything else, or too long, is noted."""
    if value is None:
        return ""
    if not isinstance(value, str):
        problems.append(f"{label} is not text")
        value = json.dumps(value) if not isinstance(value, int | float) else str(value)
    if len(value) > limit:
        problems.append(f"{label} is longer than {limit} characters")
        value = value[:limit]
    return value


def read(slug, raw):
    """The envelope's values and the faults found -- never an exception.

    Returns dict(event_id, doctype, erp_name, action, erp_modified,
    company_name, payload, raw_body, problems)."""
    problems = []
    doctype = SLUGS[slug]
    try:
        body = json.loads(raw.decode("utf-8")) if raw else None
    except (UnicodeDecodeError, ValueError):
        body = None
        problems.append("body is not valid JSON")
    if body is not None and not isinstance(body, dict):
        problems.append("body is not a JSON object")
    if not raw:
        problems.append("body is empty")

    if not isinstance(body, dict):
        return {
            "event_id": "", "doctype": doctype, "erp_name": "", "action": "", "erp_modified": "",
            "company_name": "", "payload": None, "raw_body": raw.decode("utf-8", "replace"),
            "problems": problems,
        }

    event_id = body.get("event_id")
    if not isinstance(event_id, str) or not event_id.strip():
        problems.append("event_id is missing")
        event_id = ""
    elif len(event_id) > 64:
        problems.append("event_id is longer than 64 characters")
        event_id = ""
    sent_doctype = body.get("doctype")
    if sent_doctype != doctype:
        problems.append(f"doctype is {sent_doctype!r}; this address is for {doctype!r}")
    action = _text(body.get("action"), 20, "action", problems)
    if action not in ACTIONS:
        problems.append(f"action {action!r} is not one of {', '.join(ACTIONS)}")
    name = _text(body.get("name"), 140, "name", problems)
    if not name:
        problems.append("name is missing")
    if "data" in body and not isinstance(body["data"], dict):
        problems.append("data is not an object")
    elif "data" not in body:
        problems.append("data is missing")
    if not body.get("modified"):
        problems.append("modified is missing")
    return {
        "event_id": event_id.strip(), "doctype": doctype, "erp_name": name, "action": action,
        "erp_modified": _text(body.get("modified"), 40, "modified", problems),
        "company_name": _text(body.get("company"), 140, "company", problems),
        "payload": body, "raw_body": "", "problems": problems,
    }


def receive(slug, raw, *, token, source_ip, user_agent):
    """Keep one message. Returns (message, created): created False when its
    event_id was already kept -- a retry of the same change, answered
    `duplicate`. A message without a usable event_id is keyed on a hash of its
    path and body (design §4), so resending the same body is a duplicate too."""
    values = read(slug, raw)
    event_id = values.pop("event_id") or hashlib.sha256(slug.encode() + b"\n" + raw).hexdigest()
    existing = ErpnextMessage.objects.filter(event_id=event_id).first()
    if existing is not None:
        return existing, False
    try:
        with transaction.atomic():
            message = ErpnextMessage.objects.create(
                event_id=event_id, slug=slug, received_at=timezone.now(), source_ip=source_ip,
                user_agent=(user_agent or "")[:255], token=token, **values,
            )
    except IntegrityError:          # the same event_id arriving twice at once
        return ErpnextMessage.objects.get(event_id=event_id), False
    return message, True
