"""ERPNext > ERPNext Inbox and Privileges (design/10-erpnext-integration.md §17).

Step 1 shows what ERPNext has sent, exactly as it came; nothing is approved or
written to RMS from here yet.
"""

import json

from django.db.models import Count, Q
from django.http import Http404

from apps.integrations.erpnext import ACTIONS, DOCTYPES
from apps.integrations.models import ErpnextMessage
from apps.monitoring.views import _filtered, _page
from apps.portal.permissions import PagePermissionMixin
from apps.portal.screens import PrivilegeScreenView
from theme.views import ThemedTemplateView

PAGE = "integrations.erpnext_inbox"
ACTION_BADGES = {"create": "approved", "update": "pending", "deactivate": "inactive"}


class _InboxView(PagePermissionMixin, ThemedTemplateView):
    page_code = PAGE
    required_action = "read"


def _decorate(messages):
    for message in messages:
        message.badge = ACTION_BADGES.get(message.action, "rejected")
    return messages


class InboxListView(_InboxView):
    template_name = "integrations/inbox_list.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        params = self.request.GET
        messages = ErpnextMessage.objects.all()
        if params.get("doctype") in DOCTYPES:
            messages = messages.filter(doctype=params["doctype"])
        if params.get("action") in ACTIONS:
            messages = messages.filter(action=params["action"])
        if params.get("problems") == "yes":
            messages = messages.exclude(problems=[])
        elif params.get("problems") == "no":
            messages = messages.filter(problems=[])
        text = params.get("q", "").strip()
        if text:
            match = Q(erp_name__icontains=text) | Q(event_id__icontains=text)
            if text.isdigit():
                match |= Q(pk=int(text))
            messages = messages.filter(match)
        page, query = _page(self.request, messages.defer("payload", "raw_body").order_by("-received_at", "-id"))
        counts = dict(ErpnextMessage.objects.values_list("doctype").annotate(n=Count("id")))
        context.update({
            "page": page, "rows": _decorate(list(page.object_list)), "query": query, "params": params,
            "doctypes": [(doctype, counts.get(doctype, 0)) for doctype in DOCTYPES],
            "actions": ACTIONS, "filtered": _filtered(params),
            "total": ErpnextMessage.objects.count(),
        })
        return context


class InboxDetailView(_InboxView):
    template_name = "integrations/inbox_detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        message = ErpnextMessage.objects.select_related("token").filter(pk=kwargs["pk"]).first()
        if message is None:
            raise Http404("No such message.")
        _decorate([message])
        data = (message.payload or {}).get("data") if isinstance(message.payload, dict) else None
        context.update({
            "message": message,
            "fields": [(key, value if value is None or isinstance(value, str | int | float | bool)
                        else json.dumps(value, ensure_ascii=False))
                       for key, value in sorted(data.items())] if isinstance(data, dict) else [],
            "body": json.dumps(message.payload, indent=2, ensure_ascii=False, sort_keys=True)
            if message.payload is not None else message.raw_body,
        })
        return context


class ErpnextPrivilegeView(PrivilegeScreenView):
    """ERPNext's copy of the shared privilege grid: who sees the inbox, and
    (from step 2) who may approve what ERPNext sends."""

    page_code = "integrations.privileges"
    module_code = "integrations"
    module_label = "ERPNext"
