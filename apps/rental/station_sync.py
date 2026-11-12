"""Station sync: when an order changes on one tablet, the other operator
tablets signed in at that station are told -- a visible notification that also
carries the order's id, so the tablet fetches it (orders/detail).

Owner, 26 Oct 2026 (design/notifications/push-notifications.md, "Station
sync"): operator tablets only, never the one that made the change, never a
signed-out one; shown as a notification on its own quiet channel, one tray entry
per order. Missed ones are caught up with orders/changes
(services.station_changes), so a push that is lost is never a problem: nothing
is stored or retried here, unlike apps/notifications.

Requests on an order (owner, 27 Oct 2026) are station-synced too: an order
request, a credit note or a card-discount claim raised, withdrawn, decided or
used reaches the order's station's other tablets the same quiet way
(`request_changed`) -- never the tablet that acted, and for a decision never the
asking tablet, which has its own audible alert (apps/notifications). A request
closed by a settle or a cancel is not pushed: the order's own push covers it.
Service requests (owner, 29 Oct 2026) are too: reported, approved, rejected and
back in service reach the station's other tablets (`service_request_changed`),
never the reporting tablet -- it has its own alerts.

Parked order calls (owner, 11 Nov 2026) are too, at multi-device stations only:
a parked call never reaches the push above, yet a parked booking means bikes are
out. It may not be an order on the server yet, so `orders/detail` cannot fetch
it and the message carries what the other tablets need (`call_parked`). A replay
is an ordinary order change, pushed as one.

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

# Request changes the other tablets hear of (apps/notifications Action values,
# plus `used` -- a reprint printed), and the words for them.
REQUEST_VERBS = {
    "raised": "requested",
    "withdrawn": "withdrawn",
    "cancelled": "withdrawn",           # a credit note taken back by a tablet
    "approved": "approved",
    "rejected": "rejected",
    "revoked": "approval revoked",
    "used": "printed",
}
DECISIONS = {"approved", "rejected", "revoked"}
ORDER_REQUEST, CREDIT_NOTE, CARD_DISCOUNT = "order_request", "credit_note", "card_discount"
REQUEST_LABELS = {
    "discount": "discount", "complimentary": "complimentary ride", "cancel": "cancel",
    "reprint": "invoice reprint", "order_reprint": "receipt reprint",
}


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
    """The station's other operator tablets that can be pushed to.
    `device_id` -- one device not told, or a list of them."""
    from apps.portal.auth import push_sessions
    from core.enums import Channel

    sessions = push_sessions().filter(channel=Channel.OPERATOR, branch_id=order.branch_id)
    left_out = [d for d in (device_id if isinstance(device_id, list | tuple) else [device_id]) if d]
    if left_out:
        sessions = sessions.exclude(device_id__in=left_out)
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
    from apps.rental.models import Order

    order = Order.objects.select_related("branch", "company").filter(pk=order_id).first()
    if order is None:
        return 0
    return _send(order, message(order, action, item_id, at), device_id)


def _send(order, content, device_id):
    from apps.notifications.sender import PushError, get_sender
    from apps.notifications.services import DEAD_TOKEN
    from apps.portal.session_models import AppSession

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


# -- Requests on an order ----------------------------------------------------------------


def _request_type(subject):
    from apps.discount.models import CardDiscountClaim
    from apps.rental.models import CreditNote, OrderRequest

    if isinstance(subject, OrderRequest):
        return ORDER_REQUEST
    if isinstance(subject, CreditNote):
        return CREDIT_NOTE
    if isinstance(subject, CardDiscountClaim):
        return CARD_DISCOUNT
    raise TypeError(f"Not an order request: {subject!r}")


def request_changed(subject, action, *, device=None):
    """After this transaction commits, tell the order's station's other tablets
    that a request on the order changed. `subject` is an OrderRequest,
    CreditNote or CardDiscountClaim; `device` acted (not told). A decision
    leaves out the asking tablet too: it has its own alert."""
    action = str(action)
    if action not in REQUEST_VERBS:
        return
    request_type = _request_type(subject)
    if request_type == ORDER_REQUEST and subject.kind == "cancel" and action == "approved":
        return                                   # the order's own `cancel` push says it
    left_out = {getattr(device, "pk", device)}
    if action in DECISIONS:
        left_out.add(subject.device_id)
    left_out = sorted(str(d) for d in left_out if d)
    subject_id, status, at = str(subject.pk), str(subject.status), timezone.now()

    def queue():
        try:
            from apps.rental.tasks import station_request_push

            station_request_push.delay(request_type, subject_id, action, status, left_out, at.isoformat())
        except Exception:
            log.warning("station sync for %s %s not queued", request_type, subject_id, exc_info=True)

    transaction.on_commit(queue)


def _request(request_type, subject_id):
    from apps.discount.models import CardDiscountClaim
    from apps.rental.models import CreditNote, OrderRequest

    model = {ORDER_REQUEST: OrderRequest, CREDIT_NOTE: CreditNote, CARD_DISCOUNT: CardDiscountClaim}[request_type]
    return model.objects.select_related("order__branch", "order__company").filter(pk=subject_id).first()


def request_message(subject, request_type, action, status, at):
    from core.timezones import zone_for

    order = subject.order
    kind = subject.kind if request_type == ORDER_REQUEST else ""
    label = (REQUEST_LABELS.get(kind, "request") if request_type == ORDER_REQUEST
             else "credit note" if request_type == CREDIT_NOTE else "card discount")
    return Push(
        title=f"Order {order.order_no} {label} {REQUEST_VERBS[action]}",
        body=order.branch.name,
        data={"type": "request_changed", "order_id": str(order.pk), "order_no": order.order_no,
              "request_type": request_type, "request_id": str(subject.pk), "kind": kind,
              "status": status, "action": action, "branch_id": str(order.branch_id),
              "at": at.astimezone(zone_for(order.company)).strftime(TIME_FORMAT)},
        tag=f"order:{order.pk}", collapse_key=str(order.pk),
    )


def request_push(request_type, subject_id, action, status, left_out, at):
    """The worker's side of request_changed. Returns how many were sent."""
    subject = _request(request_type, subject_id)
    if subject is None:
        return 0
    return _send(subject.order, request_message(subject, request_type, action, status, at), list(left_out))


