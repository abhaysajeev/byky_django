"""/api/v1/manager/tracking/alerts/{summary,current,history} -- vehicle
tracking for the manager app (owner, 12 Nov 2026).

Every station of the manager's company, read from what the engine writes
(VehiclePresence, PresenceEvent) -- the same figures as the web Status Board
and Missing & Alerts. Needs Read on Missing & Alerts (`tracking.alerts`).
Pushes for the same alerts: apps/tracking/alerts.py.
"""

import datetime

from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.company.scoping import branches_for, companies_for
from apps.company.services import NOT_SET, OPEN, branch_open_state
from apps.portal.authentication import AppJWTAuthentication
from apps.portal.services import has_permission
from apps.tracking import engine
from apps.tracking.alerts import PAGE
from apps.tracking.models import (
    EventKind,
    PresenceEvent,
    PresenceState,
    ReaderAssignment,
    ReaderStatus,
    TagLastSeen,
    VehiclePresence,
)
from core.api import date_field, envelope, manager_only, request_parts, whole_number_field
from core.schema import SERVER_ERROR, envelope_request, envelope_responses
from core.timezones import zone_for

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
PER_PAGE = 100
CURRENT_LIMIT = 1000
SILENT = "no_new_reads"
STATES = [s.value for s in PresenceState]
REASONS = {"station_closed": "Station closed", "not_seen": "Not heard", "weak_signal": "Signal below limit",
           "reader_offline": "Reader offline", "no_reader": "No reader", "no_new_reads": "Reader hears no tags",
           "calibrating": "Calibrating"}

_COMMON = (
    (401, "not_authenticated", "Sign in first.", {}),
    (403, "wrong_channel", "Not allowed on this app.", {}),
    (403, "forbidden", "Your role cannot see Missing & Alerts.", {}),
    SERVER_ERROR,
)
_BRANCH = """
**Stations:** every station of your company. `branch_id` narrows it to one;
an id that is not one of them returns nothing.
"""


# -- Requests ---------------------------------------------------------------------------


class SummaryRequest(serializers.Serializer):
    branch_id = whole_number_field(min_value=1, required=False, allow_null=True)


class CurrentRequest(serializers.Serializer):
    branch_id = whole_number_field(min_value=1, required=False, allow_null=True)
    state = serializers.ChoiceField(choices=["missing", "all"], required=False, default="missing")
    q = serializers.CharField(required=False, allow_blank=True, max_length=50, default="")
    low_battery = serializers.BooleanField(required=False, default=False)


class HistoryRequest(serializers.Serializer):
    from_date = date_field()
    to_date = date_field()
    branch_id = whole_number_field(min_value=1, required=False, allow_null=True)
    vehicle_id = whole_number_field(min_value=1, required=False, allow_null=True)
    page = whole_number_field(min_value=1, required=False, default=1)

    def validate(self, values):
        if values["from_date"] > values["to_date"]:
            raise serializers.ValidationError({"to_date": "must not be before from_date"})
        return values


# -- Base ------------------------------------------------------------------------------


class _TrackingView(APIView):
    authentication_classes = [AppJWTAuthentication]
    permission_classes = [IsAuthenticated]
    form_class = None

    def values(self, request, app):
        """(values, branches queryset, zone, None) or (None, None, None, the refusal)."""
        refused = manager_only(request, app)
        if refused:
            return None, None, None, refused
        if not has_permission(request.user, PAGE, "read"):
            return None, None, None, envelope("forbidden", "Your role cannot see Missing & Alerts.", http_status=403)
        _, request_data = request_parts(request)
        form = self.form_class(data=request_data)
        form.is_valid(raise_exception=True)
        values = form.validated_data
        branches = branches_for(request.user).filter(company__in=companies_for(request.user))
        if values.get("branch_id"):
            branches = branches.filter(pk=values["branch_id"])
        return values, branches, zone_for(getattr(request.user, "company", None)), None


def _time(value, zone):
    return value.astimezone(zone).strftime(TIME_FORMAT) if value else None


