"""Antenna Tracking screens (design §7). They read what the engine and the
gateway write, through the company / branch scope of every other screen; every
action writes through services.py or calibration.py.

The raw and event tables grow quickly, so those two lists filter and page in the
database, from the query string, like the logs.
"""

import datetime
from urllib.parse import urlencode

from django.conf import settings
from django.core.paginator import Paginator
from django.db import DatabaseError, connection, transaction
from django.db.models import Q
from django.http import Http404, JsonResponse
from django.urls import reverse
from django.utils import timezone
from django.views import View

from apps.company import writes
from apps.company.scoping import branches_for, companies_for
from apps.company.services import branch_open_state
from apps.portal.permissions import PagePermissionMixin
from apps.portal.screens import PrivilegeScreenView
from apps.portal.services import has_permission
from apps.tracking import bell, calibration, engine, services
from apps.tracking.models import (
    Antenna,
    BranchTrackingSettings,
    Calibration,
    CalibrationStatus,
    Direction,
    EventKind,
    PresenceEvent,
    PresenceState,
    Reader,
    ReaderAssignment,
    ReaderStatus,
    Reading,
    TagLastSeen,
    TrackingSettings,
    VehicleBaseline,
    VehiclePresence,
)
from core.timezones import zone_for
from theme.views import ThemedTemplateView

ACTIONS = ("create", "read", "update", "approve")
PER_PAGE = 100
MAX_RAW_HOURS = 24
RAW_QUERY_TIMEOUT = "5s"
ALERT_STATES = (PresenceState.MISSING,)
# chip tone per state (.scr-x-status): green present, red missing, blue out on an order, grey unknown
OUT_STATES = (PresenceState.ON_RENT, PresenceState.ON_TEST, PresenceState.IN_SERVICE)
TONE = {PresenceState.PRESENT: "met", PresenceState.MISSING: "missed", PresenceState.UNKNOWN: "idle",
        PresenceState.ON_RENT: "track", PresenceState.ON_TEST: "track", PresenceState.IN_SERVICE: "track"}
STATE_ORDER = (PresenceState.MISSING, PresenceState.UNKNOWN, PresenceState.PRESENT,
               PresenceState.ON_RENT, PresenceState.ON_TEST, PresenceState.IN_SERVICE)
BADGE = {PresenceState.PRESENT: "scr-badge-active", PresenceState.ON_RENT: "scr-badge-approved",
         PresenceState.ON_TEST: "scr-badge-approved", PresenceState.IN_SERVICE: "scr-badge-pending",
         PresenceState.MISSING: "scr-badge-missing",
         PresenceState.UNKNOWN: "scr-badge-inactive"}
REASON_LABEL = {"station_closed": "Branch closed", "not_seen": "Not heard", "weak_signal": "Signal below limit",
                "reader_offline": "Reader offline", "no_reader": "No reader",
                "no_new_reads": "Reader hears no tags", "calibrating": "Calibrating"}


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _vehicle_type(vehicle):
    return vehicle.vehicle_type.vehicle_type_name


def readers_for(user):
    """Approved readers of the user's companies. A pending reader has no company
    yet, so only a user who sees every company sees it."""
    mine = Q(company__in=companies_for(user))
    return Reader.objects.filter(mine | Q(company__isnull=True)) if user.sees_every_company \
        else Reader.objects.filter(mine)


def events_for(user):
    """The same scope for History and for acknowledging: a reader-level event
    can have no branch."""
    return PresenceEvent.objects.filter(company__in=companies_for(user)).filter(
        Q(branch__in=branches_for(user)) | Q(branch__isnull=True))


class TrackingScreenView(PagePermissionMixin, ThemedTemplateView):
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        context["perm"] = {action: has_permission(user, self.page_code, action) for action in ACTIONS}
        context["branches_list"] = list(branches_for(user).filter(is_active=True).order_by("name").values("id", "name"))
        return context


# -- Status Tracking (was Status Board) -----------------------------------------------------

ALERT_AFTER_S = 60       # legacy Alert: not heard for more than a minute, not yet missing
STATUS_PER_PAGE = 50


