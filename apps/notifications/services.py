"""Who is told what, and the sending (design/notifications/push-notifications.md).

`notify(subject, action)` is called by the approval flows inside their own
transaction, only when the status really changed. It writes one Notification
per recipient session and queues the send after commit. Recipients:

- managers -- every manager-app session of the subject's company that is open
  and holds a push token: every branch, no permission check (owner, 21 Oct
  2026);
- operator -- the operator-app session open on the device that sent the
  request, and only that device.

The push is the doorbell, the API is the door: the text says only what
happened and where (owner: minimal, no amounts or names), and the tap makes
the app read the item from the APIs it already uses.
"""

import logging
import random
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.notifications.models import (
    Action,
    AndroidChannel,
    Audience,
    Notification,
    NotificationStatus,
    SubjectType,
)
from apps.notifications.sender import PushError, get_sender
from apps.portal.auth import push_sessions
from core.enums import Channel
from core.ids import uuid7

log = logging.getLogger(__name__)

# Order request kinds, as the phone names them.
KIND_LABEL = {
    "discount": "Discount", "complimentary": "Complimentary ride", "cancel": "Cancel order",
    "reprint": "Invoice reprint", "order_reprint": "Receipt reprint",
}

# Who hears about each action.
AUDIENCES = {
    Action.RAISED: (Audience.MANAGER,),
    Action.WITHDRAWN: (Audience.MANAGER,),
    Action.CANCELLED: (Audience.MANAGER,),
    Action.CLOSED: (Audience.MANAGER,),
    Action.APPROVED: (Audience.OPERATOR, Audience.MANAGER),
    Action.REJECTED: (Audience.OPERATOR, Audience.MANAGER),
    Action.REVOKED: (Audience.OPERATOR, Audience.MANAGER),
    Action.COMPLETED: (Audience.OPERATOR, Audience.MANAGER),
}

# Firebase answers that mean the token is gone for good.
DEAD_TOKEN = {"unregistered", "sender_id_mismatch"}
# ... and the ones worth trying again.
RETRY = {"unavailable", "internal", "quota_exceeded", "unknown"}


# -- What the item is ------------------------------------------------------------------

def _describe(subject):
    """(subject_type, label, branch, device_id, order_id, extra data) of an
    OrderRequest, CardDiscountClaim, CreditNote or ServiceRequest."""
    from apps.discount.models import CardDiscountClaim
    from apps.fleet.models import ServiceRequest
    from apps.rental.models import CreditNote, OrderRequest

    if isinstance(subject, OrderRequest):
        return (SubjectType.ORDER_REQUEST, KIND_LABEL.get(subject.kind, "Approval"), subject.branch,
                subject.device_id, subject.order_id, {"kind": subject.kind})
    if isinstance(subject, CardDiscountClaim):
        return SubjectType.CARD_DISCOUNT, "Card discount", subject.branch, subject.device_id, subject.order_id, {}
    if isinstance(subject, CreditNote):
        return SubjectType.CREDIT_NOTE, "Credit note", subject.branch, subject.device_id, subject.order_id, {}
    if isinstance(subject, ServiceRequest):
        # No order: the reporting tablet hears the outcome (device_id).
        return (SubjectType.SERVICE_REQUEST, "Service request", subject.branch, subject.device_id, None,
                {"request_no": subject.request_no, "target": "vehicle" if subject.vehicle_id else "asset"})
    raise TypeError(f"Not a notification subject: {subject!r}")


def _title(label, action, audience):
    """What happened, in a few words. The operator is told the outcome of
    their own request ("Discount approved"); managers the state of a request."""
    request = label if label.endswith("request") else f"{label} request"   # "Service request"
    if action == Action.RAISED:
        return f"New {request.lower()}"
    if action == Action.REVOKED:
        return f"{label} approval revoked"
    if action == Action.COMPLETED:
        return "Back in service"
    if audience == Audience.OPERATOR:
        return f"{label} {action}"
    return f"{request} {action}"


def _channel(action, audience):
    if audience == Audience.MANAGER and action != Action.RAISED:
        return AndroidChannel.APPROVALS_UPDATES      # an item already on their phone changed: quiet
    return AndroidChannel.APPROVALS_NEW


def recipients(audience, company_id, device_id, *, now=None):
    """The session ids a push for this audience goes to."""
    sessions = push_sessions(now=now)
    if audience == Audience.MANAGER:
        sessions = sessions.filter(channel=Channel.MANAGER, user__employee__company_id=company_id)
    elif device_id is None:
        return []                      # made on the web: no tablet to tell
    else:
        sessions = sessions.filter(channel=Channel.OPERATOR, device_id=device_id)
    return list(sessions.values_list("pk", flat=True))


# -- Writing -----------------------------------------------------------------------------