def _readers(branch_ids):
    """branch -> its approved, switched-on readers (engine._branch_readers)."""
    found = {}
    for a in (ReaderAssignment.objects.filter(branch_id__in=branch_ids, to_at__isnull=True,
                                              reader__status=ReaderStatus.APPROVED, reader__enabled=True)
              .select_related("reader").order_by("reader__name", "reader__device_id")):
        found.setdefault(a.branch_id, []).append(a.reader)
    return found


# -- summary ----------------------------------------------------------------------------

_SUMMARY_SAMPLE = {
    "totals": {"present": 41, "missing": 2, "unknown": 0, "on_rent": 12, "on_test": 0, "in_service": 1,
               "low_battery": 1},
    "stations": [{
        "branch_id": 3, "name": "Zabeel Park 1", "open": True, "working_hours_set": True,
        "hears_no_tags": False,
        "counts": {"present": 41, "missing": 2, "unknown": 0, "on_rent": 12, "on_test": 0, "in_service": 1,
                   "low_battery": 1},
        "readers": [{"reader_id": 7, "name": "Zabeel gate", "online": True, "last_contact": "2026-11-12 14:03:22"}],
    }],
}


class SummaryView(_TrackingView):
    """POST /api/v1/{app}/tracking/alerts/summary -- manager app only."""

    form_class = SummaryRequest

    @extend_schema(
        tags=["Manager Tracking"],
        summary="Tracking at every station, now",
        description="One row per station that has tagged vehicles or a reader: whether it is **open** now "
                    "(Branch Working Time; tracking runs only while open), how many vehicles stand in each "
                    "state, how many tags report a low battery, and its readers.\n\n"
                    "- `open` false: outside working hours. Its vehicles show `unknown` and nothing is "
                    "raised.\n"
                    "- `working_hours_set` false: no working hours saved, so it is tracked all day "
                    "(`open` true).\n"
                    "- `hears_no_tags`: a reader is connected but hears (almost) no bikes -- check its "
                    "antennas. Those bikes show `unknown`.\n"
                    "- `totals` add the stations up, for the home badge.\n\n"
                    "Times are company time, `YYYY-MM-DD HH:MM:SS`; `last_contact` is null for a reader "
                    "never heard.\n" + _BRANCH,
        request=envelope_request("ManagerTrackingSummaryEnvelope", SummaryRequest, request_data_required=False),
        responses=envelope_responses((200, "ok", "Tracking summary.", _SUMMARY_SAMPLE), *_COMMON),
    )
    def post(self, request, app):
        values, branches, zone, refused = self.values(request, app)
        if refused:
            return refused
        rows = VehiclePresence.objects.filter(branch__in=branches)
        counts, low, silent = {}, {}, set()
        for branch_id, state, reason, battery, n in (rows.order_by().values_list(
                "branch_id", "state", "reason", "low_battery").annotate(n=Count("pk"))):
            row = counts.setdefault(branch_id, dict.fromkeys(STATES, 0))
            row[state] += n
            if battery:
                low[branch_id] = low.get(branch_id, 0) + n
            if reason == SILENT:
                silent.add(branch_id)
        readers = _readers(branches.values_list("pk", flat=True))
        now = timezone.now()
        stations, totals = [], dict.fromkeys([*STATES, "low_battery"], 0)
        for branch in branches.filter(pk__in=set(counts) | set(readers)).select_related("company").order_by("name"):
            state = branch_open_state(branch, now)
            row = {**counts.get(branch.pk, dict.fromkeys(STATES, 0)), "low_battery": low.get(branch.pk, 0)}
            for key, n in row.items():
                totals[key] += n
            stations.append({
                "branch_id": branch.pk, "name": branch.name, "open": state in (OPEN, NOT_SET),
                "working_hours_set": state != NOT_SET, "hears_no_tags": branch.pk in silent, "counts": row,
                "readers": [{"reader_id": r.pk, "name": r.name or r.device_id, "online": r.online,
                             "last_contact": _time(engine._contact(r), zone)} for r in readers.get(branch.pk, [])],
            })
        return envelope("ok", "Tracking summary.", {"totals": totals, "stations": stations})


# -- current ----------------------------------------------------------------------------

