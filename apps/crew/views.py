"""Crew Management screens.

Markup ported from the wireframe's byky_hrms app, except Attendance, which the
wireframe never had -- that one is composed from the same .scr-* parts
(DESIGN.md, "Read-only monitor screens").

Every figure comes from our own tables. Nothing is seeded, and nothing is
derived from a hash to make a filter look busy.
"""

import datetime

from django.db.models import Count, Q
from django.urls import reverse
from django.utils import timezone

from apps.company import writes as company_writes
from apps.company.models import Country, State
from apps.company.scoping import branches_for, companies_for
from apps.company.views import ACTIONS
from apps.crew import drawers, forms, scoping, services
from apps.crew.models import (
    AddressType,
    AttendanceMethod,
    AttendanceSource,
    BlockAction,
    Designation,
    DutyRosterDayType,
    Employee,
    EmployeeAddress,
    RosterCategory,
)
from apps.portal.permissions import PagePermissionMixin
from apps.portal.screens import PrivilegeScreenView
from apps.portal.services import has_permission
from theme import drawers as theme_drawers
from theme.views import ThemedTemplateView

BLOCK_REASONS = [
    "Disciplinary", "Absconding", "Document Expiry",
    "Resignation Pending", "Investigation", "Other",
]


class CrewScreenView(PagePermissionMixin, ThemedTemplateView):
    """Shared by every crew screen: the lists its drawers resolve against, and
    the permission flags its buttons check."""

    drawer_specs = drawers.SPECS

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        context.update({
            # A system user picks the company; a company user never sees the
            # field (theme/drawers.for_user). Missing here, the select rendered
            # empty and the save could not be completed by anyone.
            "companies_list": list(
                companies_for(user).filter(is_active=True).values("id", "name")
            ),
            "designations_list": list(
                scoping.designations_for(user).filter(is_active=True).values("id", "title")
            ),
            "employees_list": [
                {"id": e.pk, "name": f"{e.employee_code} — {e.full_name}"}
                for e in scoping.employees_for(user).filter(is_active=True)
            ],
            "branches_list": list(branches_for(user).filter(is_active=True).values("id", "name")),
            # Geography, not company data: someone can live in an emirate their
            # employer has no branch in. Scoping these to where the company
            # operates -- right for the Country & State screen -- would make
            # that address impossible to record.
            "states_list": list(State.objects.filter(is_active=True).values("id", "name")),
            "countries_list": list(Country.objects.filter(is_active=True).values("id", "name")),
            "address_types_list": [
                {"id": value, "name": label} for value, label in AddressType.choices
            ],
            "attendance_methods_list": [
                {"id": value, "name": label} for value, label in AttendanceMethod.choices
            ],
            "roster_categories_list": [
                {"id": value, "name": label} for value, label in RosterCategory.choices
            ],
            "perm": {
                action: has_permission(user, self.page_code, action) for action in ACTIONS
            },
        })
        return context

    def render_to_response(self, context, **response_kwargs):
        specs = theme_drawers.all_for_user(
            self.drawer_specs, self.request.user.sees_every_company
        )
        context.update(theme_drawers.resolve_all(specs, context))
        return super().render_to_response(context, **response_kwargs)


class DesignationListView(CrewScreenView):
    template_name = "crew/designation_list.html"
    page_code = "crew.designation"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        rows = []
        designations = scoping.designations_for(self.request.user).annotate(
            headcount=Count("employees", filter=Q(employees__is_active=True))
        )
        for i, designation in enumerate(designations):
            rows.append({
                "pk": designation.pk,
                "code": designation.code,
                "title": designation.title,
                "description": designation.description,
                "rank": designation.rank_order,
                "headcount": designation.headcount,
                "active": designation.is_active,
                "json_id": f"scr-record-designation-{i}",
                "fields_json": {
                    "pk": designation.pk,
                    "code": designation.code,
                    "title": designation.title,
                    "description": designation.description,
                    "rank": designation.rank_order,
                    "attendance_method": designation.attendance_method,
                    "roster_category": designation.roster_category,
                    "company": designation.company_id,
                    "is_active": designation.is_active,
                },
            })
        context.update({
            "designations": rows,
            "active_count": sum(1 for row in rows if row["active"]),
            "staff_count": scoping.employees_for(self.request.user).filter(is_active=True).count(),
            "save_url_designation": reverse("crew-designation-save"),
            "delete_url_designation": reverse("crew-designation-delete", args=[0]),
            "noun_designation": "Designation",
        })
        return context


