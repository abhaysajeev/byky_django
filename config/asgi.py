"""ASGI entry: plain HTTP goes to Django as before; websockets (/ws/...) to the
consumers. gunicorn still serves the site over WSGI -- this file is what the
`realtime` container's uvicorn runs, for the websockets."""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django_app = get_asgi_application()   # first: sets Django up before the imports below

from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from channels.security.websocket import AllowedHostsOriginValidator  # noqa: E402
from channels.sessions import SessionMiddlewareStack  # noqa: E402
from django.urls import path  # noqa: E402

from apps.rental.consumers import LiveConsumer  # noqa: E402
from core.realtime import SessionUserMiddleware  # noqa: E402

websocket_urls = [path("ws/live/", LiveConsumer.as_asgi())]

application = ProtocolTypeRouter({
    "http": django_app,
    "websocket": AllowedHostsOriginValidator(
        SessionMiddlewareStack(SessionUserMiddleware(URLRouter(websocket_urls)))
    ),
})
