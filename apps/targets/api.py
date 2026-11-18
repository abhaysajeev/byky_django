"""Targets for the device apps (owner, 18 Nov 2026) -- read only; targets are
set on the web.

POST /api/v1/operator/targets             this station's month: sales + customers + today
POST /api/v1/manager/targets/states       the month per state, sales or customers
POST /api/v1/manager/targets/branches     one state's stations
POST /api/v1/manager/targets/branch       one station's month, with its days (sales)

The employee app gets no targets: every view here refuses it (wrong_channel).
Every figure is the web's (apps/targets/services.py), so a screen and a
device never disagree. The daily target is the frozen `required` amount: fixed
for the whole business day, worked out again at the next day's start from
the revenue before it (services.ensure_frozen / freeze_due).
"""

import calendar
from collections import defaultdict

from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.company.models import Branch
from apps.company.scoping import companies_for
from apps.portal.authentication import AppJWTAuthentication
from apps.targets import services
from apps.targets.models import Target, TargetKind
from core import money
from core.api import REQUIRED, envelope, manager_only, request_parts, station_of, whole_number_field
from core.enums import Channel
from core.schema import SERVER_ERROR, envelope_request, envelope_responses

ZERO = services.ZERO


def _now():
    """The clock (tests set it)."""
    return timezone.now()


# -- Shapes ---------------------------------------------------------------------------------


def number(value, kind):
    """Money as a company-place string; customers as a whole number."""
    if value is None:
        return None
    return int(value) if kind == TargetKind.CUSTOMERS else money.fmt(value)


def figures_json(fig, kind, status=None):
    out = {"target": number(fig.amount, kind), "achieved": number(fig.achieved, kind),
           "pending": number(fig.remaining, kind), "percent": str(fig.percent)}
    if status is not None:
        out["status"] = status["key"]
    return out


def period_json(year, month):
    first, last = services.month_bounds(year, month)
    return {"year": year, "month": month, "from_date": first.isoformat(), "to_date": last.isoformat(),
            "label": f"{calendar.month_name[month]} {year}"}


def _total(rows, kind):
    """Totals of many stations: pending is each station's own shortfall added
    up, so one far above its target never hides the ones below."""
    target = sum((r["figures"].amount for r in rows), ZERO)
    achieved = sum((r["figures"].achieved for r in rows), ZERO)
    fig = services.Figures(target, achieved, services.percent(achieved, target),
                           sum((r["figures"].remaining for r in rows), ZERO))
    return {**figures_json(fig, kind), "branch_count": len(rows)}


# -- Requests -------------------------------------------------------------------------------


class MonthFields(serializers.Serializer):
    """`year` + `month`; both left out = this business month."""

    year = whole_number_field(min_value=2020, max_value=2100, required=False)
    month = whole_number_field(min_value=1, max_value=12, required=False)

    def validate(self, values):
        if ("year" in values) != ("month" in values):
            raise serializers.ValidationError({"month": "send year and month together, or neither"})
        return values


KIND = serializers.ChoiceField(choices=TargetKind.values, required=False, default=TargetKind.SALES,
                               error_messages={**REQUIRED, "invalid_choice": "must be sales or customers"})


class ManagerTargetsRequest(MonthFields):
    kind = KIND


class ManagerStateTargetsRequest(MonthFields):
    kind = KIND
    state_id = whole_number_field(min_value=1)


class ManagerBranchTargetRequest(MonthFields):
    kind = KIND
    branch_id = whole_number_field(min_value=1)


class EmptyTargetsRequest(serializers.Serializer):
    """`request_data: {}` -- the station is the session's."""


def month_of(values, company):
    if "year" in values:
        return values["year"], values["month"]
    today = services.today_for(company, _now())
    return today.year, today.month


# -- Operator --------------------------------------------------------------------------------


def _today_json(day):
    if day is None:
        return None
    achieved = day["achieved"] or ZERO
    return {"date": day["date"].isoformat(), "target": money.fmt(day["target"]), "achieved": money.fmt(achieved),
            "pending": money.fmt(max(day["target"] - achieved, ZERO)), "met": achieved >= day["target"]}


