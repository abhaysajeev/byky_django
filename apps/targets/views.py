"""Targets screens (design/08-targets.md §6, design/11-target-profiles.md §6):
Overview, Daily Progress, Target Profiles, a station's targets, one target's
detail, and the writes behind them. Every figure comes from
apps/targets/services.py; these views only scope and draw.
"""

import datetime
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views import View

from apps.company.models import State
from apps.company.scoping import branches_for, companies_for
from apps.company.writes import WriteView, _payload
from apps.portal.permissions import PagePermissionMixin
from apps.portal.screens import PrivilegeScreenView
from apps.portal.services import has_permission
from apps.targets import drawers, services
from apps.targets import periods as P
from apps.targets.models import (
    PeriodType,
    ProfileStatus,
    Target,
    TargetKind,
    TargetProfile,
    TargetProfileBranch,
)
from core.money import fmt, places_for
from theme import drawers as theme_drawers
from theme.views import ThemedTemplateView

OVERVIEW, DAILY, PROFILES = "targets.overview", "targets.daily", "targets.profiles"
# The order a station's targets and the profile picker list types in.
TYPE_ORDER = {PeriodType.MONTHLY: 0, PeriodType.ANNUAL: 1, PeriodType.CUSTOM: 2, PeriodType.DAILY: 3}


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


def scoped_profiles(user):
    return TargetProfile.objects.filter(company__in=companies_for(user)).select_related("company")


def _month(value, today):
    """(year, month) from "YYYY-MM", else today's."""
    try:
        year, month = (int(part) for part in str(value or "").split("-"))
        datetime.date(year, month, 1)
        return year, month
    except ValueError:
        return today.year, today.month


def _day(value, today):
    try:
        return datetime.date.fromisoformat(str(value))
    except ValueError:
        return today


def _kind(value):
    return TargetKind.CUSTOMERS if value == TargetKind.CUSTOMERS else TargetKind.SALES


def record(target):
    """The edit drawer's prefill: one period of one profile."""
    places = 0 if target.kind == TargetKind.CUSTOMERS else places_for(target.company, target.branch)
    return {"pk": target.pk, "title": "Edit this period", "branches": str(target.branch_id), "kind": target.kind,
            "month": f"{target.period_start:%Y-%m}", "amount": fmt(target.amount, places), "remarks": target.remarks,
            "summary": f"{target.branch.name} · {target.profile.name} · {P.target_label(target)}"}


def profile_option(profile):
    return {"id": profile.pk, "name": profile.name, "metric": profile.get_metric_display(),
            "type": profile.get_period_type_display(), "status": profile.status}


class TargetsScreenView(PagePermissionMixin, ThemedTemplateView):
    """Shared context: rights on Target Profiles (the edit drawer), today, the state filter."""

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        company = companies_for(user).order_by("name").first()
        context["business_today"] = services.today_for(company) if company else datetime.date.today()
        context["can"] = {a: has_permission(user, PROFILES, a) for a in ("create", "update", "delete")}
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

    def pick_profile(self, context, wanted_kind=None):
        """The profile a page shows: ?profile=, else the active ones covering
        today -- monthly first, sales first (or customers when asked), by name."""
        profiles = list(scoped_profiles(self.request.user).exclude(status=ProfileStatus.DRAFT))
        today = context["business_today"]
        chosen = next((p for p in profiles if p.pk == _int(self.request.GET.get("profile"))), None)
        if chosen is None:
            def rank(p):
                covers = p.starts_on <= today <= p.ends_on
                return (not covers, p.status != ProfileStatus.ACTIVE, p.metric != (wanted_kind or TargetKind.SALES),
                        TYPE_ORDER.get(p.period_type, 9), p.name.lower())
            chosen = min(profiles, key=rank) if profiles else None
        profiles.sort(key=lambda p: (p.status != ProfileStatus.ACTIVE, TYPE_ORDER.get(p.period_type, 9),
                                     p.name.lower()))
        context["profiles"] = [profile_option(p) for p in profiles]
        context["profile"] = chosen
        context["is_customers"] = bool(chosen and chosen.metric == TargetKind.CUSTOMERS)
        context["kind"] = chosen.metric if chosen else TargetKind.SALES
        return chosen

    def query(self, context, **extra):
        """The page's own query string, with `extra` changed."""
        params = {"profile": context["profile"].pk if context.get("profile") else None,
                  "state": context.get("chosen_state")}
        params.update(extra)
        return "&".join(f"{k}={v}" for k, v in params.items() if v not in (None, ""))


