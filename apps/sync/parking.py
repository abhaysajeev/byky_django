"""Parking: what every recording call goes through (design: handouts/
sync-parking-concept.html; plan "Sync parking", 13 Oct 2026).

A parkable view keeps its own rules in `perform(session, app, request_data,
replay=...)` and hands the HTTP call to `handle`, which:

1. answers a call already parked by its sync_id -- `parked` while open,
   `sync_dismissed` once the office dismissed it; a resolved one runs again,
   which every recording service answers `duplicate`;
2. parks a call for an order that already has an open issue behind it
   (a follower), from any tablet, without running it;
3. otherwise runs it. Only with `credentials.park: true` does a refusal --
   or a crash -- become an issue; without it the reply is today's.

A replay (services.replay) runs the same `perform` as the issue's own session.
"""

import contextlib
import logging
import uuid

from django.db import IntegrityError, connection, transaction
from django.utils import timezone
from rest_framework import exceptions, status

from apps.sync.models import (
    AttemptSource,
    IssueKind,
    IssueReason,
    IssueStatus,
    SyncIssue,
    SyncIssueAttempt,
)
from core.api import envelope, exception_handler, request_parts

log = logging.getLogger(__name__)

DONE = ("ok", "duplicate")
# Refusals that are not about the call itself: the tablet signs in again or
# waits, the call is fine. Never parked.
NOT_PARKABLE = {"not_authenticated", "wrong_channel", "forbidden", "rate_limited",
                "method_not_allowed", "unsupported_media_type", "not_found"}
PARKED_MESSAGE = "Sent to the office for correction."
DISMISSED_MESSAGE = "The office dismissed this call."
SERVER_ERROR_MESSAGE = "Something went wrong. Try again."

# Advisory locks are keyed (namespace, hashtext(key)); any constant will do.
LOCK_NAMESPACE = 0x5359    # "SY"

# Swagger rows every parkable call adds to its own.
SCHEMA_ROWS = (
    (200, "parked", PARKED_MESSAGE, {
        "issue_id": 12, "sync_id": "01923f8e-5b2a-7c3d-9e4f-a1b2c3d4e5f6",
        "order_id": "01923e1c-0a11-7b22-8c33-d4e5f6a7b8c9", "waiting_on": None,
        "error": {"code": "amount_mismatch", "message": "The amounts do not add up."},
    }),
    (200, "parked", PARKED_MESSAGE, {
        "issue_id": 13, "sync_id": "01923f8e-6c3b-7d4e-8f50-b2c3d4e5f6a7",
        "order_id": "01923e1c-0a11-7b22-8c33-d4e5f6a7b8c9", "waiting_on": 12, "error": None,
    }, "parked (waiting behind an earlier call)"),
    (409, "sync_dismissed", DISMISSED_MESSAGE, {
        "issue_id": 12, "sync_id": "01923f8e-5b2a-7c3d-9e4f-a1b2c3d4e5f6",
        "dismiss_reason": "Booked twice; the other booking was kept.",
    }),
)


def uuid_or_none(value):
    try:
        return uuid.UUID(str(value)) if value else None
    except (ValueError, TypeError, AttributeError):
        return None


def company_id_of(session):
    """The session's company: its device's (every app's device has one)."""
    if session.device_id:
        return session.device.company_id
    return session.user.company_id


# -- Which order a call belongs to ------------------------------------------------------

def order_key(kind, company_id, request_data):
    """The order a call belongs to, or None (attendance, or a reference the
    server cannot place). A withdraw / used / cancel names its parent, not the
    order: the parent's order if the parent is on the server, else the order
    of the parked parent call."""
    if kind in (IssueKind.ATTENDANCE, IssueKind.SERVICE_REQUEST, IssueKind.IMAGE_SHARE):
        return None                    # not about an order: nothing to queue behind
    if kind == IssueKind.BOOKING:
        return uuid_or_none(request_data.get("sync_id"))
    if kind == IssueKind.CREDIT_NOTE_CANCEL:
        return _parent_order(company_id, request_data.get("credit_note_id"), IssueKind.CREDIT_NOTE)
    if kind in (IssueKind.REQUEST_WITHDRAW, IssueKind.REQUEST_USED):
        return _parent_order(company_id, request_data.get("request_sync_id"), IssueKind.REQUEST)
    return uuid_or_none(request_data.get("order_id"))


def _parent_order(company_id, reference, parent_kind):
    from apps.rental.models import CreditNote, OrderRequest

    parent_id = uuid_or_none(reference)
    if parent_id is None:
        return None
    model = CreditNote if parent_kind == IssueKind.CREDIT_NOTE else OrderRequest
    found = model.objects.filter(pk=parent_id, company_id=company_id).values_list("order_id", flat=True).first()
    if found is None:
        found = (SyncIssue.objects.filter(sync_id=parent_id, company_id=company_id, kind=parent_kind)
                 .values_list("order_id", flat=True).first())
    return found


def head_of(company_id, key):
    """The open issue an order's calls wait behind -- its lowest-id open one."""
    if key is None:
        return None
    return (SyncIssue.objects.filter(company_id=company_id, order_id=key, status=IssueStatus.OPEN)
            .order_by("id").first())


def waiting_on(issue):
    """The head this open issue waits behind, or None when it is the head."""
    if not issue.is_open or issue.order_id is None:
        return None
    head = head_of(issue.company_id, issue.order_id)
    return head.pk if head is not None and head.pk != issue.pk else None


@contextlib.contextmanager
def queue_lock(key):
    """One call at a time per order: deciding "follower or run" and running
    must not interleave with another tablet's call, or a replay, for the same
    order. Session-level, so it spans the service's own transactions."""
    if key is None:
        yield
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_lock(%s, hashtext(%s))", [LOCK_NAMESPACE, str(key)])
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s, hashtext(%s))", [LOCK_NAMESPACE, str(key)])


