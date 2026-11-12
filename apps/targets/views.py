"""Targets screens (design/08-targets.md §6): the stations x months list, a
station's year, one month's days, and the writes behind the drawer. Every
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

PAGE = "targets.list"
ACTIONS = ("create", "read", "update", "delete")
MONTHS_AHEAD = 12


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


def _first_company(user):
    return companies_for(user).order_by("name").first()


def month_options(today):
    """This month and the next eleven, for the drawer's Month select."""
    out, year, month = [], today.year, today.month
    for _ in range(MONTHS_AHEAD):
        first = datetime.date(year, month, 1)
        out.append({"id": f"{year}-{month:02d}", "name": first.strftime("%B %Y")})
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return out


def record(target):
    """The edit drawer's prefill."""
    places = places_for(target.company, target.branch)
    return {"pk": target.pk, "title": f"{target.branch.name} · {target.period_start:%B %Y}",
            "branches": [target.branch_id], "month": f"{target.period_start:%Y-%m}",
            "amount": fmt(target.amount, places), "remarks": target.remarks}


class TargetsScreenView(PagePermissionMixin, ThemedTemplateView):
    page_code = PAGE

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        company = _first_company(user)
        today = services.today_for(company) if company else datetime.date.today()
        context["perm"] = {action: has_permission(user, PAGE, action) for action in ACTIONS}
        context["business_today"] = today
        context["branches_list"] = [{"id": b.pk, "name": b.name}
                                    for b in scoped_branches(user).filter(is_active=True).order_by("name")]
        context["months_list"] = month_options(today)
        context["drawer_target"] = theme_drawers.resolve(drawers.TARGET, context)
        context["save_url"] = reverse("targets-save")
        context["delete_url"] = reverse("targets-delete", args=[0])
        return context

    def year(self, today):
        year = _int(self.request.GET.get("year"))
        return year if year and 2000 <= year <= 2100 else today.year


# -- List: stations x months ----------------------------------------------------------------


class TargetListView(TargetsScreenView):
    template_name = "targets/list.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        params = self.request.GET
        year = self.year(context["business_today"])
        branches = scoped_branches(self.request.user).filter(is_active=True)
        states = State.objects.filter(pk__in=branches.values("location__state_id")).order_by("name")
        state = _int(params.get("state"))
        if state:
            branches = branches.filter(location__state_id=state)
        q = (params.get("q") or "").strip()
        if q:
            branches = branches.filter(name__icontains=q)
        rows = services.year_grid(branches.order_by("location__state__name", "name"), year)
        context.update({
            "year": year, "years": [year - 1, year, year + 1], "rows": rows,
            "month_names": [datetime.date(year, m, 1).strftime("%b") for m in range(1, 13)],
            "states": states, "chosen_state": state, "q": q,
        })
        return context


# -- One station's year ----------------------------------------------------------------------


class BranchView(TargetsScreenView):
    template_name = "targets/branch.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        branch = get_object_or_404(scoped_branches(self.request.user), pk=self.kwargs["pk"])
        today = services.today_for(branch.company)
        year = self.year(today)
        context.update({"branch": branch, "year": year, "years": [year - 1, year, year + 1],
                        **services.branch_year(branch, year)})
        return context


# -- One station's month ---------------------------------------------------------------------


def _month_target(user, pk, year, month):
    branch = get_object_or_404(scoped_branches(user), pk=pk)
    if not 1 <= month <= 12:
        raise Http404
    first, _ = services.month_bounds(year, month)
    target = (scoped_targets(user).filter(branch=branch, period_start=first)
              .select_related("company", "branch").first())
    return branch, first, target


class MonthView(TargetsScreenView):
    template_name = "targets/month.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        branch, first, target = _month_target(self.request.user, self.kwargs["pk"], self.kwargs["year"],
                                              self.kwargs["month"])
        _, last = services.month_bounds(first.year, first.month)
        context.update({"branch": branch, "first": first, "target": target,
                        "open": last >= services.today_for(branch.company),
                        "data_url": reverse("targets-month-data", args=[target.pk]) if target else ""})
        if target is not None:
            context.update(services.month_view(target))
            context["record"] = record(target)
        return context


