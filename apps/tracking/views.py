"""Antenna Tracking screens (design §7). They read what the engine and the
gateway write, through the company / branch scope of every other screen; every
action writes through services.py or calibration.py.

The raw and event tables grow quickly, so those two lists filter and page in the
database, from the query string, like the logs.
"""

import datetime
from urllib.parse import urlencode

from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import Http404, JsonResponse
from django.urls import reverse
from django.utils import timezone
from django.views import View

from apps.company import writes
from apps.company.models import Branch
from apps.company.scoping import branches_for, companies_for
from apps.portal.permissions import PagePermissionMixin
from apps.portal.screens import PrivilegeScreenView
from apps.portal.services import has_permission
from apps.tracking import bell, calibration, services
from apps.tracking.models import (
    Antenna,
    BranchTrackingSettings,
    Calibration,
    CalibrationMethod,
    CalibrationStatus,
    Direction,
    EventKind,
    PointZone,
    PresenceEvent,
    PresenceState,
    Reader,
    ReaderAssignment,
    ReaderStatus,
    Reading,
    TrackingSettings,
    VehiclePresence,
)
from theme.views import ThemedTemplateView

ACTIONS = ("create", "read", "update", "approve")
PER_PAGE = 100
MAX_RAW_HOURS = 24
ALERT_STATES = (PresenceState.MISSING, PresenceState.ALERT)
STATE_ORDER = (PresenceState.MISSING, PresenceState.ALERT, PresenceState.UNKNOWN, PresenceState.PRESENT,
               PresenceState.ON_RENT, PresenceState.ON_TEST, PresenceState.IN_SERVICE)
BADGE = {PresenceState.PRESENT: "scr-badge-active", PresenceState.ON_RENT: "scr-badge-approved",
         PresenceState.ON_TEST: "scr-badge-approved", PresenceState.IN_SERVICE: "scr-badge-pending",
         PresenceState.ALERT: "scr-badge-pending", PresenceState.MISSING: "scr-badge-rejected",
         PresenceState.UNKNOWN: "scr-badge-inactive"}
REASON_LABEL = {"test_overdue": "Test ride overdue", "not_seen": "Not heard", "weak_signal": "Signal below limit",
                "edge": "At the edge", "reader_offline": "Reader offline", "no_reader": "No reader"}


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def readers_for(user):
    """Approved readers of the user's companies. A pending reader has no company
    yet, so only a user who sees every company sees it."""
    mine = Q(company__in=companies_for(user))
    return Reader.objects.filter(mine | Q(company__isnull=True)) if user.sees_every_company \
        else Reader.objects.filter(mine)


class TrackingScreenView(PagePermissionMixin, ThemedTemplateView):
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        context["perm"] = {action: has_permission(user, self.page_code, action) for action in ACTIONS}
        context["branches_list"] = list(branches_for(user).filter(is_active=True).order_by("name").values("id", "name"))
        return context


# -- Status Board ---------------------------------------------------------------------------