# -- Test rides (apps/rental/test_rides.py) ----------------------------------------------

TEST_RIDE_VERBS = {"start": "started", "end": "ended", "cancel": "cancelled", "rented": "rented"}


def test_ride_changed(ride, action, *, device=None):
    """After this transaction commits, tell the station's other tablets that a
    test ride started, ended, was cancelled or was rented -- so none of them
    offers its vehicle meanwhile. `device` acted (not told)."""
    ride_id, device_id, at = str(ride.pk), getattr(device, "pk", device), timezone.now()

    def queue():
        try:
            from apps.rental.tasks import station_test_ride_push

            station_test_ride_push.delay(ride_id, action, device_id, at.isoformat())
        except Exception:
            log.warning("station sync for test ride %s not queued", ride_id, exc_info=True)

    transaction.on_commit(queue)


def _rider(ride):
    """Who rides: the customer, or the staff member."""
    return ride.customer_name or (ride.employee.full_name if ride.employee_id else "")


def test_ride_message(ride, action, at):
    from core.timezones import zone_for

    return Push(
        title=f"Test ride {ride.test_no} {TEST_RIDE_VERBS[action]}",
        body=" · ".join(part for part in (ride.vehicle.vehicle_name, _rider(ride), ride.branch.name) if part),
        data={"type": "test_ride_changed", "test_ride_id": str(ride.pk), "test_no": ride.test_no,
              "vehicle_id": str(ride.vehicle_id), "action": action, "status": ride.status,
              "branch_id": str(ride.branch_id),
              "at": at.astimezone(zone_for(ride.company)).strftime(TIME_FORMAT)},
        tag=f"test_ride:{ride.pk}", collapse_key=f"test_ride:{ride.pk}",
    )


