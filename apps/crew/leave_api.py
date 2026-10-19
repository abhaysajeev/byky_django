"""The leave APIs -- views only translate HTTP; the rules are in
apps/crew/leave.py.

Employee app (the applicant):
POST /api/v1/employee/leave/types      the leave types to pick from
POST /api/v1/employee/leave/submit     ask for leave
POST /api/v1/employee/leave/my         my requests
POST /api/v1/employee/leave/cancel     withdraw a pending request

Manager app, and employee-app Leave Request approvers:
POST /api/v1/{app}/leave/requests      the requests to decide
POST /api/v1/{app}/leave/approve
POST /api/v1/{app}/leave/reject
"""

from django.core.paginator import Paginator
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.crew import leave
from apps.crew.models import DutyRoster, DutyRosterDayType, LeaveStatus, LeaveType
from apps.crew.serializers import (
    LeaveDecisionRequest,
    LeaveIdRequest,
    LeaveListRequest,
    LeaveSubmitRequest,
    LeaveTypesRequest,
    MyLeaveListRequest,
)
from apps.portal.authentication import AppJWTAuthentication
from core.api import envelope, other_app, request_parts
from core.enums import Channel
from core.schema import SERVER_ERROR, envelope_request, envelope_responses
from core.timezones import zone_for

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
APPROVER_APPS = (Channel.MANAGER, Channel.EMPLOYEE)


def _local(moment, zone):
    return moment.astimezone(zone).strftime(TIME_FORMAT) if moment else None


def _refused(refusal):
    return envelope(refusal.code, refusal.message, http_status=refusal.status)


def _working_days(rows):
    """{request id: Working roster days in its range}, one query for a page."""
    rows = [row for row in rows if row.status in (LeaveStatus.PENDING, LeaveStatus.REJECTED)]
    if not rows:
        return {}
    days = (DutyRoster.objects
            .filter(employee_id__in={row.employee_id for row in rows}, day_type=DutyRosterDayType.WORKING,
                    date__range=(min(row.from_date for row in rows), max(row.to_date for row in rows)))
            .values_list("employee_id", "date"))
    out = {}
    for row in rows:
        out[row.pk] = sum(1 for employee_id, day in days
                          if employee_id == row.employee_id and row.from_date <= day <= row.to_date)
    return out


def leave_json(row, zone, user, channel, working):
    """One request with everything its details screen shows."""
    employee = row.employee
    return {
        "leave_request_id": str(row.pk), "reference": row.reference,
        "employee": {"employee_code": employee.employee_code, "name": employee.full_name,
                     "designation": employee.designation.title},
        "stations": [
            {"branch_id": b.pk, "code": b.short_code, "name": b.name,
             "state_id": b.location.state_id, "state": b.location.state.name}
            for b in sorted(row.stations.all(), key=lambda b: b.name)
        ],
        "leave_type": {"leave_type_id": row.leave_type_id, "name": row.leave_type.name},
        "from_date": row.from_date.isoformat(), "to_date": row.to_date.isoformat(), "days": row.days,
        "reason": row.reason, "status": row.status, "applied_at": _local(row.applied_at, zone),
        "decided_by": leave._name(row.decided_by) or None, "decided_channel": row.decided_channel or None,
        "decided_at": _local(row.decided_at, zone), "decision_remarks": row.decision_remarks,
        "status_edited": row.status_edited, "cancelled_at": _local(row.cancelled_at, zone),
        "can_cancel": channel == Channel.EMPLOYEE and leave.is_own(row, user) and row.status == LeaveStatus.PENDING,
        "can_decide": leave.can_decide(row, user, channel) if channel in APPROVER_APPS else False,
        "working_days_rostered": working.get(row.pk, 0),
        "log": [
            {"status": entry.status, "at": _local(entry.at, zone), "by": entry.by_name, "channel": entry.channel,
             "remarks": entry.remarks}
            for entry in row.log.all()
        ],
    }


def _list(rows, values, user, channel, zone):
    """One page of `rows`, with the counts per status before the status
    filter -- for the tabs."""
    counts = leave.status_counts(rows)
    if values["status"] != "all":
        rows = rows.filter(status=values["status"])
    page = Paginator(leave.with_details(rows), leave.PAGE_SIZE).get_page(values["page"])
    items = list(page.object_list)
    working = _working_days(items)
    return envelope("ok", "Leave requests.", {
        "counts": counts,
        "requests": [leave_json(row, zone, user, channel, working) for row in items],
        "page": page.number, "pages": page.paginator.num_pages, "total": page.paginator.count,
    })


# -- Samples ---------------------------------------------------------------------------

_STATION = {"branch_id": 12, "code": "CPG1", "name": "Creek Park Gate 1", "state_id": 2, "state": "Dubai"}

