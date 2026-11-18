"""Incentives for the device apps (owner, 18 Nov 2026) -- read only; runs are
calculated and approved on the web.

POST /api/v1/manager/incentives/branches   each station's month: target, achieved, its run
POST /api/v1/manager/incentives/employees  each employee's month across stations
POST /api/v1/manager/incentives/run        one station's run, by employee and by day
POST /api/v1/employee/incentives/year      my year: total earned, months
POST /api/v1/employee/incentives/month     my month: station summary and calendar

The operator app gets no incentives (wrong_channel). An employee is paid only
from APPROVED runs -- a draft can still change -- so the employee views count
approved lines only; a month whose run is still a draft says
`pending_approval`. The manager sees drafts too, marked as such.
"""

import calendar
import datetime
from collections import defaultdict
from decimal import Decimal

from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.company.models import Branch
from apps.crew.models import DutyRoster, DutyRosterDayType
from apps.incentives import services
from apps.incentives.models import Line, Run, RunStatus
from apps.portal.authentication import AppJWTAuthentication
from apps.targets import services as targets
from apps.targets.api import MonthFields, _branches, _company, _ManagerView, month_of, period_json
from apps.targets.models import Target, TargetDay, TargetKind
from core import money
from core.api import envelope, other_app, request_parts, whole_number_field
from core.enums import Channel
from core.schema import SERVER_ERROR, envelope_request, envelope_responses

ZERO = Decimal("0")


def _now():
    """The clock (tests set it)."""
    return timezone.now()


_COMMON = (
    (401, "not_authenticated", "Sign in first.", {}),
    (403, "wrong_channel", "Not allowed on this app.", {}),
    SERVER_ERROR,
)


# -- Requests ---------------------------------------------------------------------------------


class ManagerIncentivesRequest(MonthFields):
    state_id = whole_number_field(min_value=1, required=False, allow_null=True)


class ManagerRunRequest(MonthFields):
    branch_id = whole_number_field(min_value=1)


class EmployeeYearRequest(serializers.Serializer):
    year = whole_number_field(min_value=2020, max_value=2100, required=False)


class EmployeeMonthRequest(MonthFields):
    pass


# -- Manager ----------------------------------------------------------------------------------


def _run_json(run):
    if run is None:
        return None
    return {"run_id": run.pk, "status": run.status, "total": money.fmt(run.total), "work_days": run.work_days,
            "basis": money.fmt(run.basis),
            "approved_at": timezone.localtime(run.approved_at).strftime("%Y-%m-%d %H:%M:%S")
            if run.approved_at else None}


def manager_branches(company_ids, state_id, year, month, now=None):
    first, _ = targets.month_bounds(year, month)
    branches = _branches(company_ids, state_id)
    with_target = set(Target.objects.filter(branch__in=branches, kind=TargetKind.SALES, period_start=first)
                      .values_list("branch_id", flat=True))
    with_run = set(Run.objects.filter(branch__in=branches, period_start=first).values_list("branch_id", flat=True))
    rows = []
    totals = {"stations": 0, "met": 0, "approved_total": ZERO, "draft_total": ZERO}
    for branch in sorted(branches, key=lambda b: b.name.lower()):
        if branch.pk not in with_target | with_run:
            continue
        found = services.eligibility(branch, first, now)
        run = found["run"]
        rows.append({
            "branch_id": branch.pk, "code": branch.short_code, "name": branch.name,
            "state": branch.location.state.name if branch.location_id else "",
            "target": money.fmt(found["target"].amount) if found["target"] else None,
            "achieved": money.fmt(found["achieved"]), "percent": str(found["pct"]) if found["pct"] is not None else None,
            "met": found["met"], "month_ended": found["ended"], "run": _run_json(run),
        })
        totals["stations"] += 1
        totals["met"] += found["met"]
        if run is not None:
            totals["approved_total" if run.is_approved else "draft_total"] += run.total
    totals["approved_total"] = money.fmt(totals["approved_total"])
    totals["draft_total"] = money.fmt(totals["draft_total"])
    return {"period": period_json(year, month), "totals": totals, "branches": rows}


