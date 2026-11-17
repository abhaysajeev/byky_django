"""Incentive screens (design/09-incentives.md section 7): Calculate (station x
month), a run, By Employee, Schemes and the scheme form. Every figure comes
from apps/incentives/services.py; these views only scope, draw and write."""

import datetime
import io
import json

from django.db import transaction
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.views import View

from apps.company.models import BranchType, State
from apps.company.scoping import branches_for, companies_for
from apps.incentives import formula, services
from apps.incentives.models import AppliesTo, Run, Scheme, SchemeComponent
from apps.incentives.variables import CATALOGUE, MONTH_NAMES, as_list
from apps.portal.permissions import PagePermissionMixin
from apps.portal.screens import PrivilegeScreenView
from apps.portal.services import has_permission
from apps.targets import services as targets
from theme.views import ThemedTemplateView

CALCULATE, EMPLOYEES, SCHEMES = "incentives.calculate", "incentives.employees", "incentives.schemes"
ROLE_LABEL = {"primary": "Primary cashier", "secondary": "Secondary cashier", "labour": "Labour"}


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def scoped_branches(user):
    return (branches_for(user).filter(company__in=companies_for(user), branch_type=BranchType.STATION)
            .select_related("company", "location__state"))


def scoped_runs(user):
    return Run.objects.filter(company__in=companies_for(user), branch__in=branches_for(user))


def scoped_schemes(user):
    return Scheme.objects.filter(company__in=companies_for(user))


def _month(value, today):
    try:
        year, month = (int(part) for part in str(value or "").split("-"))
        return datetime.date(year, month, 1)
    except ValueError:
        return today.replace(day=1)