def _station_readers(branch_ids):
    """branch -> its approved, switched-on readers (as the engine and the manager app)."""
    found = {}
    for a in (ReaderAssignment.objects.filter(branch_id__in=branch_ids, to_at__isnull=True,
                                              reader__status=ReaderStatus.APPROVED, reader__enabled=True)
              .select_related("reader")):
        found.setdefault(a.branch_id, []).append(a.reader)
    return found


def _heard(presence, seen, own):
    """When its own station last heard the vehicle: the tag's exact row if one of
    the station's readers made it, else the engine's (stored every few minutes)."""
    last = seen.get(engine.tag_of(presence.vehicle.rfid_epc))
    return last.seen_at if last is not None and last.reader_id in own else presence.last_seen_at


def station_status(branches, now=None):
    """The old RMS Status Tracking grid (Rms_Search_Vehicle_Status_Tracking +
    AntennaDataProcessing), one row per station, from the engine's state:

    - vehicles: the station's tagged vehicles; rent / test: on_rent / on_test;
      missing: missing;
    - alert (legacy IsOnAlert): in the station (present) but not heard by its own
      reader for more than a minute, or its signal weakening -- the warning
      before missing;
    - available: present and not on alert;
    - tags read: its vehicles its own reader heard within the missing window;
    - a closed station shows 0 for everything but its vehicles, as the legacy did.
    """
    now = now or timezone.now()
    branch_ids = [b.pk for b in branches]
    presences = list(VehiclePresence.objects.filter(branch_id__in=branch_ids).select_related("vehicle"))
    readers = _station_readers(branch_ids)
    timings = engine.timings_by_branch(branch_ids)
    seen = {t.tag: t for t in TagLastSeen.objects.filter(
        tag__in={engine.tag_of(p.vehicle.rfid_epc) for p in presences})}
    by_branch = {}
    for p in presences:
        by_branch.setdefault(p.branch_id, []).append(p)

    rows = []
    for branch in branches:
        mine = by_branch.get(branch.pk, [])
        station_readers = readers.get(branch.pk, [])
        if not mine and not station_readers:
            continue
        own = {r.pk for r in station_readers}
        window = timings.get(branch.pk, engine.COMPANY_DEFAULT).missing
        counts = dict.fromkeys(("tags_read", "rent", "test", "alert", "available", "missing"), 0)
        for p in mine:
            heard = _heard(p, seen, own)
            age = (now - heard).total_seconds() if heard else None
            if own and age is not None and age <= window:
                counts["tags_read"] += 1
            if p.state == PresenceState.ON_RENT:
                counts["rent"] += 1
            elif p.state == PresenceState.ON_TEST:
                counts["test"] += 1
            elif p.state == PresenceState.MISSING:
                counts["missing"] += 1
            elif p.state == PresenceState.PRESENT:
                if p.weak_since is not None or (age is not None and age > ALERT_AFTER_S):
                    counts["alert"] += 1
                else:
                    counts["available"] += 1
        closed = branch_open_state(branch, now) == "closed"
        if closed:
            counts = dict.fromkeys(counts, 0)
        contacts = [c for c in (engine._contact(r) for r in station_readers) if c]
        antenna = ("none" if not station_readers else
                   "online" if any(r.online for r in station_readers) else "offline")
        rows.append({"id": branch.pk, "name": branch.name, "vehicles": len(mine), "closed": closed,
                     "last_updated": max(contacts) if contacts else None, "antenna": antenna, **counts})
    rows.sort(key=lambda r: (r["antenna"] != "online", -r["missing"], r["name"].lower()))
    return rows


def station_vehicles(user, branch_id):
    """One station's tagged vehicles -- missing first (STATE_ORDER), then by name."""
    scope = VehiclePresence.objects.filter(company__in=companies_for(user), branch_id=branch_id)
    presences = list(scope.select_related("vehicle__vehicle_type", "heard_by_branch"))
    tags = {p.pk: engine.tag_of(p.vehicle.rfid_epc) for p in presences}
    fresh = {t.tag: t for t in TagLastSeen.objects.filter(tag__in=set(tags.values()))}
    own_readers = set(ReaderAssignment.objects.filter(branch_id=branch_id, to_at__isnull=True)
                      .values_list("reader_id", flat=True))
    rank = {state: n for n, state in enumerate(STATE_ORDER)}
    rows = []
    for p in presences:
        # the engine stores last-heard only every few minutes; the tag's row is exact
        last = fresh.get(tags[p.pk])
        heard = last.seen_at if last is not None and last.reader_id in own_readers else p.last_seen_at
        rows.append({"name": p.vehicle.vehicle_name, "type": _vehicle_type(p.vehicle), "state": p.state,
                     "state_label": p.get_state_display(), "badge": BADGE[p.state], "tone": TONE[p.state],
                     "reason": REASON_LABEL.get(p.reason, ""), "since": p.since, "last_seen": heard,
                     "heard_at": p.heard_by_branch.name if p.heard_by_branch_id else "",
                     "low_battery": p.low_battery})
    return sorted(rows, key=lambda row: (rank[row["state"]], row["name"]))