# -- Running a call --------------------------------------------------------------------

def run(perform, session, app, request_data, *, replay=False):
    """perform's reply; a validation error (or any API error) answered in the
    envelope exactly as the exception handler would. Anything else raises."""
    try:
        return perform(session, app, request_data, replay=replay)
    except exceptions.APIException as exc:
        return exception_handler(exc, {})


def reply_parts(response):
    body = response.data if isinstance(response.data, dict) else {}
    return body.get("code", ""), body.get("message", ""), body.get("data") or {}


def parkable(response):
    code, _, _ = reply_parts(response)
    return response.status_code >= 400 and code not in DONE and code not in NOT_PARKABLE


def parked_reply(issue):
    error = {"code": issue.error_code, "message": issue.error_message} if issue.error_code else None
    return envelope("parked", PARKED_MESSAGE, {
        "issue_id": issue.pk,
        "sync_id": str(issue.sync_id),
        "order_id": str(issue.order_id) if issue.order_id else None,
        "waiting_on": waiting_on(issue),
        "error": error,
    })


def dismissed_reply(issue):
    return envelope("sync_dismissed", DISMISSED_MESSAGE, {
        "issue_id": issue.pk, "sync_id": str(issue.sync_id), "dismiss_reason": issue.dismiss_reason,
    }, http_status=status.HTTP_409_CONFLICT)


def handle(request, app, kind, perform):
    """The HTTP side of every parkable call. `perform` is the view's own."""
    credentials, request_data = request_parts(request)
    session = request.auth
    sync_id = uuid_or_none(request_data.get("sync_id"))
    if sync_id is None:
        # Not a call that can be tracked: the serializer refuses it as before.
        return run(perform, session, app, request_data)

    company_id = company_id_of(session)
    known = SyncIssue.objects.filter(sync_id=sync_id, company_id=company_id).first()
    if known is not None and known.status == IssueStatus.OPEN:
        return parked_reply(known)
    if known is not None and known.status == IssueStatus.DISMISSED:
        return dismissed_reply(known)

    park = credentials.get("park") is True
    key = order_key(kind, company_id, request_data)
    with queue_lock(key):
        if known is None:
            head = head_of(company_id, key)
            if head is not None:
                return parked_reply(_store(request, session, app, kind, sync_id, key, request_data,
                                           IssueReason.FOLLOWER))
        try:
            response = run(perform, session, app, request_data)
        except Exception:
            if not park:
                raise
            # The server took the call and crashed: logged as any crash (an
            # Error Log row with this request's id), then kept for replay.
            log.exception("parked a crashed %s call", kind)
            return parked_reply(_store(request, session, app, kind, sync_id, key, request_data,
                                       IssueReason.SERVER_ERROR, status_code=500, code="server_error",
                                       message=SERVER_ERROR_MESSAGE))
        if park and known is None and parkable(response):
            code, message, data = reply_parts(response)
            return parked_reply(_store(request, session, app, kind, sync_id, key, request_data,
                                       IssueReason.REFUSED, status_code=response.status_code, code=code,
                                       message=message, data=data, reply=response.data))
        return response


def _store(request, session, app, kind, sync_id, key, request_data, reason, *,
           status_code=None, code="", message="", data=None, reply=None):
    """The issue (and the park call as its first attempt), in a transaction of
    its own -- the refused or crashed call's work has already rolled back."""
    now = timezone.now()
    try:
        with transaction.atomic():
            issue = SyncIssue.objects.create(
                company_id=company_id_of(session), session=session, device_id=session.device_id,
                branch_id=session.branch_id, user_id=session.user_id, app=app, kind=kind,
                sync_id=sync_id, order_id=key, request_data=request_data,
                original_request_data=request_data, reason=reason, error_code=code,
                error_message=message, error_data=data or {}, received_at=now,
                request_id=getattr(request, "request_id", None),
            )
            if reason != IssueReason.FOLLOWER:
                SyncIssueAttempt.objects.create(
                    issue=issue, at=now, source=AttemptSource.PARK, request_data=request_data,
                    http_status=status_code, code=code, message=message, reply=reply,
                )
    except IntegrityError:
        # The same call parked at the same moment from a retry: one issue.
        existing = SyncIssue.objects.filter(sync_id=sync_id).first()
        if existing is None:
            raise
        return existing
    if kind == IssueKind.BOOKING:
        # The receipt was printed: its number is used whether or not the
        # order is on the server yet (rental.services.count_receipt).
        from apps.rental.services import count_receipt
        count_receipt(session, request_data.get("order_no"))
    return issue


# -- The parkable views, by kind --------------------------------------------------------

def views():
    """kind -> the view class whose `perform` runs that call."""
    from apps.crew.api import AttendanceMarkView
    from apps.discount.api import ApprovalRequestView
    from apps.fleet.maintenance_api import ImageShareCreateView, ServiceRequestCreateView
    from apps.rental import api as rental

    classes = (
        rental.OrderCreateView, rental.OrderAddView, rental.OrderReplaceView, rental.OrderRemoveView,
        rental.OrderReturnView, rental.OrderPaymentsView, rental.OrderSettleView,
        rental.CreditNoteRequestView, rental.CreditNoteCancelView, rental.OrderRequestView,
        rental.OrderRequestWithdrawView, rental.OrderRequestUsedView, ApprovalRequestView,
        AttendanceMarkView, ServiceRequestCreateView, ImageShareCreateView,
    )
    return {cls.park_kind: cls for cls in classes}


def performer(kind):
    return views()[kind]().perform