class EmployeeListView(CrewScreenView):
    template_name = "crew/employee_list.html"
    page_code = "crew.employee"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        today = timezone.localdate()

        employees = (
            scoping.employees_for(self.request.user)
            .select_related("designation", "branch", "detail")
        )

        rows = []
        for i, employee in enumerate(employees):
            detail = getattr(employee, "detail", None)
            rows.append({
                "pk": employee.pk,
                "emp_no": employee.employee_code,
                "name": employee.full_name,
                "designation": employee.designation.title,
                "branch": employee.branch.name if employee.branch_id else "",
                "status": "Blocked" if employee.is_blocked else "Active",
                "active": employee.is_active,
                # Per-row flags, from the real expiry dates, so the document
                # chips filter the grid the way the wireframe's hashed ones did.
                "visa_attention": _attention(detail, "visa_expiry", today),
                "eid_attention": _attention(detail, "emirates_id_expiry", today),
                "labor_attention": _attention(detail, "labour_card_expiry", today),
                "passport_attention": _attention(detail, "passport_expiry", today),
                "json_id": f"scr-record-employee-{i}",
                "fields_json": self._fields_json(employee, detail),
            })

        context.update({
            "employees": rows,
            "doc_expiry": services.document_expiry(employees, today),
            "active_count": sum(1 for row in rows if row["status"] == "Active"),
            "blocked_count": sum(1 for row in rows if row["status"] == "Blocked"),
            "designation_count": scoping.designations_for(self.request.user).count(),
            "save_url_employee": reverse("crew-employee-save"),
            "delete_url_employee": reverse("crew-employee-delete", args=[0]),
            "noun_employee": "Employee",
        })
        return context

    @staticmethod
    def _fields_json(employee, detail):
        return {
            "pk": employee.pk,
            "emp_no": employee.employee_code,
            "first_name": employee.first_name,
            "middle_name": employee.middle_name,
            "last_name": employee.last_name,
            "designation": employee.designation_id,
            "branch": employee.branch_id or "",
            "attendance_method": employee.attendance_method,
            "dob": employee.date_of_birth.isoformat() if employee.date_of_birth else "",
            "gender": employee.gender,
            "marital_status": employee.marital_status,
            "nationality": employee.nationality,
            "mobile": employee.mobile,
            "email": employee.email,
            "emergency_person": employee.emergency_contact_person,
            "emergency_phone": employee.emergency_phone,
            "company": employee.company_id,
            "is_active": employee.is_active,
            "passport_no": getattr(detail, "passport_number", "") or "",
            "passport_expiry": _date(getattr(detail, "passport_expiry", None)),
            "visa_no": getattr(detail, "visa_number", "") or "",
            "visa_expiry": _date(getattr(detail, "visa_expiry", None)),
            "eid_no": getattr(detail, "emirates_id_number", "") or "",
            "eid_expiry": _date(getattr(detail, "emirates_id_expiry", None)),
            "labor_no": getattr(detail, "labour_card_number", "") or "",
            "labor_expiry": _date(getattr(detail, "labour_card_expiry", None)),
            "salary_bank": getattr(detail, "salary_bank", "") or "",
            "salary_bank_account": getattr(detail, "salary_account", "") or "",
        }


