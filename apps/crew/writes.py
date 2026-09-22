"""Crew writes.

Designation and Address reuse the generic views from the Company module
unchanged. Two paths are specific to crew and live here: saving an employee
across four tables, and blocking someone.
"""

from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone

from apps.company.writes import WriteView, _errors, _payload, _translate
from apps.crew import services
from apps.crew.forms import EmployeeDetailForm, EmployeeForm
from apps.crew.models import Employee
from apps.crew.scoping import employees_for

# Drawer field id -> model field, as the Company module does it. The ids come
# from the wireframe's spec and are also used by its filters, so they are
# translated here rather than renamed there.
EMPLOYEE_FIELDS = {
    "emp_no": "employee_code",
    "dob": "date_of_birth",
    "emergency_person": "emergency_contact_person",
    "passport_no": "passport_number",
    "visa_no": "visa_number",
    "eid_no": "emirates_id_number",
    "eid_expiry": "emirates_id_expiry",
    "labor_no": "labour_card_number",
    "labor_expiry": "labour_card_expiry",
    "salary_bank_account": "salary_account",
}

DETAIL_FIELDS = {
    "passport_number", "passport_expiry", "visa_number", "visa_expiry",
    "emirates_id_number", "emirates_id_expiry", "labour_card_number",
    "labour_card_expiry", "salary_bank", "salary_account",
}


class EmployeeSaveView(WriteView):
    """One drawer, one Save, four tables.

    The employee row, their documents and their current posting are written
    together or not at all -- the split the team lead asked for is a storage
    decision, and nobody entering data should have to know about it.
    """

    model = Employee
    page_code = "crew.employee"
    noun = "Employee"

    def rows(self):
        return employees_for(self.request.user)

    def post(self, request, *args, **kwargs):
        data = _translate(_payload(request), EMPLOYEE_FIELDS)
        pk = data.get("pk") or None

        if not self.may("update" if pk else "create"):
            return self.refused()

        employee = get_object_or_404(self.rows(), pk=pk) if pk else None
        detail = getattr(employee, "detail", None) if employee else None

        form = EmployeeForm(data, instance=employee, user=request.user)
        # An empty date posts as "", which a DateField rejects; blank means
        # "not captured", which is the normal state of these documents.
        detail_data = {key: (data.get(key) or None) for key in DETAIL_FIELDS}
        detail_form = EmployeeDetailForm(detail_data, instance=detail)

        if not form.is_valid() or not detail_form.is_valid():
            return JsonResponse(
                {"ok": False, "code": "invalid",
                 "errors": _errors(form) + _errors(detail_form)},
                status=400,
            )

        with transaction.atomic():
            saved = form.save()

            document_row = detail_form.save(commit=False)
            document_row.employee = saved
            if detail is not None:
                document_row.pk = detail.pk
            document_row.save()

            # Opens the first posting on create, and on edit only when the
            # designation actually changed.
            services.set_designation(
                saved, form.cleaned_data["designation"], by=request.user
            )

        return JsonResponse({"ok": True, "pk": saved.pk, "message": "Employee saved."})


class EmployeeBlockView(WriteView):
    """Block or unblock, with the reason, and end their sessions."""

    model = Employee
    page_code = "crew.block_unblock"

    def rows(self):
        return employees_for(self.request.user)

    def post(self, request, *args, **kwargs):
        if not self.may("update"):
            return self.refused()

        data = _payload(request)
        employee = self.rows().filter(pk=data.get("employee")).first()
        if employee is None:
            return JsonResponse({
                "ok": False, "code": "invalid",
                "errors": [{"field": "Employee", "message": "Choose an employee first."}],
            }, status=400)

        blocking = str(data.get("action", "")).lower() == "block"
        if blocking and not data.get("reason"):
            return JsonResponse({
                "ok": False, "code": "invalid",
                "errors": [{"field": "Reason", "message": "Say why this person is being blocked."}],
            }, status=400)

        effective = data.get("effective_date") or None
        log = services.set_blocked(
            employee,
            blocked=blocking,
            reason=data.get("reason", ""),
            effective_date=effective or timezone.localdate(),
            remarks=data.get("remarks", ""),
            by=request.user,
        )

        return JsonResponse({
            "ok": True,
            "message": f"{employee.full_name} {'blocked' if blocking else 'unblocked'}.",
            "blocked": employee.is_blocked,
            "logged_at": log.effective_date.isoformat(),
        })


# --- Duty Roster -------------------------------------------------------------


class DutyRosterBranchShiftsView(WriteView):
    """GET .../branch-shifts/?branch=&date= -- the prefill for one day card.
    A read, not a write, but it needs the same permission and company scope as
    the screen it serves, so it shares WriteView rather than being a bare view."""

    page_code = "crew.duty_roster"

    def get(self, request, *args, **kwargs):
        from apps.company.scoping import branches_for

        if not self.may("read"):
            return self.refused()

        branch = branches_for(request.user).filter(
            pk=request.GET.get("branch"), is_active=True
        ).first()
        on_date = _date(request.GET.get("date"))
        if branch is None or on_date is None:
            return JsonResponse({"ok": True, "shift1": None, "shift2": None})

        return JsonResponse({"ok": True, **services.branch_shift_defaults(branch, on_date)})