def manager_employees(company_ids, state_id, year, month):
    first, _ = targets.month_bounds(year, month)
    branches = _branches(company_ids, state_id)
    by_company = defaultdict(list)
    for b in branches:
        by_company[b.company].append(b)
    people = []
    for company, items in by_company.items():
        people.extend(services.employee_totals(company, items, first))
    people.sort(key=lambda r: (-r["total"], r["employee"].full_name))
    return {"period": period_json(year, month), "employees": [{
        "employee_id": p["employee"].pk, "employee_code": p["employee"].employee_code,
        "name": p["employee"].full_name, "days": p["days"], "total": money.fmt(p["total"]),
        "approved": money.fmt(p["approved"]), "draft": money.fmt(p["draft"]),
        "stations": [{"branch_id": s["branch"].pk, "name": s["branch"].name, "days": s["days"],
                      "amount": money.fmt(s["amount"]), "status": s["run"].status} for s in p["stations"]],
    } for p in people]}


def manager_run(run):
    return {
        "period": period_json(run.period_start.year, run.period_start.month),
        "branch": {"branch_id": run.branch_id, "code": run.branch.short_code, "name": run.branch.name},
        "status": run.status, "target": money.fmt(run.target_amount), "achieved": money.fmt(run.achieved),
        "work_days": run.work_days, "basis": money.fmt(run.basis), "total": money.fmt(run.total),
        "scheme": run.scheme.name, **{k: v for k, v in _run_json(run).items() if k == "approved_at"},
        "employees": [{"employee_id": r["employee"].pk, "employee_code": r["employee"].employee_code,
                       "name": r["employee"].full_name, "days": r["days"], "amount": money.fmt(r["amount"]),
                       "roles": r["roles"]} for r in services.run_by_employee(run)],
        "days": [{"date": d["date"].isoformat(), "weekday": d["date"].strftime("%a"), "total": money.fmt(d["total"]),
                  "people": [{"employee_id": p["employee"].pk, "name": p["employee"].full_name, "role": p["role"],
                              "amount": money.fmt(p["amount"])} for p in d["people"]]}
                 for d in services.run_by_day(run)],
    }


_PERIOD = {"year": 2026, "month": 10, "from_date": "2026-10-01", "to_date": "2026-10-31", "label": "October 2026"}
_RUN = {"run_id": 4, "status": "approved", "total": "1240.50", "work_days": 31, "basis": "1129.032",
        "approved_at": "2026-11-03 10:15:00"}

_MANAGER_RULES = """
**Month:** `year` + `month` (both, or neither for this business month).
Read only: incentives are calculated and approved on the web. A run is
`draft` (may still be recalculated) or `approved` (final). Money in company
places.
"""


class ManagerIncentiveBranchesView(_ManagerView):
    """POST /api/v1/{app}/incentives/branches -- manager app only."""

    form_class = ManagerIncentivesRequest

    @extend_schema(
        tags=["Manager Incentives"], summary="Each station's incentive month",
        description="""
Every station with a sales target or a run that month (optionally one
`state_id`): its `target`, `achieved`, `percent`, whether it `met` the
target, whether the month has ended, and its `run` (`null` until calculated).
`totals`: stations, how many met, approved and draft totals.
""" + _MANAGER_RULES,
        request=envelope_request("ManagerIncentiveBranchesEnvelope", ManagerIncentivesRequest,
                                 request_data_required=False),
        responses=envelope_responses(
            (200, "ok", "Incentives.", {"period": _PERIOD, "totals": {"stations": 12, "met": 9,
                                                                      "approved_total": "8450.00",
                                                                      "draft_total": "1240.50"},
                                        "branches": [{"branch_id": 27, "code": "CPG1", "name": "CREEK PARK GATE 1",
                                                      "state": "Dubai", "target": "35000.00",
                                                      "achieved": "35000.00", "percent": "100.0", "met": True,
                                                      "month_ended": True, "run": _RUN}]}),
            *_COMMON,
        ),
    )
    def post(self, request, app):
        values, companies, refused = self.begin(request, app)
        if refused:
            return refused
        year, month = month_of(values, _company(request))
        return envelope("ok", "Incentives.",
                        manager_branches(companies, values.get("state_id"), year, month, _now()))