# -- Overview -------------------------------------------------------------------------------


class OverviewView(TargetsScreenView):
    template_name = "targets/overview.html"
    page_code = OVERVIEW

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        today = context["business_today"]
        profile = self.pick_profile(context, _kind(self.request.GET.get("kind")))
        if self.request.GET.get("date"):
            day = _day(self.request.GET.get("date"), today)
        elif self.request.GET.get("month"):                       # older links: ?month=YYYY-MM
            year, month = _month(self.request.GET.get("month"), today)
            day = today if (year, month) == (today.year, today.month) else datetime.date(year, month, 1)
        else:
            day = today
        context.update({"rows": [], "totals": {}, "period": None})
        if profile is None:
            return context
        spans = P.profile_periods(profile)
        day = min(max(day, spans[0][0]), spans[-1][1])
        start, end = P.period_containing(profile, day)
        base = reverse("targets-overview")
        index = spans.index((start, end))
        context.update({
            "period": {"start": start, "end": end, "label": P.period_label(profile.period_type, start, end)},
            "range_label": P.range_label(profile),
            "prev_url": f"{base}?{self.query(context, date=spans[index - 1][0])}" if index > 0 else "",
            "next_url": f"{base}?{self.query(context, date=spans[index + 1][0])}" if index + 1 < len(spans) else "",
            "is_current": start <= today <= end,
            "current_url": f"{base}?{self.query(context)}",
            "covers_today": profile.starts_on <= today <= profile.ends_on,
            "is_card": profile.period_type == PeriodType.DAILY,
            "date_value": day.isoformat(),
        })
        context.update(services.profile_overview(profile, self.branches(context), day))
        return context


# -- Daily Progress ---------------------------------------------------------------------------


class DailyView(TargetsScreenView):
    template_name = "targets/daily.html"
    page_code = DAILY

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        today = context["business_today"]
        day = _day(self.request.GET.get("date"), today)
        multi_day = [p for p in scoped_profiles(self.request.user)
                     .filter(metric=TargetKind.SALES).exclude(status=ProfileStatus.DRAFT)
                     .exclude(period_type=PeriodType.DAILY).order_by("name")]
        chosen = next((p for p in multi_day if p.pk == _int(self.request.GET.get("profile"))), None)
        context["profile"] = chosen
        context["profiles"] = [profile_option(p) for p in multi_day]
        base = reverse("targets-daily")
        context.update({
            "day": day, "is_today": day == today,
            "prev_url": f"{base}?{self.query(context, date=day - datetime.timedelta(days=1))}",
            "next_url": f"{base}?{self.query(context, date=day + datetime.timedelta(days=1))}",
            "today_url": f"{base}?{self.query(context)}",
            **services.daily(self.branches(context), day, profile=chosen),
        })
        context["show_profile"] = chosen is None and len({r["target"].profile_id for r in context["rows"]}) > 1
        return context


# -- A station's targets ---------------------------------------------------------------------


class StationView(TargetsScreenView):
    """Every target on one station overlapping a month: its monthly target,
    that month's specials and campaigns, its annual year (11 §6)."""

    template_name = "targets/station.html"
    page_code = OVERVIEW

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        branch = get_object_or_404(scoped_branches(self.request.user), pk=self.kwargs["pk"])
        today = context["business_today"]
        year, month = _month(self.request.GET.get("month"), today)
        first, last = services.month_bounds(year, month)
        base = reverse("targets-station", args=[branch.pk])
        prev_first = P.add_months(first, -1)
        next_first = P.add_months(first, 1)
        context.update({
            "branch": branch, "first": first, "month_key": f"{year}-{month:02d}",
            "prev_url": f"{base}?month={prev_first:%Y-%m}", "next_url": f"{base}?month={next_first:%Y-%m}",
            "is_current": (year, month) == (today.year, today.month), "today_url": base,
            "rows": services.station_targets(branch, first if (year, month) != (today.year, today.month) else today),
        })
        return context