class DutyRosterSaveWeekView(WriteView):
    """POST one employee's week: {employee, days: [...]}. See
    services.save_week for the shape of `days`."""

    page_code = "crew.duty_roster"

    def rows(self):
        return employees_for(self.request.user)

    def post(self, request, *args, **kwargs):
        from apps.company.scoping import branches_for

        if not self.may("create"):
            return self.refused()

        data = _payload(request)
        employee = get_object_or_404(self.rows(), pk=data.get("employee"))

        try:
            written = services.save_week(
                employee, data.get("days") or [],
                user=request.user, branches_qs=branches_for(request.user),
            )
        except services.Invalid as refused:
            return JsonResponse({"ok": False, "code": "invalid", "errors": refused.errors}, status=400)

        return JsonResponse({
            "ok": True, "count": len(written),
            "message": f"{employee.full_name}'s week saved ({len(written)} days).",
        })


class DutyRosterAssignDayView(WriteView):
    """POST one employee onto one day: {employee, date, branch, shift1_start,
    shift1_end, shift2_start, shift2_end}. Backs both Add to Day (Branch
    Roster's +Cashier/+Labour) and Edit Day (the calendar's pencil)."""

    page_code = "crew.duty_roster"

    def rows(self):
        return employees_for(self.request.user)

    def post(self, request, *args, **kwargs):
        from apps.company.scoping import branches_for

        if not self.may("create"):
            return self.refused()

        data = _payload(request)
        employee = get_object_or_404(self.rows(), pk=data.get("employee"))

        try:
            row = services.assign_day(
                employee, data, branches_qs=branches_for(request.user), user=request.user,
                require_new=bool(data.get("require_new")),
            )
        except services.Invalid as refused:
            return JsonResponse({"ok": False, "code": "invalid", "errors": refused.errors}, status=400)

        # The branch's own zone, not the raw UTC the field is stored in --
        # services._hhmm, the same conversion roster_payload reads the
        # matrix through. Without it this echoed Dubai's 08:00 back as
        # "04:00" (services.zone_for).
        from core.timezones import zone_for
        zone = zone_for(row.branch.company) if row.branch_id else None
        return JsonResponse({
            "ok": True, "message": f"{employee.full_name} set for {row.date:%d %b}.",
            "day": {
                "status": row.get_day_type_display(),
                "branch": row.branch.name if row.branch_id else None,
                "shift1_start": services._hhmm(row.shift1_start, zone),
                "shift1_end": services._hhmm(row.shift1_end, zone),
                "shift2_start": services._hhmm(row.shift2_start, zone),
                "shift2_end": services._hhmm(row.shift2_end, zone),
            },
        })


class DutyRosterRemoveDayView(WriteView):
    """POST {employee, date} -- Branch Roster's chip ✕. Deletes that one day's
    row; the employee goes back to unset for it, not Week Off."""

    page_code = "crew.duty_roster"

    def rows(self):
        return employees_for(self.request.user)

    def post(self, request, *args, **kwargs):
        if not self.may("update"):
            return self.refused()

        data = _payload(request)
        employee = get_object_or_404(self.rows(), pk=data.get("employee"))
        on_date = _date(data.get("date"))
        if on_date is None:
            return JsonResponse({
                "ok": False, "code": "invalid",
                "errors": [{"field": "Date", "message": "Which day?"}],
            }, status=400)

        removed = services.remove_day(employee, on_date, user=request.user)
        return JsonResponse({
            "ok": True,
            "message": f"{employee.full_name} removed from {on_date:%d %b}." if removed else "Nothing to remove.",
        })


class DutyRosterImportView(WriteView):
    """POST {rows: [...]} -- Bulk Import's Apply. See services.import_rows
    for the row shape; every row is judged on its own."""

    page_code = "crew.duty_roster"

    def post(self, request, *args, **kwargs):
        from apps.company.scoping import branches_for

        if not self.may("create"):
            return self.refused()

        data = _payload(request)
        written, results = services.import_rows(
            data.get("rows") or [], user=request.user,
            employees_qs=employees_for(request.user), branches_qs=branches_for(request.user),
        )
        failed = sum(1 for r in results if not r["ok"])
        return JsonResponse({
            "ok": True, "written": written, "failed": failed, "results": results,
            "message": f"{written} row{'s' if written != 1 else ''} saved" + (f", {failed} failed" if failed else "") + ".",
        })


def _date(value):
    import datetime
    try:
        return datetime.date.fromisoformat(value)
    except (TypeError, ValueError):
        return None