_CURRENT_SAMPLE = {"vehicles": [{
    "vehicle_id": 41, "vehicle_name": "MO 41", "rfid": "E2801191A5030060A1B2C3D4", "vehicle_type": "Monaco",
    "branch_id": 3, "branch": "Zabeel Park 1", "state": "missing", "state_label": "Missing",
    "reason": "not_seen", "reason_label": "Not heard", "since": "2026-11-12 13:41:05",
    "last_seen": "2026-11-12 13:30:58", "heard_at": None, "low_battery": False,
}], "total": 1}


class CurrentView(_TrackingView):
    """POST /api/v1/{app}/tracking/alerts/current -- manager app only."""

    form_class = CurrentRequest

    @extend_schema(
        tags=["Manager Tracking"],
        summary="Vehicles now: missing, or every state",
        description="Vehicles as they stand now, newest change first.\n\n"
                    "- `state`: `missing` (default) -- the open alerts; `all` -- every tracked vehicle.\n"
                    "- `q`: part of the vehicle name or RFID tag.\n"
                    "- `low_battery` true: only tags reporting a low battery (list only -- never pushed).\n\n"
                    "`state` is one of `present`, `missing`, `unknown`, `on_rent`, `on_test`, `in_service`; "
                    "`reason` explains `missing` and `unknown` (`not_seen`, `weak_signal`, `reader_offline`, "
                    "`no_reader`, `no_new_reads`, `calibrating`, `station_closed`), empty otherwise. "
                    "`heard_at` names another station whose reader hears a missing vehicle. `last_seen` is "
                    f"null if never heard. At most {CURRENT_LIMIT} rows; `total` is the full count.\n" + _BRANCH,
        request=envelope_request("ManagerTrackingCurrentEnvelope", CurrentRequest, request_data_required=False),
        responses=envelope_responses((200, "ok", "Vehicles.", _CURRENT_SAMPLE), *_COMMON),
    )
    def post(self, request, app):
        values, branches, zone, refused = self.values(request, app)
        if refused:
            return refused
        rows = VehiclePresence.objects.filter(branch__in=branches)
        if values["state"] == "missing":
            rows = rows.filter(state=PresenceState.MISSING)
        if values["low_battery"]:
            rows = rows.filter(low_battery=True)
        q = values["q"].strip()
        if q:
            rows = rows.filter(Q(vehicle__vehicle_name__icontains=q) | Q(vehicle__rfid_epc__icontains=q))
        total = rows.count()
        found = list(rows.select_related("vehicle__vehicle_type", "branch", "heard_by_branch")
                     .order_by("-since", "vehicle__vehicle_name")[:CURRENT_LIMIT])
        tags = {p.pk: engine.tag_of(p.vehicle.rfid_epc) for p in found}
        fresh = {t.tag: t for t in TagLastSeen.objects.filter(tag__in=set(tags.values()))}
        own = {}
        for a in ReaderAssignment.objects.filter(branch_id__in={p.branch_id for p in found}, to_at__isnull=True):
            own.setdefault(a.branch_id, set()).add(a.reader_id)
        vehicles = []
        for p in found:
            # the engine stores last-heard only every few minutes; the tag's own row is exact
            last = fresh.get(tags[p.pk])
            heard = last.seen_at if last is not None and last.reader_id in own.get(p.branch_id, ()) else p.last_seen_at
            vehicles.append({
                "vehicle_id": p.pk, "vehicle_name": p.vehicle.vehicle_name, "rfid": p.vehicle.rfid_epc,
                "vehicle_type": p.vehicle.vehicle_type.vehicle_type_name, "branch_id": p.branch_id,
                "branch": p.branch.name, "state": p.state, "state_label": p.get_state_display(),
                "reason": p.reason, "reason_label": REASONS.get(p.reason, ""), "since": _time(p.since, zone),
                "last_seen": _time(heard, zone),
                "heard_at": p.heard_by_branch.name if p.heard_by_branch_id else None, "low_battery": p.low_battery,
            })
        return envelope("ok", "Vehicles.", {"vehicles": vehicles, "total": total})


# -- history ----------------------------------------------------------------------------