class StatusView(TrackingScreenView):
    """Status Tracking (client, 20 Nov 2026): the old RMS grid in our styling,
    searched by State, then Branch. A station's row opens its vehicles in a
    dialog (StatusStationView); ?branch= still opens them as a page."""

    template_name = "tracking/status.html"
    page_code = "tracking.status"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user, params = self.request.user, self.request.GET
        scoped = list(branches_for(user).filter(is_active=True)
                      .select_related("location__state").order_by("name"))
        chosen = _int(params.get("branch"))
        if chosen not in {b.pk for b in scoped}:
            chosen = None
        context["chosen_branch"] = chosen
        if chosen:
            context["chosen_name"] = next(b.name for b in scoped if b.pk == chosen)
            context["vehicles"] = station_vehicles(user, chosen)
            return context

        state, station = (_int(params.get(k)) for k in ("state", "station"))
        states = {b.location.state_id: b.location.state.name for b in scoped}
        shown = [b for b in scoped
                 if (not state or b.location.state_id == state) and (not station or b.pk == station)]
        rows = station_status(shown)
        totals = {key: sum(r[key] for r in rows) for key in ("rent", "test", "alert", "missing")}
        for r in rows:          # what the page compares between refreshes to show a change
            r["sig"] = "|".join(str(r[k]) for k in ("tags_read", "vehicles", "available", "rent", "test",
                                                    "alert", "missing", "antenna", "closed"))
        page = Paginator(rows, STATUS_PER_PAGE).get_page(params.get("page"))
        query = params.copy()
        query.pop("page", None)
        context.update({
            "rows": list(page.object_list), "page": page, "query": query.urlencode(), "totals": totals,
            "states": sorted(({"id": k, "name": v} for k, v in states.items()), key=lambda o: o["name"]),
            "stations": [{"id": b.pk, "name": b.name, "state": b.location.state_id} for b in scoped],
            "f": {"state": state, "station": station},
        })
        return context


class StatusStationView(TrackingScreenView):
    """A station's vehicles for the Status Tracking dialog (client, 20 Nov 2026):
    missing first, then the rest."""

    template_name = "tracking/_status_station.html"
    page_code = "tracking.status"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        branch = branches_for(self.request.user).filter(pk=kwargs["branch_id"]).first()
        if branch is None:
            raise Http404
        context["branch"] = branch
        context["vehicles"] = station_vehicles(self.request.user, branch.pk)
        return context


class StatusMissingView(TrackingScreenView):
    """The missing star's dialog (legacy VehicleStatusTracking/MissingVehicles):
    the station's missing vehicles -- category, type, number -- with when they
    went missing and were last heard, and today's cashiers from the duty roster.
    The legacy Is Test column is left out: a vehicle on a test ride is never
    missing here."""

    template_name = "tracking/_status_missing.html"
    page_code = "tracking.status"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        branch = branches_for(self.request.user).filter(pk=kwargs["branch_id"]).first()
        if branch is None:
            raise Http404
        presences = list(VehiclePresence.objects.filter(branch=branch, state=PresenceState.MISSING)
                         .select_related("vehicle__vehicle_type__category").order_by("since"))
        own = {r.pk for r in _station_readers([branch.pk]).get(branch.pk, [])}
        seen = {t.tag: t for t in TagLastSeen.objects.filter(
            tag__in={engine.tag_of(p.vehicle.rfid_epc) for p in presences})}
        context["branch"] = branch
        context["vehicles"] = [{
            "category": (p.vehicle.vehicle_type.category.category_name
                         if p.vehicle.vehicle_type.category_id else ""),
            "type": _vehicle_type(p.vehicle), "number": p.vehicle.vehicle_code or p.vehicle.vehicle_name,
            "name": p.vehicle.vehicle_name, "since": p.since, "last_seen": _heard(p, seen, own),
        } for p in presences]
        context["cashiers"] = _cashiers_today(branch)
        return context


