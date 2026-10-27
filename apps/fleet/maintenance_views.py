"""Inventory -> Service & Maintenance and Image Sharing, on the web. Views only
read and translate; the rules are in apps/fleet/service_requests.py and
apps/fleet/image_shares.py (design/inventory/service-and-image-sharing.md).

Service & Maintenance: every report, by status tab; a report's page shows the
photo and what happened; Approve / Reject (approve right) a pending one, Back
in service (update right) an approved one -- the web is the only place for
that. Image Sharing: the shared photos, view and delete (delete right).
"""

import datetime

from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import Http404, JsonResponse
from django.urls import reverse
from django.utils import timezone

from apps.company.models import State
from apps.company.scoping import branches_for, companies_for
from apps.company.writes import WriteView, _payload
from apps.fleet import image_shares, service_requests
from apps.fleet.models import ImageShare, ServiceRequest, ServiceRequestStatus
from apps.portal.permissions import PagePermissionMixin
from apps.portal.services import has_permission
from apps.rental.models import DecisionChannel
from core.scoping import scoped_to
from core.timezones import business_date_for, zone_for
from theme.views import ThemedTemplateView

SERVICE_PAGE, SHARE_PAGE = "fleet.service_request", "fleet.image_share"
PER_PAGE = 50

# (tab, label, statuses) -- Pending first: what someone has to act on.
SERVICE_TABS = [
    ("pending", "Pending", (ServiceRequestStatus.PENDING,)),
    ("approved", "Under maintenance", (ServiceRequestStatus.APPROVED,)),
    ("completed", "Completed", (ServiceRequestStatus.COMPLETED,)),
    ("rejected", "Rejected", (ServiceRequestStatus.REJECTED,)),
]


def _date(value):
    try:
        return datetime.date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _day_start(day, zone):
    return datetime.datetime.combine(day, datetime.time.min, tzinfo=zone)