class StationMonthView(PagePermissionMixin, View):
    """The older link /station/<pk>/<year>-<month>/: the month's monthly
    target when there is one, else the station's page for that month."""

    page_code = OVERVIEW

    def get(self, request, pk, year, month):
        if not 1 <= month <= 12:
            raise Http404
        kind = _kind(request.GET.get("kind"))
        target = (scoped_targets(request.user)
                  .filter(branch_id=pk, kind=kind, period_type=PeriodType.MONTHLY,
                          period_start=datetime.date(year, month, 1)).first())
        if target is not None:
            return redirect("targets-target", pk=target.pk)
        get_object_or_404(scoped_branches(request.user), pk=pk)
        return redirect(f"{reverse('targets-station', args=[pk])}?month={year}-{month:02d}")


# -- One target -------------------------------------------------------------------------------


class TargetView(TargetsScreenView):
    """One period of one profile at one station: the day table for a
    multi-day sales target, a progress card for a Daily one, figures and pro
    rata for customers."""

    template_name = "targets/target.html"
    page_code = OVERVIEW

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        target = get_object_or_404(
            scoped_targets(self.request.user).select_related("company", "branch__location__state", "profile"),
            pk=self.kwargs["pk"])
        siblings = Target.objects.filter(profile=target.profile, branch=target.branch)
        before = siblings.filter(period_start__lt=target.period_start).order_by("-period_start").first()
        after = siblings.filter(period_start__gt=target.period_start).order_by("period_start").first()
        context.update(services.month_view(target))
        context.update({
            "branch": target.branch, "profile": target.profile, "is_customers": target.is_customers,
            "kind": target.kind, "record": record(target),
            "prev_url": reverse("targets-target", args=[before.pk]) if before else "",
            "next_url": reverse("targets-target", args=[after.pk]) if after else "",
            "station_url": f"{reverse('targets-station', args=[target.branch_id])}?month={target.period_start:%Y-%m}",
            # The 60 s refresh is for a sales period's days; a card or customers have none.
            "data_url": reverse("targets-month-data", args=[target.pk])
            if target.kind == TargetKind.SALES and not services.is_card(target) else "",
        })
        return context


class MonthDataView(PagePermissionMixin, View):
    """JSON of a target's figures, for its 60-second refresh (T9)."""

    page_code = OVERVIEW

    def get(self, request, pk):
        from apps.targets.templatetags.targets import amount, signed

        target = get_object_or_404(scoped_targets(request.user).select_related("company", "branch", "profile"),
                                   pk=pk)
        view = services.month_view(target)
        f, today = view["figures"], view["today"]
        return JsonResponse({
            "amount": amount(f.amount), "achieved": amount(f.achieved), "percent": str(f.percent),
            "remaining": amount(f.remaining), "days_left": view["days_left"], "status": view["status"],
            "today": {"date": today["date"].isoformat(), "target": amount(today["target"]),
                      "achieved": amount(today["achieved"]), "gap": signed(today["gap"]),
                      "status": today["status"]} if today else None,
        })


# -- Target Profiles ----------------------------------------------------------------------------


class ManageView(PagePermissionMixin, View):
    """Set Targets was replaced by Target Profiles (20 Nov 2026)."""

    page_code = PROFILES

    def get(self, request):
        return redirect("targets-profiles")


class ProfilesView(TargetsScreenView):
    template_name = "targets/profiles.html"
    page_code = PROFILES

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        found = scoped_profiles(self.request.user).prefetch_related("assignments")
        status = self.request.GET.get("status") or "current"
        if status == "current":
            found = found.exclude(status=ProfileStatus.ARCHIVED)
        elif status in ProfileStatus.values:
            found = found.filter(status=status)
        found = sorted(found, key=lambda p: (p.status == ProfileStatus.ARCHIVED, -p.starts_on.toordinal(),
                                             p.name.lower()))
        base = reverse("targets-profiles")
        context["status_tabs"] = [{"label": label, "on": value == status, "url": f"{base}?status={value}"}
                                  for value, label in (("current", "Current"), ("draft", "Draft"),
                                                       ("archived", "Archived"))]
        context["rows"] = services.profile_rows(found)
        context["can_profiles"] = context["can"]
        return context


