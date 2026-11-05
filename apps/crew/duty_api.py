"""The duty roster APIs -- read-only; views only translate HTTP, the rules are
in apps/crew/duty.py.

POST /api/v1/{employee|operator}/duty/my  my days (upcoming, or -- employee
                                          only -- a past range)
POST /api/v1/{manager|employee}/duty/team the team's days, by the Team
                                          Attendance hierarchy
POST /api/v1/operator/duty/branch         the device's station's days
"""

from django.core.paginator import Paginator
from drf_spectacular.utils import extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.crew import duty, services
from apps.crew.serializers import MyRosterRequest, OperatorRosterRequest, TeamRosterRequest
from apps.portal.authentication import AppJWTAuthentication
from core.api import envelope, other_app, request_parts, session_station
from core.enums import Channel
from core.schema import SERVER_ERROR, envelope_request, envelope_responses
from core.timezones import zone_for

_DAY = {
    "date": "2026-10-15", "status": "Working", "role": "cashier", "branch_id": 12, "branch": "Creek Park Gate 1",
    "branch_code": "CPG1", "state": "Dubai", "shift1_start": "15:00", "shift1_end": "02:00",
    "shift1_ends_next_day": True, "shift2_start": None, "shift2_end": None, "shift2_ends_next_day": None,
    "leave_type": None, "leave_reference": None,
}
_LEAVE_DAY = {
    **_DAY, "date": "2026-10-16", "status": "On Leave", "role": None, "branch_id": None, "branch": None,
    "branch_code": None,
    "state": None, "shift1_start": None, "shift1_end": None, "shift1_ends_next_day": None,
    "leave_type": "Annual Leave", "leave_reference": "LEV-2026-00037",
}
_NOT_SET = {key: None for key in _DAY} | {"date": "2026-10-17"}

_DAY_RULES = """
**A day** -- `status` is `Working`, `Week Off`, `Sick Leave`, `Casual Leave`,
`On Leave` (an approved leave request: `leave_type`, `leave_reference`) or
**null -- not set** (nothing rostered; never read as Working). A Working day
has its station (`branch_id`, `branch`, `branch_code` -- the QR code's
`employee_branch_code` -- and `state`, the emirate) and Shift 1 (Shift 2 when
there is one), `HH:MM` on the station's clock; `shiftN_ends_next_day` is true
for a shift past midnight (15:00 to 02:00). `role` is what a Working day is
worked as -- `cashier` or `labour`, which can differ from the employee's own
designation (a Cashier rostered as labour that day); null on any other day.
Dates `YYYY-MM-DD`. Every date of the range is listed. Read-only: rosters are
written on the web.
"""


class _RosterView(APIView):
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
        form = self.form_for(app)(data=request_data)
        form.is_valid(raise_exception=True)
        return self.perform(request, app, form.validated_data, request.user.employee)

    def form_for(self, app):
        return self.form_class


class MyRosterView(_RosterView):
    """POST /api/v1/{employee|operator}/duty/my"""

    apps = (Channel.EMPLOYEE, Channel.OPERATOR)
    form_class = MyRosterRequest

    @extend_schema(
        tags=["Operator Duty Roster", "Employee Duty Roster"], summary="My duty roster",
        description="""
The signed-in employee's own days. **My Duty Roster**: send nothing -- today
and the next six days. **Previous Duty Rosters**: `from_date` + `to_date`
(both or neither, at most 31 days); approved leave shows as `On Leave`.

**Operator app**: no `request_data` -- always the signed-in operator's own
days, **today and the next six** (counted from today, not the roster week;
"today" is the company's business day). Dates sent are ignored. The login's
`roster` block has the same day shape, for the current week as at login.
""" + _DAY_RULES,
        request=envelope_request("MyRosterEnvelope", MyRosterRequest, request_data_required=False),
        responses=envelope_responses(
            (200, "ok", "Duty roster.", {"from_date": "2026-10-15", "to_date": "2026-10-17",
                                         "days": [_DAY, _LEAVE_DAY, _NOT_SET]}),
            (400, "invalid_request", "to_date must be within 31 days of from_date.",
             {"errors": {"to_date": "must be within 31 days of from_date"}}),
            (401, "not_authenticated", "Sign in first.", {}),
            (403, "wrong_channel", "Not allowed on this app.", {}),
            SERVER_ERROR,
        ),
    )
    def post(self, request, app):
        return super().post(request, app)

    def form_for(self, app):
        return OperatorRosterRequest if app == Channel.OPERATOR else self.form_class

    def perform(self, request, app, values, employee):
        if "from_date" in values:
            from_date, to_date = values["from_date"], values["to_date"]
        else:
            from_date, to_date = duty.upcoming(employee.company)
        return envelope("ok", "Duty roster.", {
            "from_date": from_date.isoformat(), "to_date": to_date.isoformat(),
            "days": duty.my_roster(employee, from_date, to_date),
        })


