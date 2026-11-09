"""What the Tracking screens change: readers and settings. New behaviour, no
legacy source (the tablet-driven RmsCPUConfiguration was never used, design §8).
"""

import json
import urllib.error
import urllib.request

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.tracking.models import (
    Antenna,
    BranchTrackingSettings,
    Direction,
    Reader,
    ReaderAssignment,
    ReaderStatus,
    TrackingSettings,
)

MIN_SECONDS, MAX_SECONDS = 30, 3600
MIN_OFFLINE_SECONDS = 120          # below a reader's heartbeat it would flap between online and offline
PORTS = (1, 2, 3, 4)
GATEWAY_TIMEOUT = 2
HEALTHY_SECONDS = 300


class TrackingError(Exception):
    def __init__(self, message, field=""):
        super().__init__(message)
        self.message, self.field = message, field


def save_new_reader(device_id, *, branch, name="", user=None, now=None):
    """Add Reader's Save (D8): approve this device id at this station. A reader
    that has already connected is waiting as Pending, and that row is used."""
    device_id = (device_id or "").strip()
    if not device_id or len(device_id) > 16 or not device_id.isascii() or not device_id.isprintable():
        raise TrackingError("Enter the reader's device id, up to 16 characters.", "Device ID")
    with transaction.atomic():
        reader = Reader.objects.select_for_update().filter(device_id=device_id).first()
        if reader is None:
            try:
                with transaction.atomic():
                    reader = Reader.objects.create(device_id=device_id, status=ReaderStatus.PENDING, created_by=user)
            except IntegrityError:            # the reader connected in this same moment
                reader = Reader.objects.select_for_update().get(device_id=device_id)
        if reader.status == ReaderStatus.APPROVED:
            raise TrackingError("This reader is already approved.", "Device ID")
        if reader.status == ReaderStatus.BLOCKED:
            raise TrackingError("This reader is blocked. Unblock it first.", "Device ID")
        if reader.company_id not in (None, branch.company_id):
            raise TrackingError("This reader belongs to another company.", "Device ID")
        return approve(reader, company=branch.company, branch=branch, name=name, user=user, now=now)


@transaction.atomic
def assign(reader, branch, *, now=None):
    """Put the reader at a station, closing where it stood before."""
    now = now or timezone.now()
    current = ReaderAssignment.objects.select_for_update().filter(reader=reader, to_at__isnull=True).first()
    if current and current.branch_id == branch.pk:
        return current
    if current:
        current.to_at = now
        current.save(update_fields=["to_at", "modified_on"])
    return ReaderAssignment.objects.create(reader=reader, branch=branch, from_at=now)


def _ports(reader):
    for port in PORTS:
        Antenna.objects.get_or_create(reader=reader, port=port)


@transaction.atomic
def approve(reader, *, company, branch, name="", user=None, now=None):
    if reader.status == ReaderStatus.BLOCKED:
        raise TrackingError("Unblock this reader first.")
    if branch.company_id != company.pk:
        raise TrackingError("That station belongs to another company.", "Station")
    reader.company, reader.status, reader.enabled = company, ReaderStatus.APPROVED, True
    reader.name = (name or "").strip()[:100] or reader.name
    reader.modified_by = user
    reader.save(update_fields=["company", "status", "enabled", "name", "modified_by", "modified_on"])
    assign(reader, branch, now=now)
    _ports(reader)
    return reader


def edit(reader, *, name=None, branch=None, user=None, now=None):
    with transaction.atomic():
        if name is not None:
            reader.name = name.strip()[:100]
        reader.modified_by = user
        reader.save(update_fields=["name", "modified_by", "modified_on"])
        if branch is not None:
            if branch.company_id != reader.company_id:
                raise TrackingError("That station belongs to another company.", "Station")
            assign(reader, branch, now=now)
    return reader


def set_enabled(reader, on, *, user=None):
    if reader.status != ReaderStatus.APPROVED:
        raise TrackingError("Only an approved reader can be switched on or off.")
    reader.enabled, reader.modified_by = bool(on), user
    reader.save(update_fields=["enabled", "modified_by", "modified_on"])
    return reader


def set_keep_raw(reader, on, *, user=None):
    """Keep every raw read of this reader (owner, 9 Nov 2026)."""
    reader.keep_raw, reader.modified_by = bool(on), user
    reader.save(update_fields=["keep_raw", "modified_by", "modified_on"])
    return reader


def block(reader, *, user=None):
    """The gateway closes its connections from the next frame on."""
    reader.status, reader.enabled, reader.modified_by = ReaderStatus.BLOCKED, False, user
    reader.save(update_fields=["status", "enabled", "modified_by", "modified_on"])
    return reader


