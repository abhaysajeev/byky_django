"""Logging every device API call, without slowing it down.

For every request: starts the request context (core/request_context.py) the
error handler reads. For /api/v1/ requests also: reads the body before the view
does, times the call, and after the reply hands one row -- status, device, user
and the raw bodies -- to the background writer, which parses, masks and cuts
the bodies off the request thread (`finish`). The reply carries X-Request-ID,
the row's id, so a support call can quote it.

Sits right after GZipMiddleware, so it sees each reply uncompressed. Building
the row is wrapped: a logging failure never changes the reply.
"""

import json
import time

from django.utils import timezone

from apps.monitoring import writer
from apps.monitoring.redact import body_text
from core import request_context
from core.network import client_ip

API_PREFIX = "/api/v1/"
# Keys for the raw bodies a queued row carries until the writer finishes it.
RAW_REQUEST, RAW_RESPONSE = "_raw_request", "_raw_response"


class RequestLogMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = request_context.start(request)
        try:
            if not request.path.startswith(API_PREFIX):
                return self.get_response(request)
            started_at, started = timezone.now(), time.monotonic()
            try:
                body = request.body              # read now: Django keeps it for the view
            except Exception:                   # noqa: BLE001 -- an unreadable body is the view's to refuse
                body = b""
            response = self.get_response(request)
            try:
                response["X-Request-ID"] = str(request.request_id)
                writer.enqueue(writer.REQUEST, _row(request, response, body, started_at,
                                                    time.monotonic() - started))
            except Exception:                   # noqa: BLE001 -- never let the log change the reply
                writer._warn("could not build a request log row")
            return response
        finally:
            request_context.end(token)


def _json(raw):
    try:
        value = json.loads(raw) if raw else None
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _row(request, response, body, started_at, elapsed):
    """What the request thread hands over: plain values and the raw bodies.
    Parsing, masking and cutting the bodies is `finish`'s job, on the writer
    thread -- a big reply (the fares download) costs the caller nothing."""
    session = getattr(request, "app_session", None)
    user = session.user if session is not None else None
    match = request.resolver_match
    return {
        "id": request.request_id,
        "created_at": started_at,
        "app": (match.kwargs.get("app", "") if match else "") or _app_from_path(request.path),
        "method": request.method[:8],
        "path": request.path[:255],
        "url_name": (match.url_name or "")[:100] if match else "",
        "status": response.status_code,
        "duration_ms": round(elapsed * 1000),
        "ip": client_ip(request),
        "user_agent": request.META.get("HTTP_USER_AGENT", "")[:255],
        "device_id": session.device_id if session is not None else None,
        "user_id": user.pk if user is not None else None,
        "username": (user.username if user is not None else "")[:150],
        "company_id": user.company_id if user is not None else None,
        "branch_id": session.branch_id if session is not None else None,
        RAW_REQUEST: bytes(body or b""),
        RAW_RESPONSE: b"" if getattr(response, "streaming", False) else bytes(response.content),
    }


def finish(fields):
    """Turn a queued row's raw bodies into what is stored. Runs on the writer."""
    raw_request, raw_response = fields.pop(RAW_REQUEST, b""), fields.pop(RAW_RESPONSE, b"")
    sent = _json(raw_request)
    request_data = sent.get("request_data") if isinstance(sent.get("request_data"), dict) else {}
    credentials = sent.get("credentials") if isinstance(sent.get("credentials"), dict) else {}
    fields["request_body"], fields["request_truncated"] = body_text(raw_request)
    fields["response_body"], fields["response_truncated"] = body_text(raw_response)
    fields["code"] = str(_json(raw_response).get("code", ""))[:64]
    fields["installation_id"] = str(request_data.get("installation_id")
                                    or credentials.get("installation_id") or "")[:64]
    if not fields.get("username"):                     # before sign-in: who was trying
        fields["username"] = str(request_data.get("username") or "")[:150]
    return fields


def _app_from_path(path):
    parts = path[len(API_PREFIX):].split("/", 1)
    return parts[0][:16] if parts else ""
