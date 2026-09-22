"""Company Management screens.

Markup ported from the wireframe's byky_cms app; every value comes from our own
models. Nothing is seeded or fabricated, so an empty database renders empty
grids and zero tiles -- which is the truth until the masters are loaded.

Forms do not save yet: drawers open, prefill and close. Writes land with the
CRUD pass.
"""

from django.db import transaction
from django.db.models import Count
from django.http import JsonResponse
from django.urls import reverse

from apps.company import drawers, forms, scoping, writes
from apps.company import services as company_services
from apps.company.models import (
    AuthorityType,
    Branch,
    BranchApprovalAuthority,
    BranchType,
    BranchWorkingTime,
    Company,
    Country,
    Department,
    Location,
    State,
    WeekDay,
)
from apps.crew.scoping import employees_for
from apps.portal.permissions import PagePermissionMixin
from apps.portal.screens import PrivilegeScreenView
from apps.portal.services import has_permission
from theme import drawers as theme_drawers
from theme.views import ThemedTemplateView

SHIFTS = [1, 2, 3, 4]
ACTIONS = ("create", "read", "update", "delete", "approve", "print")


class CompanyScreenView(PagePermissionMixin, ThemedTemplateView):
    """Shared by every screen in this module: the reference lists its drawers
    resolve against, and the permission flags its buttons check."""

    drawer_specs = drawers.SPECS

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        context.update({
            "countries_list": list(scoping.countries_for(user).filter(is_active=True).values("id", "name")),
            "states_list": list(scoping.states_for(user).filter(is_active=True).values("id", "name")),
            "locations_list": list(scoping.locations_for(user).filter(is_active=True).values("id", "name")),
            "branches_list": list(scoping.branches_for(user).filter(is_active=True).values("id", "name")),
            "departments_list": list(scoping.departments_for(user).filter(is_active=True).values("id", "name")),
            "companies_list": list(scoping.companies_for(user).filter(is_active=True).values("id", "name")),
            # The Branch drawer's three approval-authority pickers. Held at []
            # until the crew module existed.
            "employees_list": [
                {"id": e.pk, "name": f"{e.employee_code} — {e.full_name}"}
                for e in employees_for(user).filter(is_active=True)
            ],
            "perm": {
                action: has_permission(self.request.user, self.page_code, action)
                for action in ACTIONS
            },
        })
        return context

    def render_to_response(self, context, **response_kwargs):
        # Resolved here, not in get_context_data, so a subclass's own lists are
        # already in context when a drawer's dropdowns are filled.
        specs = theme_drawers.all_for_user(
            self.drawer_specs, self.request.user.sees_every_company
        )
        context.update(theme_drawers.resolve_all(specs, context))
        return super().render_to_response(context, **response_kwargs)


def _yes_no(value):
    return "Yes" if value else "No"


class CompanyListView(CompanyScreenView):
    template_name = "company/company_list.html"
    page_code = "company.company"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        rows = []
        for i, company in enumerate(
            scoping.companies_for(self.request.user).select_related("country", "state")
        ):
            sections = drawers.company_sections(company)
            filled = [f for section in sections for f in section["fields"] if f["value"]]
            total = [f for section in sections for f in section["fields"]]
            pct = round(len(filled) / len(total) * 100) if total else 0
            rows.append({
                "name": company.name,
                "code": company.short_code,
                "state": company.state.name if company.state_id else "",
                "contact_person": company.contact_person,
                "phone": company.phone_number,
                "email": company.email,
                "active": company.is_active,
                "pk": company.pk,
                "json_id": f"scr-record-company-{i}",
                "fill_pct": pct,
                "fill_level": "high" if pct >= 80 else "mid" if pct >= 50 else "low",
                "fields_json": {
                    field["key"]: field["value"]
                    for section in sections
                    for field in section["fields"]
                },
            })
        context.update({
            "companies": rows,
            "active_count": sum(1 for row in rows if row["active"]),
            "drawer_company": theme_drawers.resolve(drawers.company_spec(), context),
            # A company user works inside one company; only platform staff pick one.
            "may_add_company": self.request.user.sees_every_company,
        })
        context["save_url_company"] = reverse("company-company-save")
        context["delete_url_company"] = reverse("company-company-delete", args=[0])
        context["noun_company"] = "Company"
        return context