def _cashiers_today(branch):
    """Today's cashiers at the station (the legacy listed the branch's cashier
    employees), from the duty roster: Primary first."""
    from apps.crew.duty import role_of
    from apps.crew.models import DutyRoster, DutyRosterDayType
    from core.timezones import business_date_for

    rows = (DutyRoster.objects.filter(branch=branch, date=business_date_for(branch.company),
                                      day_type=DutyRosterDayType.WORKING)
            .select_related("employee__designation"))
    cashiers = sorted((r for r in rows if role_of(r) == "cashier"),
                      key=lambda r: (r.cashier_type != "primary", r.employee.full_name))
    return [{"name": r.employee.full_name, "type": r.get_cashier_type_display()} for r in cashiers]


# -- Missing & Alerts -----------------------------------------------------------------------


def _day(value):
    try:
        return datetime.date.fromisoformat(value) if value else None
    except ValueError:
        return None


class AlertsView(TrackingScreenView):
    template_name = "tracking/alerts.html"
    page_code = "tracking.alerts"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user, params = self.request.user, self.request.GET
        view = "history" if params.get("view") == "history" else "current"
        branch = _int(params.get("branch"))
        if branch not in {b["id"] for b in context["branches_list"]}:
            branch = None
        context.update({"view": view, "chosen_branch": branch})

        if view == "current":
            rows = VehiclePresence.objects.filter(company__in=companies_for(user), branch__in=branches_for(user),
                                                  state__in=ALERT_STATES)
            if branch:
                rows = rows.filter(branch_id=branch)
            context["current"] = [
                {"name": r.vehicle.vehicle_name, "type": _vehicle_type(r.vehicle), "branch": r.branch.name,
                 "state_label": r.get_state_display(), "badge": BADGE[r.state], "reason": REASON_LABEL.get(r.reason, ""),
                 "since": r.since, "last_seen": r.last_seen_at, "low_battery": r.low_battery,
                 "heard_at": r.heard_by_branch.name if r.heard_by_branch_id else "", "vehicle_id": r.pk}
                for r in rows.select_related("vehicle__vehicle_type", "branch", "heard_by_branch").order_by("-since")[:500]]
            return context

        events = events_for(user)
        if branch:
            events = events.filter(branch_id=branch)
        vehicle = _int(params.get("vehicle"))
        if vehicle:
            events = events.filter(vehicle_id=vehicle)
        zone = timezone.get_current_timezone()
        start, end = _day(params.get("from")), _day(params.get("to"))
        if start:
            events = events.filter(at__gte=datetime.datetime.combine(start, datetime.time.min, zone))
        if end:
            events = events.filter(at__lt=datetime.datetime.combine(end + datetime.timedelta(days=1),
                                                                    datetime.time.min, zone))
        page = Paginator(events.select_related("vehicle__vehicle_type", "branch", "reader", "acknowledged_by")
                         .order_by("-at", "-pk"), PER_PAGE).get_page(params.get("page"))
        for event in page.object_list:
            event.what = self._what(event)
            if event.vehicle_id:
                event.subject, event.subject_sub = event.vehicle.vehicle_name, _vehicle_type(event.vehicle)
            elif event.reader_id:
                event.subject, event.subject_sub = event.reader.name or event.reader.device_id, "Reader"
            else:
                event.subject, event.subject_sub = "—", ""
        query = {k: v for k, v in params.items() if k != "page" and v}
        context.update({"page": page, "query": urlencode(query), "from": params.get("from", ""),
                        "to": params.get("to", ""), "vehicle_filter": vehicle})
        return context

    @staticmethod
    def _what(event):
        if event.kind != EventKind.STATE:
            return event.get_kind_display()
        arrow = f"{event.get_from_state_display()} → " if event.from_state else ""
        reason = REASON_LABEL.get(event.reason)
        return f"{arrow}{event.get_to_state_display()}" + (f" ({reason})" if reason else "")


