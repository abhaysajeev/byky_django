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
from django.db.models import Q
from django.utils import timezone

from apps.fleet.models import Vehicle
from apps.fleet.service_requests import in_warehouse_service
from apps.rental.models import TestRide, TestRideStatus
from apps.rental.services import active_rentals
from apps.tracking import engine, upkeep
from apps.tracking.models import (
    Antenna,
    Calibration,
    CalibrationMethod,
    CalibrationPoint,
    CalibrationStatus,
    PointZone,
    ReaderAssignment,
    Reading,
    TagLastSeen,
    TrackingSettings,
    VehicleBaseline,
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
        raise CalibrationError("Every bike must have an RFID tag and be at this reader's branch.", "Bikes")

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


# -- Observe: each vehicle's own range over a past period (owner, 18 Nov 2026) ---------------
#
# The old RMS way: no bikes parked on the boundary. Pick a past period of
# normal parking -- its readings are already stored, so the answer is
# immediate -- and every tagged bike at the station gets its own range:
# low (10th percentile), high (90th) and their average. Percentiles, not the
# raw minimum and maximum: one stray read would otherwise set the range.
# Approved, those values are fixed for the station; a bike then counts as out
# once its signal stays below its own low less the allowance (the engine's
# existing rule, with the vehicle's limits in place of the antenna's).

MIN_OBSERVE_MINUTES = 30
MIN_OBSERVE_SAMPLES = 20          # per vehicle, after each stretch's warm-up
SEGMENT_GAP_SECONDS = 300         # a longer silence starts a new smoothed stretch
DEFAULT_RETENTION_DAYS = 3


def _retention_days(reader):
    row = TrackingSettings.objects.filter(company_id=reader.company_id).first()
    return row.retention_days if row else DEFAULT_RETENTION_DAYS


def _away_intervals(vehicle_ids, window_from, window_to):
    """{vehicle_id: [(from, to)]}: rented or on a test ride during the window --
    readings then are of a bike riding, not parked, and are left out."""
    from apps.rental.models import OrderItem

    away = {}
    lines = OrderItem.objects.filter(vehicle_id__in=vehicle_ids, start_time__lt=window_to).filter(
        Q(end_time__isnull=True) | Q(end_time__gt=window_from)).values_list("vehicle_id", "start_time", "end_time")
    rides = TestRide.objects.filter(vehicle_id__in=vehicle_ids, started_at__lt=window_to).filter(
        Q(ended_at__isnull=True) | Q(ended_at__gt=window_from)).values_list("vehicle_id", "started_at", "ended_at")
    for vehicle_id, start, end in [*lines, *rides]:
        away.setdefault(vehicle_id, []).append((start, end or window_to))
    return away


def observed_series(rows, away):
    """{tag: [smoothed rssi, ...]} as the engine would have held it while the
    bike was parked: per upload the strongest read, the moving average, a new
    stretch after a long silence or a time away, and each stretch's first
    samples dropped while the average settles."""
    uploads = {}
    for tag, _port, rssi, at in rows:
        if any(start <= at < end for start, end in away.get(tag, ())):
            continue
        batch = uploads.setdefault(tag, {})
        if at not in batch or rssi > batch[at]:
            batch[at] = rssi
    out = {}
    for tag, batch in uploads.items():
        values, smoothed, last, settled = [], None, None, 0
        for at in sorted(batch):
            if last is None or (at - last).total_seconds() > SEGMENT_GAP_SECONDS:
                smoothed, settled = None, 0
            smoothed = batch[at] if smoothed is None else EWMA_ALPHA * batch[at] + (1 - EWMA_ALPHA) * smoothed
            settled += 1
            last = at
            if settled > WARM_UP_SAMPLES:
                values.append(smoothed)
        out[tag] = values
    return out


def vehicle_range(values, allowance):
    low, high = _percentile(values, 0.10), _percentile(values, 0.90)
    enter = round(low)
    return {"low_rssi": enter, "high_rssi": round(high), "mid_rssi": round((low + high) / 2),
            "enter_rssi": enter, "leave_rssi": enter - max(allowance, 1), "samples": len(values)}


@transaction.atomic
def observe(reader, window_from, window_to, *, user=None, now=None):
    """Work out each vehicle's range from a past period's readings, as a
    proposed session. Nothing changes until it is approved."""
    now = now or timezone.now()
    if settings.TRACKING_MODE != "write":
        raise CalibrationError("Tracking is in capture mode, so readings are not being saved. Switch to write mode first.")
    if Calibration.objects.filter(reader=reader, status__in=OPEN_STATES).exists():
        raise CalibrationError("This reader already has a session open. Finish or discard it first.")
    if window_from is None or window_to is None:
        raise CalibrationError("Choose the period: from and to.", "Period")
    if window_to > now:
        raise CalibrationError("The period must be in the past.", "Period")
    if (window_to - window_from).total_seconds() < MIN_OBSERVE_MINUTES * 60:
        raise CalibrationError(f"The period must be at least {MIN_OBSERVE_MINUTES} minutes.", "Period")
    days = _retention_days(reader)
    if window_from < now - datetime.timedelta(days=days):
        raise CalibrationError(f"Readings are kept for {days} days. Choose a period within them.", "Period")
    bikes = list(choosable(reader))
    if not bikes:
        raise CalibrationError("No tagged bikes are at this reader's station.")

    by_tag = {engine.tag_of(b.rfid_epc): b for b in bikes}
    away = _away_intervals([b.pk for b in bikes], window_from, window_to)
    away_by_tag = {engine.tag_of(b.rfid_epc): away.get(b.pk, []) for b in bikes}
    rows = Reading.objects.filter(reader_id=reader.pk, tag__in=list(by_tag), received_at__gte=window_from,
                                  received_at__lte=window_to).values_list("tag", "port", "rssi", "received_at")
    series_by_tag = observed_series(rows, away_by_tag)
    allowance = row.allowance_out_db if (row := TrackingSettings.objects.filter(
        company_id=reader.company_id).first()) else DEFAULT_ALLOWANCE_DB

    vehicles, skipped = {}, []
    for tag, bike in sorted(by_tag.items(), key=lambda item: item[1].vehicle_name):
        values = series_by_tag.get(tag, [])
        if len(values) < MIN_OBSERVE_SAMPLES:
            skipped.append({"tag": tag, "vehicle_id": bike.pk, "name": bike.vehicle_name, "samples": len(values)})
            continue
        vehicles[tag] = {"vehicle_id": bike.pk, "name": bike.vehicle_name, **vehicle_range(values, allowance)}
    session = Calibration.objects.create(
        reader=reader, method=CalibrationMethod.OBSERVE, status=CalibrationStatus.PROPOSED,
        tags=list(vehicles), started_at=window_from, ended_at=window_to, created_by=user,
        result={"vehicles": vehicles, "skipped": skipped, "allowance_db": allowance,
                "min_samples": MIN_OBSERVE_SAMPLES, "can_approve": bool(vehicles)})
    return session


def _approve_observe(calibration):
    """Fix each vehicle's range at this reader: the new set replaces the old."""
    VehicleBaseline.objects.filter(reader_id=calibration.reader_id).delete()
    VehicleBaseline.objects.bulk_create([VehicleBaseline(
        reader_id=calibration.reader_id, tag=tag, vehicle_id=v["vehicle_id"], low_rssi=v["low_rssi"],
        high_rssi=v["high_rssi"], mid_rssi=v["mid_rssi"], enter_rssi=v["enter_rssi"], leave_rssi=v["leave_rssi"],
        samples=v["samples"], calibration=calibration) for tag, v in calibration.result["vehicles"].items()])


@transaction.atomic
def approve(calibration, *, user, now=None):
    if calibration.status != CalibrationStatus.PROPOSED:
        raise CalibrationError("Only a proposed session can be approved.")
    if not calibration.result.get("can_approve"):
        raise CalibrationError("No limits could be worked out from this session. Discard it and try again.")
    now = now or timezone.now()
    if calibration.method == CalibrationMethod.OBSERVE:
        _approve_observe(calibration)
        calibration.status, calibration.approved_by, calibration.approved_at = CalibrationStatus.APPROVED, user, now
        calibration.check_until = now + datetime.timedelta(days=CHECK_DAYS)
        calibration.save(update_fields=["status", "approved_by", "approved_at", "check_until", "modified_on"])
        return calibration
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
    """Back to any-read-counts, for when approved limits turn out wrong: the
    antennas' limits and the vehicles' own ranges both go."""
    VehicleBaseline.objects.filter(reader=reader).delete()
    return Antenna.objects.filter(reader=reader).update(enter_rssi=None, leave_rssi=None, baseline_rssi=None,
                                                        calibration=None, drift_note="")
