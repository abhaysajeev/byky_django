"""Calibrating a reader's antennas (design §6, owner 9 Nov 2026): park the bikes
on the boundary, pick them, let the reader listen for a few minutes, approve.

Each antenna gets two limits from what it heard: `enter` (the typical boundary
signal: a bike counts as back inside at this strength) and `leave` (the weakest
boundary signal less an allowance: below this a bike counts as out). Limits are
only written when someone approves them. Until then any read counts as present
(legacy behaviour, E6).

A new design (no legacy source: RmsVehicleRFTagDetails.RSSIValue was never set,
E6).
"""

import datetime
import statistics

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.fleet.models import Vehicle
from apps.fleet.service_requests import in_warehouse_service
from apps.rental.models import TestRide, TestRideStatus
from apps.rental.services import active_rentals
from apps.tracking import engine, upkeep
from apps.tracking.models import (
    Antenna,
    Calibration,
    CalibrationPoint,
    CalibrationStatus,
    PointZone,
    ReaderAssignment,
    Reading,
    TagLastSeen,
    TrackingSettings,
)
from apps.tracking.store import EWMA_ALPHA

CHECK_DAYS = 3
OPEN_STATES = (CalibrationStatus.OPEN, CalibrationStatus.PROPOSED)
MIN_SECONDS = 300                # the bikes stay parked at least this long
MIN_UPLOADS = 8                  # per bike, so the warm-up leaves enough samples
WARM_UP_SAMPLES = 3              # the smoothed signal has not settled before this
MIN_PORT_SAMPLES = 8             # fewer: this antenna takes the combined limits
HEARD_WITHIN_SECONDS = 300
READER_ONLINE_SECONDS = 300
DEFAULT_ALLOWANCE_DB = 3


class CalibrationError(Exception):
    def __init__(self, message, field=""):
        super().__init__(message)
        self.message, self.field = message, field


def station_of(reader):
    assignment = ReaderAssignment.objects.filter(reader=reader, to_at__isnull=True).select_related("branch").first()
    return assignment.branch if assignment else None


def _names(vehicles):
    return ", ".join(v.vehicle_name for v in vehicles)


def _check_reader(reader, now):
    if settings.TRACKING_MODE != "write":
        raise CalibrationError("Tracking is in capture mode, so readings are not being saved. Switch to write mode first.")
    times = [t for t in (reader.last_heartbeat_at, reader.last_data_at) if t]
    if not times or (now - max(times)).total_seconds() >= READER_ONLINE_SECONDS:
        raise CalibrationError("This reader is not connected. Check it on the Readers page first.")


def choosable(reader):
    """The bikes that can be picked: tagged and at this reader's station."""
    branch = station_of(reader)
    if branch is None:
        return Vehicle.objects.none()
    return (Vehicle.objects.filter(company_id=reader.company_id, current_branch=branch, is_active=True)
            .exclude(rfid_epc="").order_by("vehicle_name"))


def _check_bikes(reader, vehicle_ids, now):
    wanted = {int(pk) for pk in vehicle_ids if str(pk).isdigit()}
    if not wanted:
        raise CalibrationError("Pick at least one bike parked on the boundary.", "Bikes")
    bikes = list(choosable(reader).filter(pk__in=wanted))
    if len(bikes) != len(wanted):
        raise CalibrationError("Every bike must have an RFID tag and be at this reader's station.", "Bikes")

    ids = [b.pk for b in bikes]
    rented = active_rentals(ids)
    on_test = set(TestRide.objects.filter(vehicle_id__in=ids, status=TestRideStatus.ON_TEST)
                  .values_list("vehicle_id", flat=True))
    service = in_warehouse_service(ids)
    away = [b for b in bikes if b.pk in rented or b.pk in on_test or b.pk in service]
    if away:
        raise CalibrationError(f"Out on rent, on a test ride or in service: {_names(away)}.", "Bikes")

    since = now - datetime.timedelta(seconds=HEARD_WITHIN_SECONDS)
    heard = set(TagLastSeen.objects.filter(reader=reader, seen_at__gte=since,
                                           tag__in=[engine.tag_of(b.rfid_epc) for b in bikes])
                .values_list("tag", flat=True))
    silent = [b for b in bikes if engine.tag_of(b.rfid_epc) not in heard]
    if silent:
        raise CalibrationError(f"This reader has not heard these bikes in the last 5 minutes: {_names(silent)}.", "Bikes")
    return bikes


@transaction.atomic
def start(reader, vehicle_ids, *, user=None, now=None):
    now = now or timezone.now()
    if Calibration.objects.filter(reader=reader, status__in=OPEN_STATES).exists():
        raise CalibrationError("This reader already has a session open. Finish or discard it first.")
    _check_reader(reader, now)
    bikes = _check_bikes(reader, vehicle_ids, now)
    session = Calibration.objects.create(reader=reader, tags=[engine.tag_of(b.rfid_epc) for b in bikes], started_at=now,
                                         created_by=user)
    CalibrationPoint.objects.create(calibration=session, seq=1, label="Boundary", zone=PointZone.BOUNDARY, started_at=now)
    return session


def _rows(calibration, end):
    return Reading.objects.filter(reader_id=calibration.reader_id, tag__in=calibration.tags,
                                  received_at__gte=calibration.started_at, received_at__lte=end).values_list(
        "tag", "port", "rssi", "received_at")


