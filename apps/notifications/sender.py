"""Talking to Firebase. `get_sender()` is the FCM v1 sender when a service-
account key file is configured (FCM_CREDENTIALS_FILE), and a log-only stand-in
until the Firebase projects exist: nothing is sent, rows end `skipped`."""

import logging
from datetime import timedelta

from django.conf import settings

log = logging.getLogger(__name__)


class PushError(Exception):
    """A send that did not go through. `code` is Google's error, in lower
    case (unregistered, invalid_argument, quota_exceeded, unavailable, ...)."""

    def __init__(self, code, message="", retry_after=None):
        super().__init__(message or code)
        self.code, self.retry_after = code, retry_after


class LogSender:
    """Firebase not set up yet: log what would have gone out."""

    def send(self, notification, token):
        log.info("push not sent (FCM not configured): %s -> session %s", notification.title,
                 notification.session_id)
        raise PushError("not_configured", "FCM_CREDENTIALS_FILE is not set")


class FcmSender:
    """FCM HTTP v1 through firebase-admin, signed with the service account."""

    def __init__(self, credentials_file):
        import firebase_admin
        from firebase_admin import credentials

        try:
            self.app = firebase_admin.get_app("byky")
        except ValueError:
            self.app = firebase_admin.initialize_app(credentials.Certificate(credentials_file), name="byky")

    def message(self, notification, token):
        from firebase_admin import messaging

        # A Notification row, or a station-sync push (apps/rental/station_sync.py)
        # that also sets how long it may wait and what it collapses with.
        return messaging.Message(
            token=token,
            notification=messaging.Notification(title=notification.title, body=notification.body),
            data={key: str(value) for key, value in notification.data.items()},
            android=messaging.AndroidConfig(
                priority="high",
                ttl=timedelta(seconds=getattr(notification, "ttl_seconds", None) or settings.PUSH_TTL_SECONDS),
                collapse_key=getattr(notification, "collapse_key", None),
                notification=messaging.AndroidNotification(
                    tag=notification.tag, channel_id=notification.android_channel),
            ),
        )

    def send(self, notification, token):
        from firebase_admin import exceptions, messaging

        try:
            return messaging.send(self.message(notification, token), app=self.app)
        except exceptions.FirebaseError as error:
            raise PushError(_code(error), str(error), _retry_after(error)) from None


def _code(error):
    from firebase_admin import messaging

    if isinstance(error, messaging.UnregisteredError):
        return "unregistered"
    if isinstance(error, messaging.SenderIdMismatchError):
        return "sender_id_mismatch"
    if isinstance(error, messaging.QuotaExceededError):
        return "quota_exceeded"
    code = (getattr(error, "code", "") or "").lower()
    return {"invalid_argument": "invalid_argument", "unavailable": "unavailable",
            "internal": "internal"}.get(code, "unknown")


def _retry_after(error):
    response = getattr(error, "http_response", None)
    value = response.headers.get("Retry-After") if response is not None else None
    try:
        return float(value) if value else None
    except ValueError:
        return None


_SENDER = None


def get_sender():
    global _SENDER
    if _SENDER is None:
        path = settings.FCM_CREDENTIALS_FILE
        _SENDER = FcmSender(path) if path else LogSender()
    return _SENDER
