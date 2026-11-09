"""/ws/tracking/ -- says only that something changed for the user's companies;
the page or the bell asks again over HTTP, so what a user may see is decided
in one place."""

from asgiref.sync import async_to_sync
from channels.generic.websocket import JsonWebsocketConsumer

from apps.company.scoping import companies_for
from apps.portal.services import has_permission
from apps.tracking.engine import company_group

REFUSED = 4403


class TrackingConsumer(JsonWebsocketConsumer):
    def connect(self):
        user = self.scope.get("user")
        allowed = user and (has_permission(user, "tracking.alerts", "read")
                            or has_permission(user, "tracking.status", "read"))
        if not allowed:
            self.close(code=REFUSED)
            return
        self.joined = [company_group(pk) for pk in companies_for(user).values_list("pk", flat=True)]
        for group in self.joined:
            async_to_sync(self.channel_layer.group_add)(group, self.channel_name)
        self.accept()

    def disconnect(self, code):
        for group in getattr(self, "joined", []):
            async_to_sync(self.channel_layer.group_discard)(group, self.channel_name)

    def tracking_changed(self, event):
        self.send_json({"type": "tracking.changed", "alert": event.get("alert", False)})
