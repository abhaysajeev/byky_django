"""The presence engine (design/07-tracking.md §5): every 30 s, from the newest
read of each tag and the excuses the rest of the system already knows, where
each tagged vehicle stands -- and what changed.

Set-based and small: about one row per tag, vehicle and reader. Raw readings
are never read here. The whole cycle is one transaction, so a failure leaves
nothing half written: **if the data cannot be read, the cycle is skipped, it
never flags the fleet missing** (owner, 9 Nov 2026).

Legacy: AntennaDataTracking, AntennaDataTrackingUpdation, Process_Vehicle_Missing
(the rules are cited row by row in design §1 and §5).
"""

import datetime
from typing import NamedTuple

from django.db import connection, transaction
from django.utils import timezone

from apps.company.models import Branch
from apps.fleet.models import Vehicle
from apps.fleet.service_requests import in_warehouse_service
from apps.rental.models import TestRide, TestRideStatus
from apps.rental.services import active_rentals
from apps.tracking import ops
from apps.tracking.models import (
    Antenna,
    BranchTrackingSettings,
    EventKind,
    PresenceEvent,
    PresenceState,
    Reader,
    ReaderAssignment,
    ReaderStatus,
    TagLastSeen,
    TrackingSettings,
    VehiclePresence,
)
from core.realtime import publish

LOCK_KEY = 7307001                # pg_try_advisory_xact_lock: a second engine skips
SLOW_CYCLE_SECONDS = 10

NOT_SEEN = "not_seen"
WEAK_SIGNAL = "weak_signal"
EDGE = "edge"
TEST_OVERDUE = "test_overdue"     # legacy AntennaDataTracking:62-72 (E4)
READER_OFFLINE = "reader_offline"
NO_READER = "no_reader"

P = PresenceState
EXCUSED = {P.ON_RENT, P.ON_TEST, P.IN_SERVICE, P.UNKNOWN}
ALERTING = {P.ALERT, P.MISSING}


def tag_of(epc):
    """The 8-hex form the gateway stores; an Excel import can drop leading zeros (E13)."""
    return (epc or "").strip().upper().rjust(8, "0")


def company_group(company_id):
    return f"tracking-alerts-{company_id}"


class Timings(NamedTuple):
    alert: int
    missing: int
    offline: int
    alerts_on: bool


COMPANY_DEFAULT = Timings(150, 300, 300, False)


def timings_by_branch(branch_ids):
    """{branch_id: Timings}: the branch's own value, else its company's default."""
    branches = list(Branch.objects.filter(pk__in=branch_ids).values_list("pk", "company_id"))
    company_rows = {s.company_id: s for s in TrackingSettings.objects.filter(
        company_id__in={company for _, company in branches})}
    own = {s.branch_id: s for s in BranchTrackingSettings.objects.filter(branch_id__in=branch_ids)}
    result = {}
    for pk, company in branches:
        base = company_rows.get(company)
        base = Timings(base.alert_after_s, base.missing_after_s, base.offline_after_s, False) if base \
            else COMPANY_DEFAULT
        row = own.get(pk)
        result[pk] = Timings(
            row.alert_after_s if row and row.alert_after_s is not None else base.alert,
            row.missing_after_s if row and row.missing_after_s is not None else base.missing,
            row.offline_after_s if row and row.offline_after_s is not None else base.offline,
            bool(row and row.alerts_on),
        )
    return result


class Facts(NamedTuple):
    """What one vehicle's decision depends on."""
    now: datetime.datetime
    rented: bool
    test_end: datetime.datetime | None
    in_service: bool
    has_reader: bool
    reader_online: bool
    reader_contact: datetime.datetime | None   # the branch reader's latest contact
    own_seen: datetime.datetime | None          # last heard by its OWN branch's reader
    rssi_avg: float | None                      # from that reader
    limits: tuple | None                        # (enter_rssi, leave_rssi) of the antenna
    timings: Timings
    moved: bool                                 # its branch changed since last cycle


class Verdict(NamedTuple):
    state: str
    reason: str
    clock_from: datetime.datetime | None