class TeamRosterView(_RosterView):
    """POST /api/v1/{manager|employee}/duty/team"""

    apps = (Channel.MANAGER, Channel.EMPLOYEE)
    form_class = TeamRosterRequest

    @extend_schema(
        tags=["Manager Duty Roster", "Employee Duty Roster"], summary="The team's duty roster",
        description="""
The cashiers' and labour's rosters (no one else is rostered), **grouped per
employee**, by the Team Attendance hierarchy:

* **manager** app -- the whole company;
* **employee** app -- the designation's **Team Scope** (login
  `employee.team_scope`): `company` the whole company; `branches` the
  stations the employee is a **Branch Supervisor** of (none assigned: an empty
  list); `own` -- refused `no_team_scope`.

**Who is listed** -- the whole company with no station or emirate filter:
every active cashier and labour, **rostered or not** (their days read not
set). A Branch Supervisor, or any station / emirate filter: everyone with a
**Working day in the range at a station in scope** -- with **every** day of
the range, days at other stations (named), week off and leave included.

`request_data`, all optional: `from_date` + `to_date` (both or neither; the
current roster week by default; at most 31 days), `state_id`, `branch_id`,
`category` (`cashier` / `labour`), `search` (name or code), `page` (50
employees a page, by name). `counts` -- the listed employees' days in the
range per status (`working`, `week_off`, `sick_leave`, `casual_leave`,
`on_leave`, `not_set`), across every page. `branches` -- the stations in
scope, for the pickers.
""" + _DAY_RULES,
        request=envelope_request("TeamRosterEnvelope", TeamRosterRequest, request_data_required=False),
        responses=envelope_responses(
            (200, "ok", "Team duty roster.", {
                "from_date": "2026-10-15", "to_date": "2026-10-17", "scope": "branches",
                "branches": [{"branch_id": 12, "code": "CPG1", "name": "Creek Park Gate 1", "state_id": 2,
                              "state": "Dubai"}],
                "counts": {"working": 1, "week_off": 0, "sick_leave": 0, "casual_leave": 0, "on_leave": 1,
                           "not_set": 1},
                "employees": [{"employee": {"employee_code": "BYKY014", "name": "Anil Kumar",
                                            "designation": "Cashier", "category": "cashier"},
                               "days": [_DAY, _LEAVE_DAY, _NOT_SET]}],
                "page": 1, "pages": 1, "total": 1,
            }),
            (400, "invalid_request", "category must be cashier or labour.",
             {"errors": {"category": "must be cashier or labour"}}),
            (401, "not_authenticated", "Sign in first.", {}),
            (403, "no_team_scope", "You do not see a team's attendance.", {}, "no_team_scope (employee)"),
            (403, "wrong_channel", "Not allowed on this app.", {}),
            SERVER_ERROR,
        ),
    )
    def post(self, request, app):
        return super().post(request, app)

    def perform(self, request, app, values, employee):
        company = employee.company
        try:
            scope, branch_ids = services.team_scope_of(employee, app)
        except services.AttendanceRefused as refusal:
            return envelope(refusal.code, refusal.message, http_status=refusal.status)
        if "from_date" in values:
            from_date, to_date = values["from_date"], values["to_date"]
        else:
            week = duty.current_week(company)
            from_date, to_date = week[0], week[-1]

        staff, counts = duty.team_roster(
            company, branch_ids, from_date, to_date, state_id=values.get("state_id"),
            branch_id=values.get("branch_id"), category=values.get("category", ""), search=values.get("search", ""),
        )
        page = Paginator(staff, duty.PAGE_SIZE).get_page(values["page"])
        people = list(page.object_list)
        days = duty.days_by_employee([e.pk for e in people], from_date, to_date, zone_for(company))
        message = "No stations are assigned to you." if branch_ids == set() else "Team duty roster."
        return envelope("ok", message, {
            "from_date": from_date.isoformat(), "to_date": to_date.isoformat(), "scope": scope,
            "branches": [
                {"branch_id": b.pk, "code": b.short_code, "name": b.name,
                 "state_id": b.location.state_id, "state": b.location.state.name}
                for b in services.team_branches(company, branch_ids)
            ],
            "counts": counts,
            "employees": [
                {"employee": {"employee_code": e.employee_code, "name": e.full_name,
                              "designation": e.designation.title, "category": e.designation.roster_category},
                 "days": days[e.pk]}
                for e in people
            ],
            "page": page.number, "pages": page.paginator.num_pages, "total": page.paginator.count,
        })