class MonthDataView(PagePermissionMixin, View):
    """JSON of the month view's figures, for its 60-second refresh (T9)."""

    page_code = PAGE

    def get(self, request, pk):
        target = get_object_or_404(scoped_targets(request.user).select_related("company", "branch"), pk=pk)
        view = services.month_view(target)
        places = places_for(target.company, target.branch)
        money = lambda value: fmt(value, places) if value is not None else None  # noqa: E731
        f, today = view["figures"], view["today"]
        return JsonResponse({
            "amount": money(f.amount), "achieved": money(f.achieved), "percent": str(f.percent), "tone": f.tone,
            "remaining": money(f.remaining), "days_left": view["days_left"],
            "today": {"date": today["date"].isoformat(), "planned": money(today["planned"]),
                      "required": money(today["required"]), "achieved": money(today["achieved"])} if today else None,
            "days": [{"date": d["date"].isoformat(), "planned": money(d["planned"]), "required": money(d["required"]),
                      "achieved": money(d["achieved"]), "gap": money(d["gap"]), "state": d["state"]}
                     for d in view["days"]],
        })


# -- Writes ----------------------------------------------------------------------------------


def _amount(value, company):
    places = places_for(company)
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None, "Enter the target amount."
    if not amount.is_finite() or amount <= 0:
        return None, "The target amount must be more than 0."
    if -amount.as_tuple().exponent > places:
        return None, f"Use at most {places} decimals."
    if amount >= Decimal("1e10"):
        return None, "That amount is too large."
    return amount, None


def _invalid(errors, status=400):
    return JsonResponse({"ok": False, "code": "invalid", "errors": errors}, status=status)


class TargetSaveView(WriteView):
    """POST {pk?, branches: [ids], month: "YYYY-MM", amount, remarks}. With a pk
    it changes that target's amount and remarks; without, it adds the month's
    target to each station chosen (refused stations are named)."""

    model = Target
    page_code = PAGE

    def post(self, request):
        data = _payload(request)
        remarks = str(data.get("remarks") or "").strip()[:200]
        pk = _int(data.get("pk"))
        if pk:
            return self._update(request, pk, data, remarks)
        if not self.may("create"):
            return self.refused()
        errors = []
        wanted = {_int(v) for v in (data.get("branches") or [])} - {None}
        branches = list(scoped_branches(request.user).filter(pk__in=wanted).order_by("name"))
        if not branches:
            errors.append({"field": "Stations", "message": "Choose at least one station."})
        try:
            year, month = (int(part) for part in str(data.get("month") or "").split("-"))
            datetime.date(year, month, 1)
        except ValueError:
            errors.append({"field": "Month", "message": "Choose a month."})
            year = month = None
        company = branches[0].company if branches else _first_company(request.user)
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
        message = f"Target saved for {len(saved)} station{'s' if len(saved) != 1 else ''}."
        if refused:
            message += " Not saved: " + "; ".join(f"{b.name} ({why})" for b, why in refused) + "."
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
    page_code = PAGE

    def post(self, request, pk):
        if not self.may("delete"):
            return self.refused()
        target = get_object_or_404(scoped_targets(request.user).select_related("company", "branch"), pk=pk)
        name = f"{target.branch.name}, {target.period_start:%B %Y}"
        try:
            services.delete_target(target)
        except services.TargetError as error:
            return _invalid([{"field": error.field, "message": error.message}])
        return JsonResponse({"ok": True, "message": f"Target for {name} deleted."})


class TargetPrivilegeView(PrivilegeScreenView):
    page_code = "targets.privileges"
    module_code = "targets"
    module_label = "Targets"
