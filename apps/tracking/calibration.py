"""Calibrating a reader's antennas (design §6): walk tagged bikes round the
station, stop at labelled points, and turn what the antennas heard into an
enter and a leave limit per antenna.

Limits are only written when someone approves them. Until then an antenna has
none and any read counts as present (legacy behaviour, E6). When inside and
outside overlap, no limits are proposed: the fix is physical.

A new design (no legacy source: RmsVehicleRFTagDetails.RSSIValue was never set,
E6); the method and its research basis are in design §6.
"""

import datetime
import statistics

from django.db import transaction
from django.utils import timezone

from apps.tracking import engine, upkeep
from apps.tracking.models import (
    Antenna,
    Calibration,
    CalibrationMethod,
    CalibrationPoint,
    CalibrationStatus,
    PointZone,
    Reading,
    TrackingSettings,
)

CHECK_DAYS = 3
FULL_LEAVE_MARGIN_DB = 2
OPEN_STATES = (CalibrationStatus.OPEN, CalibrationStatus.PROPOSED)


class CalibrationError(Exception):
    def __init__(self, message, field=""):
        super().__init__(message)
        self.message, self.field = message, field


def parse_tags(raw):
    """The calibration bikes' tags, from free text; each 8-hex."""
    tags = []
    for word in str(raw or "").replace(",", " ").split():
        tag = engine.tag_of(word)
        if len(tag) != 8 or any(c not in "0123456789ABCDEF" for c in tag):
            raise CalibrationError(f"{word} is not a tag number.", "Tags")
        if tag not in tags:
            tags.append(tag)
    if not tags:
        raise CalibrationError("Enter the tag number of at least one calibration bike.", "Tags")
    return tags


def start(reader, method, tags, *, user=None, now=None):
    if method not in CalibrationMethod.values:
        raise CalibrationError("Choose a method.", "Method")
    if Calibration.objects.filter(reader=reader, status__in=OPEN_STATES).exists():
        raise CalibrationError("This reader already has a session open. Finish or discard it first.")
    return Calibration.objects.create(reader=reader, method=method, tags=parse_tags(tags),
                                      started_at=now or timezone.now(), created_by=user)


def _open(calibration):
    if calibration.status != CalibrationStatus.OPEN:
        raise CalibrationError("This session is not in progress.")


def next_point(calibration, label, zone, *, variant="", now=None):
    """Close the stop in progress and start the next: readings between a stop's
    start and end belong to it."""
    _open(calibration)
    label = (label or "").strip()
    if not label:
        raise CalibrationError("Name this point, for example Corner NE.", "Point")
    if zone not in PointZone.values:
        raise CalibrationError("Choose inside, boundary or outside.", "Zone")
    now = now or timezone.now()
    calibration.points.filter(ended_at__isnull=True).update(ended_at=now)
    last = calibration.points.order_by("-seq").first()
    return CalibrationPoint.objects.create(calibration=calibration, seq=(last.seq + 1) if last else 1, label=label[:60],
                                           zone=zone, variant=(variant or "").strip()[:30], started_at=now)


