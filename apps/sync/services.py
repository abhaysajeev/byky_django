"""The office's side of parked calls: replay, dismiss, and clearing old ones.

Replay runs the parked call's own `perform` (apps/sync/parking.py::views) as
the session it arrived on -- even if that session is closed, or the device has
since been re-mapped or blocked -- so its rows are the operator's and its
station the one the work was done at. Only login-state checks are skipped;
every business rule runs. The service decides from the tablet's own times
(booked_at, returned_at, settled_at ...), so a replay judges the call as of
when it happened, not as of the replay.

A replayed head that succeeds lets its order's followers run after it, in the
order they arrived; the first that fails stops there and becomes the head.
"""

import datetime
import logging

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.sync import parking
from apps.sync.models import AttemptSource, IssueReason, IssueStatus, SyncIssue, SyncIssueAttempt

log = logging.getLogger(__name__)

BATCH = 1_000
REASON_MAX = 255


class SyncActionError(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message


def _issue(issues_qs, pk):
    issue = issues_qs.filter(pk=pk).first()
    if issue is None:
        raise SyncActionError("No such issue.")
    return issue


def _queue_key(issue):
    """What a replay or dismiss locks: the order's queue, or the call itself
    when it belongs to no order."""
    return issue.order_id or issue.sync_id


def _check_head(issue):
    if not issue.is_open:
        raise SyncActionError(f"This issue is already {issue.get_status_display().lower()}.")
    ahead = parking.waiting_on(issue)
    if ahead is not None:
        raise SyncActionError(f"This call waits behind issue {ahead}. Replay or dismiss that one first.")


def replay(issues_qs, pk, *, user, request_data=None, now=None):
    """Run a head issue again, then its order's followers. Returns
    {"resolved": [issue ids], "stopped_at": issue or None}.

    `request_data`, when given, is the corrected body; its sync_id must stay
    the call's own (it is the key every retry is matched on).
    """
    issue = _issue(issues_qs, pk)
    if request_data is not None:
        if not isinstance(request_data, dict):
            raise SyncActionError("The request must be a JSON object.")
        if str(request_data.get("sync_id", "")) != str(issue.sync_id):
            raise SyncActionError("sync_id cannot be changed: it is how the tablet's retries are matched.")

    resolved = []
    with parking.queue_lock(_queue_key(issue)):
        issue.refresh_from_db()
        _check_head(issue)
        if not _run(issue, request_data if request_data is not None else issue.request_data, user, now):
            return {"resolved": resolved, "stopped_at": issue}
        resolved.append(issue.pk)
        if issue.order_id is None:
            return {"resolved": resolved, "stopped_at": None}
        followers = (SyncIssue.objects
                     .filter(company_id=issue.company_id, order_id=issue.order_id, status=IssueStatus.OPEN)
                     .order_by("id"))
        for follower in followers:
            if not _run(follower, follower.request_data, user, now):
                return {"resolved": resolved, "stopped_at": follower}
            resolved.append(follower.pk)
    return {"resolved": resolved, "stopped_at": None}


def _run(issue, body, user, now):
    """One replay of one issue, recorded as an attempt. True if it went through."""
    now = now or timezone.now()
    perform = parking.performer(issue.kind)
    try:
        response = parking.run(perform, issue.session, issue.app, body, replay=True)
        status_code = response.status_code
        code, message, data = parking.reply_parts(response)
        reply = response.data
    except Exception:
        log.exception("replay of sync issue %s crashed", issue.pk)
        status_code, code, message, data, reply = 500, "server_error", parking.SERVER_ERROR_MESSAGE, {}, None

    done = code in parking.DONE
    with transaction.atomic():
        SyncIssueAttempt.objects.create(
            issue=issue, at=now, by=user, source=AttemptSource.REPLAY, request_data=body,
            http_status=status_code, code=code, message=message, reply=reply,
        )
        issue.request_data = body
        if done:
            issue.status = IssueStatus.RESOLVED
            issue.resolved_at, issue.resolved_by, issue.reply = now, user, reply
        else:
            issue.reason = IssueReason.SERVER_ERROR if status_code >= 500 else IssueReason.REFUSED
            issue.error_code, issue.error_message, issue.error_data = code, message, data
        issue.save()
    return done


def dismiss(issues_qs, pk, *, user, reason, now=None):
    """Give up on a head issue: it and every call waiting behind it are
    dismissed. Returns the ids dismissed. The tablet is told through the
    status call (and `sync_dismissed` on a resend)."""
    reason = (reason or "").strip()
    if not reason:
        raise SyncActionError("Give a reason: the tablet shows it to the operator.")
    if len(reason) > REASON_MAX:
        raise SyncActionError(f"Keep the reason to {REASON_MAX} characters.")
    issue = _issue(issues_qs, pk)
    now = now or timezone.now()
    with parking.queue_lock(_queue_key(issue)):
        issue.refresh_from_db()
        _check_head(issue)
        with transaction.atomic():
            issue.status, issue.dismissed_at, issue.dismissed_by = IssueStatus.DISMISSED, now, user
            issue.dismiss_reason = reason
            issue.save()
            dismissed = [issue.pk]
            if issue.order_id is not None:
                followers = SyncIssue.objects.filter(
                    company_id=issue.company_id, order_id=issue.order_id, status=IssueStatus.OPEN,
                )
                dismissed += list(followers.values_list("pk", flat=True))
                followers.update(status=IssueStatus.DISMISSED, dismissed_at=now, dismissed_by=user,
                                 dismiss_reason=reason, dismissed_with=issue)
    return dismissed


def purge(*, now=None):
    """Delete resolved and dismissed issues finished more than SYNC_ISSUE_DAYS
    ago, in batches. Open issues are never deleted. Returns the count."""
    before = (now or timezone.now()) - datetime.timedelta(days=settings.SYNC_ISSUE_DAYS)
    done = (SyncIssue.objects.filter(status=IssueStatus.RESOLVED, resolved_at__lt=before)
            | SyncIssue.objects.filter(status=IssueStatus.DISMISSED, dismissed_at__lt=before))
    total = 0
    while True:
        ids = list(done.values_list("pk", flat=True)[:BATCH])
        if not ids:
            return total
        SyncIssue.objects.filter(pk__in=ids).delete()     # attempts go with them
        total += len(ids)