def decide(prev, f):
    """One vehicle. First match wins (design §5)."""
    clock = prev.clock_from if prev else None
    if f.rented:
        return Verdict(P.ON_RENT, "", clock)
    if f.test_end is not None:
        if f.test_end >= f.now:
            return Verdict(P.ON_TEST, "", clock)
        return Verdict(P.MISSING, TEST_OVERDUE, clock)
    if f.in_service:
        return Verdict(P.IN_SERVICE, "", clock)
    if not f.has_reader:
        return Verdict(P.UNKNOWN, NO_READER, clock)
    if not f.reader_online:
        return Verdict(P.UNKNOWN, READER_OFFLINE, clock)

    # The clock restarts when the vehicle stops being excused: a bike back from
    # rent has not been heard for an hour, and that is not a reason to flag it.
    restarted = prev is None or prev.state in EXCUSED or prev.reason == TEST_OVERDUE or f.moved or clock is None
    if restarted:
        clock = f.now
    base = max(f.own_seen, clock) if f.own_seen else clock
    # Silence counts only while the reader is talking: alert (150 s) comes before
    # reader offline (300 s), and a 4G drop must not alert a whole station.
    age = ((f.reader_contact or f.now) - base).total_seconds()

    if age >= f.timings.missing:
        return Verdict(P.MISSING, NOT_SEEN, clock)
    if age >= f.timings.alert:
        return Verdict(P.ALERT, NOT_SEEN, clock)

    # RSSI only from a read made since the clock (re)started: the last one a
    # rented bike left behind was weak because it was riding away.
    fresh = f.own_seen is not None and f.own_seen >= clock
    if fresh and f.limits and f.rssi_avg is not None:
        enter, leave = f.limits
        if f.rssi_avg < leave:
            weak_before = prev is not None and prev.reason == WEAK_SIGNAL
            if weak_before and prev.state == P.MISSING:
                return Verdict(P.MISSING, WEAK_SIGNAL, clock)
            if weak_before and prev.state == P.ALERT \
                    and (f.now - prev.since).total_seconds() >= f.timings.missing:
                return Verdict(P.MISSING, WEAK_SIGNAL, clock)
            return Verdict(P.ALERT, WEAK_SIGNAL, clock)
        if f.rssi_avg < enter:                 # the band: hysteresis keeps it from flapping
            if restarted:
                return Verdict(P.ALERT, EDGE, clock)
            if prev.state in ALERTING:
                return Verdict(prev.state, EDGE, clock)
    return Verdict(P.PRESENT, "", clock)


def _branch_readers(now):
    """(branch -> [readers it can hear with], reader_id -> branch_id of every
    open assignment). Only an approved, switched-on reader counts as the branch's."""
    assignments = ReaderAssignment.objects.filter(to_at__isnull=True).select_related("reader")
    by_branch, branch_of = {}, {}
    for a in assignments:
        branch_of[a.reader_id] = a.branch_id
        if a.reader.status == ReaderStatus.APPROVED and a.reader.enabled:
            by_branch.setdefault(a.branch_id, []).append(a.reader)
    return by_branch, branch_of


def _contact(reader):
    times = [t for t in (reader.last_heartbeat_at, reader.last_data_at) if t]
    return max(times) if times else None


def _update_readers(by_branch, timings, now, branches):
    """Note who went offline or came back; returns the events."""
    events, changed = [], []
    for branch_id, readers in by_branch.items():
        offline_after = timings[branch_id].offline
        for r in readers:
            contact = _contact(r)
            online = contact is not None and (now - contact).total_seconds() < offline_after
            if online == r.online:
                continue
            r.online, r.online_changed_at = online, now
            changed.append(r)
            events.append(PresenceEvent(
                company_id=branches[branch_id], branch_id=branch_id, reader=r, at=now,
                kind=EventKind.READER_ONLINE if online else EventKind.READER_OFFLINE,
                alerts=timings[branch_id].alerts_on))
    if changed:
        # update_fields only: the gateway owns the heartbeat columns of this row
        Reader.objects.bulk_update(changed, ["online", "online_changed_at"], batch_size=200)
    return events


_WRITE = ["company", "branch", "state", "reason", "since", "last_seen_at", "heard_by_branch", "low_battery",
          "clock_from"]


