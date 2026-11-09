"""The engine's housekeeping (design §2, §4, §6), far less often than a cycle.

Hourly: partitions made ahead and dropped after the retention, the dedupe key
pruned, disk and default partition checked. Daily: antenna drift and the 3-day
check after a calibration. All of it is advisory-locked, so a second engine
does nothing.
"""

import datetime
import shutil
import statistics

from django.db import connection, transaction
from django.db.models import Max
from django.utils import timezone

from apps.tracking import engine, ops, partitions
from apps.tracking.models import (
    Antenna,
    Calibration,
    CalibrationStatus,
    PresenceState,
    RawFrame,
    TagLastSeen,
    TrackingSettings,
    VehiclePresence,
)

UPKEEP_LOCK = engine.LOCK_KEY + 1
RAW_FRAME_DAYS = 14
# Longer than store.CLOCK_TOO_OLD: a read we still accept must still be a known duplicate.
KEY_DAYS = 4
CALIBRATION_OPEN_HOURS = 2
PRUNE_BATCH = 10_000
MAX_PRUNE_BATCHES = 50          # 500k per hour at most; the next hour takes the rest
LOW_DISK_SHARE = 0.10
DRIFT_DB = 6


def retention_days():
    """The partitions are shared: the longest any company asked for."""
    return TrackingSettings.objects.aggregate(days=Max("retention_days"))["days"] or 3


def _locked(cursor):
    cursor.execute("SELECT pg_try_advisory_xact_lock(%s)", [UPKEEP_LOCK])
    return cursor.fetchone()[0]


def hourly(now=None):
    """Returns what it did, or None if another engine is doing it."""
    now = now or timezone.now()
    today = now.astimezone(datetime.UTC).date()
    keep = retention_days()
    with transaction.atomic(), connection.cursor() as cursor:
        if not _locked(cursor):
            return None
        # A slow query on the readings must not make partition changes wait,
        # with every gateway insert then queued behind them.
        cursor.execute("SET LOCAL lock_timeout = '2s'")
        repaired = partitions.repair_default(cursor)
        made = partitions.ensure(cursor, today)
        dropped = partitions.drop_older(cursor, today, keep)
        stray = partitions.default_rows(cursor)
    pruned = prune_keys(now - datetime.timedelta(days=KEY_DAYS))
    expired = Calibration.objects.filter(
        status=CalibrationStatus.OPEN, started_at__lt=now - datetime.timedelta(hours=CALIBRATION_OPEN_HOURS),
    ).update(status=CalibrationStatus.DISCARDED)
    RawFrame.objects.filter(received_at__lt=now - datetime.timedelta(days=RAW_FRAME_DAYS)).delete()
    if stray:
        ops.event("default_partition_not_empty", rows=stray)
    disk = shutil.disk_usage("/")
    if disk.free / disk.total < LOW_DISK_SHARE:
        ops.event("disk_low", free_gb=round(disk.free / 2**30, 1))
    ops.event("engine_upkeep", made=[str(d) for d in made], dropped=[str(d) for d in dropped], keys_pruned=pruned,
              repaired=[str(d) for d in repaired], calibrations_expired=expired)
    return {"made": made, "dropped": dropped, "pruned": pruned, "default_rows": stray, "repaired": repaired,
            "calibrations_expired": expired}


def prune_keys(before):
    """Dedupe keys older than the retention, in batches (the one bulk delete, §4)."""
    total = 0
    for _ in range(MAX_PRUNE_BATCHES):
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM tracking_reading_key WHERE ctid = ANY(ARRAY("
                "SELECT ctid FROM tracking_reading_key WHERE received_at < %s LIMIT %s))", [before, PRUNE_BATCH])
            total += cursor.rowcount
            if cursor.rowcount < PRUNE_BATCH:
                break
    return total


def parked_medians(reader_id):
    """{port: median rssi_avg} of the bikes this reader hears that are present."""
    present = VehiclePresence.objects.filter(state=PresenceState.PRESENT).values_list("vehicle__rfid_epc", flat=True)
    tags = {engine.tag_of(epc) for epc in present}
    by_port = {}
    for last in TagLastSeen.objects.filter(reader_id=reader_id, tag__in=tags):
        by_port.setdefault(last.port, []).append(last.rssi_avg)
    return {port: round(statistics.median(values)) for port, values in by_port.items()}


def check_drift():
    """More than 6 dB from the baseline: an antenna moved, a cable, the layout (R5)."""
    medians, warned = {}, 0
    for antenna in Antenna.objects.filter(baseline_rssi__isnull=False).select_related("reader"):
        reader_medians = medians.setdefault(antenna.reader_id, parked_medians(antenna.reader_id))
        median = reader_medians.get(antenna.port)
        note = ""
        if median is not None and abs(median - antenna.baseline_rssi) > DRIFT_DB:
            note = f"Parked bikes read {median} dBm, baseline {antenna.baseline_rssi} dBm. Recalibrate this antenna."
            warned += 1
            ops.event("antenna_drift", device=antenna.reader.device_id, port=antenna.port, median=median,
                      baseline=antenna.baseline_rssi)
        if note != antenna.drift_note:
            antenna.drift_note = note
            antenna.save(update_fields=["drift_note"])
    return warned


def check_new_calibrations(now):
    """For 3 days after approval: do parked bikes stay at or above enter, and do
    rented ones fall below leave as they ride away? (design §6 step 3)"""
    checked = 0
    for calibration in Calibration.objects.filter(status=CalibrationStatus.APPROVED, check_until__gte=now):
        states = dict(VehiclePresence.objects.values_list("vehicle__rfid_epc", "state"))
        states = {engine.tag_of(epc): state for epc, state in states.items()}
        limits = {a.port: a for a in Antenna.objects.filter(reader_id=calibration.reader_id,
                                                            enter_rssi__isnull=False)}
        parked = parked_ok = rented = rented_ok = 0
        for last in TagLastSeen.objects.filter(reader_id=calibration.reader_id):
            antenna = limits.get(last.port)
            if antenna is None:
                continue
            state = states.get(last.tag)
            if state == PresenceState.PRESENT:
                parked += 1
                parked_ok += last.rssi_avg >= antenna.enter_rssi
            elif state == PresenceState.ON_RENT:
                rented += 1
                rented_ok += last.rssi_avg < antenna.leave_rssi
        result = f"{now:%d %b}: parked at/above enter {parked_ok}/{parked}, rented below leave {rented_ok}/{rented}"
        if parked_ok < parked * 0.9 or (rented and rented_ok < rented * 0.9):
            result = f"RE-CHECK {result}"
        calibration.check_result = result[:200]
        calibration.save(update_fields=["check_result", "modified_on"])
        checked += 1
    return checked


def daily(now=None):
    now = now or timezone.now()
    with transaction.atomic(), connection.cursor() as cursor:
        if not _locked(cursor):
            return None
        return {"drift_warnings": check_drift(), "calibrations_checked": check_new_calibrations(now)}