def _shift(first, by):
    index = first.year * 12 + (first.month - 1) + by
    return datetime.date(index // 12, index % 12 + 1, 1)


class IncentivesScreen(PagePermissionMixin, ThemedTemplateView):
    """Shared: the company, today's business day, the month and state filter."""

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        self.company = companies_for(user).order_by("name").first()
        if self.company is None:
            raise Http404("No company.")
        today = targets.today_for(self.company)
        first = _month(self.request.GET.get("month"), today)
        chosen_state = _int(self.request.GET.get("state"))
        branches = scoped_branches(user).filter(is_active=True)
        state_q = f"&state={chosen_state}" if chosen_state else ""
        here = self.request.path
        context.update({
            "business_today": today, "first": first, "month_key": f"{first:%Y-%m}",
            "is_current": first == today.replace(day=1),
            "prev_url": f"{here}?month={_shift(first, -1):%Y-%m}{state_q}",
            "next_url": f"{here}?month={_shift(first, 1):%Y-%m}{state_q}",
            "this_url": f"{here}{'?state=' + str(chosen_state) if chosen_state else ''}",
            "states": State.objects.filter(pk__in=branches.values("location__state_id")).order_by("name"),
            "chosen_state": chosen_state,
            "can": {a: has_permission(user, self.page_code, a)
                    for a in ("create", "read", "update", "delete", "approve", "print")},
        })
        return context

    def branches(self, context):
        rows = scoped_branches(self.request.user).filter(is_active=True)
        if context["chosen_state"]:
            rows = rows.filter(location__state_id=context["chosen_state"])
        return rows.order_by("name")


# -- Calculate --------------------------------------------------------------------------------

class CalculateView(IncentivesScreen):
    template_name = "incentives/calculate.html"
    page_code = CALCULATE

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        first = context["first"]
        rows = [services.eligibility(b, first) for b in self.branches(context)]
        rows = [r for r in rows if r["target"] or r["run"]]
        for r in rows:
            r["status"] = _target_status(r)
            r["run_url"] = reverse("incentives-run", args=[r["run"].pk]) if r["run"] else ""
        approved = sum((r["run"].total for r in rows if r["run"] and r["run"].is_approved), services.ZERO)
        draft = sum((r["run"].total for r in rows if r["run"] and not r["run"].is_approved), services.ZERO)
        context.update({
            "rows": rows, "calculate_url": reverse("incentives-calculate-post"),
            "totals": {"met": sum(1 for r in rows if r["met"]), "stations": len(rows), "approved": approved,
                       "draft": draft, "runs": sum(1 for r in rows if r["run"])},
            "ended": context["business_today"] > targets.month_bounds(first.year, first.month)[1],
        })
        return context


def _target_status(row):
    if row["target"] is None:
        return {"key": "idle", "label": "No target"}
    if row["met"]:
        return {"key": "met", "label": "Met"}
    if row["ended"]:
        return {"key": "missed", "label": "Not met"}
    return {"key": "live", "label": "In progress"}


class RunView(IncentivesScreen):
    template_name = "incentives/run.html"
    page_code = CALCULATE

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        run = get_object_or_404(scoped_runs(self.request.user).select_related("branch", "scheme", "calculated_by",
                                                                           "approved_by"), pk=self.kwargs["pk"])
        by = "employee" if self.request.GET.get("view") == "employee" else "day"
        days = services.run_by_day(run) if by == "day" else []
        employees = services.run_by_employee(run) if by == "employee" else []
        for day in days:
            for p in day["people"]:
                p["role_label"] = ROLE_LABEL.get(p["role"], p["role"])
        for e in employees:
            e["roles_text"] = ", ".join(f"{ROLE_LABEL.get(k, k)} {v}d" for k, v in e["roles"].items())
        context.update({
            "run": run, "by": by, "days": days, "employees": employees,
            "day_url": reverse("incentives-run", args=[run.pk]), "people_count": run.lines.values("employee").distinct().count(),
            "employee_url": reverse("incentives-run", args=[run.pk]) + "?view=employee",
            "back_url": reverse("incentives-calculate") + f"?month={run.period_start:%Y-%m}",
            "calculate_url": reverse("incentives-calculate-post"),
            "approve_url": reverse("incentives-approve", args=[run.pk]),
            "export_url": reverse("incentives-run-export", args=[run.pk]),
        })
        return context


# -- By Employee --------------------------------------------------------------------------------

class EmployeesView(IncentivesScreen):
    template_name = "incentives/employees.html"
    page_code = EMPLOYEES

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        rows = services.employee_totals(self.company, list(self.branches(context)), context["first"])
        context.update({
            "rows": rows, "export_url": reverse("incentives-employees-export") + f"?month={context['month_key']}"
            + (f"&state={context['chosen_state']}" if context["chosen_state"] else ""),
            "totals": {"approved": sum((r["approved"] for r in rows), services.ZERO),
                       "draft": sum((r["draft"] for r in rows), services.ZERO), "people": len(rows)},
        })
        return context


# -- Schemes ------------------------------------------------------------------------------------

class SchemeListView(IncentivesScreen):
    template_name = "incentives/schemes.html"
    page_code = SCHEMES

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        schemes = list(scoped_schemes(self.request.user).prefetch_related("branches", "components"))
        context.update({"schemes": [{"scheme": s, "used": s.is_used, "stations": list(s.branches.all())}
                                    for s in schemes],
                        "copy_url": reverse("incentives-scheme-copy", args=[0]),
                        "delete_url": reverse("incentives-scheme-delete", args=[0])})
        return context


class SchemeFormView(IncentivesScreen):
    template_name = "incentives/scheme_form.html"
    page_code = SCHEMES

    def dispatch(self, request, *args, **kwargs):
        self.required_action = "update" if kwargs.get("pk") else "create"
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        scheme = None
        if self.kwargs.get("pk"):
            scheme = get_object_or_404(scoped_schemes(self.request.user).prefetch_related(
                "branches", "variables", "components"), pk=self.kwargs["pk"])
        data = {
            "pk": scheme.pk if scheme else None,
            "name": scheme.name if scheme else "",
            "effective_from": f"{scheme.effective_from:%Y-%m-%d}" if scheme else
            f"{context['business_today'].replace(day=1):%Y-%m-%d}",
            "effective_to": f"{scheme.effective_to:%Y-%m-%d}" if scheme and scheme.effective_to else "",
            "branches": [b.pk for b in scheme.branches.all()] if scheme else [],
            "pays_when": scheme.pays_when if scheme else "achieved >= target",
            "is_active": scheme.is_active if scheme else False,
            "notes": scheme.notes if scheme else "",
            "variables": [{"name": v.name, "formula": v.formula} for v in scheme.variables.all()] if scheme else [],
            "components": [{"name": c.name, "applies_to": c.applies_to, "formula": c.formula}
                           for c in scheme.components.all()] if scheme else
            [{"name": "", "applies_to": AppliesTo.PRIMARY, "formula": ""}],
        }
        context.update({
            "scheme": scheme, "locked": bool(scheme and scheme.is_used), "data": data,
            "catalogue": as_list(), "month_names": MONTH_NAMES,
            "applies_to": [{"value": v, "label": label} for v, label in AppliesTo.choices],
            "branches_list": [{"id": b.pk, "name": b.name} for b in self.branches({"chosen_state": None})],
            "save_url": reverse("incentives-scheme-save"), "check_url": reverse("incentives-formula-check"),
            "try_url": reverse("incentives-scheme-try"), "back_url": reverse("incentives-schemes"),
        })
        return context


class IncentivePrivilegeView(PrivilegeScreenView):
    page_code = "incentives.privileges"
    module_code = "incentives"
    module_label = "Incentives"


# -- Writes ---------------------------------------------------------------------------------------

def _payload(request):
    try:
        return json.loads(request.body or b"{}")
    except ValueError:
        return {}


def _refused():
    return JsonResponse({"ok": False, "code": "forbidden",
                         "errors": [{"field": "", "message": "You do not have permission to do that."}]}, status=403)


def _invalid(errors, status=400):
    return JsonResponse({"ok": False, "code": "invalid", "errors": errors}, status=status)


class CalculatePost(View):
    """POST {branch, month}: calculate (or again) that station's month."""

    def post(self, request):
        if not has_permission(request.user, CALCULATE, "create"):
            return _refused()
        data = _payload(request)
        branch = get_object_or_404(scoped_branches(request.user), pk=_int(data.get("branch")))
        first = _month(data.get("month"), targets.today_for(branch.company))
        try:
            run = services.calculate(branch, first, request.user)
        except services.IncentiveError as error:
            return _invalid([{"field": error.field, "message": error.message}])
        return JsonResponse({"ok": True, "url": reverse("incentives-run", args=[run.pk]),
                             "message": f"{branch.name}, {first:%B %Y} calculated."})


class ApprovePost(View):
    def post(self, request, pk):
        if not has_permission(request.user, CALCULATE, "approve"):
            return _refused()
        run = get_object_or_404(scoped_runs(request.user).select_related("branch"), pk=pk)
        try:
            services.approve(run, request.user)
        except services.IncentiveError as error:
            return _invalid([{"field": error.field, "message": error.message}])
        return JsonResponse({"ok": True, "message": f"{run.branch.name}, {run.period_start:%B %Y} approved."})


def _date(value, field, errors, required=True):
    if not value and not required:
        return None
    try:
        return datetime.date.fromisoformat(str(value))
    except ValueError:
        errors.append({"field": field, "message": "Choose a date."})
        return None


def _rows(data):
    variables = [(str(v.get("name") or "").strip(), str(v.get("formula") or "").strip())
                 for v in data.get("variables") or [] if (v.get("name") or v.get("formula"))]
    components = [(str(c.get("name") or "").strip(), str(c.get("applies_to") or ""), str(c.get("formula") or "").strip())
                  for c in data.get("components") or [] if (c.get("name") or c.get("formula"))]
    return variables, components


class SchemeSave(View):
    """POST the scheme form: header, variables, components. All checked at once."""

    def post(self, request):
        data = _payload(request)
        pk = _int(data.get("pk"))
        if not has_permission(request.user, SCHEMES, "update" if pk else "create"):
            return _refused()
        scheme = get_object_or_404(scoped_schemes(request.user), pk=pk) if pk else None
        if scheme is not None and scheme.is_used:
            return _invalid([{"field": "Scheme", "message": "An approved month uses this scheme; copy it to change it."}])
        errors = []
        name = str(data.get("name") or "").strip()[:100]
        if not name:
            errors.append({"field": "Name", "message": "Give the scheme a name."})
        start = _date(data.get("effective_from"), "Effective from", errors)
        end = _date(data.get("effective_to"), "Effective to", errors, required=False)
        if start and end and end < start:
            errors.append({"field": "Effective to", "message": "Must be on or after Effective from."})
        pays_when = str(data.get("pays_when") or "").strip()
        variables, components = _rows(data)
        errors += services.validate(pays_when, variables, components)
        company = scheme.company if scheme else companies_for(request.user).order_by("name").first()
        stations = list(scoped_branches(request.user).filter(company=company,
                                                             pk__in=[_int(b) for b in data.get("branches") or []]))
        if errors:
            return _invalid(errors)
        with transaction.atomic():
            scheme = scheme or Scheme(company=company, created_by=request.user)
            scheme.name, scheme.effective_from, scheme.effective_to = name, start, end
            scheme.pays_when, scheme.is_active = pays_when, bool(data.get("is_active"))
            scheme.notes, scheme.modified_by = str(data.get("notes") or "")[:300], request.user
            scheme.save()
            scheme.branches.set(stations)
            scheme.variables.all().delete()
            scheme.components.all().delete()
            for index, (vname, text) in enumerate(variables):
                scheme.variables.create(position=index, name=vname, formula=text)
            for index, (cname, applies, text) in enumerate(components):
                scheme.components.create(position=index, name=cname, applies_to=applies, formula=text)
            if scheme.is_active:
                clash = (Scheme.objects.filter(company=company, is_active=True).exclude(pk=scheme.pk)
                         .filter(effective_from__lte=end or datetime.date.max)
                         .exclude(effective_to__lt=start))
                mine = {b.pk for b in stations}
                for other in clash.prefetch_related("branches"):
                    theirs = {b.pk for b in other.branches.all()}
                    if mine == theirs or (not mine and not theirs):
                        transaction.set_rollback(True)
                        return _invalid([{"field": "Active", "message":
                                          f"“{other.name}” is already active for the same stations and dates. "
                                          "Switch one off or change its dates."}])
        return JsonResponse({"ok": True, "message": f"Scheme “{scheme.name}” saved.",
                             "url": reverse("incentives-schemes")})


class SchemeCopy(View):
    def post(self, request, pk):
        if not has_permission(request.user, SCHEMES, "create"):
            return _refused()
        twin = services.copy_scheme(get_object_or_404(scoped_schemes(request.user), pk=pk), request.user)
        return JsonResponse({"ok": True, "message": f"Copied as “{twin.name}” (inactive).",
                             "url": reverse("incentives-scheme-edit", args=[twin.pk])})


class SchemeDelete(View):
    def post(self, request, pk):
        if not has_permission(request.user, SCHEMES, "delete"):
            return _refused()
        scheme = get_object_or_404(scoped_schemes(request.user), pk=pk)
        if scheme.runs.exists():
            return _invalid([{"field": scheme.name, "message": "Months were calculated with it; switch it off instead."}])
        name = scheme.name
        scheme.delete()
        return JsonResponse({"ok": True, "message": f"Scheme “{name}” deleted."})


class FormulaCheck(View):
    """POST {formula, scope: "pays_when"|"any", variables: [names]}: the first
    problem, for the form's as-you-type check."""

    def post(self, request):
        if not has_permission(request.user, SCHEMES, "read"):
            return _refused()
        data = _payload(request)
        names = MONTH_NAMES if data.get("scope") == "pays_when" else \
            list(CATALOGUE) + [str(n) for n in data.get("variables") or []]
        try:
            formula.check(str(data.get("formula") or ""), names)
        except formula.FormulaError as error:
            return JsonResponse({"ok": False, "message": str(error)})
        return JsonResponse({"ok": True})


class SchemeTry(View):
    """POST the form as it stands plus {branch, date}: each rostered person's
    amount that day, before anything is saved."""

    def post(self, request):
        if not has_permission(request.user, SCHEMES, "read"):
            return _refused()
        data = _payload(request)
        branch = scoped_branches(request.user).filter(pk=_int(data.get("branch"))).first()
        errors = []
        day = _date(data.get("date"), "Day", errors)
        if branch is None:
            errors.append({"field": "Station", "message": "Choose a station."})
        variables, components = _rows(data)
        errors += services.validate(str(data.get("pays_when") or ""), variables, components)
        if errors:
            return _invalid(errors)
        rows = [SchemeComponent(name=n, applies_to=a, formula=f) for n, a, f in components]
        try:
            view = services.try_parts(str(data.get("pays_when")), services.compile_parts(variables, rows), branch, day)
        except (services.IncentiveError, formula.FormulaError) as error:
            return _invalid([{"field": getattr(error, "field", "Formula"), "message": getattr(error, "message",
                                                                                               str(error))}])
        month = view["month"]
        return JsonResponse({"ok": True, "date": f"{day:%a %d %b %Y}", "pays_now": view["pays_now"],
                             "has_target": view["target"] is not None, "has_roster": view["has_roster"],
                             "values": {k: _plain(v) for k, v in {**month, **view["counts"]}.items()},
                             "people": [{"name": p["employee"].full_name, "role": ROLE_LABEL.get(p["role"], p["role"]),
                                         "components": [[n, _plain(a)] for n, a in p["components"]],
                                         "amount": _plain(p["amount"])} for p in view["people"]]})


def _plain(value):
    return format(value.quantize(services.Decimal("0.01")).normalize(), "f") if isinstance(value, services.Decimal) \
        else value


# -- Exports ------------------------------------------------------------------------------------

def _xlsx(name, header, rows):
    from openpyxl import Workbook
    from openpyxl.styles import Font

    book = Workbook()
    sheet = book.active
    sheet.title = name[:31]
    sheet.append(header)
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    for row in rows:
        sheet.append(row)
    for column in sheet.columns:
        sheet.column_dimensions[column[0].column_letter].width = max(12, min(40, max(len(str(c.value or ""))
                                                                                     for c in column) + 2))
    out = io.BytesIO()
    book.save(out)
    response = HttpResponse(out.getvalue(),
                            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response["Content-Disposition"] = f'attachment; filename="{name}.xlsx"'
    return response


class RunExport(PagePermissionMixin, View):
    page_code = CALCULATE
    required_action = "print"

    def get(self, request, pk):
        run = get_object_or_404(scoped_runs(request.user).select_related("branch"), pk=pk)
        rows = [(line.date, line.employee.employee_code, line.employee.full_name,
                 ROLE_LABEL.get(line.role, line.role), line.component, float(line.amount))
                for line in run.lines.select_related("employee").order_by("date", "employee__first_name")]
        name = f"Incentive {run.branch.name} {run.period_start:%Y-%m}{'' if run.is_approved else ' DRAFT'}"
        return _xlsx(name, ["Date", "Employee code", "Employee", "Role", "Component", "Amount"], rows)


class EmployeesExport(PagePermissionMixin, View):
    page_code = EMPLOYEES
    required_action = "print"

    def get(self, request):
        company = companies_for(request.user).order_by("name").first()
        first = _month(request.GET.get("month"), targets.today_for(company))
        branches = scoped_branches(request.user).filter(is_active=True)
        if _int(request.GET.get("state")):
            branches = branches.filter(location__state_id=_int(request.GET.get("state")))
        rows = []
        for r in services.employee_totals(company, list(branches), first):
            for s in r["stations"]:
                rows.append((r["employee"].employee_code, r["employee"].full_name, s["branch"].name, s["days"],
                             "Approved" if s["run"].is_approved else "Draft", float(s["amount"])))
        return _xlsx(f"Incentives by employee {first:%Y-%m}",
                     ["Employee code", "Employee", "Station", "Days", "Status", "Amount"], rows)