class AcknowledgeView(writes.WriteView):
    page_code = "tracking.alerts"

    def post(self, request, pk):
        if not self.may("update"):
            return self.refused()
        event = events_for(request.user).filter(pk=pk).first()
        if event is None:
            raise Http404
        event.acknowledged_by, event.acknowledged_at = request.user, timezone.now()
        event.note = str(writes._payload(request).get("note") or "").strip()[:200]
        event.save(update_fields=["acknowledged_by", "acknowledged_at", "note"])
        return JsonResponse({"ok": True, "message": "Acknowledged."})


# -- The bell -------------------------------------------------------------------------------


class BellFeedView(View):
    def get(self, request):
        if not settings.TRACKING_ENABLED:
            return JsonResponse({"unread": 0, "items": []})
        if not has_permission(request.user, bell.PAGE, "read"):
            return JsonResponse({"unread": 0, "items": []}, status=403)
        return JsonResponse(bell.feed(request.user))


class BellReadView(View):
    def post(self, request):
        if not has_permission(request.user, bell.PAGE, "read"):
            return JsonResponse({"ok": False}, status=403)
        bell.AlertRead.objects.update_or_create(user=request.user, defaults={"last_read_at": timezone.now()})
        return JsonResponse({"ok": True})


# -- Readers --------------------------------------------------------------------------------


class ReadersView(TrackingScreenView):
    template_name = "tracking/readers.html"
    page_code = "tracking.readers"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        readers = list(readers_for(user).select_related("company").order_by("status", "name", "device_id"))
        open_now = {a.reader_id: a for a in ReaderAssignment.objects.filter(
            reader__in=readers, to_at__isnull=True).select_related("branch")}
        timings = engine.timings_by_branch({a.branch_id for a in open_now.values()})
        now = timezone.now()
        rows = []
        for r in readers:
            at = open_now.get(r.pk)
            offline_after = timings[at.branch_id].offline if at else engine.COMPANY_DEFAULT.offline
            contact = engine._contact(r)
            # worked out here, not read from Reader.online: the engine only runs in write mode
            online = contact is not None and (now - contact).total_seconds() < offline_after
            rows.append({"r": r, "branch": at.branch if at else None, "online": online,
                         "status": _reader_status(r, online), "contact": contact, "contact_ago": ago(contact, now),
                         "heartbeat_age": _age(r.last_heartbeat_at, now), "data_age": _age(r.last_data_at, now)})
        context["rows"] = rows
        context["counts"] = {s: sum(1 for x in readers if x.status == s) for s in ReaderStatus.values}
        context["online_count"] = sum(1 for row in rows if row["status"]["label"] == "Online")
        context["offline_count"] = sum(1 for row in rows if row["status"]["label"] == "Offline")
        context["all_branches"] = list(branches_for(user).filter(is_active=True).order_by("name")
                                       .values("id", "name", "company_id"))
        return context


def _age(moment, now):
    return int((now - moment).total_seconds()) if moment else None


def ago(moment, now):
    """"12 s ago", "4 min ago", "2 h ago", "3 d ago"; "Never" when there is none."""
    seconds = _age(moment, now)
    if seconds is None:
        return "Never"
    for size, unit in ((86400, "d"), (3600, "h"), (60, "min")):
        if seconds >= size:
            return f"{seconds // size} {unit} ago"
    return f"{max(seconds, 0)} s ago"


def _reader_status(reader, online):
    """One status per reader: Pending / Blocked / Online / Offline, with its chip tone."""
    if reader.status == ReaderStatus.PENDING:
        return {"key": "behind", "label": "Pending"}
    if reader.status == ReaderStatus.BLOCKED:
        return {"key": "missed", "label": "Blocked"}
    return {"key": "met", "label": "Online"} if online else {"key": "idle", "label": "Offline"}


def _branch(user, value):
    return branches_for(user).filter(pk=_int(value)).first()


class ReaderActionView(writes.WriteView):
    """POST one action on one reader; JSON {ok, message | errors}, like Devices."""

    page_code = "tracking.readers"
    permissions = ("update",)
    done = "Saved."

    def act(self, reader, data):
        raise NotImplementedError

    def post(self, request, pk=None):
        if not all(self.may(action) for action in self.permissions):
            return self.refused()
        reader = None
        if pk is not None:
            reader = readers_for(request.user).filter(pk=pk).first()
            if reader is None:
                raise Http404
        try:
            self.act(reader, writes._payload(request))
        except services.TrackingError as refused:
            return JsonResponse({"ok": False, "code": "invalid", "errors": [
                {"field": refused.field, "message": refused.message}]}, status=400)
        return JsonResponse({"ok": True, "message": self.done})