_ROW = {
    "leave_request_id": "01923e1c-0a11-7b22-8c33-d4e5f6a7b8c9", "reference": "LEV-2026-00037",
    "employee": {"employee_code": "BYKY014", "name": "Anil Kumar", "designation": "Cashier"},
    "stations": [_STATION], "leave_type": {"leave_type_id": 1, "name": "Annual Leave"},
    "from_date": "2026-10-20", "to_date": "2026-10-22", "days": 3, "reason": "Family visit",
    "status": "pending", "applied_at": "2026-10-15 09:12:40",
    "decided_by": None, "decided_channel": None, "decided_at": None, "decision_remarks": "",
    "status_edited": False, "cancelled_at": None, "can_cancel": True, "can_decide": False,
    "working_days_rostered": 2,
    "log": [{"status": "pending", "at": "2026-10-15 09:12:40", "by": "Anil Kumar", "channel": "employee",
             "remarks": ""}],
}

_DECIDED = {
    **_ROW, "status": "approved", "decided_by": "Meera K", "decided_channel": "manager",
    "decided_at": "2026-10-15 11:05:02", "decision_remarks": "Cover arranged", "status_edited": True,
    "can_cancel": False, "can_decide": False, "working_days_rostered": 0,
    "log": [
        _ROW["log"][0],
        {"status": "rejected", "at": "2026-10-15 10:01:13", "by": "Meera K", "channel": "manager",
         "remarks": "Short staffed"},
        {"status": "approved", "at": "2026-10-15 11:05:02", "by": "Meera K", "channel": "manager",
         "remarks": "Cover arranged"},
    ],
}

_COUNTS = {"pending": 1, "approved": 0, "rejected": 0, "cancelled": 0, "all": 1}

_RULES = """
**The rules** -- whole days, from **today** on (company time), calendar days
counted with both ends (20 to 22 Oct = 3 days), no balance kept. A request may
not overlap the employee's own pending or approved one. Only the applicant
cancels, and only while pending. Approvers approve or reject: **managers**
(the whole company), the **web**, and in the employee app the **Leave Request
approvers** of the request's `stations` -- the stations the employee is
rostered at during the leave plus their home branch, fixed when it is
submitted. An approver never decides their own request (a manager does).
Approved is final. A **first rejection may be changed once** (Edit Status) --
approve or reject it again; `status_edited` is then true and `can_decide`
false. An approval turns every day of the range into an **On Leave** roster
day, replacing what was rostered; `working_days_rostered` says how many
working shifts that removes (0 once decided).

**Rows** carry every detail, including the status `log` (oldest first) -- no
second call. `can_cancel` / `can_decide` say which buttons to show. Dates
`YYYY-MM-DD`; times `YYYY-MM-DD HH:MM:SS`, company time.
"""


class _LeaveView(APIView):
    authentication_classes = [AppJWTAuthentication]
    permission_classes = [IsAuthenticated]
    apps = (Channel.EMPLOYEE,)
    form_class = None

    def post(self, request, app):
        if app not in self.apps:
            return envelope("wrong_channel", "Not allowed on this app.", http_status=403)
        refused = other_app(request, app)
        if refused:
            return refused
        _, request_data = request_parts(request)
        form = self.form_class(data=request_data)
        form.is_valid(raise_exception=True)
        employee = request.user.employee
        try:
            return self.perform(request, app, form.validated_data, employee, zone_for(employee.company))
        except leave.LeaveRefused as refusal:
            return _refused(refusal)


# -- Employee app ----------------------------------------------------------------------

class LeaveTypesView(_LeaveView):
    """POST /api/v1/employee/leave/types"""

    form_class = LeaveTypesRequest

    @extend_schema(
        tags=["Employee Leave"], summary="The leave types to pick from",
        description="The company's active leave types, for the Leave Type dropdown.",
        request=envelope_request("LeaveTypesEnvelope", LeaveTypesRequest),
        responses=envelope_responses(
            (200, "ok", "Leave types.", {"leave_types": [{"leave_type_id": 1, "code": "ANNUAL",
                                                          "name": "Annual Leave"}]}),
            (401, "not_authenticated", "Sign in first.", {}),
            (403, "wrong_channel", "Not allowed on this app.", {}),
            SERVER_ERROR,
        ),
    )
    def post(self, request, app):
        return super().post(request, app)

    def perform(self, request, app, values, employee, zone):
        types = LeaveType.objects.filter(company=employee.company_id, is_active=True).order_by("name")
        return envelope("ok", "Leave types.", {"leave_types": [
            {"leave_type_id": t.pk, "code": t.code, "name": t.name} for t in types]})


