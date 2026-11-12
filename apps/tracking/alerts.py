"""Tracking alerts to the manager app (owner, 12 Nov 2026): pushed to every
signed-in manager phone whose role can read Missing & Alerts, for every
station of their company -- only for a station with Alerts on, and only while
it is open (the engine raises nothing for a closed one).

Pushed: vehicles missing (one push per station per cycle), vehicles back,
reader offline, reader back online, reader hearing no tags. Low tag battery
is listed only (tracking/alerts/current), never pushed.

Sent after the engine's cycle commits, from the worker; nothing is stored or
retried, and a failure is logged and never touches tracking.
"""

import datetime
import logging
from dataclasses import dataclass

from django.db import transaction

from apps.tracking.models import EventKind, PresenceState

log = logging.getLogger(__name__)

PAGE = "tracking.alerts"
SOUND = "tracking_alerts"        # vehicles missing, reader offline, reader hears no tags
QUIET = "tracking_updates"       # vehicles back, reader back online
TTL_SECONDS = 3600
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

MISSING, BACK = "vehicles_missing", "vehicles_back"
READER_KINDS = {EventKind.READER_OFFLINE: "reader_offline", EventKind.READER_ONLINE: "reader_online",
                EventKind.READER_SILENT: "reader_silent"}


@dataclass
class Push:
    """One message, in the shape apps/notifications/sender.py sends."""

    title: str
    body: str
    data: dict
    tag: str
    collapse_key: str
    android_channel: str
    session_id: int = 0
    ttl_seconds: int = TTL_SECONDS


def groups(events, alerting):
    """The pushes a cycle's events make, as plain data for the worker:
    [{company_id, branch_id, kind, vehicle_ids, reader_id, event_ids}].
    `alerting` = the stations open now with Alerts on."""
    by_key = {}
    for e in events:
        if e.branch_id not in alerting:
            continue
        if e.kind == EventKind.STATE and e.to_state == PresenceState.MISSING:
            kind = MISSING
        elif e.kind == EventKind.STATE and e.from_state == PresenceState.MISSING and e.to_state == PresenceState.PRESENT:
            kind = BACK
        elif e.kind in READER_KINDS:
            kind = READER_KINDS[e.kind]
        else:
            continue                      # low battery and everything else: listed, not pushed
        key = (e.branch_id, kind, e.reader_id if kind in READER_KINDS.values() else None)
        group = by_key.setdefault(key, {"company_id": e.company_id, "branch_id": e.branch_id, "kind": kind,
                                        "vehicle_ids": [], "reader_id": e.reader_id if kind in READER_KINDS.values()
                                        else None, "event_ids": []})
        if e.vehicle_id:
            group["vehicle_ids"].append(e.vehicle_id)
        group["event_ids"].append(e.pk)
    return list(by_key.values())


def after_cycle(events, now, alerting):
    """Queue the cycle's pushes once it commits. `events` are saved rows."""
    found = groups(events, alerting)
    if not found:
        return

    def queue():
        try:
            from apps.tracking.tasks import manager_alert_push

            manager_alert_push.delay(found, now.isoformat())
        except Exception:
            log.warning("tracking manager alerts not queued", exc_info=True)

    transaction.on_commit(queue)


def recipients(company_id):
    """Signed-in manager phones of the company whose role can read Missing & Alerts."""
    from apps.portal.auth import push_sessions
    from apps.portal.services import has_permission
    from core.enums import Channel

    sessions = (push_sessions().filter(channel=Channel.MANAGER, user__employee__company_id=company_id)
                .select_related("user"))
    return [s for s in sessions if has_permission(s.user, PAGE, "read")]


def _names(vehicle_ids):
    from apps.fleet.models import Vehicle

    names = dict(Vehicle.objects.filter(pk__in=vehicle_ids).values_list("pk", "vehicle_name"))
    listed = [names[pk] for pk in vehicle_ids if pk in names]
    return ", ".join(listed[:3]) + (f" +{len(listed) - 3}" if len(listed) > 3 else "")


def message(group, branch, reader, at):
    count = len(group["vehicle_ids"])
    kind = group["kind"]
    reader_name = (reader.name or reader.device_id) if reader else ""
    if kind == MISSING:
        title = f"{branch.name}: {count} vehicle{'s' if count != 1 else ''} missing"
        body, channel = _names(group["vehicle_ids"]), SOUND
    elif kind == BACK:
        title = f"{branch.name}: {count} vehicle{'s' if count != 1 else ''} back"
        body, channel = _names(group["vehicle_ids"]), QUIET
    elif kind == "reader_offline":
        title, body, channel = f"{branch.name}: reader offline", f"{reader_name} is not connected.", SOUND
    elif kind == "reader_online":
        title, body, channel = f"{branch.name}: reader back online", reader_name, QUIET
    else:
        title, body, channel = (f"{branch.name}: reader hears no tags",
                                f"{reader_name} is connected but hears no bikes. Check its antennas.", SOUND)
    from core.timezones import zone_for

    return Push(
        title=title, body=body, android_channel=channel,
        data={"type": "tracking_alert", "kind": kind, "branch_id": str(branch.pk),
              "vehicle_ids": ",".join(str(pk) for pk in group["vehicle_ids"]),
              "reader_id": str(group["reader_id"] or ""),
              "event_ids": ",".join(str(pk) for pk in group["event_ids"]),
              "at": at.astimezone(zone_for(branch.company)).strftime(TIME_FORMAT)},
        tag=f"tracking:{branch.pk}:{kind}", collapse_key=f"tracking:{branch.pk}:{kind}",
    )


def push(found, at):
    """The worker's side: send each group to the company's managers. Returns how many were sent."""
    from apps.company.models import Branch
    from apps.notifications.sender import PushError, get_sender
    from apps.notifications.services import DEAD_TOKEN
    from apps.portal.session_models import AppSession
    from apps.tracking.models import Reader

    at = at if isinstance(at, datetime.datetime) else datetime.datetime.fromisoformat(at)
    branches = {b.pk: b for b in Branch.objects.filter(pk__in={g["branch_id"] for g in found})
                .select_related("company")}
    readers = {r.pk: r for r in Reader.objects.filter(pk__in={g["reader_id"] for g in found if g["reader_id"]})}
    people = {}
    sent = 0
    for group in found:
        branch = branches.get(group["branch_id"])
        if branch is None:
            continue
        sessions = people.setdefault(group["company_id"], recipients(group["company_id"]))
        content = message(group, branch, readers.get(group["reader_id"]), at)
        for session in sessions:
            content.session_id = session.pk
            try:
                get_sender().send(content, session.push_token)
                sent += 1
            except PushError as error:
                if error.code in DEAD_TOKEN:
                    AppSession.objects.filter(pk=session.pk, push_token=session.push_token).update(push_token="")
                elif error.code != "not_configured":
                    log.warning("tracking alert to session %s failed: %s", session.pk, error.code)
    return sent