class CountryStateListView(CompanyScreenView):
    template_name = "company/country_state_list.html"
    page_code = "company.country_state"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        countries = []
        countries_qs = scoping.countries_for(self.request.user).annotate(state_count=Count("states"))
        for i, country in enumerate(countries_qs):
            countries.append({
                "name": country.name,
                "code": country.short_code,
                "states": country.state_count,
                "active": country.is_active,
                "pk": country.pk,
                "json_id": f"scr-record-country-{i}",
                "fields_json": {
                    "pk": country.pk,
                    "name": country.name,
                    "code": country.short_code,
                    "is_active": country.is_active,
                },
            })

        states = []
        state_rows = scoping.states_for(self.request.user).select_related("country").annotate(
            branch_count=Count("locations__branches", distinct=True)
        )
        for i, state in enumerate(state_rows):
            states.append({
                "name": state.name,
                "code": state.short_code,
                "country": state.country.name,
                "branches": state.branch_count,
                "active": state.is_active,
                "pk": state.pk,
                "json_id": f"scr-record-state-{i}",
                "fields_json": {
                    "pk": state.pk,
                    "name": state.name,
                    "code": state.short_code,
                    "country": state.country_id,
                    "is_active": state.is_active,
                },
            })

        context.update({
            "countries": countries,
            "states": states,
            "total_branches": scoping.branches_for(self.request.user).count(),
            # Vehicles and assets belong to modules that do not exist yet, so
            # these tiles read zero and their links stay inert.
            "total_fleet": 0,
            "total_assets": 0,
        })
        context.update({
            "save_url_country": reverse("company-country-save"),
            "delete_url_country": reverse("company-country-delete", args=[0]),
            "noun_country": "Country",
            "save_url_state": reverse("company-state-save"),
            "delete_url_state": reverse("company-state-delete", args=[0]),
            "noun_state": "State",
        })
        return context


class LocationListView(CompanyScreenView):
    template_name = "company/location_list.html"
    page_code = "company.location"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        rows = []
        for i, location in enumerate(
            scoping.locations_for(self.request.user).select_related("country", "state")
        ):
            rows.append({
                "code": location.short_code,
                "name": location.name,
                "country": location.country.name,
                "state": location.state.name,
                "landmark": location.landmark,
                "active": location.is_active,
                "pk": location.pk,
                "json_id": f"scr-record-location-{i}",
                "fields_json": {
                    "pk": location.pk,
                    "code": location.short_code,
                    "name": location.name,
                    "country": location.country_id,
                    "state": location.state_id,
                    "landmark": location.landmark,
                    "is_active": location.is_active,
                },
            })
        context["locations"] = rows
        context["save_url_location"] = reverse("company-location-save")
        context["delete_url_location"] = reverse("company-location-delete", args=[0])
        context["noun_location"] = "Location"
        return context


class DepartmentListView(CompanyScreenView):
    template_name = "company/department_list.html"
    page_code = "company.department"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        rows = []
        for i, department in enumerate(scoping.departments_for(self.request.user)):
            rows.append({
                "code": department.short_code,
                "name": department.name,
                "active": department.is_active,
                "pk": department.pk,
                "json_id": f"scr-record-department-{i}",
                "fields_json": {
                    "pk": department.pk,
                    "code": department.short_code,
                    "name": department.name,
                    "company": department.company_id,
                    "is_active": department.is_active,
                },
            })
        context["departments"] = rows
        context["save_url_department"] = reverse("company-department-save")
        context["delete_url_department"] = reverse("company-department-delete", args=[0])
        context["noun_department"] = "Department"
        return context