class ManagerIncentiveEmployeesView(_ManagerView):
    """POST /api/v1/{app}/incentives/employees -- manager app only."""

    form_class = ManagerIncentivesRequest

    @extend_schema(
        tags=["Manager Incentives"], summary="Each employee's incentive for a month",
        description="""
Everyone paid in the month's runs (optionally one `state_id`), highest
first: `total` = `approved` + `draft`, `days` worked across stations, and per
station its days, amount and run status.
""" + _MANAGER_RULES,
        request=envelope_request("ManagerIncentiveEmployeesEnvelope", ManagerIncentivesRequest,
                                 request_data_required=False),
        responses=envelope_responses(
            (200, "ok", "Incentives.", {"period": _PERIOD, "employees": [{
                "employee_id": 51, "employee_code": "BYKY001", "name": "Anas K", "days": 27, "total": "337.50",
                "approved": "337.50", "draft": "0.00",
                "stations": [{"branch_id": 27, "name": "CREEK PARK GATE 1", "days": 14, "amount": "175.00",
                              "status": "approved"}]}]}),
            *_COMMON,
        ),
    )
    def post(self, request, app):
        values, companies, refused = self.begin(request, app)
        if refused:
            return refused
        year, month = month_of(values, _company(request))
        return envelope("ok", "Incentives.", manager_employees(companies, values.get("state_id"), year, month))


class ManagerIncentiveRunView(_ManagerView):
    """POST /api/v1/{app}/incentives/run -- manager app only."""

    form_class = ManagerRunRequest

    @extend_schema(
        tags=["Manager Incentives"], summary="One station's incentive run",
        description="""
One station's calculated month: the figures it was worked out from
(`target`, `achieved`, `work_days`, `basis` = achieved / working days), the
`total`, then `employees` (highest first, with their roles' day counts) and
`days` (who was paid what each working day).
""" + _MANAGER_RULES,
        request=envelope_request("ManagerIncentiveRunEnvelope", ManagerRunRequest),
        responses=envelope_responses(
            (200, "ok", "Run.", {"period": _PERIOD, "branch": {"branch_id": 27, "code": "CPG1",
                                                              "name": "CREEK PARK GATE 1"},
                                 "status": "approved", "target": "35000.00", "achieved": "35000.00",
                                 "work_days": 28, "basis": "1250.000", "total": "350.00",
                                 "scheme": "Standard incentive", "approved_at": "2026-11-03 10:15:00",
                                 "employees": [{"employee_id": 51, "employee_code": "BYKY001", "name": "Anas K",
                                                "days": 14, "amount": "175.00", "roles": {"primary": 14}}],
                                 "days": [{"date": "2026-10-01", "weekday": "Thu", "total": "25.00",
                                           "people": [{"employee_id": 51, "name": "Anas K", "role": "primary",
                                                       "amount": "12.50"}]}]}),
            (404, "no_run", "That station's month has not been calculated.", {}),
            *_COMMON,
        ),
    )
    def post(self, request, app):
        values, companies, refused = self.begin(request, app)
        if refused:
            return refused
        year, month = month_of(values, _company(request))
        first, _ = targets.month_bounds(year, month)
        run = (Run.objects.filter(company_id__in=companies, branch_id=values["branch_id"], period_start=first)
               .select_related("branch", "scheme").first())
        if run is None:
            return envelope("no_run", "That station's month has not been calculated.", http_status=404)
        return envelope("ok", "Run.", manager_run(run))


