"""Station sync: when an order changes on one tablet, the other operator
tablets signed in at that station are told -- a visible notification that also
carries the order's id, so the tablet fetches it (orders/detail).

Owner, 26 Oct 2026 (design/notifications/push-notifications.md, "Station
sync"): operator tablets only, never the one that made the change, never a
signed-out one; shown as a notification on its own quiet channel, one tray entry
per order. Missed ones are caught up with orders/changes
(services.station_changes), so a push that is lost is never a problem: nothing
is stored or retried here, unlike apps/notifications.

Sent after the change commits, from the worker; a failure to queue or send is
logged and never touches the order.
"""

import logging
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from apps.rental.models import OrderAction

log = logging.getLogger(__name__)

CHANNEL = "station_activity"     # quiet; the app creates it
TTL_SECONDS = 600                # after that the catch-up call covers it
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# The order changes the other tablets hear of, and the words for them.
VERBS = {
    OrderAction.BOOK: "booked",
    OrderAction.ADD: "vehicle added",
    OrderAction.REPLACE: "vehicle replaced",
    OrderAction.REMOVE: "vehicle removed",
    OrderAction.RETURN: "vehicle returned",
    OrderAction.PAYMENT: "payment received",
    OrderAction.SETTLE: "settled",
    "cancel": "cancelled",
}
CANCEL = "cancel"


@dataclass
class Push:
    """One message, in the shape apps/notifications/sender.py sends."""

    title: str
    body: str
    data: dict
    tag: str
    collapse_key: str
    session_id: int = 0
    android_channel: str = CHANNEL
    ttl_seconds: int = TTL_SECONDS


def order_changed(order, action, *, device=None, item=None, new_item=None):
    """After this transaction commits, tell the station's other tablets that
    `order` changed. `device` made the change (it is not told); a web or
    manager action has none."""
    if action not in VERBS:
        return                                   # a request / credit-note call: not an order change
    order_id, device_id = order.pk, getattr(device, "pk", device)
    item_id = getattr(item, "pk", None) or getattr(new_item, "pk", None)
    at = timezone.now()

    def queue():
        try:
            from apps.rental.tasks import station_push

            station_push.delay(str(order_id), str(action), device_id, str(item_id) if item_id else None,
                               at.isoformat())
        except Exception:
            log.warning("station sync for order %s not queued", order_id, exc_info=True)

    transaction.on_commit(queue)


def recipients(order, device_id):
    """The station's other operator tablets that can be pushed to."""
    from apps.portal.auth import push_sessions
    from core.enums import Channel

    sessions = push_sessions().filter(channel=Channel.OPERATOR, branch_id=order.branch_id)
    if device_id:
        sessions = sessions.exclude(device_id=device_id)
    return list(sessions.only("pk", "push_token"))


def _vehicles(order, item_id):
    """The vehicle(s) the change is about, for the body."""
    from apps.rental.models import OrderItem

    if item_id:
        item = OrderItem.objects.select_related("vehicle").filter(pk=item_id).first()
        if item is not None:
            return item.vehicle.vehicle_name
    names = list(order.items.select_related("vehicle").order_by("start_time", "created_on")
                 .values_list("vehicle__vehicle_name", flat=True))
    if len(names) > 2:
        return f"{', '.join(names[:2])} +{len(names) - 2}"
    return ", ".join(names)


def message(order, action, item_id, at):
    from core.timezones import zone_for

    zone = zone_for(order.company)
    vehicles = _vehicles(order, item_id)
    return Push(
        title=f"Order {order.order_no} {VERBS[action]}",
        body=" · ".join(part for part in (vehicles, order.branch.name) if part),
        data={"type": "order_changed", "order_id": str(order.pk), "order_no": order.order_no,
              "action": str(action), "branch_id": str(order.branch_id),
              "at": at.astimezone(zone).strftime(TIME_FORMAT)},
        tag=f"order:{order.pk}", collapse_key=str(order.pk),
    )


def push(order_id, action, device_id, item_id, at):
    """Send the message to each of the station's other tablets (the worker's
    side of order_changed). Returns how many were sent."""
    from apps.notifications.sender import PushError, get_sender
    from apps.notifications.services import DEAD_TOKEN
    from apps.portal.session_models import AppSession
    from apps.rental.models import Order

    order = Order.objects.select_related("branch", "company").filter(pk=order_id).first()
    if order is None:
        return 0
    content = message(order, action, item_id, at)
    sent = 0
    for session in recipients(order, device_id):
        content.session_id = session.pk
        try:
            get_sender().send(content, session.push_token)
            sent += 1
        except PushError as error:
            if error.code in DEAD_TOKEN:
                # The token is dead: no later push should try it. A new login brings a new one.
                AppSession.objects.filter(pk=session.pk, push_token=session.push_token).update(push_token="")
            elif error.code != "not_configured":
                log.warning("station sync to session %s failed: %s", session.pk, error.code)
    return sent