_HISTORY_SAMPLE = {"events": [
    {"event_id": 9021, "at": "2026-11-12 13:41:05", "kind": "vehicle_missing", "kind_label": "Vehicle missing",
     "branch_id": 3, "branch": "Zabeel Park 1", "vehicle_id": 41, "vehicle_name": "MO 41", "reader_id": None,
     "reader": None, "reason": "not_seen", "reason_label": "Not heard", "acknowledged": False},
    {"event_id": 9018, "at": "2026-11-12 13:20:00", "kind": "reader_offline", "kind_label": "Reader offline",
     "branch_id": 3, "branch": "Zabeel Park 1", "vehicle_id": None, "vehicle_name": None, "reader_id": 7,
     "reader": "Zabeel gate", "reason": "", "reason_label": "", "acknowledged": False},
], "page": 1, "pages": 1, "total_events": 2}
_DATES_INVALID = (400, "invalid_request", "to_date must not be before from_date.",
                  {"errors": {"to_date": "must not be before from_date"}})
KINDS = {EventKind.LOW_BATTERY: ("low_battery", "Low battery"),
         EventKind.READER_OFFLINE: ("reader_offline", "Reader offline"),
         EventKind.READER_ONLINE: ("reader_online", "Reader back online"),
         EventKind.READER_SILENT: ("reader_silent", "Reader hears no tags")}


def _kind(event):
    if event.kind != EventKind.STATE:
        return KINDS[event.kind]
    if event.to_state == PresenceState.MISSING:
        return "vehicle_missing", "Vehicle missing"
    return "vehicle_back", "Vehicle back"


class HistoryView(_TrackingView):
    """POST /api/v1/{app}/tracking/alerts/history -- manager app only."""

    form_class = HistoryRequest

    @extend_schema(
        tags=["Manager Tracking"],
        summary="Tracking alerts, past and present",
        description="What happened, newest first, " f"{PER_PAGE} per page.\n\n"
                    "`kind`: `vehicle_missing`, `vehicle_back` (a missing vehicle left Missing -- found, "
                    "rented, put in service...), `low_battery`, `reader_offline`, `reader_online`, "
                    "`reader_silent` (connected, hears no tags). A vehicle event has `vehicle_id`; a reader "
                    "event has `reader_id`. `acknowledged` is true once someone acknowledged it on the web.\n\n"
                    "**Dates:** `from_date` and `to_date`, both required, `YYYY-MM-DD`, company time, both "
                    "days included. `vehicle_id` narrows it to one vehicle.\n" + _BRANCH,
        request=envelope_request("ManagerTrackingHistoryEnvelope", HistoryRequest),
        responses=envelope_responses((200, "ok", "Tracking history.", _HISTORY_SAMPLE), _DATES_INVALID, *_COMMON),
    )
    def post(self, request, app):
        values, branches, zone, refused = self.values(request, app)
        if refused:
            return refused
        start = datetime.datetime.combine(values["from_date"], datetime.time.min, zone)
        end = datetime.datetime.combine(values["to_date"] + datetime.timedelta(days=1), datetime.time.min, zone)
        events = PresenceEvent.objects.filter(
            company__in=companies_for(request.user), branch__in=branches, at__gte=start, at__lt=end).filter(
            ~Q(kind=EventKind.STATE) | Q(to_state=PresenceState.MISSING) | Q(from_state=PresenceState.MISSING))
        if values.get("vehicle_id"):
            events = events.filter(vehicle_id=values["vehicle_id"])
        paginator = Paginator(events.select_related("branch", "vehicle", "reader").order_by("-at", "-pk"), PER_PAGE)
        page = paginator.get_page(values["page"])
        rows = []
        for e in page.object_list if values["page"] <= paginator.num_pages else []:
            kind, label = _kind(e)
            rows.append({
                "event_id": e.pk, "at": _time(e.at, zone), "kind": kind, "kind_label": label,
                "branch_id": e.branch_id, "branch": e.branch.name if e.branch_id else None,
                "vehicle_id": e.vehicle_id, "vehicle_name": e.vehicle.vehicle_name if e.vehicle_id else None,
                "reader_id": e.reader_id,
                "reader": (e.reader.name or e.reader.device_id) if e.reader_id else None,
                "reason": e.reason, "reason_label": REASONS.get(e.reason, ""),
                "acknowledged": e.acknowledged_at is not None,
            })
        return envelope("ok", "Tracking history.", {
            "events": rows, "page": values["page"], "pages": paginator.num_pages if paginator.count else 0,
            "total_events": paginator.count})
