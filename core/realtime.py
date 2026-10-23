"""Live updates to open web pages, over websockets (Django Channels).

Two halves:

- `publish(group, message)` -- called by the code that changed something. It
  sends only after the transaction commits, so a page never shows a change
  that was rolled back, and it never raises: if Redis is down the change is
  still saved, the page just catches up on its next reconnect.
- `SessionUserMiddleware` -- who is on the other end of a websocket. The
  project has no django.contrib.auth, so Channels' AuthMiddlewareStack cannot
  be used; this reads the same session keys the web sign-in writes and checks
  the AppSession is still open, exactly as core.middleware does for a page.
"""

import logging
from types import SimpleNamespace

from asgiref.sync import async_to_sync
from channels.db import database_sync_to_async
from channels.layers import get_channel_layer
from channels.middleware import BaseMiddleware
from django.db import transaction

from core.middleware import _load_user

log = logging.getLogger(__name__)


def publish(group, message):
    """Send `message` (a dict with a "type") to every socket in `group`, once
    the current transaction commits -- or now, outside one."""

    def send():
        try:
            layer = get_channel_layer()
            if layer is not None:
                async_to_sync(layer.group_send)(group, message)
        except Exception:                      # never let a live update break the save
            log.warning("Live update to %s not sent", group, exc_info=True)

    transaction.on_commit(send)


@database_sync_to_async
def _user_for(session):
    return _load_user(SimpleNamespace(session=session))


class SessionUserMiddleware(BaseMiddleware):
    """Puts `scope["user"]` (a User or None) on a websocket. Sits inside
    Channels' SessionMiddlewareStack, which supplies `scope["session"]`."""

    async def __call__(self, scope, receive, send):
        session = scope.get("session")
        scope["user"] = await _user_for(session) if session is not None else None
        return await super().__call__(scope, receive, send)
