"""ERROR records into the Error Logs screen, with the request they belong to.

Attached to the root logger (settings.LOGGING), so it sees what the code
already logs: API crashes (core/api.py exception_handler), web 500s (Django's
"django.request"), and every log.exception() in the apps. The request, user,
device and app come from core/request_context.py.

Guards:
  - records from "apps.monitoring" itself and from the database layer are
    ignored, so a broken log table cannot feed itself;
  - Django's own "Internal Server Error" line (no traceback) after an error
    that has already been logged for the same request is skipped, so a crash
    is one row, not two.
"""

import logging
import traceback as traceback_module

from django.utils import timezone

from apps.monitoring import writer
from core import request_context

IGNORED = ("apps.monitoring", "django.db.backends")


class DatabaseErrorHandler(logging.Handler):
    def emit(self, record):
        try:
            if record.name.startswith(IGNORED):
                return
            context = request_context.current()
            request = context.request if context is not None else getattr(record, "request", None)
            # Django logs "Internal Server Error" after the middleware chain has
            # returned -- outside the request context, but with the request
            # attached. Skip it when this request's error is already recorded.
            if record.name == "django.request" and not record.exc_info \
                    and getattr(request, "_error_logged", False):
                return
            writer.enqueue(writer.ERROR, _row(record, context))
            if request is not None:
                request._error_logged = True
        except Exception:                    # noqa: BLE001
            self.handleError(record)


def _row(record, context):
    request = context.request if context is not None else getattr(record, "request", None)
    exc_type = record.exc_info[0] if record.exc_info and record.exc_info[0] else None
    trace = "".join(traceback_module.format_exception(*record.exc_info)) if exc_type else (record.stack_info or "")
    row = {
        "created_at": timezone.now(),
        "level": record.levelname[:10],
        "logger": record.name[:150],
        "message": record.getMessage(),
        "exception_type": f"{exc_type.__module__}.{exc_type.__qualname__}"[:200] if exc_type else "",
        "traceback": trace,
        "source": "other",
    }
    if request is None:
        return row
    path = getattr(request, "path", "") or ""
    is_api = path.startswith("/api/")
    session = getattr(request, "app_session", None)
    user = session.user if session is not None else _web_user(request)
    match = getattr(request, "resolver_match", None)
    row.update({
        "source": "api" if is_api else "web",
        "request_id": getattr(request, "request_id", None) or (context.id if context else None),
        "method": (getattr(request, "method", "") or "")[:8],
        "path": path[:255],
        "app": (match.kwargs.get("app", "") if match and is_api else "")[:16],
        "user_id": getattr(user, "pk", None),
        "username": (getattr(user, "username", "") or "")[:150],
        "company_id": getattr(user, "company_id", None),
        "device_id": session.device_id if session is not None else None,
    })
    return row


def _web_user(request):
    """The signed-in web user, if the session middleware got that far --
    without loading anything the request has not already loaded."""
    user = getattr(request, "user", None)
    return user if getattr(user, "pk", None) else None