def series(rows):
    """{tag: [(port, smoothed rssi), ...]}: exactly what the engine would have
    held for this bike -- per upload the strongest read, then the moving average
    (store.LAST_SEEN). Limits measured on any other number would not fit."""
    uploads = {}
    for tag, port, rssi, at in rows:
        batch = uploads.setdefault(tag, {})
        if at not in batch or rssi > batch[at][1]:
            batch[at] = (port, rssi)
    out = {}
    for tag, batch in uploads.items():
        smoothed, points = None, []
        for at in sorted(batch):
            port, rssi = batch[at]
            smoothed = rssi if smoothed is None else EWMA_ALPHA * rssi + (1 - EWMA_ALPHA) * smoothed
            points.append((port, smoothed))
        out[tag] = points
    return out


def _percentile(values, share):
    ordered = sorted(values)
    position = (len(ordered) - 1) * share
    low, high = int(position), min(int(position) + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _limits(values, allowance):
    enter, leave = round(statistics.median(values)), round(_percentile(values, 0.10) - allowance)
    return enter, min(leave, enter - 1)


def _allowance(calibration):
    row = TrackingSettings.objects.filter(company_id=calibration.reader.company_id).first()
    return row.allowance_out_db if row else DEFAULT_ALLOWANCE_DB


def propose(calibration, rows):
    """Limits per antenna. An antenna that was strongest for too few samples
    takes the limits of all the antennas together."""
    allowance = _allowance(calibration)
    per_port, pooled = {}, []
    for points in series(rows).values():
        for port, value in points[WARM_UP_SAMPLES:]:
            per_port.setdefault(port, []).append(value)
            pooled.append(value)
    if not pooled:
        return {"ports": {}, "can_approve": False, "allowance_db": allowance}
    pooled_limits = _limits(pooled, allowance)
    ports = {}
    for port in range(1, 5):
        values = per_port.get(port, [])
        own = len(values) >= MIN_PORT_SAMPLES
        enter, leave = _limits(values, allowance) if own else pooled_limits
        ports[str(port)] = {"enter_rssi": enter, "leave_rssi": leave, "samples": len(values),
                            "source": "own" if own else "pooled"}
    return {"ports": ports, "can_approve": True, "allowance_db": allowance}


def vehicle_names(tags):
    names = {}
    for name, epc in Vehicle.objects.exclude(rfid_epc="").values_list("vehicle_name", "rfid_epc"):
        names[engine.tag_of(epc)] = name
    return {tag: names.get(tag, tag) for tag in tags}


def progress(calibration, now=None):
    """What the screen shows while the bikes are parked."""
    now = now or timezone.now()
    uploads = {tag: set() for tag in calibration.tags}
    for tag, _port, _rssi, at in _rows(calibration, now):
        uploads[tag].add(at)
    names = vehicle_names(calibration.tags)
    elapsed = int((now - calibration.started_at).total_seconds())
    bikes = [{"tag": tag, "name": names[tag], "uploads": len(seen), "enough": len(seen) >= MIN_UPLOADS}
             for tag, seen in uploads.items()]
    return {"elapsed": elapsed, "need_seconds": MIN_SECONDS, "need_uploads": MIN_UPLOADS, "bikes": bikes,
            "ready": elapsed >= MIN_SECONDS and all(b["enough"] for b in bikes)}


def _open(calibration):
    if calibration.status != CalibrationStatus.OPEN:
        raise CalibrationError("This session is not in progress.")


def finish(calibration, *, now=None):
    _open(calibration)
    now = now or timezone.now()
    state = progress(calibration, now)
    if state["elapsed"] < MIN_SECONDS:
        left = MIN_SECONDS - state["elapsed"]
        raise CalibrationError(f"Keep the bikes parked for another {left} seconds.")
    short = [b["name"] for b in state["bikes"] if not b["enough"]]
    if short:
        raise CalibrationError(f"Not enough readings yet from: {', '.join(short)}.")
    calibration.result = propose(calibration, _rows(calibration, now))
    calibration.result["bikes"] = state["bikes"]
    calibration.ended_at, calibration.status = now, CalibrationStatus.PROPOSED
    calibration.save(update_fields=["result", "ended_at", "status", "modified_on"])
    calibration.points.filter(ended_at__isnull=True).update(ended_at=now)
    return calibration


@transaction.atomic
def approve(calibration, *, user, now=None):
    if calibration.status != CalibrationStatus.PROPOSED:
        raise CalibrationError("Only a proposed session can be approved.")
    if not calibration.result.get("can_approve"):
        raise CalibrationError("No limits could be worked out from this session. Discard it and try again.")
    now = now or timezone.now()
    medians = upkeep.parked_medians(calibration.reader_id)
    for port, proposal in calibration.result["ports"].items():
        Antenna.objects.update_or_create(reader_id=calibration.reader_id, port=int(port), defaults={
            "enter_rssi": proposal["enter_rssi"], "leave_rssi": proposal["leave_rssi"], "calibration": calibration,
            "baseline_rssi": medians.get(int(port)), "drift_note": ""})
    calibration.status, calibration.approved_by, calibration.approved_at = CalibrationStatus.APPROVED, user, now
    calibration.check_until = now + datetime.timedelta(days=CHECK_DAYS)
    calibration.save(update_fields=["status", "approved_by", "approved_at", "check_until", "modified_on"])
    return calibration


def discard(calibration):
    if calibration.status not in OPEN_STATES:
        raise CalibrationError("This session is already closed.")
    calibration.status = CalibrationStatus.DISCARDED
    calibration.save(update_fields=["status", "modified_on"])
    return calibration


def clear_limits(reader):
    """Back to any-read-counts, for when approved limits turn out wrong."""
    return Antenna.objects.filter(reader=reader).update(enter_rssi=None, leave_rssi=None, baseline_rssi=None,
                                                        calibration=None, drift_note="")