# -- Employee ---------------------------------------------------------------------------------


def _month_status(runs, ended):
    """approved / pending_approval / not_calculated / in_progress for the
    stations an employee worked in a month. `runs`: {branch_id: Run or None}."""
    if not ended:
        return "in_progress"
    found = [r for r in runs.values() if r is not None]
    if any(not r.is_approved for r in found):
        return "pending_approval"
    if found:
        return "approved"
    return "not_calculated"


def _sales_figures(company, branch_ids, first, today):
    """{branch_id: Figures} for the stations' sales targets of the month."""
    rows = list(Target.objects.filter(branch_id__in=branch_ids, kind=TargetKind.SALES, period_start=first)
                .select_related("company", "branch"))
    got = targets.achieved_for(rows, today)
    return {t.branch_id: targets.figures(t.amount, got.get(t.pk, ZERO)) for t in rows}, rows


def employee_year(employee, year, now=None):
    company = employee.company
    today = targets.today_for(company, now)
    working = (DutyRoster.objects.filter(employee=employee, date__year=year, date__lte=today,
                                         day_type=DutyRosterDayType.WORKING, branch__isnull=False)
               .values_list("date", "branch_id"))
    months = defaultdict(lambda: {"days": set(), "branches": set()})
    for day, branch_id in working:
        entry = months[day.month]
        entry["days"].add(day)
        entry["branches"].add(branch_id)
    approved = defaultdict(lambda: ZERO)
    for line in Line.objects.filter(employee=employee, date__year=year, run__status=RunStatus.APPROVED):
        approved[line.date.month] += line.amount
    out = []
    for month in sorted(months, reverse=True):
        entry = months[month]
        first, last = targets.month_bounds(year, month)
        figs, _ = _sales_figures(company, entry["branches"], first, today)
        runs = {b: Run.objects.filter(branch_id=b, period_start=first).first() for b in entry["branches"]}
        met = bool(figs) and len(figs) == len(entry["branches"]) and all(f.remaining == ZERO for f in figs.values())
        out.append({"year": year, "month": month, "label": f"{calendar.month_name[month]} {year}",
                    "days_worked": len(entry["days"]), "branches": len(entry["branches"]),
                    "target_achieved": met, "status": _month_status(runs, today > last),
                    "amount": money.fmt(approved[month])})
    return {"year": year, "total_earned": money.fmt(sum(approved.values(), ZERO)),
            "shifts_logged": sum(len(m["days"]) for m in months.values()), "months": out}


