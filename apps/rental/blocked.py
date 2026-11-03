"""Blocked Customers: who is blocked now, every block and unblock since, and
Block / Unblock with a reason (owner, 3 Nov 2026 -- client change request 4).

The list is read here; the change is services.set_customer_blocked (and the
Customer form, which logs through services.log_block_change). The tablets keep
reading Customer.is_blocked (customers/blocked), unchanged.
"""

import datetime
import io

from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.urls import reverse
from django.utils import timezone
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

from apps.company.writes import WriteView, _payload
from apps.portal.services import has_permission
from apps.rental import services
from apps.rental.models import Customer, CustomerBlockAction, CustomerBlockLog
from apps.rental.scoping import customers_for
from core.scoping import scoped_to
from core.timezones import zone_for

PAGE = "rental.blocked_customer"
PER_PAGE = 50
TABS = (("blocked", "Blocked now"), ("history", "History"))


def block_log_for(user):
    return scoped_to(CustomerBlockLog.objects.all(), user)


def _date(value):
    try:
        return datetime.date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _between(rows, field, params, zone):
    start, end = _date(params.get("from")), _date(params.get("to"))
    if start:
        rows = rows.filter(**{f"{field}__gte": datetime.datetime.combine(start, datetime.time.min, tzinfo=zone)})
    if end:
        rows = rows.filter(**{f"{field}__lt": datetime.datetime.combine(
            end + datetime.timedelta(days=1), datetime.time.min, tzinfo=zone)})
    return rows


def _person(user):
    return (user.display_name or user.username) if user else ""


def blocked_now(user, params, zone):
    rows = customers_for(user).filter(is_blocked=True).select_related("blocked_by")
    text = params.get("q", "").strip()
    if text:
        rows = rows.filter(Q(first_name__icontains=text) | Q(last_name__icontains=text)
                           | Q(mobile_full__icontains=text) | Q(customer_code__icontains=text))
    if params.get("by", "").isdigit():
        rows = rows.filter(blocked_by_id=int(params["by"]))
    return _between(rows, "blocked_at", params, zone).order_by("-blocked_at", "first_name", "pk")


def history(user, params, zone):
    rows = block_log_for(user).select_related("customer", "created_by")
    text = params.get("q", "").strip()
    if text:
        rows = rows.filter(Q(customer__first_name__icontains=text) | Q(customer__last_name__icontains=text)
                           | Q(customer__mobile_full__icontains=text) | Q(customer__customer_code__icontains=text)
                           | Q(reason__icontains=text))
    if params.get("by", "").isdigit():
        rows = rows.filter(created_by_id=int(params["by"]))
    if params.get("action") in CustomerBlockAction.values:
        rows = rows.filter(action=params["action"])
    return _between(rows, "at", params, zone)


def blocked_row(customer, zone):
    return {
        "pk": customer.pk, "code": customer.customer_code, "name": customer.full_name,
        "mobile": customer.mobile_full, "reason": customer.block_reason,
        "at": customer.blocked_at.astimezone(zone) if customer.blocked_at else None,
        "by": _person(customer.blocked_by),
    }


def history_row(entry, zone):
    return {
        "pk": entry.customer_id, "code": entry.customer.customer_code, "name": entry.customer.full_name,
        "mobile": entry.customer.mobile_full, "action": entry.action, "action_label": entry.get_action_display(),
        "reason": entry.reason, "at": entry.at.astimezone(zone), "by": _person(entry.created_by),
    }


def tab_of(params):
    tab = params.get("tab", "blocked")
    return tab if tab in dict(TABS) else "blocked"


