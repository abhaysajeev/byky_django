"""The server's own logs: every device API call, and every error.

Append-only, written in batches by apps/monitoring/writer.py and purged by age
(30 days of requests, 90 of errors: settings.MONITORING_*_DAYS).

Plain ids rather than foreign keys: a log must outlive the device, user or
company it mentions, and writing one must never lock or wait on those rows.
Names worth reading later (the user's) are copied in; the rest are looked up
when a page of logs is shown.

Indexes: a BRIN on created_at is tiny and fits an append-only time series; the
btree on created_at serves "newest first", which BRIN cannot sort.
"""

from django.contrib.postgres.indexes import BrinIndex
from django.db import models
from django.db.models import Q

from core.ids import uuid7


class RequestLog(models.Model):
    """One call a device app made to /api/v1/{app}/..., and what it was told."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)   # = the X-Request-ID
    created_at = models.DateTimeField()
    app = models.CharField(max_length=16, blank=True)             # operator / manager / employee
    method = models.CharField(max_length=8)
    path = models.CharField(max_length=255)
    url_name = models.CharField(max_length=100, blank=True)
    status = models.PositiveSmallIntegerField()
    code = models.CharField(max_length=64, blank=True)             # the envelope's "code"
    duration_ms = models.PositiveIntegerField()
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True)
    installation_id = models.CharField(max_length=64, blank=True)
    device_id = models.BigIntegerField(null=True, blank=True)
    user_id = models.BigIntegerField(null=True, blank=True)
    username = models.CharField(max_length=150, blank=True)
    company_id = models.BigIntegerField(null=True, blank=True)
    branch_id = models.BigIntegerField(null=True, blank=True)
    # Masked JSON (passwords, tokens), cut at settings.MONITORING_BODY_LIMIT.
    request_body = models.TextField(blank=True)
    response_body = models.TextField(blank=True)
    request_truncated = models.BooleanField(default=False)
    response_truncated = models.BooleanField(default=False)

    class Meta:
        db_table = "request_log"
        ordering = ["-created_at"]
        indexes = [
            BrinIndex(fields=["created_at"], name="request_log_created_brin"),
            models.Index(fields=["-created_at"], name="request_log_newest"),
            models.Index(fields=["installation_id", "-created_at"], name="request_log_device"),
            models.Index(fields=["status"], condition=Q(status__gte=400), name="request_log_failed"),
        ]

    def __str__(self):
        return f"{self.method} {self.path} {self.status}"


class ErrorLog(models.Model):
    """One ERROR (or worse) record from anywhere in the server."""

    class Source(models.TextChoices):
        API = "api", "Device API"
        WEB = "web", "Web screen"
        OTHER = "other", "Background"

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    created_at = models.DateTimeField()
    level = models.CharField(max_length=10)
    logger = models.CharField(max_length=150)
    message = models.TextField()
    exception_type = models.CharField(max_length=200, blank=True)
    traceback = models.TextField(blank=True)
    source = models.CharField(max_length=8, choices=Source.choices)
    # The request it happened under: an API call's RequestLog has this id.
    request_id = models.UUIDField(null=True, blank=True)
    method = models.CharField(max_length=8, blank=True)
    path = models.CharField(max_length=255, blank=True)
    app = models.CharField(max_length=16, blank=True)
    user_id = models.BigIntegerField(null=True, blank=True)
    username = models.CharField(max_length=150, blank=True)
    company_id = models.BigIntegerField(null=True, blank=True)
    installation_id = models.CharField(max_length=64, blank=True)
    device_id = models.BigIntegerField(null=True, blank=True)

    class Meta:
        db_table = "error_log"
        ordering = ["-created_at"]
        indexes = [
            BrinIndex(fields=["created_at"], name="error_log_created_brin"),
            models.Index(fields=["-created_at"], name="error_log_newest"),
            models.Index(fields=["request_id"], name="error_log_request"),
        ]

    def __str__(self):
        return f"{self.level} {self.logger}: {self.message[:60]}"