def _fy_choices(start_month, today):
    """Financial years to pick from: two back, five ahead."""
    first, _ = P.financial_year(today, start_month)
    out = []
    for n in range(-2, 6):
        start = P.add_months(first, 12 * n)
        end = P.add_months(start, 12) - datetime.timedelta(days=1)
        out.append({"value": start.isoformat(), "end": end.isoformat(), "label": P.fy_label(start, end)})
    return out


class ProfileFormView(TargetsScreenView):
    """Add or edit a profile: its details, its branches with their amounts,
    and an annual profile's month weights (11 §6)."""

    template_name = "targets/profile_form.html"
    page_code = PROFILES

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        pk = self.kwargs.get("pk")
        profile = get_object_or_404(scoped_profiles(self.request.user), pk=pk) if pk else None
        if profile is None and not context["can"]["create"]:
            raise Http404
        company = profile.company if profile else companies_for(self.request.user).order_by("name").first()
        start_month = P.fy_start_month(company) if company else 1
        today = context["business_today"]
        branches = scoped_branches(self.request.user).filter(company=company).order_by("name") if company else []
        assignments = list(profile.assignments.select_related("branch")) if profile else []
        places = places_for(company) if company else 2
        weights = (services.weights_of(profile) if profile else None) or P.default_weights(
            P.financial_year(profile.starts_on if profile else today, start_month)[0])
        month_names = [P.add_months(datetime.date(2000, start_month, 1), i).strftime("%B") for i in range(12)]
        context.update({
            "profile": profile, "company": company,
            "locked": sorted(services.profile_locked_fields(profile)) if profile else [],
            "branch_options": [{"id": b.pk, "name": b.name} for b in branches if b.is_active
                               or any(a.branch_id == b.pk for a in assignments)],
            "chosen_branches": [a.branch_id for a in assignments if a.active],
            "fy_choices": _fy_choices(start_month, today),
            "fy_start_name": month_names[0],
            "back_url": reverse("targets-profiles"),
            "save_url": reverse("targets-profile-save"),
            "delete_profile_url": reverse("targets-profile-delete", args=[profile.pk]) if profile else "",
            "can_delete_profile": bool(profile and services.can_delete_profile(profile)),
            "metrics": TargetKind.choices, "types": PeriodType.choices, "statuses": ProfileStatus.choices,
            "data": {
                "pk": profile.pk if profile else None,
                "name": profile.name if profile else "",
                "metric": profile.metric if profile else TargetKind.SALES,
                "period_type": profile.period_type if profile else PeriodType.MONTHLY,
                "starts_on": (profile.starts_on if profile else today.replace(day=1)).isoformat(),
                "ends_on": (profile.ends_on if profile else P.month_bounds(today.year, today.month)[1]).isoformat(),
                "status": profile.status if profile else ProfileStatus.DRAFT,
                "remarks": profile.remarks if profile else "",
                "places": places,
                "assignments": [{"id": a.branch_id, "name": a.branch.name, "active": a.active,
                                 "amount": fmt(a.amount, 0 if profile.is_customers else places)}
                                for a in assignments],
                "weights": {str(k): str(v) for k, v in weights.items()},
                "month_names": month_names,
                "default_weights": {str(k): str(v) for k, v in P.default_weights(
                    P.financial_year(today, start_month)[0]).items()},
            },
        })
        return context


def _amount(value, company, kind=TargetKind.SALES):
    """A posted target: AED in the company's decimals, or for customers a
    whole number (C1)."""
    places = 0 if kind == TargetKind.CUSTOMERS else places_for(company)
    try:
        amount = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None, "Enter the target amount."
    if not amount.is_finite() or amount <= 0:
        return None, "The target amount must be more than 0."
    if -amount.as_tuple().exponent > places:
        if not places:
            return None, "A customer target is a whole number of customers."
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


def _date(value):
    try:
        return datetime.date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