def test_ride_push(ride_id, action, device_id, at):
    """The worker's side of test_ride_changed. Returns how many were sent."""
    from apps.rental.models import TestRide

    ride = TestRide.objects.select_related("vehicle", "branch", "company", "employee").filter(pk=ride_id).first()
    if ride is None:
        return 0
    return _send(ride, test_ride_message(ride, action, at), device_id)


# -- Service requests (owner, 29 Oct 2026) ---------------------------------------------------

SERVICE_VERBS = {"raised": "reported", "approved": "under maintenance", "rejected": "rejected",
                 "completed": "back in service"}


def service_request_changed(req, action):
    """After this transaction commits, tell the station's other tablets that a
    service request was reported, approved, rejected or put back in service --
    so their vehicle lists (under maintenance, warehouse service) follow. The
    reporting tablet is not told: it reported it, and has its own audible alert
    for the decisions (apps/notifications)."""
    req_id, device_id, at = str(req.pk), req.device_id, timezone.now()

    def queue():
        try:
            from apps.rental.tasks import station_service_push

            station_service_push.delay(req_id, action, device_id, at.isoformat())
        except Exception:
            log.warning("station sync for service request %s not queued", req_id, exc_info=True)

    transaction.on_commit(queue)


def service_message(req, action, at):
    from core.timezones import zone_for

    target = req.vehicle.vehicle_name if req.vehicle_id else req.asset.asset_code
    issue = req.issue_type.name if req.issue_type_id else req.issue
    return Push(
        title=f"Service {req.request_no} {SERVICE_VERBS[action]}",
        body=" · ".join(part for part in (target, issue, req.branch.name) if part),
        data={"type": "service_request_changed", "service_request_id": str(req.pk), "request_no": req.request_no,
              "target": "vehicle" if req.vehicle_id else "asset",
              # Push data is strings only: "" for the target it is not.
              "vehicle_id": str(req.vehicle_id) if req.vehicle_id else "",
              "asset_id": str(req.asset_id) if req.asset_id else "",
              "action": action, "status": req.status,
              "needs_warehouse_service": "true" if req.needs_warehouse_service else "false",
              "branch_id": str(req.branch_id),
              "at": at.astimezone(zone_for(req.company)).strftime(TIME_FORMAT)},
        tag=f"service_request:{req.pk}", collapse_key=f"service_request:{req.pk}",
    )


def service_push(req_id, action, device_id, at):
    """The worker's side of service_request_changed. Returns how many were sent."""
    from apps.fleet.models import ServiceRequest

    req = (ServiceRequest.objects.select_related("vehicle", "asset", "issue_type", "branch", "company")
           .filter(pk=req_id).first())
    if req is None:
        return 0
    return _send(req, service_message(req, action, at), device_id)


# -- Parked order calls ------------------------------------------------------------------

# The parked calls the other tablets hear of (sync.IssueKind values), and the words.
PARKED_VERBS = {
    "booking": "booking parked",
    "add": "vehicle add parked",
    "replace": "vehicle replace parked",
    "remove": "vehicle remove parked",
    "return": "vehicle return parked",
    "payments": "payment parked",
    "settle": "settle parked",
}


def call_parked(issue):
    """After a call is parked, tell the station's other tablets. `issue` is new;
    a resend of a call already parked never gets here."""
    if issue.kind not in PARKED_VERBS:
        return                                   # attendance, a customer, ...: not an order call
    issue_id, at = issue.pk, timezone.now()

    def queue():
        try:
            from apps.rental.tasks import station_parked_push

            station_parked_push.delay(issue_id, at.isoformat())
        except Exception:
            log.warning("station sync for parked call %s not queued", issue_id, exc_info=True)

    transaction.on_commit(queue)


