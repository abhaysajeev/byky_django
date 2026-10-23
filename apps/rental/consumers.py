"""Websocket consumers for the rental screens (config/asgi.py routes them).

The socket only says *which* order changed; the page fetches the row over
plain HTTP (views.LiveRowsView), so what a user may see is decided in one
place, by the same scoping as every other screen.
"""

from asgiref.sync import async_to_sync
from channels.generic.websocket import JsonWebsocketConsumer

from apps.company.scoping import companies_for
from apps.portal.services import has_permission
from apps.rental.live import company_group

REFUSED = 4403        # closed before accept: not signed in, or no Live Monitoring read


class LiveConsumer(JsonWebsocketConsumer):
    """/ws/live/ -- every order change of the user's companies."""

    def connect(self):
        user = self.scope.get("user")
        if not user or not has_permission(user, "rental.live", "read"):
            self.close(code=REFUSED)
            return
        self.joined = [company_group(pk) for pk in companies_for(user).values_list("pk", flat=True)]
        for group in self.joined:
            async_to_sync(self.channel_layer.group_add)(group, self.channel_name)
        self.accept()

    def disconnect(self, code):
        for group in getattr(self, "joined", []):
            async_to_sync(self.channel_layer.group_discard)(group, self.channel_name)

    def order_changed(self, event):
        self.send_json({"type": "order.changed", "order": event["order"]})