class BranchListView(CompanyScreenView):
    template_name = "company/branch_list.html"
    page_code = "company.branch"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        rows = []
        branches = (
            scoping.branches_for(self.request.user)
            .select_related("company", "location", "location__state")
            .prefetch_related("departments")
        )

        # One query for every branch's approvers, rather than three per row.
        authorities = {}
        for row in BranchApprovalAuthority.objects.filter(branch__in=branches):
            authorities.setdefault((row.branch_id, row.authority_type), []).append(
                row.employee_id
            )

        for i, branch in enumerate(branches):
            rows.append({
                "code": branch.short_code,
                "name": branch.name,
                "company": branch.company.name,
                "location": branch.location.name,
                "state": branch.location.state.name,
                "branch_type": branch.get_branch_type_display(),
                "is_hotel": branch.is_hotel,
                "app_payment": branch.accepts_app_payment,
                "multi_user": branch.is_multi_device,
                "test_vehicle": branch.allows_test_ride,
                "hotel_commission": branch.hotel_commission,
                "departments": ", ".join(d.name for d in branch.departments.all()),
                "active": branch.is_active,
                # No vehicle model yet, so every branch reports zero and the
                # vehicle list opens empty.
                "fleet": 0,
                "vehicles_json_id": f"scr-vehicles-branch-{i}",
                "vehicles_json": [],
                "pk": branch.pk,
                "json_id": f"scr-record-branch-{i}",
                "fields_json": {
                    "pk": branch.pk,
                    "code": branch.short_code,
                    "name": branch.name,
                    "company": branch.company_id,
                    "country": branch.location.state.country_id,
                    "state": branch.location.state_id,
                    "location": branch.location_id,
                    "branch_type": branch.branch_type,
                    "is_hotel": branch.is_hotel,
                    "app_payment": branch.accepts_app_payment,
                    "multi_user": branch.is_multi_device,
                    "test_vehicle": branch.allows_test_ride,
                    "hotel_commission": str(branch.hotel_commission),
                    "station_no": branch.station_number,
                    "address": branch.address,
                    "latitude": str(branch.latitude or ""),
                    "longitude": str(branch.longitude or ""),
                    "contact_no": branch.contact_no,
                    "departments": [d.pk for d in branch.departments.all()],
                    "leave_request_authority": authorities.get(
                        (branch.pk, AuthorityType.LEAVE_REQUEST), []),
                    "maintenance_authority": authorities.get(
                        (branch.pk, AuthorityType.MAINTENANCE), []),
                    "rms_app_authority": authorities.get(
                        (branch.pk, AuthorityType.RMS_APP_REQUEST), []),
                    "is_active": branch.is_active,
                },
            })

        by_type = {
            row["branch_type"]: sum(1 for r in rows if r["branch_type"] == row["branch_type"])
            for row in rows
        }
        context.update({
            "branches": rows,
            "total_fleet": 0,
            "assets_deployed": 0,
            "ho_count": by_type.get(BranchType.HEAD_OFFICE.label, 0),
            "depot_count": by_type.get(BranchType.DEPOT.label, 0),
            "station_count": by_type.get(BranchType.STATION.label, 0),
            "inactive_count": sum(1 for row in rows if not row["active"]),
            "branch_types_list": [{"id": value, "name": label} for value, label in BranchType.choices],
        })
        context["save_url_branch"] = reverse("company-branch-save")
        context["delete_url_branch"] = reverse("company-branch-delete", args=[0])
        context["noun_branch"] = "Branch"
        return context


class BranchWorkingTimeView(CompanyScreenView):
    """The weekly shift matrix, one branch at a time.

    The page renders an empty week; byky-working-time.js loads the chosen
    branch's schedule from BranchWorkingTimeSchedule and saves through
    BranchWorkingTimeSave, so the page never has to reload. `?branch=<id>`
    preselects a branch, which is what keeps it across a refresh.
    """

    template_name = "company/branch_working_time_edit.html"
    page_code = "company.branch_working_time"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        branches = (
            scoping.branches_for(self.request.user)
            .filter(is_active=True)
            .select_related("location")
            .annotate(shift_count=Count("working_times"))
            .order_by("name")
        )
        selected = self.request.GET.get("branch") or ""
        context.update({
            "days": [{"value": value, "label": label} for value, label in WeekDay.choices],
            "shifts": SHIFTS,
            "wt_branches": [
                {
                    "id": b.pk, "name": b.name, "code": b.short_code,
                    "location": b.location.name if b.location_id else "",
                    "has_schedule": b.shift_count > 0,
                }
                for b in branches
            ],
            "selected_branch": selected if selected.isdigit() else "",
            "schedule_url": reverse("company-branch-working-time-schedule", args=[0]),
            "save_url": reverse("company-branch-working-time-save"),
            "assign_url": reverse("company-branch-working-time-assign"),
        })
        return context