class StatusView(TrackingScreenView):
    template_name = "tracking/status.html"
    page_code = "tracking.status"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        rows = VehiclePresence.objects.filter(company__in=companies_for(user), branch__in=branches_for(user))
        chosen = _int(self.request.GET.get("branch"))
        if chosen not in {b["id"] for b in context["branches_list"]}:
            chosen = None

        scope = rows.filter(branch_id=chosen) if chosen else rows
        totals = dict(scope.order_by().values_list("state").annotate(n=Count("pk")))
        names = dict(PresenceState.choices)
        context["tiles"] = [{"label": names[s], "value": totals.get(s, 0), "badge": BADGE[s]} for s in STATE_ORDER]

        if chosen:
            vehicles = scope.select_related("vehicle", "heard_by_branch").order_by("state", "vehicle__vehicle_code")
            rank = {state: n for n, state in enumerate(STATE_ORDER)}
            context["vehicles"] = sorted(
                [self._vehicle(v) for v in vehicles], key=lambda row: (rank[row["state"]], row["code"]))
        else:
            counts = {}
            for branch_id, state, n in rows.order_by().values_list("branch_id", "state").annotate(n=Count("pk")):
                counts.setdefault(branch_id, {})[state] = n
            branches = Branch.objects.filter(pk__in=counts).order_by("name")
            context["branch_rows"] = [
                {"id": b.pk, "name": b.name, "cells": [counts[b.pk].get(s, 0) for s in STATE_ORDER]} for b in branches]
            context["state_labels"] = [names[s] for s in STATE_ORDER]
        context["chosen_branch"] = chosen
        return context

    @staticmethod
    def _vehicle(v):
        return {"code": v.vehicle.vehicle_code, "name": v.vehicle.vehicle_name, "state": v.state,
                "state_label": v.get_state_display(), "badge": BADGE[v.state],
                "reason": REASON_LABEL.get(v.reason, ""), "since": v.since, "last_seen": v.last_seen_at,
                "heard_at": v.heard_by_branch.name if v.heard_by_branch_id else "", "low_battery": v.low_battery}


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
                {"code": r.vehicle.vehicle_code, "name": r.vehicle.vehicle_name, "branch": r.branch.name,
                 "state_label": r.get_state_display(), "badge": BADGE[r.state], "reason": REASON_LABEL.get(r.reason, ""),
                 "since": r.since, "last_seen": r.last_seen_at, "low_battery": r.low_battery,
                 "heard_at": r.heard_by_branch.name if r.heard_by_branch_id else "", "vehicle_id": r.pk}
                for r in rows.select_related("vehicle", "branch", "heard_by_branch").order_by("-since")[:500]]
            return context

        events = PresenceEvent.objects.filter(company__in=companies_for(user)).filter(
            Q(branch__in=branches_for(user)) | Q(branch__isnull=True))
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
        page = Paginator(events.select_related("vehicle", "branch", "reader", "acknowledged_by").order_by("-at", "-pk"),
                         PER_PAGE).get_page(params.get("page"))
        for event in page.object_list:
            event.what = self._what(event)
            event.subject = (event.vehicle.vehicle_code if event.vehicle_id
                             else (event.reader.name or event.reader.device_id if event.reader_id else "—"))
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
        event = PresenceEvent.objects.filter(pk=pk, company__in=companies_for(request.user)).first()
        if event is None:
            raise Http404
        event.acknowledged_by, event.acknowledged_at = request.user, timezone.now()
        event.note = str(writes._payload(request).get("note") or "").strip()[:200]
        event.save(update_fields=["acknowledged_by", "acknowledged_at", "note"])
        return JsonResponse({"ok": True, "message": "Acknowledged."})


# -- The bell -------------------------------------------------------------------------------


class BellFeedView(View):
    def get(self, request):
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
        now = timezone.now()
        rows = []
        for r in readers:
            times = [t for t in (r.last_heartbeat_at, r.last_data_at) if t]
            at = open_now.get(r.pk)
            rows.append({"r": r, "branch": at.branch if at else None, "last": max(times) if times else None,
                         "age": int((now - max(times)).total_seconds()) if times else None})
        context["rows"] = rows
        context["counts"] = {s: sum(1 for x in readers if x.status == s) for s in ReaderStatus.values}
        context["companies"] = list(companies_for(user).filter(is_active=True).values("id", "name"))
        context["all_branches"] = list(branches_for(user).filter(is_active=True).order_by("name")
                                       .values("id", "name", "company_id"))
        return context


def _company(user, value):
    return companies_for(user).filter(pk=_int(value)).first()


def _branch(user, value):
    return branches_for(user).filter(pk=_int(value)).first()


class ReaderActionView(writes.WriteView):
    """POST one action on one reader; JSON {ok, message | errors}, like Devices."""

    page_code = "tracking.readers"
    permission = "update"
    done = "Saved."

    def act(self, reader, data):
        raise NotImplementedError

    def post(self, request, pk=None):
        if not self.may(self.permission):
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
    permission = "create"
    done = "Reader added. It starts working once approved."

    def act(self, reader, data):
        company = _company(self.request.user, data.get("company"))
        if company is None:
            raise services.TrackingError("Choose a company.", "Company")
        services.add_reader(data.get("device_id"), company=company, name=data.get("name", ""), user=self.request.user)


class ReaderApproveView(ReaderActionView):
    permission = "approve"
    done = "Reader approved and switched on."

    def act(self, reader, data):
        user = self.request.user
        branch = _branch(user, data.get("branch"))
        if branch is None:
            raise services.TrackingError("Choose the station this reader stands at.", "Station")
        services.approve(reader, company=branch.company, branch=branch, name=data.get("name", ""), user=user)


