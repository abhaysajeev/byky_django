"""The report screens: the page, its JSON (/data/) and its Excel export
(/export/) -- all three from one `Report.build`, with the same filters from the
same query string, so the download is exactly what the screen shows.

Read to see a report; Print to export it (the project's export rule, as on
the vehicle list). The JSON answers 401 once signed out and 403 without the
permission, so the page can say so instead of following a redirect.
"""

import datetime
import json
from decimal import Decimal
from urllib.parse import urlencode

from django.http import HttpResponse, JsonResponse
from django.urls import reverse
from django.utils import timezone
from django.views import View

from apps.portal.permissions import PagePermissionMixin
from apps.portal.services import has_permission
from apps.reports import xlsx
from apps.reports.base import COUNT, DATA, DATE, DATETIME, MONEY, PERCENT
from apps.reports.common import Ctx, person
from apps.reports.filters import CHOICE_FILTERS, ID_FILTERS, TEXT_FILTERS, Filters
from apps.reports.options import PARENT, names, options
from core.timezones import zone_for
from theme.views import ThemedTemplateView

PER_PAGE = 100
EXPORT_LIMIT = 50_000      # rows; past this the export asks for narrower filters instead of a huge file
STAMP = "%d/%m/%Y %I:%M %p"

# The filter bar's own words -- the stations' screens say emirate and station.
SCREEN_LABELS = {
    "state": "Emirate", "branch": "Station", "cashier": "Cashier", "category": "Category",
    "vehicle_type": "Vehicle type", "card_type": "Card type", "card_grade": "Card grade",
    "asset_type": "Asset type", "customer": "Customer name", "phone": "Phone no", "order_no": "Order no",
    "vehicle_no": "Vehicle no", "credit_note_no": "Credit note no", "device": "Device name",
    "rfid": "RFID tag no", "card_no": "Card number", "asset": "Asset code or serial", "status": "Status",
}
# Shown in the bar itself; the rest under More filters.
PRIMARY = ("state", "branch")


def _context(request, report):
    user = request.user
    zone = zone_for(getattr(user, "company", None))
    return Ctx(user=user, zone=zone, f=Filters(request.GET, report, zone, timezone.now()), now=timezone.now())


def _page(request):
    value = request.GET.get("page", "")
    return int(value) if value.isdigit() and int(value) > 0 else 1


def _value(value, kind):
    if value is None:
        return None
    if kind == DATETIME and isinstance(value, datetime.datetime):
        return value.strftime("%d/%m/%Y %I:%M:%S %p")
    if kind == DATE and isinstance(value, datetime.date):
        return value.strftime("%d/%m/%Y")
    if kind == MONEY and isinstance(value, Decimal | int):
        return f"{Decimal(value):.3f}"
    if kind == PERCENT and isinstance(value, Decimal | int | float):
        return f"{Decimal(value):.2f}"
    if kind == COUNT:
        return value
    return str(value)


def _cells(cells, columns):
    return {c.key: _value(cells.get(c.key), c.kind) for c in columns if c.key in cells}


def payload(report, result, ctx, labels):
    columns = report.screen_columns(result.columns)
    return {
        "columns": [{"key": c.key, "label": c.label, "kind": c.kind, "part": c.part, "sums": c.sums}
                    for c in columns],
        "rows": [{"kind": r.kind, "label": r.label, "cells": _cells(r.cells, columns),
                  "lines": [_cells(line, columns) for line in r.lines] if r.lines is not None else None}
                 for r in result.rows],
        "kpis": [{"label": k.label, "value": _value(k.value, k.kind), "kind": k.kind} for k in result.kpis],
        "count": result.count, "unit": list(report.unit), "page": result.page, "pages": result.pages,
        "first": result.first, "last": result.last, "size": result.size, "note": result.note,
        "blocks": result.blocks, "scope": scope(report, ctx, labels),
        "query": urlencode(ctx.f.query()), "limit": EXPORT_LIMIT,
    }


def scope(report, ctx, labels):
    """The grey line under the title: the period and the filters applied."""
    parts = [ctx.f.period()] if report.dated else []
    return parts + ctx.f.describe(labels)


class _Report:
    report = None

    @property
    def page_code(self):
        return self.report.page_code


class ReportView(_Report, PagePermissionMixin, ThemedTemplateView):
    template_name = "reports/report.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        report, request = self.report, self.request
        ctx = _context(request, report)
        opts = options(request.user, report.filters)
        result = report.build(ctx, _page(request), PER_PAGE)
        inputs = ctx.f.inputs()
        bar = []
        for key in report.filters:
            kind = "select" if key in ID_FILTERS else "status" if key in CHOICE_FILTERS else "text"
            bar.append({
                "key": key, "label": SCREEN_LABELS[key], "kind": kind, "value": inputs.get(key, ""),
                "options": opts.get(key, []) if kind == "select" else [
                    {"id": v, "name": n} for v, n in report.statuses],
                "parent": PARENT.get(key), "primary": key in PRIMARY,
                "placeholder": SCREEN_LABELS[key] if key in TEXT_FILTERS else "",
            })
        context.update({
            "report": report,
            "bar": bar,
            "primary_bar": [b for b in bar if b["primary"]],
            "more_bar": [b for b in bar if not b["primary"]],
            "inputs": inputs,
            "initial": payload(report, result, ctx, names(opts)),
            "data_url": reverse(f"reports-{report.slug}-data"),
            "export_url": reverse(f"reports-{report.slug}-export"),
            "can_export": has_permission(request.user, report.page_code, "print"),
        })
        return context


class ReportDataView(_Report, View):
    def dispatch(self, request, *args, **kwargs):
        if not getattr(request, "user", None):
            return JsonResponse({"detail": "Signed out."}, status=401)
        if not has_permission(request.user, self.report.page_code, "read"):
            return JsonResponse({"detail": "Not allowed."}, status=403)
        return super().dispatch(request, *args, **kwargs)

    def get(self, request):
        ctx = _context(request, self.report)
        result = self.report.build(ctx, _page(request), PER_PAGE)
        return JsonResponse(payload(self.report, result, ctx, names(options(request.user, self.report.filters))))


class ReportExportView(_Report, PagePermissionMixin, View):
    required_action = "print"

    def get(self, request):
        report = self.report
        ctx = _context(request, report)
        size = report.size(ctx) if hasattr(report, "size") else None
        if size is not None and size > EXPORT_LIMIT:
            return _too_big(size, report)
        result = report.build(ctx)
        rows = sum(max(len(r.lines or []), 1) for r in result.rows if r.kind == DATA)
        if rows > EXPORT_LIMIT:
            return _too_big(rows, report)
        labels = names(options(request.user, report.filters))
        lines = [
            report.sheet_title,
            "    ".join(scope(report, ctx, labels) + ([result.note] if result.note else [])) or "All",
            f"Report Generated On : {timezone.localtime(ctx.now, ctx.zone).strftime(STAMP)}"
            f"        Report Generated By : {person(request.user)}",
        ]
        body = xlsx.workbook(report, result, lines)
        f = ctx.f
        name = report.sheet_title.replace(" ", "_")
        if report.dated:
            name += f"_{f.start:%Y%m%d}_{f.end:%Y%m%d}"
        response = HttpResponse(body, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        response["Content-Disposition"] = f'attachment; filename="{name}.xlsx"'
        return response


def _too_big(rows, report):
    return HttpResponse(
        json.dumps({"detail": f"{rows:,} {report.unit[1]} -- more than {EXPORT_LIMIT:,} rows. "
                              "Narrow the dates or filters and export again."}),
        status=413, content_type="application/json")
