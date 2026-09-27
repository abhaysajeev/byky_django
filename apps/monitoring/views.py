"""Device Requests and Error Logs: read-only, system users only.

Both pages are system_only in the registry, so has_permission refuses a
company user whatever their role ticks, and the sidebar never lists them.

The tables grow by thousands of rows a day, so unlike the masters' lists
(filtered in the browser) these filter and paginate in the database, from the
query string -- a filtered page can be bookmarked or sent to someone.
"""

import datetime
import json
import uuid

from django.conf import settings
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404
from django.utils import timezone

from apps.company.models import Branch, Company
from apps.devices.models import Device
from apps.monitoring.models import ErrorLog, RequestLog
from apps.portal.permissions import PagePermissionMixin
from theme.views import ThemedTemplateView

PER_PAGE = 50
APPS = ("operator", "manager", "employee")
STATUS_CLASSES = {"2xx": (200, 299), "3xx": (300, 399), "4xx": (400, 499), "5xx": (500, 599)}
LEVELS = ("ERROR", "CRITICAL")


def _day(value):
    try:
        return datetime.date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _in_days(queryset, params):
    """created_at within [from, to], both whole days in the server's zone."""
    start, end = _day(params.get("from")), _day(params.get("to"))
    zone = timezone.get_current_timezone()
    if start:
        queryset = queryset.filter(created_at__gte=datetime.datetime.combine(start, datetime.time.min, zone))
    if end:
        queryset = queryset.filter(created_at__lt=datetime.datetime.combine(
            end + datetime.timedelta(days=1), datetime.time.min, zone))
    return queryset


def _page(request, queryset):
    page = Paginator(queryset, PER_PAGE).get_page(request.GET.get("page"))
    query = request.GET.copy()
    query.pop("page", None)
    return page, query.urlencode()


def _names(rows):
    """Device numbers, company and branch names for one page of rows."""
    device_ids = {r.device_id for r in rows if r.device_id}
    installs = {r.installation_id for r in rows if getattr(r, "installation_id", "")}
    devices = Device.objects.filter(Q(pk__in=device_ids) | Q(installation_id__in=installs)) \
        .values_list("pk", "installation_id", "device_registration_id")
    by_id = {pk: number for pk, _, number in devices}
    by_install = {install: number for _, install, number in devices}
    companies = dict(Company.objects.filter(pk__in={r.company_id for r in rows if r.company_id})
                     .values_list("pk", "name"))
    branch_ids = {getattr(r, "branch_id", None) for r in rows} - {None}
    branches = dict(Branch.objects.filter(pk__in=branch_ids).values_list("pk", "name"))
    for row in rows:
        row.device_no = by_id.get(row.device_id) or by_install.get(getattr(row, "installation_id", ""))
        row.company_name = companies.get(row.company_id, "")
        row.branch_name = branches.get(getattr(row, "branch_id", None), "")
    return rows


def _pretty(text):
    """A stored body, indented for reading (it may be cut short: then as is)."""
    try:
        return json.dumps(json.loads(text), indent=2, ensure_ascii=False)
    except (ValueError, TypeError):
        return text


class MonitoringView(PagePermissionMixin, ThemedTemplateView):
    required_action = "read"


class RequestListView(MonitoringView):
    template_name = "monitoring/request_list.html"
    page_code = "monitoring.request_log"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        params = self.request.GET
        logs = _in_days(RequestLog.objects.all(), params)
        if params.get("app") in APPS:
            logs = logs.filter(app=params["app"])
        if params.get("status") in STATUS_CLASSES:
            low, high = STATUS_CLASSES[params["status"]]
            logs = logs.filter(status__gte=low, status__lte=high)
        if params.get("code", "").strip():
            logs = logs.filter(code__iexact=params["code"].strip())
        device = params.get("device", "").strip()
        if device:
            match = Q(installation_id=device)
            if device.isdigit():
                found = Device.objects.filter(device_registration_id=int(device)).first()
                if found:
                    match |= Q(device_id=found.pk) | Q(installation_id=found.installation_id)
            logs = logs.filter(match)
        if params.get("path", "").strip():
            logs = logs.filter(path__icontains=params["path"].strip())
        if params.get("company", "").isdigit():
            logs = logs.filter(company_id=int(params["company"]))
        page, query = _page(self.request, logs.defer("request_body", "response_body"))
        context.update({
            "page": page, "rows": _names(list(page.object_list)), "query": query, "params": params,
            "apps": APPS, "status_classes": STATUS_CLASSES, "companies": Company.objects.order_by("name"),
            "keep_days": settings.MONITORING_REQUEST_DAYS,
        })
        return context


class RequestDetailView(MonitoringView):
    template_name = "monitoring/request_detail.html"
    page_code = "monitoring.request_log"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        log = RequestLog.objects.filter(pk=kwargs["pk"]).first()
        if log is None:
            raise Http404("No such request.")
        _names([log])
        context.update({
            "log": log,
            "request_body": _pretty(log.request_body),
            "response_body": _pretty(log.response_body),
            "errors": ErrorLog.objects.filter(request_id=log.pk).order_by("created_at"),
        })
        return context


class ErrorListView(MonitoringView):
    template_name = "monitoring/error_list.html"
    page_code = "monitoring.error_log"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        params = self.request.GET
        errors = _in_days(ErrorLog.objects.all(), params)
        if params.get("level") in LEVELS:
            errors = errors.filter(level=params["level"])
        if params.get("source") in ErrorLog.Source.values:
            errors = errors.filter(source=params["source"])
        text = params.get("q", "").strip()
        if text:
            errors = errors.filter(Q(message__icontains=text) | Q(exception_type__icontains=text)
                                   | Q(path__icontains=text) | Q(logger__icontains=text))
        try:
            errors = errors.filter(request_id=uuid.UUID(params["request"])) if params.get("request") else errors
        except ValueError:
            errors = errors.none()
        page, query = _page(self.request, errors.defer("traceback"))
        context.update({
            "page": page, "rows": _names(list(page.object_list)), "query": query, "params": params,
            "levels": LEVELS, "sources": ErrorLog.Source.choices, "keep_days": settings.MONITORING_ERROR_DAYS,
        })
        return context


class ErrorDetailView(MonitoringView):
    template_name = "monitoring/error_detail.html"
    page_code = "monitoring.error_log"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        error = ErrorLog.objects.filter(pk=kwargs["pk"]).first()
        if error is None:
            raise Http404("No such error.")
        _names([error])
        context.update({
            "error": error,
            "request_log": RequestLog.objects.filter(pk=error.request_id).only("pk", "status", "path").first()
            if error.request_id else None,
        })
        return context
