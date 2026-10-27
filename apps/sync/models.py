"""Parked sync calls: a tablet's upload the server refused, kept for the office.

A tablet records offline and uploads later. When an upload is refused and the
operator cannot fix it on the tablet, they park it: the call is resent with
`credentials.park: true`, the server runs it once more, and if it is still
refused stores it here and answers `parked`. Later calls for the same order,
from any tablet, are parked behind it (followers). Office staff correct the
body on the web and replay it, as the original session; its followers replay
after it, in the order they arrived (apps/sync/parking.py, services.py).

The head of an order's queue is never stored: it is the order's lowest-id open
issue, so resolving or dismissing one moves the queue on by itself.
"""

from django.core.serializers.json import DjangoJSONEncoder
from django.db import models

from core.enums import Channel


class IssueKind(models.TextChoices):
    BOOKING = "booking", "Booking"
    ADD = "add", "Add vehicle"
    REPLACE = "replace", "Replace vehicle"
    REMOVE = "remove", "Remove vehicle"
    RETURN = "return", "Return vehicle"
    PAYMENTS = "payments", "Payments"
    SETTLE = "settle", "Settle"
    CREDIT_NOTE = "credit_note", "Credit note request"
    CREDIT_NOTE_CANCEL = "credit_note_cancel", "Credit note cancel"
    REQUEST = "request", "Manager request"
    REQUEST_WITHDRAW = "request_withdraw", "Request withdraw"
    REQUEST_USED = "request_used", "Reprint printed"
    CARD_DISCOUNT_APPROVAL = "card_discount_approval", "Card discount approval"
    ATTENDANCE = "attendance", "Attendance"
    SERVICE_REQUEST = "service_request", "Service request"
    IMAGE_SHARE = "image_share", "Image share"
    ASSET_CHECK = "asset_check", "Asset check"


class IssueReason(models.TextChoices):
    REFUSED = "refused", "Refused"
    SERVER_ERROR = "server_error", "Server error"
    FOLLOWER = "follower", "Waiting behind an earlier call"


class IssueStatus(models.TextChoices):
    OPEN = "open", "Open"
    RESOLVED = "resolved", "Resolved"
    DISMISSED = "dismissed", "Dismissed"


class AttemptSource(models.TextChoices):
    PARK = "park", "Parked by the tablet"
    REPLAY = "replay", "Replayed from the web"


class SyncIssue(models.Model):
    # A whole number, as the tablet shows it ("issue 12").
    id = models.BigAutoField(primary_key=True)

    company = models.ForeignKey("company.Company", on_delete=models.PROTECT, related_name="+")
    # The session it arrived on: a replay runs as this session, even closed.
    session = models.ForeignKey("portal.AppSession", on_delete=models.PROTECT, related_name="+")
    device = models.ForeignKey("devices.Device", null=True, blank=True, on_delete=models.PROTECT,
                               related_name="+")
    branch = models.ForeignKey("company.Branch", null=True, blank=True, on_delete=models.PROTECT,
                               related_name="+")
    user = models.ForeignKey("core.User", on_delete=models.PROTECT, related_name="+")
    app = models.CharField(max_length=16, choices=Channel.choices)

    kind = models.CharField(max_length=32, choices=IssueKind.choices)
    sync_id = models.UUIDField(unique=True)
    # The order the call belongs to -- its queue. None for attendance.
    order_id = models.UUIDField(null=True, blank=True)

    request_data = models.JSONField(encoder=DjangoJSONEncoder)              # as last tried
    original_request_data = models.JSONField(encoder=DjangoJSONEncoder)     # as the tablet sent it

    reason = models.CharField(max_length=16, choices=IssueReason.choices)
    error_code = models.CharField(max_length=64, blank=True)
    error_message = models.TextField(blank=True)
    error_data = models.JSONField(default=dict, blank=True, encoder=DjangoJSONEncoder)

    status = models.CharField(max_length=16, choices=IssueStatus.choices, default=IssueStatus.OPEN)
    received_at = models.DateTimeField()
    # The Device Request (and any Error Log) of the call that parked it.
    request_id = models.UUIDField(null=True, blank=True)
    reply = models.JSONField(null=True, blank=True, encoder=DjangoJSONEncoder)   # the reply that resolved it

    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey("core.User", null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="+")
    dismissed_at = models.DateTimeField(null=True, blank=True)
    dismissed_by = models.ForeignKey("core.User", null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name="+")
    dismiss_reason = models.CharField(max_length=255, blank=True)
    dismissed_with = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL,
                                       related_name="+")

    class Meta:
        db_table = "sync_issue"
        ordering = ["-id"]
        indexes = [
            models.Index(fields=["order_id", "id"], condition=models.Q(status="open"),
                         name="sync_issue_open_order"),
            models.Index(fields=["device", "-id"], name="sync_issue_device"),
            models.Index(fields=["status", "-id"], name="sync_issue_status"),
        ]

    def __str__(self):
        return f"Issue {self.pk}"

    @property
    def is_open(self):
        return self.status == IssueStatus.OPEN


class SyncIssueAttempt(models.Model):
    """Every run of a parked call: the tablet's park call, then each replay --
    with the body as it was run and what the server answered."""

    issue = models.ForeignKey(SyncIssue, on_delete=models.CASCADE, related_name="attempts")
    at = models.DateTimeField()
    by = models.ForeignKey("core.User", null=True, blank=True, on_delete=models.SET_NULL,
                           related_name="+")                     # None: the tablet's own park call
    source = models.CharField(max_length=16, choices=AttemptSource.choices)
    request_data = models.JSONField(encoder=DjangoJSONEncoder)
    http_status = models.PositiveSmallIntegerField()
    code = models.CharField(max_length=64)
    message = models.TextField(blank=True)
    reply = models.JSONField(null=True, blank=True, encoder=DjangoJSONEncoder)

    class Meta:
        db_table = "sync_issue_attempt"
        ordering = ["at", "id"]

    def __str__(self):
        return f"{self.issue} {self.source} {self.code}"
