"""Fare screens: the list, the add/edit page and its two endpoints.

The page edits a whole fare in the browser and posts it in one piece to
`save/`. While the user works, it posts the same unsaved fare to `check/`,
which answers with errors, rules that never apply, holiday warnings and the
Test fare result -- so every judgement is made by apps.fare.pricing on the
server and the browser never re-implements the precedence.
"""

from django.db.models import Count, Q
from django.http import Http404, JsonResponse
from django.urls import reverse

from apps.company import writes as company_writes
from apps.company.models import UAE_WEEK
from apps.company.scoping import branches_for, companies_for
from apps.fare import payload, pricing, services
from apps.fare.models import Fare, FareLevel
from apps.fare.scoping import fares_for
from apps.fleet.scoping import categories_for, vehicle_types_for
from apps.portal.permissions import PagePermissionMixin
from apps.portal.screens import PrivilegeScreenView
from apps.portal.services import has_permission
from core.timezones import business_date_for
from theme.views import ThemedTemplateView

PAGE = "fare.fare"
ACTIONS = ("create", "read", "update", "delete", "print")


class FareScreenView(PagePermissionMixin, ThemedTemplateView):
    page_code = PAGE

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        context["perm"] = {action: has_permission(user, self.page_code, action) for action in ACTIONS}
        return context


def _period(fare, today):
    if fare.valid_to < today:
        return "Expired"
    if fare.valid_from > today:
        return "Upcoming"
    return "Current"


class FareListView(FareScreenView):
    template_name = "fare/fare_list.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        fares = (
            fares_for(user)
            .select_related("company", "vehicle_type__category")
            .prefetch_related("branch_links__branch")
            .annotate(season_count=Count("seasons", distinct=True),
                      rule_count=Count("rules", filter=Q(rules__season__isnull=True), distinct=True))
            .order_by("vehicle_type__vehicle_type_name", "package_minutes", "-valid_from")
        )
        today_of = {}
        rows = []
        for fare in fares:
            today = today_of.setdefault(fare.company_id, business_date_for(fare.company))
            branches = sorted(link.branch.name for link in fare.branch_links.all())
            is_company = fare.level == FareLevel.COMPANY
            rows.append({
                "pk": fare.pk,
                "name": fare.vehicle_type.vehicle_type_name,
                "category": fare.vehicle_type.category.category_name,
                "company": fare.company.name,
                "package": fare.package_minutes,
                "level": "Company" if is_company else "Branch",
                "scope": "All branches" if is_company else f"{len(branches)} branch{'es' if len(branches) != 1 else ''}",
                "branches": ", ".join(branches),
                "validity": f"{pricing.date_label(fare.valid_from)} – {pricing.date_label(fare.valid_to)}",
                "period": _period(fare, today),
                "base_fare": fare.base_fare,
                "seasons": fare.season_count,
                "rules": fare.rule_count,
                "active": fare.is_active,
                "edit_url": reverse("fare-fare-edit", args=[fare.pk]),
            })
        context.update({
            "fares": rows,
            "count_active": sum(1 for r in rows if r["active"]),
            "count_current": sum(1 for r in rows if r["active"] and r["period"] == "Current"),
            "count_inactive": sum(1 for r in rows if not r["active"]),
            "sees_every_company": user.sees_every_company,
            "categories": sorted({r["category"] for r in rows}),
            "delete_url": reverse("fare-fare-delete", args=[0]),
        })
        return context


BLANK_PRICE = {"base_fare": "", "grace_minutes": 0, "concurrent_interval_minutes": "",
               "concurrent_fare": "", "concurrent_grace_minutes": 0}


def _blank(user):
    return {"pk": None, "lock_version": 0, "company": user.company_id, "vehicle_type": None,
            "level": FareLevel.COMPANY, "branches": [], "valid_from": "", "valid_to": "",
            "is_active": True, "package_minutes": "", "base": dict(BLANK_PRICE), "rules": [], "seasons": []}