# --- writes ------------------------------------------------------------------
# Saving and deleting live in writes.py; these bind them to each entity.
# Country, State and Location carry no company column -- they are shared
# reference data (apps/company/scoping.py) -- so they are not scoped by one.

class CompanySave(writes.EntitySaveView):
    model = Company
    form_class = forms.CompanyForm
    page_code = "company.company"
    noun = "Company"
    scope_field = "id"


class CompanyDelete(writes.EntityDeleteView):
    model = Company
    page_code = "company.company"
    noun = "Company"
    scope_field = "id"


class CountrySave(writes.EntitySaveView):
    model = Country
    field_map = {"code": "short_code"}
    form_class = forms.CountryForm
    page_code = "company.country_state"
    noun = "Country"
    scoped = False


class CountryDelete(writes.EntityDeleteView):
    model = Country
    page_code = "company.country_state"
    noun = "Country"
    scoped = False


class StateSave(writes.EntitySaveView):
    model = State
    field_map = {"code": "short_code"}
    form_class = forms.StateForm
    page_code = "company.country_state"
    noun = "State"
    scoped = False


class StateDelete(writes.EntityDeleteView):
    model = State
    page_code = "company.country_state"
    noun = "State"
    scoped = False


class LocationSave(writes.EntitySaveView):
    model = Location
    field_map = {"code": "short_code"}
    form_class = forms.LocationForm
    page_code = "company.location"
    noun = "Location"
    scoped = False


class LocationDelete(writes.EntityDeleteView):
    model = Location
    page_code = "company.location"
    noun = "Location"
    scoped = False


class DepartmentSave(writes.EntitySaveView):
    model = Department
    field_map = {"code": "short_code"}
    form_class = forms.DepartmentForm
    page_code = "company.department"
    noun = "Department"


class DepartmentDelete(writes.EntityDeleteView):
    model = Department
    page_code = "company.department"
    noun = "Department"


# Drawer field id -> the authority it records. One table with a type, not three
# columns (design/02-company.md 3.3).
AUTHORITY_FIELDS = {
    "leave_request_authority": AuthorityType.LEAVE_REQUEST,
    "maintenance_authority": AuthorityType.MAINTENANCE,
    "rms_app_authority": AuthorityType.RMS_APP_REQUEST,
}


class BranchSave(writes.EntitySaveView):
    model = Branch
    field_map = {
        "code": "short_code",
        "app_payment": "accepts_app_payment",
        "multi_user": "is_multi_device",
        "test_vehicle": "allows_test_ride",
        "station_no": "station_number",
    }
    form_class = forms.BranchForm
    page_code = "company.branch"
    noun = "Branch"

    def after_save(self, instance, data):
        """The three Approval Authority pickers.

        They are not on BranchForm because they write a second table, and they
        were collected by the drawer and then dropped -- three filled lists that
        looked recorded and were not. Each save replaces that branch's rows for
        the types the drawer posted, so unpicking someone removes their
        authority.
        """
        allowed = set(
            employees_for(self.request.user)
            .filter(company=instance.company_id)
            .values_list("pk", flat=True)
        )
        for field, authority in AUTHORITY_FIELDS.items():
            if field not in data:
                continue                       # that section was not on screen
            picked = {int(pk) for pk in (data.get(field) or []) if str(pk).isdigit()}
            rows = BranchApprovalAuthority.objects.filter(
                branch=instance, authority_type=authority
            )
            rows.exclude(employee_id__in=picked).delete()
            held = set(rows.values_list("employee_id", flat=True))
            for employee_id in picked & allowed - held:
                BranchApprovalAuthority.objects.create(
                    branch=instance, employee_id=employee_id, authority_type=authority
                )


