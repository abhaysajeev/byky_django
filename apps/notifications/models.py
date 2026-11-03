"""Push notifications to the apps (FCM v1), first release: approval requests.

One row per notification per recipient session, written in the same
transaction as the change it tells about (an outbox): if Redis or Firebase is
down the row waits and the minute sweep sends it. The rows are also what the
in-app notification list will read later (`read_at`).

Owner decisions, 21 Oct 2026: a push goes to an app on a device only while
that app is signed in there (the token lives on the AppSession); managers get
every branch of their company; a decision goes only to the device that sent
the request; the text says only what happened and where.
"""

from django.db import models

from core.models import TimeStampedModel


class SubjectType(models.TextChoices):
    ORDER_REQUEST = "order_request", "Order request"
    CARD_DISCOUNT = "card_discount", "Card discount request"
    CREDIT_NOTE = "credit_note", "Credit note request"
    SERVICE_REQUEST = "service_request", "Service request"
    # The employee app (owner, 30 Oct 2026).
    LEAVE_REQUEST = "leave_request", "Leave request"
    DUTY_ROSTER = "duty_roster", "Duty roster"          # subject_id: one per save, not a row
    ATTENDANCE = "attendance", "Attendance"


class Action(models.TextChoices):
    RAISED = "raised", "Raised"
    WITHDRAWN = "withdrawn", "Withdrawn"
    CANCELLED = "cancelled", "Cancelled"
    CLOSED = "closed", "Closed"
    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"
    REVOKED = "revoked", "Revoked"
    COMPLETED = "completed", "Back in service"     # a service request, on the web
    UPDATED = "updated", "Updated"                 # a duty roster saved
    MARKED = "marked", "Marked"                    # attendance marked for them on a tablet


class Audience(models.TextChoices):
    MANAGER = "manager", "Manager app"
    OPERATOR = "operator", "Operator app"
    EMPLOYEE = "employee", "Employee app"


class AndroidChannel(models.TextChoices):
    """The Android notification channels the apps create."""

    APPROVALS_NEW = "approvals_new", "Approvals"              # with sound
    APPROVALS_UPDATES = "approvals_updates", "Approval updates"   # quiet
    EMPLOYEE_ALERTS = "employee_alerts", "Employee alerts"        # with sound (employee app)


class NotificationStatus(models.TextChoices):
    PENDING = "pending", "Waiting to send"
    SENT = "sent", "Sent"
    FAILED = "failed", "Failed"
    SKIPPED = "skipped", "Not sent"      # signed out by then, or Firebase not set up


class Notification(TimeStampedModel):
    id = models.UUIDField(primary_key=True, editable=False)
    company = models.ForeignKey("company.Company", on_delete=models.PROTECT, related_name="+")
    subject_type = models.CharField(max_length=20, choices=SubjectType.choices)
    subject_id = models.UUIDField()
    action = models.CharField(max_length=12, choices=Action.choices)
    audience = models.CharField(max_length=10, choices=Audience.choices)
    # The app on the device it is for; the token is read from it when sending.
    session = models.ForeignKey("portal.AppSession", on_delete=models.PROTECT, related_name="notifications")

    # What the phone shows. `tag` is the same for every push about one item,
    # so a later one replaces the earlier on the phone.
    title = models.CharField(max_length=120)
    body = models.CharField(max_length=200)
    tag = models.CharField(max_length=60)
    android_channel = models.CharField(max_length=24, choices=AndroidChannel.choices)
    data = models.JSONField(default=dict)       # what the tap opens; strings only (FCM)

    status = models.CharField(max_length=10, choices=NotificationStatus.choices,
                              default=NotificationStatus.PENDING)
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField()
    sent_at = models.DateTimeField(null=True, blank=True)
    fcm_message_id = models.CharField(max_length=200, blank=True)
    error = models.CharField(max_length=255, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)       # for the in-app list (later)

    class Meta:
        db_table = "notification"
        ordering = ["-created_on"]
        indexes = [
            models.Index(fields=["status", "next_attempt_at"]),
            models.Index(fields=["session", "created_on"]),
            models.Index(fields=["subject_type", "subject_id"]),
        ]

    def __str__(self):
        return f"{self.title} -> {self.session_id}"