class ReaderAddView(ReaderActionView):
    """Add Reader's Save: it approves too, so it needs both rights."""

    permissions = ("create", "approve")
    done = "Reader saved and switched on."

    def act(self, reader, data):
        user = self.request.user
        branch = _branch(user, data.get("branch"))
        if branch is None:
            raise services.TrackingError("Choose the branch this reader stands at.", "Branch")
        services.save_new_reader(data.get("device_id"), branch=branch, name=data.get("name", ""), user=user)


class ReaderTestView(View):
    """Test connection (read-only). We cannot dial a reader, it calls us over
    4G, so this reports what we have heard from that device id."""

    def get(self, request):
        if not has_permission(request.user, "tracking.readers", "read"):
            return JsonResponse({"ok": False}, status=403)
        device_id = (request.GET.get("device_id") or "").strip()
        reader = Reader.objects.filter(device_id=device_id).first() if device_id else None
        # a device id typed in may be a pending reader, with no company yet
        if reader is not None and reader.company_id and not companies_for(request.user).filter(
                pk=reader.company_id).exists():
            reader = None
        return JsonResponse({"ok": True, **services.connection_report(reader)})


class ReaderApproveView(ReaderActionView):
    permissions = ("approve",)
    done = "Reader approved and switched on."

    def act(self, reader, data):
        user = self.request.user
        branch = _branch(user, data.get("branch"))
        if branch is None:
            raise services.TrackingError("Choose the branch this reader stands at.", "Branch")
        services.approve(reader, company=branch.company, branch=branch, name=data.get("name", ""), user=user)


class ReaderEditView(ReaderActionView):
    done = "Reader saved."

    def act(self, reader, data):
        user = self.request.user
        branch = None
        if data.get("branch"):
            branch = _branch(user, data.get("branch"))
            if branch is None:
                raise services.TrackingError("Choose a branch.", "Branch")
        services.edit(reader, name=data.get("name"), branch=branch, user=user)


class ReaderToggleView(ReaderActionView):
    done = "Saved."

    def act(self, reader, data):
        services.set_enabled(reader, bool(data.get("enabled")), user=self.request.user)


class ReaderKeepRawView(ReaderActionView):
    done = "Saved."

    def act(self, reader, data):
        services.set_keep_raw(reader, bool(data.get("enabled")), user=self.request.user)


class ReaderBlockView(ReaderActionView):
    permissions = ("approve",)
    done = "Reader blocked."

    def act(self, reader, data):
        services.block(reader, user=self.request.user)


class ReaderUnblockView(ReaderActionView):
    permissions = ("approve",)
    done = "Reader unblocked. It is pending approval again."

    def act(self, reader, data):
        services.unblock(reader, user=self.request.user)


# -- Antennas & Calibration ---------------------------------------------------------------


