"""POST /api/v1/{app}/employees -- the employee master for the operator app
(owner, 28 Oct 2026): every field of the company's active, approved
employees, so the tablet can pick one -- the staff member on a staff test
ride (apps/rental/test_rides.py). Documents (passport, visa, Emirates ID,
labour card, bank) are in employee_detail and are not sent."""

from django.core.paginator import Paginator
from django.db.models import Q
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.crew.models import Employee
from apps.portal.authentication import AppJWTAuthentication
from core.api import envelope, request_parts, session_station, text_field, whole_number_field
from core.enums import ApprovalStatus, Channel
from core.schema import SERVER_ERROR, envelope_request, envelope_responses

PAGE_SIZE = 50


class EmployeesRequest(serializers.Serializer):
    search = text_field(max_length=100, required=False, allow_blank=True)
    page = whole_number_field(min_value=1, required=False, default=1)


def employees_for(company, search=""):
    """The company's active, approved employees, by code -- optionally those
    whose code or name contains `search`."""
    rows = (Employee.objects.filter(company=company, is_active=True, approval_status=ApprovalStatus.APPROVED)
            .select_related("designation", "branch"))
    search = (search or "").strip()
    if search:
        rows = rows.filter(Q(employee_code__icontains=search) | Q(first_name__icontains=search)
                           | Q(middle_name__icontains=search) | Q(last_name__icontains=search))
    return rows.order_by("employee_code", "pk")


def employee_json(employee):
    """Every field of the employee master (crew.Employee)."""
    designation, branch = employee.designation, employee.branch
    return {
        "employee_id": employee.pk, "employee_code": employee.employee_code,
        "first_name": employee.first_name, "middle_name": employee.middle_name,
        "last_name": employee.last_name, "full_name": employee.full_name,
        "designation": {"id": designation.pk, "title": designation.title,
                        "roster_category": designation.roster_category or None},
        "branch": None if branch is None else {"id": branch.pk, "code": branch.short_code, "name": branch.name},
        "photo_url": employee.photo.url if employee.photo else None,
        "attendance_method": employee.attendance_method or None,
        "date_of_birth": employee.date_of_birth.isoformat() if employee.date_of_birth else None,
        "gender": employee.gender or None, "marital_status": employee.marital_status or None,
        "nationality": employee.nationality, "mobile": employee.mobile, "email": employee.email,
        "emergency_contact_person": employee.emergency_contact_person,
        "emergency_phone": employee.emergency_phone, "is_blocked": employee.is_blocked,
    }


_SAMPLE = {
    "employee_id": 41, "employee_code": "BYKY014", "first_name": "Anil", "middle_name": "", "last_name": "Kumar",
    "full_name": "Anil Kumar", "designation": {"id": 3, "title": "Cashier", "roster_category": "cashier"},
    "branch": {"id": 27, "code": "ADC1", "name": "Abu Dhabi Corniche 1"}, "photo_url": None,
    "attendance_method": None, "date_of_birth": "1994-03-12", "gender": "male", "marital_status": "single",
    "nationality": "Indian", "mobile": "971501112233", "email": "anil@byky.ae",
    "emergency_contact_person": "Suresh Kumar", "emergency_phone": "971502223344", "is_blocked": False,
}

_DESCRIPTION = """
The **employee master**: the company's active, approved employees, every field
(`crew.Employee`), by employee code. 50 a page; `search` matches the code or
any part of the name. `is_blocked` employees are listed but cannot be picked
for a staff test ride (`employee_blocked`). `branch` is the home branch (the
duty roster decides where they work on a day). Documents (passport, visa,
Emirates ID, labour card, bank) are not sent.

Used by the operator app to pick the staff member on a **staff test ride**
(`test-rides`, `employee_id`).
"""


class EmployeesView(APIView):
    """POST /api/v1/{app}/employees -- operator app only."""

    authentication_classes = [AppJWTAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["Operator Employees"], summary="The employee master (active employees)", description=_DESCRIPTION,
        request=envelope_request("EmployeesEnvelope", EmployeesRequest, request_data_required=False),
        responses=envelope_responses(
            (200, "ok", "Employees.", {"page": 1, "pages": 1, "total": 1, "employees": [_SAMPLE]}),
            (401, "not_authenticated", "Sign in first.", {}),
            (403, "wrong_channel", "Not allowed on this app.", {}),
            (409, "device_not_mapped", "This device has no station.", {}),
            (409, "branch_inactive", "This station is closed.", {}),
            SERVER_ERROR,
        ),
    )
    def post(self, request, app):
        if app != Channel.OPERATOR or request.auth.channel != Channel.OPERATOR:
            return envelope("wrong_channel", "Not allowed on this app.", http_status=403)
        branch, refused = session_station(request)
        if refused:
            return refused
        _, request_data = request_parts(request)
        form = EmployeesRequest(data=request_data or {})
        form.is_valid(raise_exception=True)
        page = Paginator(employees_for(branch.company, form.validated_data.get("search")), PAGE_SIZE).get_page(
            form.validated_data["page"])
        return envelope("ok", "Employees.", {
            "page": page.number, "pages": page.paginator.num_pages, "total": page.paginator.count,
            "employees": [employee_json(employee) for employee in page.object_list],
        })