class LeaveSubmitView(_LeaveView):
    """POST /api/v1/employee/leave/submit"""

    form_class = LeaveSubmitRequest

    @extend_schema(
        tags=["Employee Leave"], summary="Ask for leave",
        description="""
`sync_id` (a **UUIDv7** made on the phone, resent unchanged on a retry -- a
second call answers `duplicate` with the stored request), `leave_type_id`
(from `leave/types`), `from_date`, `to_date`, `reason` (optional). The reply
is the request as `leave/my` lists it.
""" + _RULES,
        request=envelope_request("LeaveSubmitEnvelope", LeaveSubmitRequest),
        responses=envelope_responses(
            (200, "ok", "Leave request submitted.", _ROW),
            (200, "duplicate", "Already submitted.", _ROW),
            (400, "invalid_request", "to_date must be on or after from_date.",
             {"errors": {"to_date": "must be on or after from_date"}}),
            (400, "unknown_leave_type", "No such leave type.", {}),
            (400, "leave_in_past", "Leave can start today at the earliest.", {}),
            (401, "not_authenticated", "Sign in first.", {}),
            (403, "wrong_channel", "Not allowed on this app.", {}),
            (409, "leave_overlaps", "These dates overlap pending leave LEV-2026-00031 (20 Oct - 21 Oct).", {}),
            (409, "sync_id_conflict", "That sync_id is already used.", {}),
            SERVER_ERROR,
        ),
    )
    def post(self, request, app):
        return super().post(request, app)

    def perform(self, request, app, values, employee, zone):
        row, created = leave.submit(request.user, values)
        data = leave_json(leave.with_details(leave.my_requests(employee)).get(pk=row.pk), zone, request.user,
                          app, _working_days([row]))
        if not created:
            return envelope("duplicate", "Already submitted.", data)
        return envelope("ok", "Leave request submitted.", data)


class MyLeaveView(_LeaveView):
    """POST /api/v1/employee/leave/my"""

    form_class = MyLeaveListRequest

    @extend_schema(
        tags=["Employee Leave"], summary="My leave requests",
        description="""
The signed-in employee's own requests, newest first. `status` (`all` --
default -- `pending`, `approved`, `rejected`, `cancelled`), `from_date` /
`to_date` (requests overlapping that range), `page` (50 a page). `counts`
follow the dates, not the status -- for the tabs.
""" + _RULES,
        request=envelope_request("MyLeaveEnvelope", MyLeaveListRequest),
        responses=envelope_responses(
            (200, "ok", "Leave requests.", {"counts": _COUNTS, "requests": [_ROW], "page": 1, "pages": 1,
                                            "total": 1}),
            (401, "not_authenticated", "Sign in first.", {}),
            (403, "wrong_channel", "Not allowed on this app.", {}),
            SERVER_ERROR,
        ),
    )
    def post(self, request, app):
        return super().post(request, app)

    def perform(self, request, app, values, employee, zone):
        rows = leave.filtered(leave.my_requests(employee), from_date=values.get("from_date"),
                              to_date=values.get("to_date"))
        return _list(rows, values, request.user, app, zone)


class LeaveCancelView(_LeaveView):
    """POST /api/v1/employee/leave/cancel"""

    form_class = LeaveIdRequest

    @extend_schema(
        tags=["Employee Leave"], summary="Withdraw a pending leave request",
        description="Only the applicant, only while pending. The reply is the cancelled request." + _RULES,
        request=envelope_request("LeaveCancelEnvelope", LeaveIdRequest),
        responses=envelope_responses(
            (200, "ok", "Leave request cancelled.", {**_ROW, "status": "cancelled", "can_cancel": False,
                                                     "cancelled_at": "2026-10-15 09:30:00"}),
            (401, "not_authenticated", "Sign in first.", {}),
            (403, "not_your_leave", "You can cancel only your own leave request.", {}),
            (403, "wrong_channel", "Not allowed on this app.", {}),
            (404, "unknown_leave", "No such leave request.", {}),
            (409, "leave_not_pending", "This request is already approved.", {}),
            SERVER_ERROR,
        ),
    )
    def post(self, request, app):
        return super().post(request, app)

    def perform(self, request, app, values, employee, zone):
        row = leave.cancel(request.user, values["leave_request_id"])
        row = leave.with_details(leave.my_requests(employee)).get(pk=row.pk)
        return envelope("ok", "Leave request cancelled.", leave_json(row, zone, request.user, app, {}))


# -- Approvers -------------------------------------------------------------------------