class ProfileSaveView(WriteView):
    """POST {pk?, name, metric, period_type, starts_on, ends_on, status,
    remarks, branches: [{id, amount}], weights: {1..12: %}}. Saves the
    profile, its stations and weights, then (when active) makes or updates
    its targets; refused stations are named."""

    model = TargetProfile
    page_code = PROFILES

    def post(self, request):
        data = _payload(request)
        pk = _int(data.get("pk"))
        profile = get_object_or_404(scoped_profiles(request.user), pk=pk) if pk else None
        if not self.may("update" if profile else "create"):
            return self.refused()
        company = profile.company if profile else companies_for(request.user).order_by("name").first()
        if company is None:
            return _invalid([{"field": "Company", "message": "You have no company to set targets for."}])
        errors = []
        name = str(data.get("name") or "").strip()[:100]
        if not name:
            errors.append({"field": "Name", "message": "Give the profile a name."})
        elif scoped_profiles(request.user).filter(company=company, name__iexact=name).exclude(
                pk=profile.pk if profile else None).exists():
            errors.append({"field": "Name", "message": "Another profile already has this name."})
        metric = data.get("metric") if data.get("metric") in TargetKind.values else None
        period_type = data.get("period_type") if data.get("period_type") in PeriodType.values else None
        status = data.get("status") if data.get("status") in ProfileStatus.values else ProfileStatus.DRAFT
        if metric is None:
            errors.append({"field": "Metric", "message": "Choose sales or customers."})
        if period_type is None:
            errors.append({"field": "Type", "message": "Choose the type."})
        locked = services.profile_locked_fields(profile) if profile else set()
        if profile and "metric" in locked and metric != profile.metric:
            errors.append({"field": "Metric", "message": "This profile already has targets; make a new profile "
                                                         "for another metric."})
        if profile and "period_type" in locked and period_type != profile.period_type:
            errors.append({"field": "Type", "message": "This profile already has targets; make a new profile "
                                                       "for another type."})
        starts_on, ends_on = _date(data.get("starts_on")), _date(data.get("ends_on"))
        if starts_on is None or ends_on is None:
            errors.append({"field": "Period", "message": "Choose when the profile starts and ends."})
        elif ends_on < starts_on:
            errors.append({"field": "Period", "message": "The end can't be before the start."})
        elif period_type:
            starts_on, ends_on = P.normalise_range(period_type, starts_on, ends_on, P.fy_start_month(company))
            if (ends_on - starts_on).days > 366 * 5:
                errors.append({"field": "Period", "message": "A profile can cover at most five years."})
            elif profile and not services.shrink_allowed(profile, starts_on, ends_on):
                errors.append({"field": "Period", "message": "Periods that have started can't be cut off; the "
                                                             "range can only grow."})
        rows, seen = [], set()
        branches = {b.pk: b for b in scoped_branches(request.user).filter(company=company)}
        for item in data.get("branches") or []:
            branch = branches.get(_int((item or {}).get("id")))
            if branch is None or branch.pk in seen:
                continue
            seen.add(branch.pk)
            amount, problem = _amount((item or {}).get("amount"), company, metric or TargetKind.SALES)
            if problem:
                errors.append({"field": branch.name, "message": problem})
            rows.append((branch, amount))
        weights = None
        if period_type == PeriodType.ANNUAL:
            try:
                weights = {int(k): Decimal(str(v)) for k, v in (data.get("weights") or {}).items()}
            except (InvalidOperation, ValueError, TypeError):
                weights = {}
            if not P.weights_valid(weights):
                errors.append({"field": "Month weights", "message": "The 12 month weights must each be 0 or more "
                                                                    "and add up to 100%."})
        if status == ProfileStatus.ACTIVE and not rows:
            errors.append({"field": "Branches", "message": "Add at least one branch before making it active."})
        if errors:
            return _invalid(errors)
        with transaction.atomic():
            profile = self._save(request, profile, company, name, metric, period_type, starts_on, ends_on, status,
                                 str(data.get("remarks") or "").strip()[:200], rows, weights)
            made, changed, refused = services.sync_profile(profile, request.user)
        return JsonResponse({"ok": True, "pk": profile.pk, "message": self._message(profile, made, changed, refused),
                             "refused": [{"branch": r.branch.name, "reason": r.reason} for r in refused],
                             "redirect": reverse("targets-profile-edit", args=[profile.pk])})

    def _save(self, request, profile, company, name, metric, period_type, starts_on, ends_on, status, remarks,
              rows, weights):
        user = request.user
        if profile is None:
            profile = TargetProfile(company=company, created_by=user)
        profile.name, profile.metric, profile.period_type = name, metric, period_type
        profile.starts_on, profile.ends_on, profile.status = starts_on, ends_on, status
        profile.remarks, profile.modified_by = remarks, user
        profile.save()
        if weights is not None:
            services.set_weights(profile, weights)
        services.drop_outside(profile)
        wanted = {branch.pk: amount for branch, amount in rows}
        for assignment in list(profile.assignments.select_related("branch")):
            if assignment.branch_id not in wanted:
                if assignment.active:
                    services.remove_branch(profile, assignment.branch)
                continue
            amount = wanted.pop(assignment.branch_id)
            if assignment.amount != amount or not assignment.active:
                assignment.amount, assignment.active = amount, True
                assignment.save(update_fields=["amount", "active"])
        TargetProfileBranch.objects.bulk_create([TargetProfileBranch(profile=profile, branch_id=b, amount=a)
                                                 for b, a in wanted.items()])
        return profile

    @staticmethod
    def _message(profile, made, changed, refused):
        if profile.status != ProfileStatus.ACTIVE:
            return f"“{profile.name}” saved as {profile.get_status_display().lower()}; it makes no targets."
        parts = [f"“{profile.name}” saved."]
        if made:
            parts.append(f"{made} target{'s' if made != 1 else ''} made.")
        if changed:
            parts.append(f"{changed} updated to the new amount.")
        if refused:
            parts.append("Not set: " + "; ".join(f"{r.branch.name} — {r.reason}" for r in refused) + ".")
        return " ".join(parts)


