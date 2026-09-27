"""The request being served, reachable from anywhere during it.

Set by apps.monitoring.middleware for every request, read by the error-log
handler: an error logged deep inside a service still knows which request,
user, device and app it belongs to, without anyone passing them down.

A context variable, not a thread-local: correct under threads and asyncio alike.
Holding the request itself (not copies of its fields) means nothing is looked up
unless an error is actually logged.
"""

import contextvars
from dataclasses import dataclass, field

from core.ids import uuid7


@dataclass
class RequestContext:
    request: object
    id: object = field(default_factory=uuid7)


_current = contextvars.ContextVar("byky_request_context", default=None)


def start(request):
    """Begin a request's context; returns the token `end` needs."""
    context = RequestContext(request)
    request.request_id = context.id
    return _current.set(context)


def end(token):
    _current.reset(token)


def current():
    """The RequestContext being served, or None outside a request."""
    return _current.get()