def _percentile(values, share):
    ordered = sorted(values)
    position = (len(ordered) - 1) * share
    low, high = int(position), min(int(position) + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def point_samples(calibration):
    """[(point, {port: [rssi...]})] from the raw reads of the session's tags. One
    query over the session's whole span; raw readings are kept for days, so a
    session has to be finished within the retention."""
    points = list(calibration.points.all())
    if not points:
        return []
    start_at, end_at = points[0].started_at, max(p.ended_at or timezone.now() for p in points)
    rows = Reading.objects.filter(reader_id=calibration.reader_id, tag__in=calibration.tags,
                                  received_at__gte=start_at, received_at__lte=end_at).values_list(
        "port", "received_at", "rssi")
    result = [(p, {}) for p in points]
    for port, at, rssi in rows:
        for point, by_port in result:
            if point.started_at <= at < (point.ended_at or end_at + datetime.timedelta(seconds=1)):
                by_port.setdefault(port, []).append(rssi)
                break
    return result


def _limits(zones, method, allowance_in, allowance_out):
    """(enter, leave, note): limits from {zone: [rssi]}, or None and why not."""
    inside, boundary, outside = (zones.get(z, []) for z in ("inside", "boundary", "outside"))
    if method == CalibrationMethod.FULL:
        if not inside or not outside:
            return None, None, "Needs both inside and outside points."
        enter, far = _percentile(inside, 0.10), _percentile(outside, 0.90)
        if far > enter:
            return None, None, f"Inside and outside overlap: outside reaches {far:.0f} dBm, inside drops to {enter:.0f}."
        leave = far + FULL_LEAVE_MARGIN_DB
    else:
        if not boundary:
            return None, None, "Needs boundary points."
        middle = statistics.median(boundary)
        if outside and _percentile(outside, 0.90) > middle:
            return None, None, (f"Outside spots read as strong as the boundary "
                                f"({_percentile(outside, 0.90):.0f} dBm against {middle:.0f}).")
        enter, leave = middle + allowance_in, _percentile(boundary, 0.10) - allowance_out
    enter, leave = round(enter), round(leave)
    if enter <= leave:
        return None, None, f"The limits would cross (enter {enter}, leave {leave})."
    return enter, leave, ""


def propose(calibration, samples=None):
    """Per port: reads and median per zone, and the proposed limits or why there
    are none. A point counts for the antenna that heard it strongest, the same
    feature the engine decides on; a port never strongest takes the pooled limits."""
    settings_row = TrackingSettings.objects.filter(company_id=calibration.reader.company_id).first()
    allowance_in = settings_row.allowance_in_db if settings_row else 3
    allowance_out = settings_row.allowance_out_db if settings_row else 3

    samples = point_samples(calibration) if samples is None else samples
    points, per_port, pooled = [], {}, {}
    for point, by_port in samples:
        medians = {port: statistics.median(values) for port, values in by_port.items()}
        strongest = max(medians, key=medians.get) if medians else None
        points.append({"seq": point.seq, "label": point.label, "zone": point.zone, "variant": point.variant,
                       "reads": sum(len(v) for v in by_port.values()), "strongest": strongest,
                       "medians": {str(p): round(m, 1) for p, m in sorted(medians.items())}})
        if strongest is not None:
            per_port.setdefault(strongest, {}).setdefault(point.zone, []).extend(by_port[strongest])
            pooled.setdefault(point.zone, []).extend(by_port[strongest])

    pooled_enter, pooled_leave, pooled_note = _limits(pooled, calibration.method, allowance_in, allowance_out)
    ports = {}
    for port in range(1, 5):
        zones = per_port.get(port)
        if zones:
            enter, leave, note = _limits(zones, calibration.method, allowance_in, allowance_out)
            source = "own"
        else:
            enter, leave, note, source = pooled_enter, pooled_leave, pooled_note, "pooled"
        ports[str(port)] = {
            "enter_rssi": enter, "leave_rssi": leave, "note": note, "source": source,
            "zones": {zone: {"reads": len(values), "median": round(statistics.median(values), 1)}
                      for zone, values in (zones or {}).items()},
        }
    return {"points": points, "ports": ports, "can_approve": all(p["enter_rssi"] is not None for p in ports.values())}


def finish(calibration, *, now=None):
    _open(calibration)
    now = now or timezone.now()
    calibration.points.filter(ended_at__isnull=True).update(ended_at=now)
    if not calibration.points.exists():
        raise CalibrationError("Record at least one point first.")
    calibration.result = propose(calibration)
    calibration.ended_at, calibration.status = now, CalibrationStatus.PROPOSED
    calibration.save(update_fields=["result", "ended_at", "status", "modified_on"])
    return calibration


@transaction.atomic
def approve(calibration, *, user, now=None):
    if calibration.status != CalibrationStatus.PROPOSED:
        raise CalibrationError("Only a proposed session can be approved.")
    if not calibration.result.get("can_approve"):
        raise CalibrationError("No limits were proposed for every antenna: fix the antenna or re-survey.")
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
