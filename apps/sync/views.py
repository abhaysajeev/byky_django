"""Logs › Sync Issues: parked tablet calls, for the office to correct and
replay. System users only (`monitoring.sync_issues` is system-only in the
registry), next to Device Requests and Error Logs.

Filtered and paged in the database from the query string, like the other
Logs screens.
"""

import json

from django.conf import settings
from django.db.models import Q
from django.http import Http404, JsonResponse
from django.urls import reverse

from apps.company import writes
from apps.company.scoping import branches_for, companies_for
from apps.monitoring.models import ErrorLog, RequestLog
from apps.monitoring.views import MonitoringView, _filtered, _page
from apps.portal.services import has_permission
from apps.rental.models import Order
from apps.sync import parking, services
from apps.sync.models import IssueKind, IssueStatus, SyncIssue
from core.scoping import scoped_to

PAGE = "monitoring.sync_issues"


def issues_for(user):
    return scoped_to(SyncIssue.objects.all(), user, field="company")


def may_act(user):
    """Replay and Dismiss: system admins only (owner, 1 Nov 2026). A company
    user sees their own company's issues (Read) but never acts on them,
    whatever their role's Update tick says."""
    return bool(getattr(user, "sees_every_company", False)) and has_permission(user, PAGE, "update")


def _pretty(value):
    return json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True)


def _customer_label(data):
    """A parked customer create, as the Order column shows it: name and phone."""
    name = " ".join(str(part) for part in (data.get("first_name"), data.get("last_name")) if part)
    return " · ".join(str(part) for part in (name, data.get("full_number")) if part)


def _annotate(rows):
    """Order numbers (a customer create: the customer) and each open row's
    head -- its order's, or the parked create of the customer a booking
    names -- for one page of rows."""
    order_ids = {r.order_id for r in rows if r.order_id}
    numbers = dict(Order.objects.filter(pk__in=order_ids).values_list("pk", "order_no"))
    heads = {}
    for order_id, pk in (SyncIssue.objects.filter(order_id__in=order_ids, status=IssueStatus.OPEN)
                         .order_by("id").values_list("order_id", "id")):
        heads.setdefault(order_id, pk)
    named = {parking.customer_sync_id(r.kind, r.request_data) for r in rows if r.is_open} - {None}
    customers = dict(SyncIssue.objects.filter(sync_id__in=named, kind=IssueKind.CUSTOMER_CREATE,
                                              status=IssueStatus.OPEN).values_list("sync_id", "id"))
    for row in rows:
        if row.kind == IssueKind.CUSTOMER_CREATE:
            row.order_no = _customer_label(row.original_request_data)
        else:
            # A booking not on the server yet still carries its own receipt number.
            row.order_no = numbers.get(row.order_id) or (
                row.original_request_data.get("order_no") if row.kind == IssueKind.BOOKING else "")
        head = heads.get(row.order_id) if row.is_open else None
        row.waiting_on = head if head is not None and head != row.pk else None
        row.waiting_on_customer = False
        if row.waiting_on is None and row.is_open:
            row.waiting_on = customers.get(parking.customer_sync_id(row.kind, row.request_data))
            row.waiting_on_customer = row.waiting_on is not None
    return rows


class SyncIssueListView(MonitoringView):
    template_name = "sync/issue_list.html"
    page_code = PAGE

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        params = self.request.GET
        status = params.get("status", IssueStatus.OPEN)
        issues = issues_for(self.request.user).select_related("company", "branch", "device", "user")
        if status in IssueStatus.values:
            issues = issues.filter(status=status)
        if params.get("kind") in IssueKind.values:
            issues = issues.filter(kind=params["kind"])
        if params.get("company", "").isdigit():
            issues = issues.filter(company_id=int(params["company"]))
        if params.get("branch", "").isdigit():
            issues = issues.filter(branch_id=int(params["branch"]))
        text = params.get("q", "").strip()
        if text:
            match = Q(error_code__icontains=text) | Q(error_message__icontains=text)
            if text.isdigit():
                match |= Q(pk=int(text))
            order_ids = Order.objects.filter(order_no__icontains=text).values_list("pk", flat=True)[:200]
            match |= Q(order_id__in=list(order_ids)) | Q(original_request_data__order_no__icontains=text)
            # A parked customer create, by its phone.
            match |= Q(kind=IssueKind.CUSTOMER_CREATE, original_request_data__full_number__icontains=text)
            as_uuid = parking.uuid_or_none(text)
            if as_uuid:
                match |= Q(sync_id=as_uuid) | Q(order_id=as_uuid)
            issues = issues.filter(match)
        page, query = _page(self.request, issues.order_by("-id"))
        rows = _annotate(list(page.object_list))
        context.update({
            "page": page, "rows": rows, "query": query, "params": params, "status": status,
            "statuses": IssueStatus.choices, "kinds": IssueKind.choices,
            "keep_days": settings.SYNC_ISSUE_DAYS,
            "filtered": _filtered({k: v for k, v in params.items() if k != "status"}),
            "companies": companies_for(self.request.user).order_by("name").values_list("id", "name"),
            "branches": branches_for(self.request.user).order_by("name").values_list("id", "name"),
        })
        return context