def context_for(request):
    user, params = request.user, request.GET
    zone = zone_for(getattr(user, "company", None))
    tab = tab_of(params)
    since = timezone.now() - datetime.timedelta(days=30)
    log = block_log_for(user)
    rows = blocked_now(user, params, zone) if tab == "blocked" else history(user, params, zone)
    page = Paginator(rows, PER_PAGE).get_page(params.get("page"))
    make = blocked_row if tab == "blocked" else history_row
    filters = params.copy()
    for key in ("page", "tab"):
        filters.pop(key, None)
    query = filters.copy()
    query["tab"] = tab
    people = {e.created_by_id: _person(e.created_by)
              for e in log.select_related("created_by").exclude(created_by=None).order_by("created_by_id")
              .distinct("created_by_id")}
    return {
        "tab": tab, "tabs": [{"value": v, "label": label} for v, label in TABS],
        "rows": [make(r, zone) for r in page.object_list], "page": page,
        "query": query.urlencode(), "filter_query": filters.urlencode(), "params": params,
        "filtered": any(params.get(k) for k in ("q", "from", "to", "by", "action")),
        "kpis": {
            "blocked": customers_for(user).filter(is_blocked=True).count(),
            "blocked_30": log.filter(action=CustomerBlockAction.BLOCK, at__gte=since).count(),
            "unblocked_30": log.filter(action=CustomerBlockAction.UNBLOCK, at__gte=since).count(),
        },
        "people": sorted(people.items(), key=lambda kv: kv[1].lower()),
        "actions": CustomerBlockAction.choices,
        # Block / Unblock need the customer's own Update permission (Q17).
        "may_change": has_permission(user, "rental.customer", "update"),
        "may_export": has_permission(user, PAGE, "print"),
        "block_url": reverse("rental-blocked-customer-change"),
        "export_url": reverse("rental-blocked-customer-export"),
        "unblocked_customers": [
            {"id": c.pk, "label": f"{c.full_name} · {c.mobile_full}"}
            for c in customers_for(user).filter(is_blocked=False, is_active=True)
            .order_by("first_name", "last_name")[:3000]
        ],
    }


class BlockChange(WriteView):
    """POST {customer, blocked: true|false, reason} -- Block or Unblock."""

    model = Customer
    page_code = "rental.customer"

    def post(self, request, *args, **kwargs):
        if not self.may("update"):
            return self.refused()
        data = _payload(request)
        customer = customers_for(request.user).filter(pk=data.get("customer") or 0).first()
        if customer is None:
            return JsonResponse({"ok": False, "code": "invalid",
                                 "errors": [{"field": "Customer", "message": "Choose a customer."}]}, status=400)
        blocked = data.get("blocked") in (True, "true", "1", 1, "yes")
        try:
            services.set_customer_blocked(customer, blocked, data.get("reason"), request.user)
        except services.BlockRefused as refused:
            return JsonResponse({"ok": False, "code": "invalid",
                                 "errors": [{"field": refused.field, "message": refused.message}]}, status=400)
        verb = "blocked" if blocked else "unblocked"
        return JsonResponse({"ok": True, "message": f"{customer.full_name} {verb}."})


def workbook(request):
    """The current tab, with its filters, as .xlsx."""
    user, params = request.user, request.GET
    zone = zone_for(getattr(user, "company", None))
    tab = tab_of(params)
    wb = Workbook()
    ws = wb.active
    ws.title = "Blocked customers" if tab == "blocked" else "Block history"
    if tab == "blocked":
        heads = ["Customer Code", "Customer", "Mobile", "Reason", "Blocked At", "Blocked By"]
        data = [(r["code"], r["name"], r["mobile"], r["reason"],
                 r["at"].replace(tzinfo=None) if r["at"] else None, r["by"])
                for r in (blocked_row(c, zone) for c in blocked_now(user, params, zone).iterator())]
    else:
        heads = ["Customer Code", "Customer", "Mobile", "Action", "Reason", "At", "By"]
        data = [(r["code"], r["name"], r["mobile"], r["action_label"], r["reason"], r["at"].replace(tzinfo=None),
                 r["by"])
                for r in (history_row(e, zone) for e in history(user, params, zone).iterator())]
    ws.append([ws.title])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append([f"Generated on {timezone.now().astimezone(zone):%d/%m/%Y %H:%M} by {_person(user)}"])
    ws.append(heads)
    for cell in ws[3]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="F2F2F2")
    for row in data:
        ws.append(list(row))
    for column in ws.columns:
        letter = column[0].column_letter
        ws.column_dimensions[letter].width = min(48, max(12, *(len(str(c.value or "")) + 2 for c in column[2:])))
        for cell in column[3:]:
            if isinstance(cell.value, datetime.datetime):
                cell.number_format = "dd/mm/yyyy hh:mm"
    out = io.BytesIO()
    wb.save(out)
    name = f"{'blocked-customers' if tab == 'blocked' else 'customer-block-history'}-{timezone.now():%Y%m%d}.xlsx"
    response = HttpResponse(out.getvalue(),
                            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response["Content-Disposition"] = f'attachment; filename="{name}"'
    return response