def employee_month(employee, year, month, now=None):
    company = employee.company
    today = targets.today_for(company, now)
    first, last = targets.month_bounds(year, month)
    rows = list(DutyRoster.objects.filter(employee=employee, date__gte=first, date__lte=last)
                .select_related("branch", "employee__designation").order_by("date"))
    worked = [r for r in rows if r.day_type == DutyRosterDayType.WORKING and r.branch_id]
    branch_ids = {r.branch_id for r in worked}
    figs, sales_targets = _sales_figures(company, branch_ids, first, today)
    if sales_targets:
        targets.ensure_frozen(sales_targets, today)          # today's target is fixed from the day's start
    day_targets = {(d.target.branch_id, d.date): targets.day_target(d)
                   for d in TargetDay.objects.filter(target__in=sales_targets).select_related("target")}
    achieved = targets.achieved_by_day(company, branch_ids, first, min(last, today)) if first <= today else {}
    lines = defaultdict(lambda: ZERO)
    per_branch = defaultdict(lambda: ZERO)
    for line in Line.objects.filter(employee=employee, date__gte=first, date__lte=last,
                                    run__status=RunStatus.APPROVED).select_related("run"):
        lines[(line.run.branch_id, line.date)] += line.amount
        per_branch[line.run.branch_id] += line.amount
    runs = {b: Run.objects.filter(branch_id=b, period_start=first).first() for b in branch_ids}

    calendar_rows = []
    by_date = {r.date: r for r in rows}
    day = first
    while day <= last:
        row = by_date.get(day)
        entry = {"date": day.isoformat(), "weekday": day.strftime("%a"),
                 "day_type": row.day_type if row else "not_set", "branch": None, "role": None,
                 "target": None, "achieved": None, "to_target": None, "met": None, "amount": None}
        if row is not None and row.day_type == DutyRosterDayType.WORKING and row.branch_id:
            goal = day_targets.get((row.branch_id, day))
            got = achieved.get((row.branch_id, day), ZERO) if day <= today else None
            entry.update({
                "branch": {"branch_id": row.branch_id, "name": row.branch.name},
                "role": services.person_role(row),
                "target": money.fmt(goal) if goal is not None else None,
                "achieved": money.fmt(got) if got is not None else None,
                "to_target": money.fmt(max(goal - got, ZERO)) if goal is not None and got is not None else None,
                "met": (got >= goal) if goal is not None and got is not None else None,
                "amount": money.fmt(lines.get((row.branch_id, day), ZERO)),
            })
        calendar_rows.append(entry)
        day += datetime.timedelta(days=1)

    branches = {b.pk: b for b in Branch.objects.filter(pk__in=branch_ids).select_related("location__state")}
    summary = []
    for branch_id in sorted(branch_ids, key=lambda b: branches[b].name.lower()):
        branch, fig = branches[branch_id], figs.get(branch_id)
        summary.append({
            "branch_id": branch_id, "name": branch.name,
            "state": branch.location.state.name if branch.location_id else "",
            "days": sum(1 for r in worked if r.branch_id == branch_id),
            "target": money.fmt(fig.amount) if fig else None, "achieved": money.fmt(fig.achieved) if fig else None,
            "pending": money.fmt(fig.remaining) if fig else None,
            "target_achieved": bool(fig) and fig.remaining == ZERO,
            "amount": money.fmt(per_branch[branch_id]),
        })
    return {
        "period": period_json(year, month),
        "days_worked": len({r.date for r in worked}), "branches": len(branch_ids),
        "amount": money.fmt(sum(per_branch.values(), ZERO)), "status": _month_status(runs, today > last),
        "amount_to_achieve": money.fmt(sum((f.remaining for f in figs.values()), ZERO)),
        "branch_summary": summary, "calendar": calendar_rows,
    }


class _EmployeeView(APIView):
    """Employee app only, the signed-in employee's own figures."""

    authentication_classes = [AppJWTAuthentication]
    permission_classes = [IsAuthenticated]
    form_class = None

    def post(self, request, app):
        if app != Channel.EMPLOYEE:
            return envelope("wrong_channel", "Not allowed on this app.", http_status=403)
        refused = other_app(request, app)
        if refused:
            return refused
        employee = getattr(request.user, "employee", None)
        if employee is None:
            return envelope("not_an_employee", "This sign-in has no employee.", http_status=409)
        _, request_data = request_parts(request)
        form = self.form_class(data=request_data)
        form.is_valid(raise_exception=True)
        return self.perform(employee, form.validated_data)


_EMPLOYEE_RULES = """
**Only approved incentive counts:** amounts come from runs a manager approved
on the web; a month whose run is still a draft says `pending_approval`
(its amount stays at what is approved, usually `0.00`). Month `status`:
`approved`, `pending_approval`, `not_calculated` (ended, no run yet -- e.g.
the target was not met), `in_progress` (the month is not over).
"""


