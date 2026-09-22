"""The session table.

One row per login: it *is* the open session while `logged_out_at` is empty, and
the login history once it is set (design/03-login.md section 7).

Replaces the legacy `UserTracking` (web) and the login half of
`SfaDeviceMapping` plus `SfaDeviceMappingHistory` (apps). The legacy mixed two
jobs in one row -- device-to-branch mapping and who is logged in; here the
mapping lives on Device and the session lives here.
"""

from django.db import models

from core.enums import Channel
from core.models import TimeStampedModel


class LogoutReason(models.TextChoices):
    USER_LOGOUT = "user_logout", "Signed out"
    FORCED = "forced", "Logged out by an administrator"
    EMPLOYEE_BLOCKED = "employee_blocked", "Employee blocked"
    DEVICE_BLOCKED = "device_blocked", "Device blocked"
    REPLACED = "replaced", "Replaced by a newer sign-in"
    EXPIRED = "expired", "Expired"
    TOKEN_REUSE = "token_reuse", "Refresh token reused"


class AppSession(TimeStampedModel):
    user = models.ForeignKey("core.User", on_delete=models.PROTECT, related_name="sessions")
    channel = models.CharField(max_length=16, choices=Channel.choices)

    device = models.ForeignKey(
        "devices.Device", null=True, blank=True,
        on_delete=models.PROTECT, related_name="sessions",
    )
    branch = models.ForeignKey(
        "company.Branch", null=True, blank=True,
        on_delete=models.PROTECT, related_name="+",
    )

    logged_in_at = models.DateTimeField()
    logged_out_at = models.DateTimeField(null=True, blank=True)
    logout_reason = models.CharField(max_length=24, choices=LogoutReason.choices, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)

    # Apps: the current refresh token, hashed and rotated on every refresh.
    refresh_token_hash = models.CharField(max_length=128, blank=True)
    refresh_expires_at = models.DateTimeField(null=True, blank=True)
    refresh_rotated_at = models.DateTimeField(null=True, blank=True)

    app_version = models.CharField(max_length=20, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = "app_session"
        ordering = ["-logged_in_at"]
        constraints = [
            # One open session per user per app. The web reaches this by closing
            # older sessions; the apps by refusing the new login.
            models.UniqueConstraint(
                fields=["user", "channel"],
                condition=models.Q(logged_out_at__isnull=True),
                name="uniq_open_session_per_user_channel",
            ),
        ]
        indexes = [
            models.Index(fields=["device", "logged_out_at"]),
            models.Index(fields=["logged_in_at"]),
        ]

    def __str__(self):
        return f"{self.user} on {self.channel}"

    @property
    def is_open(self):
        return self.logged_out_at is None