def operator_targets(branch, now=None):
    company = branch.company
    today = services.today_for(company, now)
    year, month = today.year, today.month
    out = {"branch": {"branch_id": branch.pk, "code": branch.short_code, "name": branch.name,
                      "state": branch.location.state.name if branch.location_id else ""},
           "period": period_json(year, month), "business_date": today.isoformat(),
           "sales": None, "customers": None}
    for kind in (TargetKind.SALES, TargetKind.CUSTOMERS):
        target = services.month_targets([branch.pk], year, month, kind).first()
        if target is None:
            continue
        view = services.month_view(target, now)
        entry = figures_json(view["figures"], kind, view["status"])
        if kind == TargetKind.SALES:
            entry["today"] = _today_json(view["today"])
            entry["days_left"] = view["days_left"]
        out[kind] = entry
    return out


_OPERATOR_SAMPLE = {
    "branch": {"branch_id": 27, "code": "CPG1", "name": "CREEK PARK GATE 1", "state": "Dubai"},
    "period": {"year": 2026, "month": 9, "from_date": "2026-09-01", "to_date": "2026-09-30", "label": "September 2026"},
    "business_date": "2026-09-18",
    "sales": {"target": "15000.00", "achieved": "10000.00", "pending": "5000.00", "percent": "66.7", "status": "track",
              "today": {"date": "2026-09-18", "target": "384.62", "achieved": "120.00", "pending": "264.62",
                        "met": False},
              "days_left": 13},
    "customers": {"target": 100, "achieved": 70, "pending": 30, "percent": "70.0", "status": "track"},
}

_COMMON = (
    (401, "not_authenticated", "Sign in first.", {}),
    (403, "wrong_channel", "Not allowed on this app.", {}),
    SERVER_ERROR,
)


class OperatorTargetsView(APIView):
    """POST /api/v1/{app}/targets -- operator app only."""

    authentication_classes = [AppJWTAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["Operator Targets"], summary="This station's targets for the month",
        description="""
The home card: this station's **Collection** (sales, AED) and **New Customer**
targets for the current business month -- `target`, `achieved`, `pending`
(`target - achieved`, never below 0), `percent`, and `status`
(`met`, `track`, `behind`, `missed`, `idle`). Either is `null` when the
station has no such target this month. Only the signed-in station's.

`sales.today` is **today's target**: the month's shortfall before today spread
over the days left (today included). It is fixed when the business day
starts and **does not change during the day**; tomorrow's is worked out from
the revenue up to tonight. `today.achieved` grows as orders complete.

Achieved: completed orders booked in the month (net, less approved credit
notes); customers = unique customers on those orders.
""",
        request=envelope_request("OperatorTargetsEnvelope", EmptyTargetsRequest, request_data_required=False),
        responses=envelope_responses(
            (200, "ok", "Targets.", _OPERATOR_SAMPLE),
            (409, "device_not_mapped", "This device has no station.", {}),
            (409, "branch_inactive", "This station is closed.", {}),
            *_COMMON,
        ),
    )
    def post(self, request, app):
        if app != Channel.OPERATOR:
            return envelope("wrong_channel", "Not allowed on this app.", http_status=403)
        branch, refused = station_of(request.auth)
        if refused:
            return refused
        return envelope("ok", "Targets.", operator_targets(branch, _now()))


# -- Manager ---------------------------------------------------------------------------------


class _ManagerView(APIView):
    authentication_classes = [AppJWTAuthentication]
    permission_classes = [IsAuthenticated]
    form_class = None

    def begin(self, request, app):
        """(values, company_ids, None) or (None, None, the refusal)."""
        refused = manager_only(request, app)
        if refused:
            return None, None, refused
        _, request_data = request_parts(request)
        form = self.form_class(data=request_data)
        form.is_valid(raise_exception=True)
        return form.validated_data, list(companies_for(request.user).values_list("id", flat=True)), None