class EmployeeAddressListView(CrewScreenView):
    template_name = "crew/employee_address_list.html"
    page_code = "crew.employee_address"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        rows = []
        addresses = (
            scoping.addresses_for(self.request.user)
            .select_related("employee", "state", "country")
        )
        for i, address in enumerate(addresses):
            rows.append({
                "pk": address.pk,
                "employee": address.employee.full_name,
                "emp_no": address.employee.employee_code,
                "type": address.get_address_type_display(),
                "line1": address.line1,
                "city": address.city,
                "state": address.state.name if address.state_id else "",
                "current": address.is_current,
                "json_id": f"scr-record-address-{i}",
                "fields_json": {
                    "pk": address.pk,
                    "employee": address.employee_id,
                    "address_type": address.address_type,
                    "line1": address.line1,
                    "line2": address.line2,
                    "building": address.building,
                    "flat": address.flat,
                    "city": address.city,
                    "state": address.state_id or "",
                    "country": address.country_id or "",
                    "zip_code": address.zip_code,
                    "landlord_name": address.landlord_name,
                    "landlord_phone": address.landlord_phone,
                    "contact_person": address.contact_person,
                    "contact_phone": address.contact_phone,
                    "is_active": address.is_current,
                },
            })
        context.update({
            "addresses": rows,
            "save_url_address": reverse("crew-address-save"),
            "delete_url_address": reverse("crew-address-delete", args=[0]),
            "noun_address": "Address",
        })
        return context


class BlockUnblockView(CrewScreenView):
    template_name = "crew/block_unblock.html"
    page_code = "crew.block_unblock"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        log = (
            scoping.block_log_for(self.request.user)
            .select_related("employee")[:100]
        )
        context.update({
            "reasons": BLOCK_REASONS,
            "block_log": [
                {
                    "employee": row.employee.full_name,
                    "emp_no": row.employee.employee_code,
                    "action": row.get_action_display(),
                    "is_block": row.action == BlockAction.BLOCK,
                    "reason": row.reason,
                    "effective_date": row.effective_date,
                    "remarks": row.remarks,
                }
                for row in log
            ],
            "blocked_count": scoping.employees_for(self.request.user).filter(is_blocked=True).count(),
            "block_url": reverse("crew-employee-block"),
        })
        return context


class AttendanceListView(CrewScreenView):
    """A read-only monitor: no drawer, no Add, no delete.

    Punches are written by the apps. Until those exist this renders its headers
    and says so, which is the honest state rather than a hidden screen.
    """

    template_name = "crew/attendance_list.html"
    page_code = "crew.attendance"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        today = timezone.localdate()

        punches = (
            scoping.attendance_for(self.request.user)
            .select_related("employee", "branch", "scanned_by", "device")[:200]
        )

        rows = [
            {
                "employee": punch.employee.full_name,
                "emp_no": punch.employee.employee_code,
                "source": punch.get_source_display(),
                "punched_at": punch.punched_at,
                "business_date": punch.business_date,
                "branch": punch.branch.name if punch.branch_id else "",
                "scanned_by": punch.scanned_by.display_name if punch.scanned_by_id else "",
                "device": punch.device.name if punch.device_id else "",
            }
            for punch in punches
        ]

        todays = scoping.attendance_for(self.request.user).filter(business_date=today)
        context.update({
            "punches": rows,
            "today_count": todays.count(),
            "present_count": todays.values("employee").distinct().count(),
            "station_count": todays.exclude(branch=None).values("branch").distinct().count(),
            "self_count": todays.filter(source=AttendanceSource.SELF).count(),
            "sources_list": [{"id": v, "name": label} for v, label in AttendanceSource.choices],
            "today": today,
        })
        return context


def _attention(detail, field, today):
    """"yes" when a document has expired or is close to it.

    The wireframe hashed the employee code to fill these in; the filter
    mechanism is the same, the answer is now true.
    """
    return "yes" if services.expiry_state(getattr(detail, field, None), today) else ""