def _length(delta):
    """'2 d 4 h', '3 h 10 m', '25 m' -- how long something has been out."""
    minutes = max(int(delta.total_seconds() // 60), 0)
    days, rest = divmod(minutes, 1440)
    hours, mins = divmod(rest, 60)
    if days:
        return f"{days} d {hours} h"
    return f"{hours} h {mins} m" if hours else f"{mins} m"


class _InventoryScreen(PagePermissionMixin, ThemedTemplateView):
    actions = ("read",)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        branches = branches_for(user).filter(is_active=True)
        context.update({
            "perm": {a: has_permission(user, self.page_code, a) for a in self.actions},
            "branches": list(branches.order_by("name").values("id", "name", "location__state_id")),
            "states": list(State.objects.filter(locations__branches__in=branches).distinct()
                           .order_by("name").values("id", "name")),
        })
        return context


def _where(rows, params):
    """The shared filters: emirate, station, date range (company time)."""
    if params.get("state", "").isdigit():
        rows = rows.filter(branch__location__state_id=int(params["state"]))
    if params.get("branch", "").isdigit():
        rows = rows.filter(branch_id=int(params["branch"]))
    return rows


# -- Service & Maintenance ---------------------------------------------------------------


def _service_rows(request, zone):
    params = request.GET
    rows = _where(scoped_to(ServiceRequest.objects.all(), request.user), params)
    if params.get("target") == "vehicle":
        rows = rows.filter(vehicle__isnull=False)
    elif params.get("target") == "asset":
        rows = rows.filter(asset__isnull=False)
    text = params.get("q", "").strip()
    if text:
        rows = rows.filter(Q(request_no__icontains=text) | Q(vehicle__vehicle_name__icontains=text)
                           | Q(vehicle__vehicle_code__icontains=text) | Q(asset__asset_code__icontains=text)
                           | Q(issue__icontains=text) | Q(requested_by__display_name__icontains=text))
    start, end = _date(params.get("from")), _date(params.get("to"))
    if start:
        rows = rows.filter(requested_at__gte=_day_start(start, zone))
    if end:
        rows = rows.filter(requested_at__lt=_day_start(end + datetime.timedelta(days=1), zone))
    return rows


def _target(req):
    if req.vehicle_id:
        return {"kind": "Vehicle", "name": req.vehicle.vehicle_name, "code": req.vehicle.identifier}
    return {"kind": "Asset", "name": req.asset.asset_code, "code": req.asset.asset_type.asset_type_name}


def _out_for(req, now):
    """How long the vehicle or asset has been out of service (to now while
    open, to back in service once completed); nothing once rejected."""
    if req.status == ServiceRequestStatus.REJECTED:
        return None
    return _length((req.completed_at or now) - req.requested_at)


class ServiceRequestListView(_InventoryScreen):
    template_name = "fleet/service_request_list.html"
    page_code = SERVICE_PAGE

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user, params = self.request.user, self.request.GET
        company = getattr(user, "company", None)
        zone = zone_for(company)
        tabs = {value: statuses for value, _, statuses in SERVICE_TABS}
        tab = params.get("tab") if params.get("tab") in tabs else "pending"
        rows = _service_rows(self.request, zone)
        counts = dict(rows.order_by().values_list("status").annotate(n=Count("pk")))
        listed = service_requests.with_rows(rows.filter(status__in=tabs[tab])).order_by(
            "requested_at" if tab == "pending" else "-requested_at", "pk")   # the oldest waiting first
        page = Paginator(listed, PER_PAGE).get_page(params.get("page"))
        now = timezone.now()
        query = params.copy()
        query.pop("page", None)
        filter_query = query.copy()
        filter_query.pop("tab", None)
        scope = scoped_to(ServiceRequest.objects.all(), user)
        month_start = _day_start(business_date_for(company).replace(day=1), zone)
        context.update({
            "tab": tab, "tab_label": dict((v, label) for v, label, _ in SERVICE_TABS)[tab],
            "tabs": [{"value": v, "label": label, "count": sum(counts.get(s, 0) for s in statuses)}
                     for v, label, statuses in SERVICE_TABS],
            "kpis": [
                {"label": "Pending", "value": scope.filter(status=ServiceRequestStatus.PENDING).count()},
                {"label": "Under maintenance", "value": scope.filter(status=ServiceRequestStatus.APPROVED).count()},
                {"label": "Back in service this month", "value": scope.filter(
                    status=ServiceRequestStatus.COMPLETED, completed_at__gte=month_start).count()},
            ],
            "rows": [{
                "request_no": r.request_no, "station": r.branch.name, "target": _target(r), "issue": r.issue,
                "requested_by": r.requested_by.display_name if r.requested_by_id else "",
                "requested_at": r.requested_at.astimezone(zone), "status": r.status,
                "out_for": _out_for(r, now), "detail_url": reverse("fleet-service-request-detail", args=[r.pk]),
            } for r in page.object_list],
            "page": page, "query": query.urlencode(), "filter_query": filter_query.urlencode(), "params": params,
            "filtered": any(params.get(k) for k in ("q", "state", "branch", "target", "from", "to")),
        })
        return context


class ServiceRequestDetailView(_InventoryScreen):
    template_name = "fleet/service_request_detail.html"
    page_code = SERVICE_PAGE
    actions = ("read", "approve", "update")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        req = service_requests.with_rows(scoped_to(ServiceRequest.objects.all(), self.request.user)
                                         .filter(pk=self.kwargs["pk"])).select_related("device").first()
        if req is None:
            raise Http404("No such service request.")
        zone = zone_for(req.company)

        def local(moment):
            return moment.astimezone(zone) if moment else None

        target = _target(req)
        if req.vehicle_id:
            target["url"] = f"{reverse('fleet-vehicle-list')}?q={req.vehicle.vehicle_code}"
        context.update({
            "req": req, "target": target,
            "requested_at": local(req.requested_at), "decided_at": local(req.decided_at),
            "completed_at": local(req.completed_at), "out_for": _out_for(req, timezone.now()),
            "tablet": req.device.device_registration_id if req.device_id else "",
            "photo": reverse("photo", args=[req.photo_id, "display"]) if req.photo_id else None,
            "photo_full": reverse("photo", args=[req.photo_id, "original"]) if req.photo_id else None,
            "list_url": reverse("fleet-service-request-list"),
            "approve_url": reverse("fleet-service-request-approve", args=[req.pk]),
            "reject_url": reverse("fleet-service-request-reject", args=[req.pk]),
            "complete_url": reverse("fleet-service-request-complete", args=[req.pk]),
        })
        return context


class _ServiceWrite(WriteView):
    model = ServiceRequest
    page_code = SERVICE_PAGE
    action = "approve"

    def company_ids(self):
        return list(companies_for(self.request.user).values_list("id", flat=True))

    def answer(self, act, message):
        if not self.may(self.action):
            return self.refused()
        try:
            req = act(_payload(self.request))
        except service_requests.ServiceRefused as refused:
            return JsonResponse({"ok": False, "code": "invalid", "errors": [{"field": "", "message": refused.message}]},
                                status=400)
        return JsonResponse({"ok": True, "message": message,
                             "redirect": reverse("fleet-service-request-detail", args=[req.pk])})


class ServiceRequestApprove(_ServiceWrite):
    def post(self, request, pk):
        return self.answer(lambda data: service_requests.approve(
            request.user, pk, self.company_ids(), DecisionChannel.WEB, str(data.get("note") or "")),
            "Approved: under maintenance.")


class ServiceRequestReject(_ServiceWrite):
    def post(self, request, pk):
        return self.answer(lambda data: service_requests.reject(
            request.user, pk, self.company_ids(), DecisionChannel.WEB, str(data.get("note") or "")),
            "Rejected.")


class ServiceRequestComplete(_ServiceWrite):
    action = "update"

    def post(self, request, pk):
        return self.answer(lambda data: service_requests.complete(
            request.user, pk, self.company_ids(), str(data.get("note") or "")), "Back in service.")


# -- Image Sharing -----------------------------------------------------------------------


class ImageShareListView(_InventoryScreen):
    template_name = "fleet/image_share_list.html"
    page_code = SHARE_PAGE
    actions = ("read", "delete")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user, params = self.request.user, self.request.GET
        zone = zone_for(getattr(user, "company", None))
        rows = _where(image_shares.visible(scoped_to(ImageShare.objects.all(), user)), params)
        text = params.get("q", "").strip()
        if text:
            rows = rows.filter(Q(remarks__icontains=text) | Q(shared_by__display_name__icontains=text)
                               | Q(shared_by__username__icontains=text))
        rows = image_shares.between(rows, zone, _date(params.get("from")), _date(params.get("to")))
        page = Paginator(rows.order_by("-shared_at", "-pk"), PER_PAGE).get_page(params.get("page"))
        query = params.copy()
        query.pop("page", None)
        context.update({
            "rows": [{
                "pk": s.pk, "remarks": s.remarks, "station": s.branch.name,
                "emirate": s.branch.location.state.name,
                "shared_by": (s.shared_by.display_name or s.shared_by.username) if s.shared_by_id else "",
                "tablet": s.device.device_registration_id if s.device_id else "",
                "shared_at": s.shared_at.astimezone(zone), "received_at": s.created_on.astimezone(zone),
                "thumb": reverse("photo", args=[s.photo_id, "thumb"]),
                "full": reverse("photo", args=[s.photo_id, "original"]),
                "delete_url": reverse("fleet-image-share-delete", args=[s.pk]),
            } for s in page.object_list],
            "page": page, "query": query.urlencode(), "params": params,
            "filtered": any(params.get(k) for k in ("q", "state", "branch", "from", "to")),
        })
        return context


class ImageShareDelete(WriteView):
    model = ImageShare
    page_code = SHARE_PAGE

    def post(self, request, pk):
        if not self.may("delete"):
            return self.refused()
        company_ids = list(companies_for(request.user).values_list("id", flat=True))
        try:
            image_shares.delete(request.user, pk, company_ids)
        except image_shares.ShareRefused as refused:
            return JsonResponse({"ok": False, "code": "invalid", "errors": [{"field": "", "message": refused.message}]},
                                status=400)
        return JsonResponse({"ok": True, "message": "Image deleted.", "redirect": reverse("fleet-image-share-list")})