class ProfileDeleteView(WriteView):
    model = TargetProfile
    page_code = PROFILES

    def post(self, request, pk):
        if not self.may("delete"):
            return self.refused()
        profile = get_object_or_404(scoped_profiles(request.user), pk=pk)
        if not services.can_delete_profile(profile):
            return _invalid([{"field": "Profile", "message": "This profile has started, or has an incentive "
                                                             "scheme; archive it instead."}])
        name = profile.name
        with transaction.atomic():
            profile.targets.all().delete()                # none has started (checked above)
            profile.delete()
        return JsonResponse({"ok": True, "message": f"“{name}” deleted.", "redirect": reverse("targets-profiles")})


# -- Editing one period -----------------------------------------------------------------------


class TargetSaveView(WriteView):
    """POST {pk, amount, remarks}: change one period's amount or remarks (it is
    then kept when the profile's amount changes). Without a pk, the older
    "set this month's target" call: {branches, month, amount, remarks, kind}
    on the company's monthly profile."""

    model = Target
    page_code = PROFILES

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
        kind = _kind(data.get("kind"))
        amount, problem = _amount(data.get("amount"), company, kind)
        if problem:
            errors.append({"field": "Target amount", "message": problem})
        if errors:
            return _invalid(errors)
        saved, refused = [], []
        try:
            for company_id in sorted({b.company_id for b in branches}):
                mine = [b for b in branches if b.company_id == company_id]
                done, skipped = services.create_targets(mine[0].company, mine, year, month, amount, remarks,
                                                        request.user, kind=kind)
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
        target = get_object_or_404(scoped_targets(request.user).select_related("company", "branch", "profile"),
                                   pk=pk)
        amount, problem = _amount(data.get("amount"), target.company, target.kind)
        if problem:
            return _invalid([{"field": "Target amount", "message": problem}])
        try:
            services.update_target(target, amount, remarks, request.user)
        except services.TargetError as error:
            return _invalid([{"field": error.field, "message": error.message}])
        return JsonResponse({"ok": True, "message": f"Target for {target.branch.name} saved.", "pk": target.pk})


class TargetDeleteView(WriteView):
    model = Target
    page_code = PROFILES

    def post(self, request, pk):
        if not self.may("delete"):
            return self.refused()
        target = get_object_or_404(scoped_targets(request.user).select_related("company", "branch", "profile"),
                                   pk=pk)
        name = f"{target.branch.name}, {P.target_label(target)}"
        try:
            services.delete_target(target)
        except services.TargetError as error:
            return _invalid([{"field": error.field, "message": error.message}])
        return JsonResponse({"ok": True, "message": f"Target for {name} removed."})


class TargetPrivilegeView(PrivilegeScreenView):
    page_code = "targets.privileges"
    module_code = "targets"
    module_label = "Targets"