class SyncIssueDetailView(MonitoringView):
    template_name = "sync/issue_detail.html"
    page_code = PAGE

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        issue = (issues_for(self.request.user)
                 .select_related("company", "branch", "device", "user", "session", "resolved_by",
                                 "dismissed_by")
                 .filter(pk=kwargs["pk"]).first())
        if issue is None:
            raise Http404("No such issue.")
        _annotate([issue])
        queue = []
        if issue.order_id:
            queue = _annotate(list(SyncIssue.objects.filter(company_id=issue.company_id, order_id=issue.order_id)
                                   .exclude(pk=issue.pk).order_by("id")))
        # A customer create: the parked bookings that name it (they replay after it).
        bookings = []
        if issue.kind == IssueKind.CUSTOMER_CREATE:
            bookings = _annotate([
                booking for booking in SyncIssue.objects.filter(company_id=issue.company_id, kind=IssueKind.BOOKING)
                .select_related("device").order_by("id")
                if parking.customer_sync_id(booking.kind, booking.original_request_data) == issue.sync_id
            ])
        context.update({
            "issue": issue,
            "is_head": issue.is_open and issue.waiting_on is None,
            "may_update": may_act(self.request.user),
            "may_see_requests": has_permission(self.request.user, "monitoring.request_log", "read"),
            "may_see_errors": has_permission(self.request.user, "monitoring.error_log", "read"),
            "body": _pretty(issue.request_data),
            "original": _pretty(issue.original_request_data),
            "attempts": [(a, _pretty(a.reply) if a.reply is not None else "") for a in issue.attempts.all()],
            "queue": queue,
            "bookings": bookings,
            "request_log": RequestLog.objects.filter(pk=issue.request_id).only("pk", "status", "path").first()
            if issue.request_id else None,
            "error_logs": list(ErrorLog.objects.filter(request_id=issue.request_id).only("pk", "created_at"))
            if issue.request_id else [],
            "urls": {
                "replay": reverse("sync-issue-replay", args=[issue.pk]),
                "dismiss": reverse("sync-issue-dismiss", args=[issue.pk]),
            },
        })
        return context


class _IssueAction(writes.WriteView):
    """POST one office action on one issue; JSON {ok, message | errors}."""

    page_code = PAGE

    def post(self, request, pk):
        if not may_act(request.user):
            return self.refused()
        data = writes._payload(request)
        try:
            message = self.act(issues_for(request.user), pk, data)
        except services.SyncActionError as refused:
            return JsonResponse({"ok": False, "code": "invalid",
                                 "errors": [{"field": "", "message": refused.message}]}, status=400)
        return JsonResponse({"ok": True, "message": message})


class SyncIssueReplay(_IssueAction):
    def act(self, issues, pk, data):
        body = data.get("request_data")
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except ValueError as error:
                raise services.SyncActionError(f"The request is not valid JSON: {error}.") from error
        outcome = services.replay(issues, pk, user=self.request.user, request_data=body)
        resolved, stopped = outcome["resolved"], outcome["stopped_at"]
        if stopped is None:
            return f"Recorded: {_issues(resolved)}."
        refused = f"issue {stopped.pk} was refused: {stopped.error_code} -- {stopped.error_message}"
        if resolved:
            return f"Recorded: {_issues(resolved)}. Then {refused}"
        return refused[0].upper() + refused[1:]


class SyncIssueDismiss(_IssueAction):
    def act(self, issues, pk, data):
        dismissed = services.dismiss(issues, pk, user=self.request.user, reason=data.get("reason"))
        return f"Dismissed: {_issues(dismissed)}."


def _issues(ids):
    return ("issue " if len(ids) == 1 else "issues ") + ", ".join(str(i) for i in ids)

