"""Crew Management screens.

Markup ported from the wireframe's byky_hrms app, except Attendance, which the
wireframe never had -- that one is composed from the same .scr-* parts
(DESIGN.md, "Read-only monitor screens").

Every figure comes from our own tables. Nothing is seeded, and nothing is
derived from a hash to make a filter look busy.
"""

import csv
import datetime

from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.db.models.functions import TruncDate
from django.http import Http404, StreamingHttpResponse
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
    DutyRoster,
    DutyRosterDayType,
    Employee,
    EmployeeAddress,
    PunchType,
    RosterCategory,
)
from apps.portal.permissions import PagePermissionMixin
from apps.portal.screens import PrivilegeScreenView
from apps.portal.services import has_permission
from core.ordering import recent_first
from core.timezones import zone_for
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
        designations = recent_first(scoping.designations_for(self.request.user)).annotate(
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
            recent_first(scoping.employees_for(self.request.user))
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
            recent_first(scoping.addresses_for(self.request.user))
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


# -- Attendance ------------------------------------------------------------
#
# Read-only: punches are written by the apps (apps/crew/api.py). One row per
# employee per day, opening onto that day's punch-in/punch-out pairs. A day is
# the company-local date of the punch-in; its punch-out may fall the next day.
# Filtered and paged by the server -- punches grow every day.

ATTENDANCE_PER_PAGE = 50

DAY_STATUSES = [
    ("complete", "Complete"),
    ("on_duty", "On duty"),
    ("missing", "Missing punch-out"),
]
_DAY_STATUS_FILTER = {
    "complete": Q(open_count=0, missing_count=0),
    "on_duty": Q(open_count__gt=0),
    "missing": Q(missing_count__gt=0),
}


def _date_param(value, default):
    try:
        return datetime.date.fromisoformat(value) if value else default
    except ValueError:
        return default


def _attendance_filters(request):
    """(punch-ins, zone, from_day, to_day) -- the user's own, narrowed by the
    query string. Shared by the list and the export.

      q            employee name or code
      from / to    company-local days (default: today)
      branch       a branch id -- the employee's or the scanning one
      source       qr_scan / self
      designation  a designation id
    """
    params = request.GET
    zone = zone_for(getattr(request.user, "company", None))
    today = timezone.now().astimezone(zone).date()
    from_day = _date_param(params.get("from"), today)
    to_day = _date_param(params.get("to"), today)
    if to_day < from_day:
        from_day, to_day = to_day, from_day

    start = datetime.datetime.combine(from_day, datetime.time.min, tzinfo=zone)
    end = datetime.datetime.combine(to_day + datetime.timedelta(days=1), datetime.time.min, tzinfo=zone)
    punch_ins = scoping.attendance_for(request.user).filter(
        punch_type=PunchType.PUNCH_IN, rms_scan_time__gte=start, rms_scan_time__lt=end,
    )
    text = params.get("q", "").strip()
    if text:
        punch_ins = punch_ins.filter(Q(employee_code__icontains=text) | Q(employee_name__icontains=text))
    if params.get("branch", "").isdigit():
        branch = int(params["branch"])
        punch_ins = punch_ins.filter(Q(employee_branch_id=branch) | Q(rms_branch_id=branch))
    if params.get("source") in AttendanceSource.values:
        punch_ins = punch_ins.filter(source=params["source"])
    if params.get("designation", "").isdigit():
        punch_ins = punch_ins.filter(employee__designation_id=int(params["designation"]))
    return punch_ins, zone, from_day, to_day


def _attendance_days(request, punch_ins, zone):
    """One row per (employee, day), newest day first, with the counts its
    status comes from -- narrowed by `status` in the query string."""
    stale = timezone.now() - services.OPEN_PUNCH_WINDOW
    days = (
        punch_ins
        .annotate(day=TruncDate("rms_scan_time", tzinfo=zone))
        .values("employee", "day")
        .annotate(
            shifts=Count("pk"),
            open_count=Count("pk", filter=Q(punch_out__isnull=True, rms_scan_time__gt=stale)),
            missing_count=Count("pk", filter=Q(punch_out__isnull=True, rms_scan_time__lte=stale)),
        )
        .order_by("-day", "employee__employee_code")
    )
    status = request.GET.get("status")
    if status in _DAY_STATUS_FILTER:
        days = days.filter(_DAY_STATUS_FILTER[status])
    return days


def _day_status(day):
    if day["missing_count"]:
        return "missing", "Missing punch-out"
    if day["open_count"]:
        return "on_duty", "On duty"
    return "complete", "Complete"


def _duration(minutes):
    if minutes is None:
        return ""
    hours, mins = divmod(minutes, 60)
    return f"{hours}h {mins:02d}m" if hours else f"{mins}m"


def _sessions_by_day(punch_ins, keys, zone):
    """{(employee_id, day): [session, ...]} for the given day rows only."""
    employees = {employee for employee, _ in keys}
    rows = (
        punch_ins.filter(employee_id__in=employees)
        .select_related(
            "employee_branch", "rms_branch", "rms_employee",
            "punch_out", "punch_out__employee_branch", "punch_out__rms_branch",
        )
        .order_by("rms_scan_time")
    )
    grouped = {}
    for session in services.attendance_sessions(rows):
        key = (session[0].employee_id, session[0].rms_scan_time.astimezone(zone).date())
        if key in keys:
            grouped.setdefault(key, []).append(session)
    return grouped


def _gps(lat, lng):
    return f"{lat}, {lng}" if lat is not None else ""


def _punch_detail(punch, zone, day):
    """The rows one punch shows on the detail page -- the same for a punch-in
    and its punch-out. A QR scan carries both phones; a self punch only the
    manager's own."""
    at = punch.rms_scan_time.astimezone(zone)
    rows = [
        ("Time", at.strftime("%H:%M:%S") + ("  (+1 day)" if at.date() > day else "")),
        ("Source", punch.get_source_display()),
        ("Employee branch", punch.employee_branch.name if punch.employee_branch_id else "—"),
    ]
    if punch.source == AttendanceSource.QR_SCAN:
        rows += [
            ("Scanned at", punch.rms_branch.name if punch.rms_branch_id else "—"),
            ("Scanned by", f"{punch.rms_employee_name} ({punch.rms_employee_code})"),
            ("QR generated", punch.qr_generation_time.astimezone(zone).strftime("%H:%M:%S")
             if punch.qr_generation_time else "—"),
            ("Employee phone", punch.employee_installation_id or "—"),
            ("Employee GPS", _gps(punch.employee_latitude, punch.employee_longitude) or "—"),
            ("RMS device", punch.rms_installation_id),
            ("RMS GPS", _gps(punch.rms_latitude, punch.rms_longitude) or "—"),
        ]
    else:
        rows += [
            ("Manager phone", punch.rms_installation_id),
            ("Phone GPS", _gps(punch.rms_latitude, punch.rms_longitude) or "—"),
        ]
    # When the server got it -- far from Time means the phone's clock is off
    # or the punch waited offline.
    rows.append(("Received", punch.created_on.astimezone(zone).strftime("%d %b %Y, %H:%M:%S")))
    return rows


_SESSION_STATUS = {
    services.SessionStatus.CLOSED: ("complete", "Complete"),
    services.SessionStatus.OPEN: ("on_duty", "On duty"),
    services.SessionStatus.MISSING_PUNCH_OUT: ("missing", "Missing punch-out"),
}


class AttendanceListView(CrewScreenView):
    """A read-only monitor: no drawer, no Add, no edit, no delete -- punches
    are written by the apps, and what the apps recorded is not rewritten here."""

    template_name = "crew/attendance_list.html"
    page_code = "crew.attendance"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        punch_ins, zone, from_day, to_day = _attendance_filters(self.request)
        days = _attendance_days(self.request, punch_ins, zone)
        page = Paginator(days, ATTENDANCE_PER_PAGE).get_page(self.request.GET.get("page"))

        keys = {(d["employee"], d["day"]) for d in page.object_list}
        sessions = _sessions_by_day(punch_ins, keys, zone) if keys else {}
        employees = Employee.objects.select_related("designation").in_bulk({employee for employee, _ in keys})

        list_query = self.request.GET.urlencode()
        rows = []
        for d in page.object_list:
            employee = employees[d["employee"]]
            day_sessions = sessions.get((d["employee"], d["day"]), [])
            status, status_label = _day_status(d)
            first_in = day_sessions[0][0].rms_scan_time.astimezone(zone) if day_sessions else None
            outs = [s[1].rms_scan_time.astimezone(zone) for s in day_sessions if s[1] is not None]
            last_out = max(outs) if outs else None
            worked = [s[3] for s in day_sessions if s[3] is not None]
            rows.append({
                "name": employee.full_name,
                "code": employee.employee_code,
                "designation": employee.designation.title,
                "day": d["day"],
                "first_in": first_in.strftime("%H:%M") if first_in else "",
                "last_out": last_out.strftime("%H:%M") if last_out else "",
                "last_out_next_day": bool(last_out and last_out.date() > d["day"]),
                "worked": _duration(sum(worked)) if worked else "",
                "shifts": d["shifts"],
                "branch": day_sessions[0][0].employee_branch.name
                if day_sessions and day_sessions[0][0].employee_branch_id else "",
                "status": status,
                "status_label": status_label,
                "detail_url": reverse("crew-attendance-detail", args=[d["employee"], d["day"].isoformat()])
                + (f"?{list_query}" if list_query else ""),
            })

        query = self.request.GET.copy()
        query.pop("page", None)
        now = timezone.now()
        today = now.astimezone(zone).date()
        today_start = datetime.datetime.combine(today, datetime.time.min, tzinfo=zone)
        todays = scoping.attendance_for(user).filter(
            punch_type=PunchType.PUNCH_IN, rms_scan_time__gte=today_start,
            rms_scan_time__lt=today_start + datetime.timedelta(days=1),
        )
        open_ins = scoping.attendance_for(user).filter(punch_type=PunchType.PUNCH_IN, punch_out__isnull=True)
        context.update({
            "rows": rows,
            "page": page,
            "query": query.urlencode(),
            "params": self.request.GET,
            "from_day": from_day,
            "to_day": to_day,
            "filtered": any(
                self.request.GET.get(k)
                for k in ("q", "from", "to", "branch", "source", "status", "designation")
            ),
            "present_count": todays.values("employee").distinct().count(),
            "on_duty_count": open_ins.filter(rms_scan_time__gt=now - services.OPEN_PUNCH_WINDOW).count(),
            "missing_count": punch_ins.filter(
                punch_out__isnull=True, rms_scan_time__lte=now - services.OPEN_PUNCH_WINDOW,
            ).count(),
            "qr_today": todays.filter(source=AttendanceSource.QR_SCAN).count(),
            "self_today": todays.filter(source=AttendanceSource.SELF).count(),
            "sources_list": [{"id": v, "name": label} for v, label in AttendanceSource.choices],
            "statuses_list": [{"id": v, "name": label} for v, label in DAY_STATUSES],
            "export_url": reverse("crew-attendance-export"),
        })
        return context


class AttendanceDetailView(CrewScreenView):
    """One employee's punches on one company-local day -- what a list row
    stands for. Read-only, like the list."""

    template_name = "crew/attendance_detail.html"
    page_code = "crew.attendance"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        zone = zone_for(getattr(user, "company", None))
        try:
            day = datetime.date.fromisoformat(self.kwargs["day"])
        except ValueError:
            raise Http404("No such day.") from None
        employee = (scoping.employees_for(user).select_related("designation", "branch")
                    .filter(pk=self.kwargs["employee_id"]).first())
        if employee is None:
            raise Http404("No such employee.")

        start = datetime.datetime.combine(day, datetime.time.min, tzinfo=zone)
        punch_ins = (
            scoping.attendance_for(user)
            .filter(employee=employee, punch_type=PunchType.PUNCH_IN,
                    rms_scan_time__gte=start, rms_scan_time__lt=start + datetime.timedelta(days=1))
            .select_related("employee_branch", "rms_branch", "punch_out",
                            "punch_out__employee_branch", "punch_out__rms_branch")
            .order_by("rms_scan_time")
        )
        sessions = services.attendance_sessions(punch_ins)
        if not sessions:
            raise Http404("No attendance that day.")

        shifts = []
        for n, (punch_in, punch_out, status, worked) in enumerate(sessions, start=1):
            key, label = _SESSION_STATUS[status]
            shifts.append({
                "n": n, "status": key, "status_label": label, "worked": _duration(worked),
                "punch_in": _punch_detail(punch_in, zone, day),
                "punch_out": _punch_detail(punch_out, zone, day) if punch_out else None,
            })
        statuses = {shift["status"] for shift in shifts}
        day_status = next(s for s in ("missing", "on_duty", "complete") if s in statuses)
        outs = [out.rms_scan_time.astimezone(zone) for _, out, _, _ in sessions if out is not None]
        last_out = max(outs) if outs else None
        worked = [w for *_, w in sessions if w is not None]

        roster = DutyRoster.objects.filter(employee=employee, date=day).select_related("branch").first()
        if roster is None:
            rostered = "Not rostered"
        else:
            times = [f"{services._hhmm(a, zone)}–{services._hhmm(b, zone)}"
                     for a, b in ((roster.shift1_start, roster.shift1_end), (roster.shift2_start, roster.shift2_end))
                     if a and b]
            rostered = " · ".join(
                part for part in (roster.get_day_type_display(), roster.branch.name if roster.branch_id else "",
                                  ", ".join(times)) if part
            )

        list_url = reverse("crew-attendance-list")
        query = self.request.GET.urlencode()
        context.update({
            "employee": employee,
            "day": day,
            "status": day_status,
            "status_label": dict(_SESSION_STATUS.values())[day_status],
            "first_in": sessions[0][0].rms_scan_time.astimezone(zone).strftime("%H:%M"),
            "last_out": last_out.strftime("%H:%M") if last_out else "",
            "last_out_next_day": bool(last_out and last_out.date() > day),
            "worked": _duration(sum(worked)) if worked else "",
            "rostered": rostered,
            "shifts": shifts,
            "list_url": f"{list_url}?{query}" if query else list_url,
        })
        return context


class _Echo:
    """A file-like that hands back what it is given: csv.writer writes each row
    straight into the streamed response instead of building it in memory."""

    def write(self, value):
        return value


ATTENDANCE_EXPORT_COLUMNS = (
    "Date", "Employee Code", "Employee Name", "Punch In", "Punch Out", "Worked (min)", "Status",
    "Source", "Employee Branch", "Scanned At Branch", "Scanned By Code", "Scanned By",
    "QR Generated", "Employee Device", "RMS Device",
    "Employee Latitude", "Employee Longitude", "RMS Latitude", "RMS Longitude",
)


class AttendanceExport(CrewScreenView):
    """Every punch-in/punch-out pair the list's filters match, as CSV -- all
    pages, streamed. Needs Print."""

    page_code = "crew.attendance"
    required_action = "print"

    def get(self, request, *args, **kwargs):
        punch_ins, zone, from_day, to_day = _attendance_filters(request)
        keys = {(d["employee"], d["day"]) for d in _attendance_days(request, punch_ins, zone)}
        sessions = _sessions_by_day(punch_ins, keys, zone) if keys else {}
        writer = csv.writer(_Echo())

        def stamp(moment):
            return moment.astimezone(zone).strftime("%Y-%m-%d %H:%M:%S") if moment else ""

        def rows():
            yield "﻿"                                  # BOM: Excel reads the file as UTF-8
            yield writer.writerow(ATTENDANCE_EXPORT_COLUMNS)
            for (_, day), day_sessions in sorted(sessions.items(), key=lambda item: (item[0][1], item[1][0][0].employee_code)):
                for punch_in, punch_out, status, worked in day_sessions:
                    yield writer.writerow([
                        day.isoformat(), punch_in.employee_code, punch_in.employee_name,
                        stamp(punch_in.rms_scan_time), stamp(punch_out.rms_scan_time) if punch_out else "",
                        worked if worked is not None else "", status,
                        punch_in.get_source_display(),
                        punch_in.employee_branch.name if punch_in.employee_branch_id else "",
                        punch_in.rms_branch.name if punch_in.rms_branch_id else "",
                        punch_in.rms_employee_code, punch_in.rms_employee_name,
                        stamp(punch_in.qr_generation_time),
                        punch_in.employee_installation_id, punch_in.rms_installation_id,
                        punch_in.employee_latitude or "", punch_in.employee_longitude or "",
                        punch_in.rms_latitude or "", punch_in.rms_longitude or "",
                    ])

        response = StreamingHttpResponse(rows(), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = (
            f'attachment; filename="attendance-{from_day.isoformat()}-to-{to_day.isoformat()}.csv"'
        )
        return response


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
