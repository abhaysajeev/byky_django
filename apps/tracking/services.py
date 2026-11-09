"""What the Tracking screens change: readers and settings. New behaviour, no
legacy source (the tablet-driven RmsCPUConfiguration was never used, design §8).
"""

from django.db import transaction
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
PORTS = (1, 2, 3, 4)


class TrackingError(Exception):
    def __init__(self, message, field=""):
        super().__init__(message)
        self.message, self.field = message, field


def add_reader(device_id, *, company, name="", user=None):
    """An admin can add a reader before it connects (D8). It starts pending."""
    device_id = (device_id or "").strip()
    if not device_id or len(device_id) > 16 or not device_id.isascii() or not device_id.isprintable():
        raise TrackingError("Enter the reader's device id, up to 16 characters.", "Device ID")
    if Reader.objects.filter(device_id=device_id).exists():
        raise TrackingError("A reader with this device id already exists.", "Device ID")
    return Reader.objects.create(device_id=device_id, company=company, name=(name or "").strip()[:100],
                                 status=ReaderStatus.PENDING, created_by=user)


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


# -- Tracking Settings: company default, per-branch overrides (owner, 9 Nov 2026) ---------


def _seconds(value, label, *, required):
    if value in (None, ""):
        if required:
            raise TrackingError("Enter a number of seconds.", label)
        return None
    try:
        number = int(str(value).strip())
    except ValueError:
        raise TrackingError("Enter a whole number of seconds.", label) from None
    if not MIN_SECONDS <= number <= MAX_SECONDS:
        raise TrackingError("Must be between 30 seconds and 60 minutes.", label)
    return number


def _check_row(alert, missing, label):
    if alert >= missing:
        raise TrackingError(f"Alert after must be less than missing after ({alert} s, {missing} s).", label)


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
    """Everything at once: a company default can make a station's override
    invalid, so the whole table is checked before anything is written."""
    default = data.get("default") or {}
    alert = _seconds(default.get("alert_after_s"), "Company default: Alert after", required=True)
    missing = _seconds(default.get("missing_after_s"), "Company default: Missing after", required=True)
    offline = _seconds(default.get("offline_after_s"), "Company default: Reader offline after", required=True)
    _check_row(alert, missing, "Company default")
    retention = _number(data.get("retention_days"), "Raw retention", 1, 14)

    posted = {str(row.get("branch")): row for row in data.get("branches") or []}
    rows = []
    for branch in branches:
        row = posted.get(str(branch.pk)) or {}
        own_alert = _seconds(row.get("alert_after_s"), f"{branch.name}: Alert after", required=False)
        own_missing = _seconds(row.get("missing_after_s"), f"{branch.name}: Missing after", required=False)
        own_offline = _seconds(row.get("offline_after_s"), f"{branch.name}: Reader offline after", required=False)
        _check_row(own_alert if own_alert is not None else alert,
                   own_missing if own_missing is not None else missing, branch.name)
        rows.append((branch, bool(row.get("alerts_on")), own_alert, own_missing, own_offline))

    TrackingSettings.objects.update_or_create(company=company, defaults={
        "alert_after_s": alert, "missing_after_s": missing, "offline_after_s": offline,
        "retention_days": retention, "modified_by": user})
    for branch, alerts_on, own_alert, own_missing, own_offline in rows:
        BranchTrackingSettings.objects.update_or_create(branch=branch, defaults={
            "alerts_on": alerts_on, "alert_after_s": own_alert, "missing_after_s": own_missing,
            "offline_after_s": own_offline, "modified_by": user})
