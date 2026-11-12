"""The web notification bell (design §7.1): the feed one user may see.

Reads `presence_event` only. A single per-user marker (AlertRead) says what has
been read, so there are no per-recipient rows.
"""

import datetime

from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from apps.company.scoping import branches_for, companies_for
from apps.portal.services import has_permission
from apps.tracking.models import AlertRead, EventKind, PresenceEvent, PresenceState

PAGE = "tracking.alerts"
LATEST = 12
MAX_COUNT = 99
EPOCH = datetime.datetime(2000, 1, 1, tzinfo=datetime.UTC)

_TITLES = {EventKind.LOW_BATTERY: "Low tag battery", EventKind.READER_OFFLINE: "Reader offline",
           EventKind.READER_ONLINE: "Reader back online", EventKind.READER_SILENT: "Reader hears no tags"}
_STATES = {PresenceState.MISSING: "Vehicle missing"}
_REASONS = {"not_seen": "not heard by the antennas",
            "weak_signal": "signal below the limit"}
_CRITICAL = (EventKind.READER_OFFLINE, EventKind.READER_SILENT)


def bell_for(user):
    """What the navbar needs, or None: no bell feed with tracking off or
    without the privilege. The flag first: with tracking off, a page costs no
    extra query."""
    if not settings.TRACKING_ENABLED:
        return None
    if not getattr(user, "is_authenticated", False) or not has_permission(user, PAGE, "read"):
        return None
    return {"feed_url": reverse("tracking-bell-feed"), "read_url": reverse("tracking-bell-read"),
            "csrf_cookie": settings.CSRF_COOKIE_NAME}


def events_for(user):
    """The user's companies, and within them the branches they can see."""
    return PresenceEvent.objects.filter(company__in=companies_for(user), alerts=True,
                                        branch__in=branches_for(user))


def _item(event, read_at):
    vehicle = event.vehicle.vehicle_name if event.vehicle_id else ""
    branch = event.branch.name if event.branch_id else ""
    if event.kind == EventKind.STATE:
        title = _STATES.get(event.to_state, event.get_to_state_display())
        why = _REASONS.get(event.reason, "")
    else:
        title, why = _TITLES.get(event.kind, event.get_kind_display()), ""
    if event.reader_id:
        vehicle = event.reader.name or event.reader.device_id
    severity = "critical" if event.to_state == PresenceState.MISSING or event.kind in _CRITICAL else "warning"
    target = reverse("tracking-alerts") + "?view=history"
    if event.vehicle_id:
        target += f"&vehicle={event.vehicle_id}"
    return {"id": event.pk, "title": title, "subject": " · ".join(x for x in (vehicle, branch) if x),
            "detail": " · ".join(x for x in (vehicle, branch, why) if x), "severity": severity,
            "at": event.at.isoformat(), "time": timezone.localtime(event.at).strftime("%H:%M"), "url": target,
            "is_read": event.at <= read_at}


def feed(user):
    read_at = AlertRead.objects.filter(user=user).values_list("last_read_at", flat=True).first() or EPOCH
    events = events_for(user)
    latest = events.select_related("vehicle", "branch", "reader").order_by("-at", "-pk")[:LATEST]
    unread = len(events.filter(at__gt=read_at).values_list("pk", flat=True)[:MAX_COUNT + 1])
    return {"unread": unread, "items": [_item(event, read_at) for event in latest]}