def _branches(company_ids, state_id=None):
    rows = Branch.objects.filter(company_id__in=company_ids, is_active=True).select_related("location__state")
    if state_id:
        rows = rows.filter(location__state_id=state_id)
    return list(rows)


def _company(request):
    return getattr(request.user, "company", None)


def _rows(branches, year, month, kind, now):
    """[{branch, state, figures, status}] -- stations with a target this month."""
    by_id = {b.pk: b for b in branches}
    out = []
    for row in services.overview(branches, year, month, now, kind)["rows"]:
        branch = by_id[row["target"].branch_id]
        out.append({"branch": branch, "state": branch.location.state if branch.location_id else None,
                    "figures": row["figures"], "status": row["status"]})
    return out


def manager_states(company_ids, year, month, kind, now=None):
    rows = _rows(_branches(company_ids), year, month, kind, now)
    per_state = defaultdict(list)
    for row in rows:
        per_state[row["state"]].append(row)
    states = [{"state_id": s.pk if s else None, "code": s.short_code if s else "", "name": s.name if s else "",
               **_total(items, kind)} for s, items in per_state.items()]
    states.sort(key=lambda s: s["name"].lower())
    return {"period": period_json(year, month), "kind": kind, "total": _total(rows, kind), "states": states}


def manager_branches(company_ids, state_id, year, month, kind, now=None):
    rows = _rows(_branches(company_ids, state_id), year, month, kind, now)
    state = rows[0]["state"] if rows else None
    if state is None:
        from apps.company.models import State

        state = State.objects.filter(pk=state_id).first()
    return {"period": period_json(year, month), "kind": kind,
            "state": {"state_id": state_id, "name": state.name if state else "", **_total(rows, kind)},
            "branches": [{"branch_id": r["branch"].pk, "code": r["branch"].short_code, "name": r["branch"].name,
                          **figures_json(r["figures"], kind, r["status"])} for r in rows]}


def manager_branch(target, now=None):
    view = services.month_view(target, now)
    kind = target.kind
    branch = target.branch
    days = [{"date": d["date"].isoformat(), "weekday": d["date"].strftime("%a"),
             "planned": money.fmt(d["planned"]), "target": money.fmt(d["target"]),
             "achieved": money.fmt(d["achieved"]), "gap": money.fmt(d["gap"]), "status": d["status"]["key"]}
            for d in view["days"]]
    return {"period": period_json(target.period_start.year, target.period_start.month), "kind": kind,
            "branch": {"branch_id": branch.pk, "code": branch.short_code, "name": branch.name},
            **figures_json(view["figures"], kind, view["status"]),
            "business_date": view["business_today"].isoformat(), "days_left": view["days_left"],
            "today": _today_json(view["today"]) if kind == TargetKind.SALES else None, "days": days}


_FIG = {"target": "40000.00", "achieved": "28800.00", "pending": "11200.00", "percent": "72.0"}
_PERIOD = {"year": 2026, "month": 11, "from_date": "2026-11-01", "to_date": "2026-11-30", "label": "November 2026"}

_MANAGER_RULES = """
**Month:** `year` + `month` (send both, or neither for this business month).
**`kind`:** `sales` (Collection, AED, default) or `customers` (unique
customers, whole numbers). Only stations with a target that month count.
`pending` is `target - achieved`, never below 0; a total's `pending` adds up
each station's own shortfall, so a station far above its target never hides
the ones below. `status` per station: `met`, `track`, `behind`, `missed`,
`idle`. Read only -- targets are set on the web.
"""