def _cycle(now):
    vehicles = list(Vehicle.objects.filter(is_active=True, current_branch__isnull=False).exclude(rfid_epc="")
                    .values("pk", "company_id", "current_branch_id", "rfid_epc"))
    ids = [v["pk"] for v in vehicles]
    tags = {v["pk"]: tag_of(v["rfid_epc"]) for v in vehicles}
    branch_ids = {v["current_branch_id"] for v in vehicles}

    by_branch, branch_of = _branch_readers(now)
    branch_ids |= set(by_branch)
    timings = timings_by_branch(branch_ids)
    companies = dict(Branch.objects.filter(pk__in=branch_ids).values_list("pk", "company_id"))
    events = _update_readers(by_branch, timings, now, companies)

    seen = {t.tag: t for t in TagLastSeen.objects.filter(tag__in=set(tags.values()))}
    limits = {(a.reader_id, a.port): (a.enter_rssi, a.leave_rssi) for a in Antenna.objects.filter(
        enter_rssi__isnull=False, leave_rssi__isnull=False)}
    rented = active_rentals(ids)
    tests = dict(TestRide.objects.filter(vehicle_id__in=ids, status=TestRideStatus.ON_TEST)
                 .values_list("vehicle_id", "expected_end_at"))
    service = in_warehouse_service(ids)
    previous = {p.pk: p for p in VehiclePresence.objects.all()}

    new_rows, updates, counts = [], [], {}
    for v in vehicles:
        pk, branch_id = v["pk"], v["current_branch_id"]
        prev = previous.get(pk)
        last = seen.get(tags[pk])
        readers = by_branch.get(branch_id, [])
        reader_ids = {r.pk for r in readers}
        contacts = [c for c in (_contact(r) for r in readers if r.online) if c]
        own = last is not None and last.reader_id in reader_ids
        own_seen = last.seen_at if own else (prev.last_seen_at if prev else None)
        heard_at = branch_of.get(last.reader_id) if last is not None and not own else None

        verdict = decide(prev, Facts(
            now=now, rented=pk in rented, test_end=tests.get(pk), in_service=pk in service,
            has_reader=bool(readers), reader_online=any(r.online for r in readers),
            reader_contact=max(contacts) if contacts else None, own_seen=own_seen,
            rssi_avg=last.rssi_avg if own else None,
            limits=limits.get((last.reader_id, last.port)) if own else None,
            timings=timings[branch_id], moved=prev is not None and prev.branch_id != branch_id))

        counts[verdict.state] = counts.get(verdict.state, 0) + 1
        low = bool(last and last.low_battery)
        heard_by = heard_at if verdict.state in ALERTING and heard_at != branch_id else None
        alerts_on = timings[branch_id].alerts_on

        if prev is None or prev.state != verdict.state:
            if prev is not None or verdict.state in ALERTING:
                events.append(PresenceEvent(
                    company_id=v["company_id"], branch_id=branch_id, vehicle_id=pk, kind=EventKind.STATE, at=now,
                    from_state=prev.state if prev else "", to_state=verdict.state, reason=verdict.reason,
                    alerts=alerts_on and verdict.state in ALERTING))
        if low and not (prev and prev.low_battery):
            events.append(PresenceEvent(
                company_id=v["company_id"], branch_id=branch_id, vehicle_id=pk, kind=EventKind.LOW_BATTERY, at=now,
                alerts=alerts_on))

        if prev is None:
            new_rows.append(VehiclePresence(
                vehicle_id=pk, company_id=v["company_id"], branch_id=branch_id, state=verdict.state,
                reason=verdict.reason, since=now, last_seen_at=own_seen, heard_by_branch_id=heard_by,
                low_battery=low, clock_from=verdict.clock_from))
            continue
        since = prev.since if (prev.state, prev.reason) == (verdict.state, verdict.reason) else now
        after = (v["company_id"], branch_id, verdict.state, verdict.reason, since, own_seen, heard_by, low,
                 verdict.clock_from)
        before = (prev.company_id, prev.branch_id, prev.state, prev.reason, prev.since, prev.last_seen_at,
                  prev.heard_by_branch_id, prev.low_battery, prev.clock_from)
        if after != before:
            prev.company_id, prev.branch_id, prev.state, prev.reason, prev.since, prev.last_seen_at, \
                prev.heard_by_branch_id, prev.low_battery, prev.clock_from = after
            updates.append(prev)

    if new_rows:
        VehiclePresence.objects.bulk_create(new_rows, batch_size=500)
    if updates:
        VehiclePresence.objects.bulk_update(updates, _WRITE, batch_size=500)
    gone = set(previous) - set(ids)
    if gone:
        VehiclePresence.objects.filter(pk__in=gone).delete()
    if events:
        PresenceEvent.objects.bulk_create(events, batch_size=500)

    # The socket only says "look again"; the page asks over HTTP, so what a user
    # may see is decided in one place.
    touched = {r.company_id for r in new_rows} | {r.company_id for r in updates} | {e.company_id for e in events}
    alerted = {e.company_id for e in events if e.alerts}
    for company_id in touched:
        publish(company_group(company_id), {"type": "tracking.changed", "alert": company_id in alerted})
    return {"vehicles": len(vehicles), "states": counts, "written": len(new_rows) + len(updates),
            "events": len(events)}


def run_cycle(now=None):
    """One pass, in one transaction. None = another engine holds the lock."""
    now = now or timezone.now()
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_xact_lock(%s)", [LOCK_KEY])
            if not cursor.fetchone()[0]:
                return None
        return _cycle(now)


def tick(now=None):
    """A cycle that cannot hurt: any failure is logged and the cycle is skipped.
    Nothing was written (one transaction), and the next tick starts clean."""
    started = timezone.now()
    try:
        result = run_cycle(now)
    except Exception as failure:              # noqa: BLE001 -- the loop must outlive any one cycle
        connection.close()                    # a broken connection must not be reused
        ops.event("engine_cycle_failed", error=f"{type(failure).__name__}: {failure}"[:200])
        return None
    took = (timezone.now() - started).total_seconds()
    if result is None:
        ops.event("engine_cycle_skipped", reason="another engine holds the lock")
        return None
    ops.event("engine_slow_cycle" if took > SLOW_CYCLE_SECONDS else "engine_cycle", seconds=round(took, 3), **result)
    return result