class EmployeeIncentiveYearView(_EmployeeView):
    """POST /api/v1/{app}/incentives/year -- employee app only."""

    form_class = EmployeeYearRequest

    @extend_schema(
        tags=["Employee Incentives"], summary="My incentives for a year",
        description="""
The Incentives Dashboard: `total_earned` (approved, the year), `shifts_logged`
(Working roster days up to today) and every month the employee worked,
newest first -- days worked, how many stations, whether every station they
worked met its sales target (`target_achieved`), `status` and `amount`.
`year` defaults to this one.
""" + _EMPLOYEE_RULES,
        request=envelope_request("EmployeeIncentiveYearEnvelope", EmployeeYearRequest, request_data_required=False),
        responses=envelope_responses(
            (200, "ok", "Incentives.", {"year": 2026, "total_earned": "5000.00", "shifts_logged": 104, "months": [
                {"year": 2026, "month": 8, "label": "August 2026", "days_worked": 27, "branches": 3,
                 "target_achieved": True, "status": "approved", "amount": "337.50"}]}),
            (409, "not_an_employee", "This sign-in has no employee.", {}),
            *_COMMON,
        ),
    )
    def post(self, request, app):
        return super().post(request, app)

    def perform(self, employee, values):
        year = values.get("year") or targets.today_for(employee.company, _now()).year
        return envelope("ok", "Incentives.", employee_year(employee, year, _now()))


_DAY = {"date": "2026-08-01", "weekday": "Sat", "day_type": "working",
        "branch": {"branch_id": 27, "name": "CREEK PARK GATE 1"}, "role": "primary", "target": "1166.67",
        "achieved": "1320.00", "to_target": "0.00", "met": True, "amount": "12.50"}
_OFF = {"date": "2026-08-07", "weekday": "Fri", "day_type": "week_off", "branch": None, "role": None,
        "target": None, "achieved": None, "to_target": None, "met": None, "amount": None}


class EmployeeIncentiveMonthView(_EmployeeView):
    """POST /api/v1/{app}/incentives/month -- employee app only."""

    form_class = EmployeeMonthRequest

    @extend_schema(
        tags=["Employee Incentives"], summary="My incentive month: stations and calendar",
        description="""
The month breakdown. `amount_to_achieve`: what the stations the employee
worked still need to meet their sales targets (each station's shortfall,
added up).

* **`branch_summary`** -- per station worked: days, its month `target`,
  `achieved`, `pending`, `target_achieved`, and the employee's approved
  `amount` there.
* **`calendar`** -- every day of the month: `day_type` (`working`,
  `week_off`, `sick_leave`, `casual_leave`, `on_leave`, `not_set`). A working
  day has its station, `role` (`primary`, `secondary`, `labour`), the
  station's **daily target** for that day (fixed at the day's start from the
  revenue before it), `achieved`, `to_target` (never below 0), `met`, and the
  approved `amount`. Days not reached yet have `achieved: null`.
""" + _EMPLOYEE_RULES,
        request=envelope_request("EmployeeIncentiveMonthEnvelope", EmployeeMonthRequest,
                                 request_data_required=False),
        responses=envelope_responses(
            (200, "ok", "Incentives.", {
                "period": {"year": 2026, "month": 8, "from_date": "2026-08-01", "to_date": "2026-08-31",
                           "label": "August 2026"},
                "days_worked": 27, "branches": 3, "amount": "337.50", "status": "approved",
                "amount_to_achieve": "0.00",
                "branch_summary": [{"branch_id": 27, "name": "CREEK PARK GATE 1", "state": "Dubai", "days": 14,
                                    "target": "35000.00", "achieved": "36100.00", "pending": "0.00",
                                    "target_achieved": True, "amount": "175.00"}],
                "calendar": [_DAY, _OFF]}),
            (409, "not_an_employee", "This sign-in has no employee.", {}),
            *_COMMON,
        ),
    )
    def post(self, request, app):
        return super().post(request, app)

    def perform(self, employee, values):
        year, month = month_of(values, employee.company)
        return envelope("ok", "Incentives.", employee_month(employee, year, month, _now()))