class BranchDelete(writes.EntityDeleteView):
    model = Branch
    page_code = "company.branch"
    noun = "Branch"


def _schedule_invalid(errors):
    return JsonResponse({"ok": False, "code": "invalid", "errors": errors}, status=400)


def _replace_schedule(branch, rows, user):
    """Swap the branch's whole week for `rows`. Called inside a transaction."""
    BranchWorkingTime.objects.filter(branch=branch).delete()
    fresh = [BranchWorkingTime(branch=branch, created_by=user, **row) for row in rows]
    for row in fresh:
        row.apply_approval_defaults()
    BranchWorkingTime.objects.bulk_create(fresh)


class BranchWorkingTimeSchedule(writes.WriteView):
    """GET one branch's saved week, for the screen to fill its matrix."""

    model = BranchWorkingTime
    page_code = "company.branch_working_time"

    def get(self, request, branch_id, *args, **kwargs):
        if not self.may("read"):
            return self.refused()
        branch = scoping.branches_for(request.user).filter(pk=branch_id).first()
        if branch is None:
            return JsonResponse({
                "ok": False, "code": "not_found",
                "errors": [{"field": "Branch", "message": "That branch was not found."}],
            }, status=404)
        return JsonResponse({
            "ok": True, "branch": {"id": branch.pk, "name": branch.name},
            "shifts": company_services.schedule_for(branch),
        })


class BranchWorkingTimeSave(writes.WriteView):
    """The 7x4 matrix posts as a whole.

    A schedule is one thing, not 28 things: replacing the branch's rows in one
    transaction means a half-saved week can never exist. The rules are in
    apps/company/services.py::validate_schedule.
    """

    model = BranchWorkingTime
    page_code = "company.branch_working_time"

    def post(self, request, *args, **kwargs):
        if not self.may("update"):
            return self.refused()

        data = writes._payload(request)
        branch = scoping.branches_for(request.user).filter(pk=_int(data.get("branch"))).first()
        if branch is None:
            return _schedule_invalid([{"field": "Branch", "message": "Choose a branch first."}])

        rows, errors = company_services.validate_schedule(data.get("shifts", []))
        if errors:
            return _schedule_invalid(errors)

        with transaction.atomic():
            _replace_schedule(branch, rows, request.user)

        return JsonResponse({
            "ok": True, "message": f"{branch.name} schedule saved.",
            "shifts": company_services.schedule_for(branch),
        })


class BranchWorkingTimeAssign(writes.WriteView):
    """Copy one week onto many branches: "Assign to multiple branches".

    All or nothing. Every id must be one of the user's own branches, or
    nothing is written -- a partly assigned schedule would leave the user
    guessing which branches took it.
    """

    model = BranchWorkingTime
    page_code = "company.branch_working_time"

    def post(self, request, *args, **kwargs):
        if not self.may("update"):
            return self.refused()

        data = writes._payload(request)
        raw_ids = data.get("branches")
        ids = {_int(value) for value in raw_ids} if isinstance(raw_ids, list) else set()
        ids.discard(None)
        if not ids:
            return _schedule_invalid([{"field": "Branches", "message": "Pick at least one branch."}])

        branches = list(scoping.branches_for(request.user).filter(pk__in=ids))
        if len(branches) != len(ids):
            return _schedule_invalid([{
                "field": "Branches", "message": "One or more branches were not found.",
            }])

        rows, errors = company_services.validate_schedule(data.get("shifts", []))
        if errors:
            return _schedule_invalid(errors)

        with transaction.atomic():
            for branch in branches:
                _replace_schedule(branch, rows, request.user)

        count = len(branches)
        return JsonResponse({
            "ok": True,
            "message": f"Schedule assigned to {count} branch{'es' if count != 1 else ''}.",
            "branches": sorted(branch.pk for branch in branches),
        })


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class CompanyPrivilegeView(PrivilegeScreenView):
    """Module 2's copy of the shared privilege grid."""

    page_code = "company.privileges"
    module_code = "company"
    module_label = "Company Management"