class FareFormView(FareScreenView):
    """Add (no pk) or edit (pk). Editing needs only `read`: without `update`
    the page opens read-only, and Test fare still works."""

    template_name = "fare/fare_form.html"

    def dispatch(self, request, *args, **kwargs):
        self.required_action = "read" if kwargs.get("pk") else "create"
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        pk = self.kwargs.get("pk")
        fare = None
        if pk:
            fare = services.with_children(fares_for(user).filter(pk=pk)).first()
            if fare is None:
                raise Http404("No such fare.")
        initial = payload.serialise(fare) if fare else _blank(user)
        can_save = context["perm"]["update" if fare else "create"]

        # What the page shows before the first live check: badges and notes
        # for the saved fare, computed by the same code the check endpoint runs.
        annotations = {"never": {}, "notes": {}, "holidays": []}
        if fare:
            answer = services.check(user, initial)
            annotations = {key: answer[key] for key in annotations}

        linked_type = fare.vehicle_type_id if fare else None
        linked_branches = {link.branch_id for link in fare.branch_links.all()} if fare else set()
        vehicle_types = (vehicle_types_for(user).select_related("category")
                         .filter(Q(is_active=True) | Q(pk=linked_type)).order_by("vehicle_type_name"))
        branches = (branches_for(user).filter(Q(is_active=True) | Q(pk__in=linked_branches))
                    .order_by("name"))
        options = {
            "vehicle_types": [
                {"id": v.pk, "name": v.vehicle_type_name, "company": v.company_id,
                 "category": v.category.category_name,
                 "tax": str(v.tax_percentage) if v.tax_percentage is not None else ""}
                for v in vehicle_types
            ],
            "branches": [{"id": b.pk, "name": b.name, "company": b.company_id} for b in branches],
            "days": [{"value": day.value, "short": day.label[:3]} for day in UAE_WEEK],
        }
        context.update({
            "fare": fare,
            "is_edit": fare is not None,
            "can_save": can_save,
            "initial": {**initial, **annotations},
            "options": options,
            "vehicle_types": options["vehicle_types"],
            "branches": options["branches"],
            "categories": list(categories_for(user).filter(is_active=True)
                               .order_by("category_name").values_list("category_name", flat=True)),
            "companies": list(companies_for(user).filter(is_active=True).order_by("name").values("id", "name")),
            "sees_every_company": user.sees_every_company,
            "save_url": reverse("fare-fare-save"),
            "check_url": reverse("fare-fare-check"),
            "list_url": reverse("fare-fare-list"),
        })
        return context


class FareSave(company_writes.WriteView):
    model = Fare
    page_code = PAGE

    def post(self, request, *args, **kwargs):
        data = company_writes._payload(request)
        if not self.may("update" if data.get("pk") else "create"):
            return self.refused()
        try:
            fare, warnings = services.save_fare(request.user, data)
        except services.NotFound:
            return JsonResponse({"ok": False, "code": "not_found",
                                 "errors": [{"field": "", "message": "This fare no longer exists."}]}, status=404)
        except services.Stale as stale:
            return JsonResponse({
                "ok": False, "code": "stale",
                "errors": [{"field": "", "message": str(stale)}],
                "note": "Reload the page to see their changes, then make yours again.",
            }, status=409)
        except services.Invalid as invalid:
            return JsonResponse({"ok": False, "code": "invalid", "errors": invalid.errors}, status=400)
        return JsonResponse({"ok": True, "pk": fare.pk, "lock_version": fare.lock_version,
                             "message": "Fare saved.", "warnings": warnings})


class FareCheck(company_writes.WriteView):
    """The form's live check and the Test fare dialog. Writes nothing."""

    model = Fare
    page_code = PAGE

    def post(self, request, *args, **kwargs):
        if not self.may("read"):
            return self.refused()
        data = company_writes._payload(request)
        fare = data.get("fare") if isinstance(data.get("fare"), dict) else {}
        test = data.get("test") if isinstance(data.get("test"), dict) else None
        return JsonResponse({"ok": True, **services.check(request.user, fare, test)})


class FareDelete(company_writes.EntityDeleteView):
    model = Fare
    page_code = PAGE
    noun = "Fare"


class FarePrivilegeView(PrivilegeScreenView):
    """Fare & Offers' copy of the shared privilege grid."""

    page_code = "fare.privileges"
    module_code = "fare"
    module_label = "Fare & Offers"