def _as_id(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _line_vehicle(line):
    return _as_id(line.get("vehicle_id")) if isinstance(line, dict) else None


def _parked_lines(issue):
    """The lines of the booking parked for this issue's order, if there is one."""
    from apps.sync.models import IssueKind, SyncIssue

    if issue.order_id is None:
        return []
    booking = (SyncIssue.objects.filter(company_id=issue.company_id, order_id=issue.order_id,
                                        kind=IssueKind.BOOKING).order_by("id").first())
    lines = (booking.request_data or {}).get("items") if booking else None
    return lines if isinstance(lines, list) else []


def _vehicle_ids(issue):
    """The vehicles a parked call is about, read from its body as sent: it was
    parked because something in it was refused, so nothing here is assumed."""
    from apps.rental.models import OrderItem
    from apps.sync.parking import uuid_or_none

    data = issue.request_data if isinstance(issue.request_data, dict) else {}
    if issue.kind == "booking":
        lines = data.get("items")
        found = [_line_vehicle(line) for line in lines] if isinstance(lines, list) else []
    elif issue.kind == "add":
        found = [_line_vehicle(data.get("item"))]
    elif issue.kind == "replace":
        found = [_line_vehicle(data.get("new_item"))]
    elif issue.kind in ("remove", "return"):
        item_id = uuid_or_none(data.get("item_id"))
        on_server = (OrderItem.objects.filter(pk=item_id, order__company_id=issue.company_id)
                     .values_list("vehicle_id", flat=True).first()) if item_id else None
        if on_server is None and item_id:
            on_server = next((_line_vehicle(line) for line in _parked_lines(issue)
                              if isinstance(line, dict) and line.get("sync_id") == str(item_id)), None)
        found = [on_server]
    else:
        found = []
    return [vehicle for vehicle in found if vehicle is not None]


def _parked_order_no(issue):
    """The order's number: from the booking's body, the order, or the parked booking."""
    from apps.rental.models import Order
    from apps.sync.models import IssueKind, SyncIssue

    data = issue.request_data if isinstance(issue.request_data, dict) else {}
    if issue.kind == "booking":
        return str(data.get("order_no") or "")
    if issue.order_id is None:
        return ""
    number = (Order.objects.filter(pk=issue.order_id, company_id=issue.company_id)
              .values_list("order_no", flat=True).first())
    if number:
        return number
    booking = (SyncIssue.objects.filter(company_id=issue.company_id, order_id=issue.order_id,
                                        kind=IssueKind.BOOKING).order_by("id").first())
    return str((booking.request_data or {}).get("order_no") or "") if booking else ""


def _names(vehicle_ids, company_id):
    from apps.fleet.models import Vehicle

    by_id = dict(Vehicle.objects.filter(pk__in=vehicle_ids, company_id=company_id)
                 .values_list("pk", "vehicle_name"))
    names = [by_id[pk] for pk in vehicle_ids if pk in by_id]
    if len(names) > 2:
        return f"{', '.join(names[:2])} +{len(names) - 2}"
    return ", ".join(names)


def parked_message(issue, at):
    from core.timezones import zone_for

    vehicle_ids = _vehicle_ids(issue)
    number = _parked_order_no(issue)
    return Push(
        title=f"Order {number}: {PARKED_VERBS[issue.kind]}" if number else f"Order: {PARKED_VERBS[issue.kind]}",
        body=" · ".join(part for part in (_names(vehicle_ids, issue.company_id), issue.branch.name) if part),
        data={"type": "order_parked", "action": issue.kind, "issue_id": str(issue.pk),
              "order_id": str(issue.order_id) if issue.order_id else "", "order_no": number,
              "vehicle_ids": ",".join(str(pk) for pk in vehicle_ids), "branch_id": str(issue.branch_id),
              "at": at.astimezone(zone_for(issue.company)).strftime(TIME_FORMAT)},
        tag=f"parked:{issue.pk}", collapse_key=f"parked:{issue.pk}",
    )


def parked_push(issue_id, at):
    """The worker's side of call_parked; a single-device station has no one to
    tell. Returns how many were sent."""
    from apps.sync.models import SyncIssue

    issue = SyncIssue.objects.select_related("branch", "company").filter(pk=issue_id).first()
    if issue is None or issue.branch_id is None or not issue.branch.is_multi_device:
        return 0
    return _send(issue, parked_message(issue, at), issue.device_id)