def _date(value):
    return value.isoformat() if isinstance(value, datetime.date) else ""


# --- writes ------------------------------------------------------------------
# Designation and Address use the generic views from the company module;
# Employee and Block/Unblock have their own (apps/crew/writes.py).

class DesignationSave(company_writes.EntitySaveView):
    model = Designation
    form_class = forms.DesignationForm
    field_map = {"rank": "rank_order", "title": "title"}
    page_code = "crew.designation"
    noun = "Designation"


class DesignationDelete(company_writes.EntityDeleteView):
    model = Designation
    page_code = "crew.designation"
    noun = "Designation"


class AddressSave(company_writes.EntitySaveView):
    model = EmployeeAddress
    form_class = forms.EmployeeAddressForm
    page_code = "crew.employee_address"
    noun = "Address"
    scoped = False          # scoped through its employee, in the form

    def rows(self):
        return scoping.addresses_for(self.request.user)


class AddressDelete(company_writes.EntityDeleteView):
    model = EmployeeAddress
    page_code = "crew.employee_address"
    noun = "Address"
    scoped = False

    def rows(self):
        return scoping.addresses_for(self.request.user)


class EmployeeDelete(company_writes.EntityDeleteView):
    model = Employee
    page_code = "crew.employee"
    noun = "Employee"

    def rows(self):
        return scoping.employees_for(self.request.user)


class CrewPrivilegeView(PrivilegeScreenView):
    """Module 3's copy of the shared privilege grid."""

    page_code = "crew.privileges"
    module_code = "crew"
    module_label = "Human Resources"


# --- Duty Roster ----------------------------------------------------------------
#
# design/duty roster/duty-roster.md. Built on the client's own partially
# designed wireframe (byky-main/.../apps/byky_hrms/{templates,drawers.py,
# .../byky-hrms-duty-roster.js}), re-wired to real data: everywhere that
# project computed a deterministic hash to fill every cell, this reads real
# DutyRoster rows and leaves an unset day blank rather than inventing a status
# for it. No "State" filter -- employees carry no state of their own (a
# deliberate simplification, see duty-roster.md), so By Branch lists every
# branch that has any roster-eligible staff scheduled this month, unfiltered.


class DutyRosterListView(CrewScreenView):
    """State Wise / Branch Roster / Employee View, one consolidated payload
    (roster_json) all three tabs and the Bulk Import section read client-side
    -- static/js/byky-duty-roster.js."""

    template_name = "crew/duty_roster_list.html"
    page_code = "crew.duty_roster"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        today = timezone.localdate()

        year = _int(self.request.GET.get("year")) or today.year
        month = _int(self.request.GET.get("month")) or today.month

        payload = services.roster_payload(user, year, month)
        counts = services.roster_counts(payload["employees"], payload["matrix"], payload["today"])

        for i, e in enumerate(payload["employees"]):
            e["json_id"] = f"scr-record-roster-emp-{i}"

        context.update({
            "roster_counts": counts,
            "roster_employees": payload["employees"],
            "roster_branches": payload["branches"],
            "roster_weeks": payload["weeks"],
            "roster_json": payload,
            "roster_week_labels": [w["label"] for w in payload["weeks"]],
            "import_columns": [
                "Employee Code", "Employee Name", "Date", "Day Type", "Branch",
                "Shift1 Start", "Shift1 End", "Shift2 Start", "Shift2 End", "Remarks",
            ],
            "roster_day_types": [{"id": v, "name": label} for v, label in DutyRosterDayType.choices],
            "roster_urls": {
                "branch_shifts": reverse("crew-duty-roster-branch-shifts"),
                "save_week": reverse("crew-duty-roster-save-week"),
                "assign_day": reverse("crew-duty-roster-assign-day"),
                "remove_day": reverse("crew-duty-roster-remove-day"),
                "import": reverse("crew-duty-roster-import"),
            },
        })
        return context


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