class ReaderEditView(ReaderActionView):
    done = "Reader saved."

    def act(self, reader, data):
        user = self.request.user
        branch = None
        if data.get("branch"):
            branch = _branch(user, data.get("branch"))
            if branch is None:
                raise services.TrackingError("Choose a station.", "Station")
        services.edit(reader, name=data.get("name"), branch=branch, user=user)


class ReaderToggleView(ReaderActionView):
    done = "Saved."

    def act(self, reader, data):
        services.set_enabled(reader, bool(data.get("enabled")), user=self.request.user)


class ReaderBlockView(ReaderActionView):
    permission = "approve"
    done = "Reader blocked."

    def act(self, reader, data):
        services.block(reader, user=self.request.user)


class ReaderUnblockView(ReaderActionView):
    permission = "approve"
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
        context.update({"readers": readers, "reader": chosen, "directions": Direction.choices,
                        "methods": CalibrationMethod.choices, "zones": PointZone.choices})
        if chosen is None:
            return context
        have = {a.port: a for a in Antenna.objects.filter(reader=chosen)}
        context["antennas"] = [have.get(port) or Antenna(reader=chosen, port=port) for port in services.PORTS]
        sessions = list(Calibration.objects.filter(reader=chosen).order_by("-started_at")[:6])
        active = next((s for s in sessions if s.status in calibration.OPEN_STATES), None)
        context.update({"session": active, "history": [s for s in sessions if s is not active][:5]})
        if active and active.status == CalibrationStatus.OPEN:
            context["points"] = [
                {"seq": p.seq, "label": p.label, "zone": p.get_zone_display(), "variant": p.variant,
                 "started_at": p.started_at, "ended_at": p.ended_at,
                 "reads": sum(len(values) for values in by_port.values())}
                for p, by_port in calibration.point_samples(active)]
        return context


class AntennaDirectionView(ReaderActionView):
    page_code = "tracking.antennas"
    done = "Direction saved."

    def act(self, reader, data):
        services.set_direction(reader, _int(data.get("port")), data.get("direction", ""))


class CalibrationView(writes.WriteView):
    """Start, next point, finish, approve, discard: one reader's session."""

    page_code = "tracking.antennas"

    def post(self, request, pk, action):
        need = "approve" if action == "approve" else "update"
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
            calibration.start(reader, data.get("method"), data.get("tags"), user=self.request.user)
            return "Session started. Add the first point."
        session = Calibration.objects.filter(reader=reader, status__in=calibration.OPEN_STATES).first()
        if session is None:
            raise calibration.CalibrationError("No session is open for this reader.")
        if action == "point":
            calibration.next_point(session, data.get("label"), data.get("zone"), variant=data.get("variant", ""))
            return "Recording at this point."
        if action == "finish":
            calibration.finish(session)
            return "Session finished. Review the proposed limits."
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


class RawView(TrackingScreenView):
    template_name = "tracking/raw.html"
    page_code = "tracking.raw"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        params = self.request.GET
        readers = list(readers_for(self.request.user).order_by("name", "device_id"))
        reader = _int(params.get("reader"))
        if reader not in {r.pk for r in readers}:
            reader = None
        tag = (params.get("tag") or "").strip().upper()[:8]
        now = timezone.localtime()
        end = _moment(params.get("to")) or now
        start = _moment(params.get("from")) or end - datetime.timedelta(hours=1)
        too_long = end - start > datetime.timedelta(hours=MAX_RAW_HOURS)
        if too_long:
            start = end - datetime.timedelta(hours=MAX_RAW_HOURS)

        rows = Reading.objects.filter(reader_id__in=[reader] if reader else [r.pk for r in readers],
                                      received_at__gte=start, received_at__lte=end)
        if tag:
            rows = rows.filter(tag=tag.rjust(8, "0"))
        page = Paginator(rows.order_by("-received_at"), PER_PAGE).get_page(params.get("page"))
        names = {r.pk: r.name or r.device_id for r in readers}
        for row in page.object_list:
            row.reader_name = names.get(row.reader_id, "")
        query = {k: v for k, v in params.items() if k != "page" and v}
        context.update({"readers": readers, "chosen_reader": reader, "tag": tag, "page": page,
                        "query": urlencode(query), "too_long": too_long,
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