class LeaveRequestsView(_LeaveView):
    """POST /api/v1/{manager|employee}/leave/requests"""

    apps = APPROVER_APPS
    form_class = LeaveListRequest

    @extend_schema(
        tags=["Manager Leave", "Employee Leave"], summary="Leave requests to decide",
        description="""
**Manager app**: every request in the company. **Employee app**: the requests
at the stations where the signed-in employee is a **Leave Request approver**
(Branch screen), never their own -- anyone else is refused
`not_a_leave_approver` (the login reply's `employee.can_approve_leave` says
whether to show the screen).

Filters, all optional: `status` (`all` default, `pending`, `approved`,
`rejected`, `cancelled`), `state_id` (emirate) and `branch_id` (station) of
the request's stations, `from_date` / `to_date` (leave overlapping the range),
`search` (employee name or code), `page` (50 a page). Newest first. `counts`
follow every filter but `status`, for the tabs and the pending badge.
""" + _RULES,
        request=envelope_request("LeaveRequestsEnvelope", LeaveListRequest),
        responses=envelope_responses(
            (200, "ok", "Leave requests.", {"counts": _COUNTS, "requests": [{**_ROW, "can_cancel": False,
                                                                             "can_decide": True}],
                                            "page": 1, "pages": 1, "total": 1}),
            (401, "not_authenticated", "Sign in first.", {}),
            (403, "not_a_leave_approver", "You do not approve leave.", {}, "not_a_leave_approver (employee)"),
            (403, "wrong_channel", "Not allowed on this app.", {}),
            SERVER_ERROR,
        ),
    )
    def post(self, request, app):
        return super().post(request, app)

    def perform(self, request, app, values, employee, zone):
        rows = leave.filtered(
            leave.decidable(request.user, app), state_id=values.get("state_id"), branch_id=values.get("branch_id"),
            from_date=values.get("from_date"), to_date=values.get("to_date"), search=values.get("search", ""),
        )
        return _list(rows, values, request.user, app, zone)


_DECISION_RESPONSES = (
    (401, "not_authenticated", "Sign in first.", {}),
    (403, "own_leave", "Your own leave is decided by a manager.", {}, "own_leave (employee)"),
    (403, "not_a_leave_approver", "You do not approve leave.", {}, "not_a_leave_approver (employee)"),
    (403, "wrong_channel", "Not allowed on this app.", {}),
    (404, "unknown_leave", "No such leave request.", {}),
    (409, "leave_decided", "This request is already approved.", {}),
    SERVER_ERROR,
)


class _DecisionView(_LeaveView):
    apps = APPROVER_APPS
    form_class = LeaveDecisionRequest
    approve = True

    def perform(self, request, app, values, employee, zone):
        row, replaced = leave.decide(request.user, app, values["leave_request_id"], self.approve,
                                     values.get("remarks", ""), now=timezone.now())
        row = leave.with_details(leave.decidable(request.user, app)).get(pk=row.pk)
        data = {**leave_json(row, zone, request.user, app, {}), "working_days_replaced": replaced}
        return envelope("ok", "Leave approved." if self.approve else "Leave rejected.", data)


class LeaveApproveView(_DecisionView):
    """POST /api/v1/{manager|employee}/leave/approve"""

    @extend_schema(
        tags=["Manager Leave", "Employee Leave"], summary="Approve a leave request",
        description="""
`leave_request_id`, `remarks` (optional). A pending request, or a first
rejection being changed (once). The reply is the request, plus
`working_days_replaced` -- the working shifts the approval turned into leave.
`leave_overlaps` when another approved request already covers some of these
days (possible only when changing an old rejection).
""" + _RULES,
        request=envelope_request("LeaveApproveEnvelope", LeaveDecisionRequest),
        responses=envelope_responses(
            (200, "ok", "Leave approved.", {**_DECIDED, "working_days_replaced": 2}),
            (409, "leave_overlaps", "These dates overlap approved leave LEV-2026-00031 (20 Oct - 21 Oct).", {}),
            *_DECISION_RESPONSES,
        ),
    )
    def post(self, request, app):
        return super().post(request, app)


class LeaveRejectView(_DecisionView):
    """POST /api/v1/{manager|employee}/leave/reject"""

    approve = False

    @extend_schema(
        tags=["Manager Leave", "Employee Leave"], summary="Reject a leave request",
        description="`leave_request_id`, `remarks` (optional). A pending request, or a first rejection "
                    "confirmed in its one-time edit." + _RULES,
        request=envelope_request("LeaveRejectEnvelope", LeaveDecisionRequest),
        responses=envelope_responses(
            (200, "ok", "Leave rejected.", {**_ROW, "status": "rejected", "decided_by": "Meera K",
                                            "decided_channel": "manager", "decided_at": "2026-10-15 10:01:13",
                                            "decision_remarks": "Short staffed", "can_cancel": False,
                                            "can_decide": True, "working_days_replaced": 0}),
            *_DECISION_RESPONSES,
        ),
    )
    def post(self, request, app):
        return super().post(request, app)
