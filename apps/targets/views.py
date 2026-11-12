"""Targets screens (design/08-targets.md §6): Overview, Daily Progress, Set
Targets, a station's month detail, and the writes behind the drawer. Every
figure comes from apps/targets/services.py; these views only scope and draw.
"""

import datetime
from decimal import Decimal, InvalidOperation

from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.views import View

from apps.company.models import State
from apps.company.scoping import branches_for, companies_for
from apps.company.writes import WriteView, _payload
from apps.portal.permissions import PagePermissionMixin
from apps.portal.screens import PrivilegeScreenView
from apps.portal.services import has_permission
from apps.targets import drawers, services
from apps.targets.models import Target
from core.money import fmt, places_for
from theme import drawers as theme_drawers
from theme.views import ThemedTemplateView

OVERVIEW, DAILY, MANAGE = "targets.overview", "targets.daily", "targets.manage"


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def scoped_branches(user):
    return (branches_for(user).filter(company__in=companies_for(user))
            .select_related("company", "location__state"))


def scoped_targets(user):
    return Target.objects.filter(branch__in=branches_for(user), company__in=companies_for(user))


def _month(value, today):
    """(year, month) from "YYYY-MM", else today's."""
    try:
        year, month = (int(part) for part in str(value or "").split("-"))
        datetime.date(year, month, 1)
        return year, month
    except ValueError:
        return today.year, today.month


def _shift(year, month, by):
    index = year * 12 + (month - 1) + by
    return index // 12, index % 12 + 1


def _day(value, today):
    try:
        return datetime.date.fromisoformat(str(value))
    except ValueError:
        return today


def record(target):
    """The edit drawer's prefill."""
    places = places_for(target.company, target.branch)
    return {"pk": target.pk, "title": "Edit target", "branches": str(target.branch_id),
            "month": f"{target.period_start:%Y-%m}", "amount": fmt(target.amount, places), "remarks": target.remarks,
            "summary": f"{target.branch.name} · {target.period_start:%B %Y}"}


class TargetsScreenView(PagePermissionMixin, ThemedTemplateView):
    """Shared context: rights on Set Targets (the drawer), today, the emirate filter."""

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        company = companies_for(user).order_by("name").first()
        context["business_today"] = services.today_for(company) if company else datetime.date.today()
        context["can"] = {a: has_permission(user, MANAGE, a) for a in ("create", "update", "delete")}
        context["drawer_target"] = theme_drawers.resolve(drawers.TARGET, context)
        context["save_url"] = reverse("targets-save")
        context["delete_url"] = reverse("targets-delete", args=[0])
        branches = scoped_branches(user).filter(is_active=True)
        context["states"] = State.objects.filter(pk__in=branches.values("location__state_id")).order_by("name")
        context["chosen_state"] = _int(self.request.GET.get("state"))
        return context

    def branches(self, context):
        rows = scoped_branches(self.request.user).filter(is_active=True)
        if context["chosen_state"]:
            rows = rows.filter(location__state_id=context["chosen_state"])
        return rows.order_by("name")

    def month_context(self, context, url_name):
        year, month = _month(self.request.GET.get("month"), context["business_today"])
        first, last = services.month_bounds(year, month)
        state = f"&state={context['chosen_state']}" if context["chosen_state"] else ""

        def link(by):
            y, m = _shift(year, month, by)
            return f"{reverse(url_name)}?month={y}-{m:02d}{state}"

        context.update({"year": year, "month": month, "first": first, "last": last,
                        "prev_url": link(-1), "next_url": link(1),
                        "is_current": (year, month) == (context["business_today"].year,
                                                        context["business_today"].month),
                        "ended": last < context["business_today"]})
        return year, month


# -- Overview -------------------------------------------------------------------------------


class OverviewView(TargetsScreenView):
    template_name = "targets/overview.html"
    page_code = OVERVIEW

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        year, month = self.month_context(context, "targets-overview")
        context.update(services.overview(self.branches(context), year, month))
        return context


# -- Daily Progress ---------------------------------------------------------------------------


class DailyView(TargetsScreenView):
    template_name = "targets/daily.html"
    page_code = DAILY

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        today = context["business_today"]
        day = _day(self.request.GET.get("date"), today)
        state = f"&state={context['chosen_state']}" if context["chosen_state"] else ""
        base = reverse("targets-daily")
        context.update({
            "day": day, "is_today": day == today,
            "prev_url": f"{base}?date={day - datetime.timedelta(days=1):%Y-%m-%d}{state}",
            "next_url": f"{base}?date={day + datetime.timedelta(days=1):%Y-%m-%d}{state}",
            **services.daily(self.branches(context), day),
        })
        return context


# -- Set Targets ----------------------------------------------------------------------------


class ManageView(TargetsScreenView):
    template_name = "targets/manage.html"
    page_code = MANAGE

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        year, month = self.month_context(context, "targets-manage")
        rows = services.manage_rows(list(self.branches(context)), year, month)
        for row in rows:
            if row["target"] is not None:
                row["record_id"] = f"tgt-{row['target'].pk}"
                row["record"] = record(row["target"])
        context.update({"rows": rows, "month_key": f"{year}-{month:02d}",
                        "set_count": sum(1 for r in rows if r["target"] is not None)})
        return context


# -- A station's month ------------------------------------------------------------------------


