"""ERPNext integration, step 1: receive and keep (design/10-erpnext-integration.md
§17). Nothing here writes an RMS master table -- approval comes in step 2."""

from django.db import models


class ErpnextToken(models.Model):
    """A bearer token ERPNext sends (RMS Settings -> Auth Token on their side).
    Only its SHA-256 is kept; the token itself is shown once, by the
    `erpnext_token create` command. One per environment that posts here."""

    label = models.CharField(max_length=60, unique=True)
    token_hash = models.CharField(max_length=64, unique=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "erpnext_token"
        ordering = ["label"]

    def __str__(self):
        return self.label


class MessageStatus(models.TextChoices):
    RECEIVED = "received", "Received"      # step 2 adds pending / approved / rejected ...


class ErpnextMessage(models.Model):
    """One message from ERPNext, kept exactly as it came (the inbox).

    The envelope's keys are copied into columns for listing; the whole body is
    kept in `payload`, or in `raw_body` when it is not a JSON object. A message
    with something wrong in it is still kept, with the faults in `problems`:
    ERPNext treats any refusal but 401/413 as final (rms_sync.send), so data
    problems are dealt with here, never bounced back (design E2)."""

    event_id = models.CharField(max_length=64, unique=True)
    slug = models.CharField(max_length=32)
    doctype = models.CharField(max_length=60, blank=True)
    erp_name = models.CharField(max_length=140, blank=True)
    action = models.CharField(max_length=20, blank=True)
    erp_modified = models.CharField(max_length=40, blank=True)      # as sent; parsed in step 2
    company_name = models.CharField(max_length=140, blank=True)
    payload = models.JSONField(null=True, blank=True)
    raw_body = models.TextField(blank=True)
    problems = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=20, choices=MessageStatus.choices, default=MessageStatus.RECEIVED)
    received_at = models.DateTimeField()
    source_ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True)
    token = models.ForeignKey(ErpnextToken, null=True, blank=True, on_delete=models.PROTECT,
                              related_name="messages")

    class Meta:
        db_table = "erpnext_message"
        ordering = ["-received_at", "-id"]
        indexes = [
            models.Index(fields=["doctype", "erp_name", "erp_modified"], name="erpnext_msg_record"),
            models.Index(fields=["-received_at"], name="erpnext_msg_newest"),
        ]

    def __str__(self):
        return f"{self.doctype} {self.erp_name} ({self.action})"