def unblock(reader, *, user=None):
    if reader.status != ReaderStatus.BLOCKED:
        raise TrackingError("This reader is not blocked.")
    reader.status, reader.modified_by = ReaderStatus.PENDING, user
    reader.save(update_fields=["status", "modified_by", "modified_on"])
    return reader


def set_direction(reader, port, direction):
    if port not in PORTS:
        raise TrackingError("No such antenna port.")
    if direction and direction not in Direction.values:
        raise TrackingError("Choose a direction.", "Direction")
    antenna, _ = Antenna.objects.get_or_create(reader=reader, port=port)
    antenna.direction = direction or ""
    antenna.save(update_fields=["direction", "modified_on"])
    return antenna


# -- Test connection --------------------------------------------------------------------


def gateway_status():
    """Is our side up? Answers whatever happens: the gateway may not exist here."""
    try:
        with urllib.request.urlopen(settings.TRACKING_GATEWAY_HEALTH_URL, timeout=GATEWAY_TIMEOUT) as reply:
            body = json.load(reply)
    except urllib.error.HTTPError as reply:      # 503: running, but its database is not answering
        try:
            body = json.load(reply)
        except ValueError:
            body = {}
    except (OSError, ValueError):
        return {"running": False, "enabled": False, "mode": "", "db": False, "ok": False}
    enabled, db = bool(body.get("enabled")), bool(body.get("db"))
    return {"running": True, "enabled": enabled, "mode": body.get("mode", ""), "db": db, "ok": enabled and db}


def connection_report(reader, now=None):
    """What we have heard from a reader. `reader` is None when never heard."""
    now = now or timezone.now()
    gateway = gateway_status()
    def age(moment):
        return int((now - moment).total_seconds()) if moment else None
    heartbeat, data = (age(reader.last_heartbeat_at), age(reader.last_data_at)) if reader else (None, None)
    contact = min((a for a in (heartbeat, data) if a is not None), default=None)
    connected = contact is not None and contact < HEALTHY_SECONDS
    sending = data is not None and data < HEALTHY_SECONDS
    return {
        "gateway": gateway,
        "reader": {"known": reader is not None, "status": reader.status if reader else "",
                   "name": reader.name if reader else "", "ip": str(reader.last_ip or "") if reader else "",
                   "firmware": reader.firmware if reader else "", "heartbeat_age": heartbeat, "data_age": data},
        "connected": connected,
        "sending": sending,
        "healthy": gateway["ok"] and connected and sending,
    }


# -- Tracking Settings: company default, per-branch overrides (owner, 9 Nov 2026) ---------


def _seconds(value, label, *, required, low=MIN_SECONDS):
    if value in (None, ""):
        if required:
            raise TrackingError("Enter a number of seconds.", label)
        return None
    try:
        number = int(str(value).strip())
    except ValueError:
        raise TrackingError("Enter a whole number of seconds.", label) from None
    if not low <= number <= MAX_SECONDS:
        raise TrackingError(f"Must be between {low} seconds and 60 minutes.", label)
    return number


def _number(value, label, low, high):
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise TrackingError("Enter a whole number.", label) from None
    if not low <= number <= high:
        raise TrackingError(f"Must be between {low} and {high}.", label)
    return number


@transaction.atomic
def save_settings(company, data, branches, *, user=None):
    """Everything at once, so a bad row saves nothing."""
    default = data.get("default") or {}
    missing = _seconds(default.get("missing_after_s"), "Company default: Missing after", required=True)
    offline = _seconds(default.get("offline_after_s"), "Company default: Reader offline after", required=True,
                       low=MIN_OFFLINE_SECONDS)
    retention = _number(data.get("retention_days"), "Raw retention", 1, 14)
    allowance = _number(data.get("allowance_out_db"), "Boundary allowance", 1, 15)

    posted = {str(row.get("branch")): row for row in data.get("branches") or []}
    rows = []
    for branch in branches:
        row = posted.get(str(branch.pk)) or {}
        own_missing = _seconds(row.get("missing_after_s"), f"{branch.name}: Missing after", required=False)
        own_offline = _seconds(row.get("offline_after_s"), f"{branch.name}: Reader offline after", required=False,
                               low=MIN_OFFLINE_SECONDS)
        rows.append((branch, bool(row.get("alerts_on")), own_missing, own_offline))

    TrackingSettings.objects.update_or_create(company=company, defaults={
        "missing_after_s": missing, "offline_after_s": offline, "retention_days": retention,
        "allowance_out_db": allowance, "modified_by": user})
    for branch, alerts_on, own_missing, own_offline in rows:
        BranchTrackingSettings.objects.update_or_create(branch=branch, defaults={
            "alerts_on": alerts_on, "missing_after_s": own_missing, "offline_after_s": own_offline,
            "modified_by": user})