def notify(subject, action, *, now=None):
    """Write the notifications for one change and queue them after commit.
    Returns the rows written."""
    now = now or timezone.now()
    subject_type, label, branch, device_id, order_id, extra = _describe(subject)
    tag = f"{subject_type}:{subject.pk}"
    rows = []
    for audience in AUDIENCES[action]:
        for session_id in recipients(audience, subject.company_id, device_id, now=now):
            pk = uuid7()
            rows.append(Notification(
                id=pk, company_id=subject.company_id, subject_type=subject_type, subject_id=subject.pk,
                action=action, audience=audience, session_id=session_id,
                title=_title(label, action, audience), body=branch.name if branch else "",
                tag=tag, android_channel=_channel(action, audience),
                data={"notification_id": str(pk), "subject_type": subject_type, "subject_id": str(subject.pk),
                      "action": action, "order_id": str(order_id) if order_id else "",
                      "branch_id": str(branch.pk) if branch else "", **extra},
                next_attempt_at=now,
            ))
    if rows:
        Notification.objects.bulk_create(rows)
        ids = [str(row.pk) for row in rows]
        transaction.on_commit(lambda: queue(ids))
    return rows


def notify_all(subjects, action, *, now=None):
    for subject in subjects:
        notify(subject, action, now=now)


def queue(ids, countdown=None):
    """Hand the rows to the worker. If the queue cannot be reached they stay
    pending and the minute sweep sends them."""
    from apps.notifications.tasks import send

    try:
        send.apply_async(args=[ids], countdown=countdown)
    except Exception:  # noqa: BLE001 -- a broker outage must not fail the change that caused it
        log.warning("could not queue %d notifications", len(ids), exc_info=True)


# -- Sending -----------------------------------------------------------------------------

def _live(session, now):
    return (session.logged_out_at is None and session.refresh_expires_at is not None
            and session.refresh_expires_at > now and bool(session.push_token))


def _backoff(attempts, error):
    """Google's rule: double the wait each time, with jitter; quota errors
    start at a minute; Retry-After wins when it asks for longer."""
    base = 60 if error.code == "quota_exceeded" else 1
    delay = base * (2 ** (attempts - 1)) * random.uniform(1, 1.5)  # noqa: S311 -- jitter, not security
    return max(delay, error.retry_after or 0)


def send_one(notification_id, *, now=None):
    """Send one pending notification. Returns the seconds after which it
    should be tried again, or None."""
    now = now or timezone.now()
    with transaction.atomic():
        row = (Notification.objects.select_for_update(skip_locked=True, of=("self",)).select_related("session")
               .filter(pk=notification_id, status=NotificationStatus.PENDING).first())
        if row is None:
            return None                # sent already, or another worker has it
        session = row.session
        if not _live(session, now):
            row.status, row.error = NotificationStatus.SKIPPED, "signed_out"
            row.save(update_fields=["status", "error", "modified_on"])
            return None
        row.attempts += 1
        try:
            row.fcm_message_id = get_sender().send(row, session.push_token) or ""
        except PushError as error:
            return _failed(row, session, error, now)
        row.status, row.sent_at, row.error = NotificationStatus.SENT, now, ""
        row.save(update_fields=["status", "sent_at", "error", "attempts", "fcm_message_id", "modified_on"])
    return None


def _failed(row, session, error, now):
    retry_in = None
    row.error = f"{error.code}: {error}"[:255]
    if error.code == "not_configured":
        row.status = NotificationStatus.SKIPPED
    elif error.code in DEAD_TOKEN:
        row.status = NotificationStatus.FAILED
        # The token is dead: no later push should try it. A new login brings a new one.
        type(session).objects.filter(pk=session.pk, push_token=session.push_token).update(push_token="")
    elif error.code in RETRY and row.attempts < settings.PUSH_MAX_ATTEMPTS:
        retry_in = _backoff(row.attempts, error)
        row.next_attempt_at = now + timedelta(seconds=retry_in)
    else:
        row.status = NotificationStatus.FAILED
    row.save(update_fields=["status", "error", "attempts", "next_attempt_at", "modified_on"])
    return retry_in


def due(now=None, limit=500):
    """Pending rows whose time has come -- the sweep's work."""
    return (Notification.objects.filter(status=NotificationStatus.PENDING, next_attempt_at__lte=now or timezone.now())
            .order_by("next_attempt_at").values_list("pk", flat=True)[:limit])


def purge(now=None):
    """Rows past the keep period, except those still waiting."""
    cutoff = (now or timezone.now()) - timedelta(days=settings.PUSH_KEEP_DAYS)
    deleted, _ = Notification.objects.filter(created_on__lt=cutoff).exclude(
        status=NotificationStatus.PENDING).delete()
    return deleted