class ManagerTargetStatesView(_ManagerView):
    """POST /api/v1/{app}/targets/states -- manager app only."""

    form_class = ManagerTargetsRequest

    @extend_schema(
        tags=["Manager Targets"], summary="Targets per state for a month",
        description="The month's total and the states breakdown." + _MANAGER_RULES,
        request=envelope_request("ManagerTargetStatesEnvelope", ManagerTargetsRequest, request_data_required=False),
        responses=envelope_responses(
            (200, "ok", "Targets.", {"period": _PERIOD, "kind": "sales",
                                     "total": {**_FIG, "branch_count": 11},
                                     "states": [{"state_id": 2, "code": "DXB", "name": "Dubai", **_FIG,
                                                 "branch_count": 5}]}),
            (400, "invalid_request", "send year and month together, or neither.",
             {"errors": {"month": "send year and month together, or neither"}}),
            *_COMMON,
        ),
    )
    def post(self, request, app):
        values, companies, refused = self.begin(request, app)
        if refused:
            return refused
        year, month = month_of(values, _company(request))
        return envelope("ok", "Targets.", manager_states(companies, year, month, values["kind"], _now()))


class ManagerTargetBranchesView(_ManagerView):
    """POST /api/v1/{app}/targets/branches -- manager app only."""

    form_class = ManagerStateTargetsRequest

    @extend_schema(
        tags=["Manager Targets"], summary="One state's stations and their targets",
        description="The state's total and every station of it with a target that month." + _MANAGER_RULES,
        request=envelope_request("ManagerTargetBranchesEnvelope", ManagerStateTargetsRequest),
        responses=envelope_responses(
            (200, "ok", "Targets.", {"period": _PERIOD, "kind": "sales",
                                     "state": {"state_id": 2, "name": "Dubai", **_FIG, "branch_count": 5},
                                     "branches": [{"branch_id": 27, "code": "CPG1", "name": "CREEK PARK GATE 1",
                                                   "target": "15000.00", "achieved": "10800.00",
                                                   "pending": "4200.00", "percent": "72.0", "status": "track"}]}),
            (400, "invalid_request", "state_id is required.", {"errors": {"state_id": "is required"}}),
            *_COMMON,
        ),
    )
    def post(self, request, app):
        values, companies, refused = self.begin(request, app)
        if refused:
            return refused
        year, month = month_of(values, _company(request))
        return envelope("ok", "Targets.",
                        manager_branches(companies, values["state_id"], year, month, values["kind"], _now()))


class ManagerTargetBranchView(_ManagerView):
    """POST /api/v1/{app}/targets/branch -- manager app only."""

    form_class = ManagerBranchTargetRequest

    @extend_schema(
        tags=["Manager Targets"], summary="One station's month",
        description="""
One station's target for the month. For `sales`, `days` lists every day:
`planned` (the even split), `target` (the day's target -- fixed at the day's
start from the revenue before it; the plan for a day not reached yet),
`achieved` and `gap` (`null` for a day not reached), and `status`
(`met`, `short`, `live`, `idle`). `customers` has no days.
""" + _MANAGER_RULES,
        request=envelope_request("ManagerTargetBranchEnvelope", ManagerBranchTargetRequest),
        responses=envelope_responses(
            (200, "ok", "Target.", {"period": _PERIOD, "kind": "sales",
                                    "branch": {"branch_id": 27, "code": "CPG1", "name": "CREEK PARK GATE 1"},
                                    **_FIG, "status": "track", "business_date": "2026-11-18", "days_left": 13,
                                    "today": {"date": "2026-11-18", "target": "861.54", "achieved": "300.00",
                                              "pending": "561.54", "met": False},
                                    "days": [{"date": "2026-11-01", "weekday": "Sun", "planned": "1333.33",
                                              "target": "1333.33", "achieved": "1500.00", "gap": "166.67",
                                              "status": "met"}]}),
            (404, "no_target", "That station has no such target that month.", {}),
            *_COMMON,
        ),
    )
    def post(self, request, app):
        values, companies, refused = self.begin(request, app)
        if refused:
            return refused
        year, month = month_of(values, _company(request))
        first, _ = services.month_bounds(year, month)
        target = (Target.objects.filter(company_id__in=companies, branch_id=values["branch_id"],
                                        kind=values["kind"], period_start=first)
                  .select_related("company", "branch").first())
        if target is None:
            return envelope("no_target", "That station has no such target that month.", http_status=404)
        return envelope("ok", "Target.", manager_branch(target, _now()))