def _employee_json(employee):
    return {"employee_code": employee.employee_code, "name": employee.full_name,
            "designation": employee.designation.title, "category": employee.designation.roster_category}


class BranchRosterView(_RosterView):
    """POST /api/v1/operator/duty/branch"""

    apps = (Channel.OPERATOR,)
    form_class = OperatorRosterRequest

    @extend_schema(
        tags=["Operator Duty Roster"], summary="This station's duty roster",
        description="""
The roster of **the station this device is mapped to** (from the session --
the device's mapping at login; never a station the app sends). No
`request_data`.

**Days** -- today and the next six (counted from today, not the roster week;
"today" is the company's business day).

**Who is listed** -- every active cashier and labour with at least one
**Working** day at this station in those seven days, by name. Each has all
seven days, so a day at another station names it, and week off and leave
show as such. No paging.
""" + _DAY_RULES,
        request=envelope_request("BranchRosterEnvelope", OperatorRosterRequest, request_data_required=False),
        responses=envelope_responses(
            (200, "ok", "Station duty roster.", {
                "from_date": "2026-10-15", "to_date": "2026-10-21",
                "branch": {"branch_id": 12, "code": "CPG1", "name": "Creek Park Gate 1"},
                "employees": [{"employee": {"employee_code": "BYKY014", "name": "Anil Kumar",
                                            "designation": "Cashier", "category": "cashier"},
                               "days": [_DAY, _LEAVE_DAY, _NOT_SET]}],
            }),
            (401, "not_authenticated", "Sign in first.", {}),
            (403, "wrong_channel", "Not allowed on this app.", {}),
            (409, "device_not_mapped", "This device has no station.", {}),
            (409, "branch_inactive", "This station is closed.", {}),
            SERVER_ERROR,
        ),
    )
    def post(self, request, app):
        return super().post(request, app)

    def perform(self, request, app, values, employee):
        branch, refused = session_station(request)
        if refused:
            return refused
        company = branch.company
        from_date, to_date = duty.upcoming(company)
        staff, _ = duty.team_roster(company, {branch.pk}, from_date, to_date)
        people = list(staff)
        days = duty.days_by_employee([e.pk for e in people], from_date, to_date, zone_for(company))
        return envelope("ok", "Station duty roster.", {
            "from_date": from_date.isoformat(), "to_date": to_date.isoformat(),
            "branch": {"branch_id": branch.pk, "code": branch.short_code, "name": branch.name},
            "employees": [{"employee": _employee_json(e), "days": days[e.pk]} for e in people],
        })