class AntennasView(TrackingScreenView):
    template_name = "tracking/antennas.html"
    page_code = "tracking.antennas"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        readers = list(readers_for(self.request.user).filter(status=ReaderStatus.APPROVED)
                       .order_by("name", "device_id"))
        chosen = next((r for r in readers if r.pk == _int(self.request.GET.get("reader"))), None) \
            or (readers[0] if readers else None)
        context.update({"readers": readers, "reader": chosen, "directions": Direction.choices})
        if chosen is None:
            return context
        have = {a.port: a for a in Antenna.objects.filter(reader=chosen)}
        antennas = [have.get(port) or Antenna(reader=chosen, port=port) for port in services.PORTS]
        sessions = list(Calibration.objects.filter(reader=chosen).order_by("-started_at")[:6])
        active = next((s for s in sessions if s.status in calibration.OPEN_STATES), None)
        context.update({"antennas": antennas, "has_limits": any(a.enter_rssi is not None for a in antennas),
                        "session": active, "history": [s for s in sessions if s is not active][:5]})
        if active and active.status == CalibrationStatus.OPEN:
            context["progress"] = calibration.progress(active)
        elif active is None:
            context["bike_options"] = [
                {"id": v.pk, "name": v.vehicle_name, "tag": engine.tag_of(v.rfid_epc)}
                for v in calibration.choosable(chosen)]
            # Observe: a past period, the last 3 hours by default (company time).
            zone = zone_for(chosen.company) if chosen.company_id else timezone.get_current_timezone()
            now = timezone.localtime(timezone.now(), zone).replace(second=0, microsecond=0)
            context.update({
                "observe_to": now.strftime("%Y-%m-%dT%H:%M"),
                "observe_from": (now - datetime.timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M"),
                "observe_min": (now - datetime.timedelta(days=calibration._retention_days(chosen)))
                .strftime("%Y-%m-%dT%H:%M"),
                "retention_days": calibration._retention_days(chosen),
                "observe_minutes": calibration.MIN_OBSERVE_MINUTES,
            })
        context["baseline_count"] = VehicleBaseline.objects.filter(reader=chosen).count()
        return context


class AntennaDirectionView(ReaderActionView):
    page_code = "tracking.antennas"
    done = "Direction saved."

    def act(self, reader, data):
        services.set_direction(reader, _int(data.get("port")), data.get("direction", ""))


class CalibrationStatusView(View):
    """What the screen polls while the bikes are parked."""

    def get(self, request, pk):
        if not has_permission(request.user, "tracking.antennas", "read"):
            return JsonResponse({"ok": False}, status=403)
        reader = readers_for(request.user).filter(pk=pk).first()
        session = reader and Calibration.objects.filter(reader=reader, status=CalibrationStatus.OPEN).first()
        if not session:
            return JsonResponse({"ok": False, "closed": True})
        return JsonResponse({"ok": True, **calibration.progress(session)})


class CalibrationView(writes.WriteView):
    """Start, finish, approve, discard, clear limits: one reader's calibration."""

    page_code = "tracking.antennas"

    def post(self, request, pk, action):
        need = "approve" if action in ("approve", "clear") else "update"
        if not self.may(need):
            return self.refused()
        reader = readers_for(request.user).filter(pk=pk, status=ReaderStatus.APPROVED).first()
        if reader is None:
            raise Http404
        data = writes._payload(request)
        try:
            message = self._do(action, reader, data)
        except calibration.CalibrationError as refused:
            return JsonResponse({"ok": False, "code": "invalid", "errors": [
                {"field": refused.field, "message": refused.message}]}, status=400)
        return JsonResponse({"ok": True, "message": message})

    def _do(self, action, reader, data):
        if action == "start":
            calibration.start(reader, data.get("vehicles") or [], user=self.request.user)
            return "Listening. Keep the bikes parked on the boundary."
        if action == "observe":
            zone = zone_for(reader.company)
            session = calibration.observe(reader, _local_moment(data.get("window_from"), zone),
                                          _local_moment(data.get("window_to"), zone), user=self.request.user)
            found, skipped = len(session.result["vehicles"]), len(session.result["skipped"])
            return (f"Worked out {found} vehicle{'s' if found != 1 else ''}'s range"
                    + (f"; {skipped} had too few readings" if skipped else "") + ". Review and approve.")
        if action == "clear":
            calibration.clear_limits(reader)
            return "Limits cleared. Any read now counts as present."
        session = Calibration.objects.filter(reader=reader, status__in=calibration.OPEN_STATES).first()
        if session is None:
            raise calibration.CalibrationError("No session is open for this reader.")
        if action == "finish":
            calibration.finish(session)
            return "Finished. Review the proposed limits."
        if action == "approve":
            calibration.approve(session, user=self.request.user)
            return "Limits approved. A daily check runs for the next 3 days."
        if action == "discard":
            calibration.discard(session)
            return "Session discarded."
        raise Http404


# -- Raw readings ---------------------------------------------------------------------------


def _moment(value):
    try:
        moment = datetime.datetime.fromisoformat(value) if value else None
    except ValueError:
        return None
    if moment is not None and timezone.is_naive(moment):
        moment = timezone.make_aware(moment, timezone.get_current_timezone())
    return moment


def _local_moment(value, zone):
    """`YYYY-MM-DDTHH:MM` from a date-time field, read in the company's zone; None if unreadable."""
    try:
        moment = datetime.datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None
    if moment is not None and timezone.is_naive(moment):
        moment = timezone.make_aware(moment, zone)
    return moment


class RawView(TrackingScreenView):
    """Prev / Next only: counting a day of readings on every page load would be
    the most expensive thing this screen does, for a number nobody needs."""

    template_name = "tracking/raw.html"
    page_code = "tracking.raw"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        params = self.request.GET
        readers = list(readers_for(self.request.user).order_by("name", "device_id"))
        chosen = next((r for r in readers if r.pk == _int(params.get("reader"))), None) \
            or (readers[0] if readers else None)
        tag = (params.get("tag") or "").strip().upper()[:8]
        now = timezone.localtime()
        end = _moment(params.get("to")) or now
        start = _moment(params.get("from")) or end - datetime.timedelta(hours=1)
        too_long = end - start > datetime.timedelta(hours=MAX_RAW_HOURS)
        if too_long:
            start = end - datetime.timedelta(hours=MAX_RAW_HOURS)

        page_no = max(_int(params.get("page")) or 1, 1)
        rows, timed_out = [], False
        if chosen is not None:
            offset = (page_no - 1) * PER_PAGE
            try:
                with transaction.atomic():
                    with connection.cursor() as cursor:
                        cursor.execute(f"SET LOCAL statement_timeout = '{RAW_QUERY_TIMEOUT}'")
                    found = Reading.objects.filter(reader_id=chosen.pk, received_at__gte=start, received_at__lte=end)
                    if tag:
                        found = found.filter(tag=tag.rjust(8, "0"))
                    rows = list(found.order_by("-received_at")[offset:offset + PER_PAGE + 1])
            except DatabaseError:
                timed_out = True
        has_next = len(rows) > PER_PAGE
        query = {k: v for k, v in params.items() if k != "page" and v}
        context.update({
            "readers": readers, "chosen_reader": chosen, "tag": tag, "rows": rows[:PER_PAGE], "too_long": too_long,
            "timed_out": timed_out, "page_no": page_no,
            "prev_query": urlencode({**query, "page": page_no - 1}) if page_no > 1 else "",
            "next_query": urlencode({**query, "page": page_no + 1}) if has_next else "",
            "from": timezone.localtime(start).strftime("%Y-%m-%dT%H:%M"),
            "to": timezone.localtime(end).strftime("%Y-%m-%dT%H:%M")})
        return context


# -- Tracking Settings ----------------------------------------------------------------------


class SettingsView(TrackingScreenView):
    template_name = "tracking/settings.html"
    page_code = "tracking.settings"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        companies = list(companies_for(user).filter(is_active=True).order_by("name").values("id", "name"))
        chosen = _int(self.request.GET.get("company"))
        if chosen not in {c["id"] for c in companies}:
            chosen = companies[0]["id"] if companies else None
        context.update({"companies": companies, "chosen_company": chosen})
        if chosen is None:
            return context
        settings_row = TrackingSettings.objects.filter(company_id=chosen).first() or TrackingSettings(company_id=chosen)
        branches = list(branches_for(user).filter(company_id=chosen, is_active=True).order_by("name"))
        own = {s.branch_id: s for s in BranchTrackingSettings.objects.filter(branch__in=branches)}
        context.update({"default": settings_row, "save_url": reverse("tracking-settings-save", args=[chosen]),
                        "min_offline": services.MIN_OFFLINE_SECONDS,
                        "rows": [{"branch": b, "own": own.get(b.pk)} for b in branches]})
        return context


class SettingsSaveView(writes.WriteView):
    page_code = "tracking.settings"

    def post(self, request, pk):
        if not self.may("update"):
            return self.refused()
        company = companies_for(request.user).filter(pk=pk).first()
        if company is None:
            raise Http404
        branches = list(branches_for(request.user).filter(company=company, is_active=True))
        try:
            services.save_settings(company, writes._payload(request), branches, user=request.user)
        except services.TrackingError as refused:
            return JsonResponse({"ok": False, "code": "invalid", "errors": [
                {"field": refused.field, "message": refused.message}]}, status=400)
        return JsonResponse({"ok": True, "message": "Tracking settings saved. They apply within 30 seconds."})


class TrackingPrivilegeView(PrivilegeScreenView):
    page_code = "tracking.privileges"
    module_code = "tracking"
    module_label = "Tracking"