class StationView(TargetsScreenView):
    template_name = "targets/station.html"
    page_code = OVERVIEW

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        branch = get_object_or_404(scoped_branches(self.request.user), pk=self.kwargs["pk"])
        year, month = self.kwargs["year"], self.kwargs["month"]
        if not 1 <= month <= 12:
            raise Http404
        first, last = services.month_bounds(year, month)
        target = (scoped_targets(self.request.user).filter(branch=branch, period_start=first)
                  .select_related("company", "branch").first())
        prev_y, prev_m = _shift(year, month, -1)
        next_y, next_m = _shift(year, month, 1)
        context.update({
            "branch": branch, "first": first, "target": target, "month_key": f"{year}-{month:02d}",
            "prev_url": reverse("targets-station", args=[branch.pk, prev_y, prev_m]),
            "next_url": reverse("targets-station", args=[branch.pk, next_y, next_m]),
            "open": last >= services.today_for(branch.company),
            "data_url": reverse("targets-month-data", args=[target.pk]) if target else "",
        })
        if target is not None:
            context.update(services.month_view(target))
            context["record"] = record(target)
        return context


class MonthDataView(PagePermissionMixin, View):
    """JSON of a station month's figures, for its 60-second refresh (T9)."""

    page_code = OVERVIEW

    def get(self, request, pk):
        from apps.targets.templatetags.targets import amount, signed

        target = get_object_or_404(scoped_targets(request.user).select_related("company", "branch"), pk=pk)
        view = services.month_view(target)
        f, today = view["figures"], view["today"]
        return JsonResponse({
            "amount": amount(f.amount), "achieved": amount(f.achieved), "percent": str(f.percent),
            "remaining": amount(f.remaining), "days_left": view["days_left"], "status": view["status"],
            "today": {"date": today["date"].isoformat(), "target": amount(today["target"]),
                      "achieved": amount(today["achieved"]), "gap": signed(today["gap"]),
                      "status": today["status"]} if today else None,
        })


# -- Writes ----------------------------------------------------------------------------------


def _amount(value, company):
    places = places_for(company)
    try:
        amount = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None, "Enter the target amount."
    if not amount.is_finite() or amount <= 0:
        return None, "The target amount must be more than 0."
    if -amount.as_tuple().exponent > places:
        return None, f"Use at most {places} decimals."
    if amount >= Decimal("1e10"):
        return None, "That amount is too large."
    return amount, None


def _ids(value):
    """Station ids sent as a list or as "1,2,3"."""
    if isinstance(value, str):
        value = value.split(",")
    return {_int(v) for v in (value or [])} - {None}


def _invalid(errors, status=400):
    return JsonResponse({"ok": False, "code": "invalid", "errors": errors}, status=status)


class TargetSaveView(WriteView):
    """POST {pk?, branches: [ids] or "1,2", month: "YYYY-MM", amount, remarks}. With
    a pk it changes that target's amount and remarks; without, it sets the month's
    target for each station chosen (refused stations are named)."""

    model = Target
    page_code = MANAGE

    def post(self, request):
        data = _payload(request)
        remarks = str(data.get("remarks") or "").strip()[:200]
        pk = _int(data.get("pk"))
        if pk:
            return self._update(request, pk, data, remarks)
        if not self.may("create"):
            return self.refused()
        errors = []
        branches = list(scoped_branches(request.user).filter(pk__in=_ids(data.get("branches"))).order_by("name"))
        if not branches:
            errors.append({"field": "Branches", "message": "Choose at least one branch."})
        try:
            year, month = (int(part) for part in str(data.get("month") or "").split("-"))
            datetime.date(year, month, 1)
        except ValueError:
            errors.append({"field": "Month", "message": "Choose a month."})
            year = month = None
        company = branches[0].company if branches else companies_for(request.user).first()
        amount, problem = _amount(data.get("amount"), company)
        if problem:
            errors.append({"field": "Target amount", "message": problem})
        if errors:
            return _invalid(errors)
        saved, refused = [], []
        try:
            for company_id in sorted({b.company_id for b in branches}):
                mine = [b for b in branches if b.company_id == company_id]
                done, skipped = services.create_targets(mine[0].company, mine, year, month, amount, remarks,
                                                        request.user)
                saved += done
                refused += skipped
        except services.TargetError as error:
            return _invalid([{"field": error.field, "message": error.message}])
        if not saved:
            return _invalid([{"field": b.name, "message": f"Not saved: {why}."} for b, why in refused])
        message = f"Target set for {len(saved)} branch{'es' if len(saved) != 1 else ''}."
        if refused:
            message += " Skipped: " + "; ".join(f"{b.name} ({why})" for b, why in refused) + "."
        return JsonResponse({"ok": True, "message": message, "saved": [t.pk for t in saved],
                             "refused": [{"branch": b.name, "reason": why} for b, why in refused]})

    def _update(self, request, pk, data, remarks):
        if not self.may("update"):
            return self.refused()
        target = get_object_or_404(scoped_targets(request.user).select_related("company", "branch"), pk=pk)
        amount, problem = _amount(data.get("amount"), target.company)
        if problem:
            return _invalid([{"field": "Target amount", "message": problem}])
        try:
            services.update_target(target, amount, remarks, request.user)
        except services.TargetError as error:
            return _invalid([{"field": error.field, "message": error.message}])
        return JsonResponse({"ok": True, "message": f"Target for {target.branch.name} saved.", "pk": target.pk})


class TargetDeleteView(WriteView):
    model = Target
    page_code = MANAGE

    def post(self, request, pk):
        if not self.may("delete"):
            return self.refused()
        target = get_object_or_404(scoped_targets(request.user).select_related("company", "branch"), pk=pk)
        name = f"{target.branch.name}, {target.period_start:%B %Y}"
        try:
            services.delete_target(target)
        except services.TargetError as error:
            return _invalid([{"field": error.field, "message": error.message}])
        return JsonResponse({"ok": True, "message": f"Target for {name} removed."})


class TargetPrivilegeView(PrivilegeScreenView):
    page_code = "targets.privileges"
    module_code = "targets"
    module_label = "Targets"
