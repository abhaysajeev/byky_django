"""Targets for the device apps (owner, 18 Nov 2026) -- read only; targets are
set on the web.

POST /api/v1/operator/targets             this station's month: sales + customers + today,
                                          and `profiles`: every target active today
POST /api/v1/manager/targets/states       the month per state, sales or customers
POST /api/v1/manager/targets/branches     one state's stations
POST /api/v1/manager/targets/branch       one station's month, with its days (sales)
POST /api/v1/manager/targets/profiles     the target profiles running in a month
POST /api/v1/manager/targets/target       one target of any profile, by target_id

Target Profiles (design/11-target-profiles.md): the month calls keep their
meaning -- the station's *monthly* target -- unless `profile_id` (+ `date`)
picks another profile's period. Everything new is added; nothing is removed.

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
from apps.targets import periods, services
from apps.targets.models import PeriodType, ProfileStatus, Target, TargetKind, TargetProfile
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


def target_period_json(item):
    """A target's (or an incentive run's) own period: any type (11 §7)."""
    kind = getattr(item, "period_type", None) or item.profile.period_type
    return {"from_date": item.period_start.isoformat(), "to_date": item.period_end.isoformat(),
            "label": periods.period_label(kind, item.period_start, item.period_end)}


def profile_json(profile):
    return {"profile_id": profile.pk, "name": profile.name, "type": profile.period_type, "metric": profile.metric}


def target_json(target, now=None, view=None):
    """One target of any profile: figures, status, `met`, and for a multi-day
    sales target today's (frozen) figure and the days left."""
    view = view or services.month_view(target, now)
    kind = target.kind
    multi_day = kind == TargetKind.SALES and not view["card"]
    return {"target_id": target.pk, **profile_json(target.profile), "period": target_period_json(target),
            **figures_json(view["figures"], kind, view["status"]),
            "met": view["figures"].achieved >= target.amount,
            "today": _today_json(view["today"]) if multi_day else None,
            "days_left": view["days_left"]}


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


class ProfileFields(MonthFields):
    """Optional `profile_id` (+ `date`, a day inside the wanted period; today
    when left out): another profile's period instead of the month's monthly
    target. `kind` is then the profile's metric."""

    profile_id = whole_number_field(min_value=1, required=False)
    date = serializers.DateField(required=False, error_messages={**REQUIRED, "invalid": "must be YYYY-MM-DD"})


class ManagerTargetsRequest(ProfileFields):
    kind = KIND


class ManagerStateTargetsRequest(ProfileFields):
    kind = KIND
    state_id = whole_number_field(min_value=1)


class ManagerBranchTargetRequest(ProfileFields):
    kind = KIND
    branch_id = whole_number_field(min_value=1)


class ManagerProfilesRequest(MonthFields):
    """The profiles running in a month (both left out: this month)."""


class ManagerTargetRequest(serializers.Serializer):
    target_id = whole_number_field(min_value=1)


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
    # Every target active today, of every profile (11 §7): the monthly ones
    # above again, plus specials, campaigns and the year.
    active = sorted(services.targets_on([branch.pk], today), key=lambda t: (
        _TYPE_ORDER.get(t.period_type, 9), t.kind, t.profile.name.lower()))
    out["profiles"] = [target_json(t, now) for t in active]
    return out


_TYPE_ORDER = {PeriodType.MONTHLY: 0, PeriodType.ANNUAL: 1, PeriodType.CUSTOM: 2, PeriodType.DAILY: 3}


_OPERATOR_SAMPLE = {
    "branch": {"branch_id": 27, "code": "CPG1", "name": "CREEK PARK GATE 1", "state": "Dubai"},
    "period": {"year": 2026, "month": 9, "from_date": "2026-09-01", "to_date": "2026-09-30", "label": "September 2026"},
    "business_date": "2026-09-18",
    "sales": {"target": "15000.00", "achieved": "10000.00", "pending": "5000.00", "percent": "66.7", "status": "track",
              "today": {"date": "2026-09-18", "target": "384.62", "achieved": "120.00", "pending": "264.62",
                        "met": False},
              "days_left": 13},
    "customers": {"target": 100, "achieved": 70, "pending": 30, "percent": "70.0", "status": "track"},
    "profiles": [
        {"target_id": 412, "profile_id": 3, "name": "Monthly sales", "type": "monthly", "metric": "sales",
         "period": {"from_date": "2026-09-01", "to_date": "2026-09-30", "label": "September 2026"},
         "target": "15000.00", "achieved": "10000.00", "pending": "5000.00", "percent": "66.7", "status": "track",
         "met": False, "today": {"date": "2026-09-18", "target": "384.62", "achieved": "120.00",
                                 "pending": "264.62", "met": False}, "days_left": 13},
        {"target_id": 518, "profile_id": 9, "name": "National Day special", "type": "daily", "metric": "sales",
         "period": {"from_date": "2026-09-18", "to_date": "2026-09-18", "label": "18 Sep 2026"},
         "target": "2000.00", "achieved": "120.00", "pending": "1880.00", "percent": "6.0", "status": "live",
         "met": False, "today": None, "days_left": 1},
    ],
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

**`profiles`** (added with Target Profiles): every target of this station
active today, of any profile -- the monthly ones above again, plus one-day
specials, campaign ranges and the financial year. Profiles are independent:
the same orders count toward each. Per item: `type` (`daily`, `monthly`,
`custom`, `annual`), `metric` (`sales`, `customers`), `period`, the figures,
`status`, `met`, and for a multi-day sales target `today` (fixed at the day's
start) and `days_left`; `today` is `null` for a one-day or customer target.
`status` of a one-day target: `met`, `live` (today, not met yet), `short`
(ended, not met), `idle`.
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


def _profile(values, company_ids):
    """(profile, day) asked for, or (None, None). Unknown profile -> None profile
    with day False, so the caller can refuse it."""
    if not values.get("profile_id"):
        return None, None
    profile = TargetProfile.objects.filter(pk=values["profile_id"], company_id__in=company_ids).first()
    return profile, values.get("date")


def _rows(branches, year, month, kind, now, profile=None, day=None):
    """[{branch, state, figures, status}] -- stations with a target this month,
    or in the chosen profile's period containing `day`."""
    by_id = {b.pk: b for b in branches}
    out = []
    if profile is not None:
        found = services.profile_overview(profile, branches, day or services.today_for(profile.company, now), now)
    else:
        found = services.overview(branches, year, month, now, kind)
    for row in found["rows"]:
        branch = by_id[row["target"].branch_id]
        out.append({"branch": branch, "state": branch.location.state if branch.location_id else None,
                    "figures": row["figures"], "status": row["status"], "target": row["target"]})
    return out


def _period_for(rows, profile, year, month):
    if profile is not None and rows:
        return target_period_json(rows[0]["target"])
    return period_json(year, month)


def manager_states(company_ids, year, month, kind, now=None, profile=None, day=None):
    rows = _rows(_branches(company_ids), year, month, kind, now, profile, day)
    kind = profile.metric if profile else kind
    per_state = defaultdict(list)
    for row in rows:
        per_state[row["state"]].append(row)
    states = [{"state_id": s.pk if s else None, "code": s.short_code if s else "", "name": s.name if s else "",
               **_total(items, kind)} for s, items in per_state.items()]
    states.sort(key=lambda s: s["name"].lower())
    out = {"period": _period_for(rows, profile, year, month), "kind": kind, "total": _total(rows, kind),
           "states": states}
    if profile is not None:
        out["profile"] = profile_json(profile)
    return out


def manager_branches(company_ids, state_id, year, month, kind, now=None, profile=None, day=None):
    rows = _rows(_branches(company_ids, state_id), year, month, kind, now, profile, day)
    kind = profile.metric if profile else kind
    state = rows[0]["state"] if rows else None
    if state is None:
        from apps.company.models import State

        state = State.objects.filter(pk=state_id).first()
    out = {"period": _period_for(rows, profile, year, month), "kind": kind,
           "state": {"state_id": state_id, "name": state.name if state else "", **_total(rows, kind)},
           "branches": [{"branch_id": r["branch"].pk, "code": r["branch"].short_code, "name": r["branch"].name,
                         "target_id": r["target"].pk, **figures_json(r["figures"], kind, r["status"])}
                        for r in rows]}
    if profile is not None:
        out["profile"] = profile_json(profile)
    return out


def manager_branch(target, now=None):
    view = services.month_view(target, now)
    kind = target.kind
    branch = target.branch
    days = [{"date": d["date"].isoformat(), "weekday": d["date"].strftime("%a"),
             "planned": money.fmt(d["planned"]), "target": money.fmt(d["target"]),
             "achieved": money.fmt(d["achieved"]), "gap": money.fmt(d["gap"]), "status": d["status"]["key"]}
            for d in view["days"]]
    monthly = target.period_type == PeriodType.MONTHLY
    period = period_json(target.period_start.year, target.period_start.month) if monthly \
        else target_period_json(target)
    return {"period": period, "kind": kind, "target_id": target.pk, "profile": profile_json(target.profile),
            "card": view["card"],
            "branch": {"branch_id": branch.pk, "code": branch.short_code, "name": branch.name},
            **figures_json(view["figures"], kind, view["status"]),
            "met": view["figures"].achieved >= target.amount,
            "business_date": view["business_today"].isoformat(), "days_left": view["days_left"],
            "today": _today_json(view["today"]) if kind == TargetKind.SALES and not view["card"] else None,
            "days": days}


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

**Target Profiles:** without `profile_id` these calls read each station's
**monthly** target of the month, exactly as before. Send `profile_id` (from
`targets/profiles`) and optionally `date` (a day inside the wanted period;
today when left out) to read that profile's period instead -- `kind` is then
the profile's metric, `period` is that period, and `profile` is added.
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
        profile, day = _profile(values, companies)
        if values.get("profile_id") and profile is None:
            return envelope("no_profile", "No such target profile.", http_status=404)
        return envelope("ok", "Targets.", manager_states(companies, year, month, values["kind"], _now(), profile, day))


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
        profile, day = _profile(values, companies)
        if values.get("profile_id") and profile is None:
            return envelope("no_profile", "No such target profile.", http_status=404)
        return envelope("ok", "Targets.", manager_branches(companies, values["state_id"], year, month,
                                                           values["kind"], _now(), profile, day))


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
        profile, day = _profile(values, companies)
        if values.get("profile_id") and profile is None:
            return envelope("no_profile", "No such target profile.", http_status=404)
        found = Target.objects.filter(company_id__in=companies, branch_id=values["branch_id"])
        if profile is not None:
            day = day or services.today_for(profile.company, _now())
            found = found.filter(profile=profile, period_start__lte=day, period_end__gte=day)
        else:
            found = found.filter(kind=values["kind"], period_type=PeriodType.MONTHLY, period_start=first)
        target = found.select_related("company", "branch", "profile").first()
        if target is None:
            return envelope("no_target", "That station has no such target that month.", http_status=404)
        return envelope("ok", "Target.", manager_branch(target, _now()))


def manager_profiles(company_ids, year, month, now=None):
    """The profiles with a target in the month, each with its periods touching
    it and its station count."""
    first, last = services.month_bounds(year, month)
    found = (TargetProfile.objects.filter(company_id__in=company_ids, starts_on__lte=last, ends_on__gte=first)
             .exclude(status=ProfileStatus.DRAFT).order_by("name"))
    out = []
    for profile in found:
        spans = [(a, b) for a, b in periods.profile_periods(profile) if a <= last and b >= first]
        stations = (Target.objects.filter(profile=profile, period_start__lte=last, period_end__gte=first)
                    .values("branch_id").distinct().count())
        if not stations:
            continue
        out.append({**profile_json(profile), "status": profile.status, "branch_count": stations,
                    "periods": [{"from_date": a.isoformat(), "to_date": b.isoformat(),
                                 "label": periods.period_label(profile.period_type, a, b)} for a, b in spans]})
    out.sort(key=lambda p: (_TYPE_ORDER.get(p["type"], 9), p["name"].lower()))
    return {"period": period_json(year, month), "profiles": out}


_PROFILE_SAMPLE = {"profile_id": 9, "name": "National Day special", "type": "daily", "metric": "sales"}


class ManagerTargetProfilesView(_ManagerView):
    """POST /api/v1/{app}/targets/profiles -- manager app only."""

    form_class = ManagerProfilesRequest

    @extend_schema(
        tags=["Manager Targets"], summary="Target profiles running in a month",
        description="""
Every target profile with at least one station target touching the month
(`year` + `month`, or neither for this month): its `type` (`daily`,
`monthly`, `custom`, `annual`), `metric`, how many stations, and its periods
that touch the month. Pass a `profile_id` (and a `date` from a period) to
`targets/states`, `targets/branches` or `targets/branch` to read it.
""",
        request=envelope_request("ManagerTargetProfilesEnvelope", ManagerProfilesRequest,
                                 request_data_required=False),
        responses=envelope_responses(
            (200, "ok", "Profiles.", {"period": _PERIOD, "profiles": [
                {**_PROFILE_SAMPLE, "status": "active", "branch_count": 4,
                 "periods": [{"from_date": "2026-12-02", "to_date": "2026-12-02", "label": "2 Dec 2026"}]}]}),
            *_COMMON,
        ),
    )
    def post(self, request, app):
        values, companies, refused = self.begin(request, app)
        if refused:
            return refused
        year, month = month_of(values, _company(request))
        return envelope("ok", "Profiles.", manager_profiles(companies, year, month, _now()))


class ManagerTargetView(_ManagerView):
    """POST /api/v1/{app}/targets/target -- manager app only."""

    form_class = ManagerTargetRequest

    @extend_schema(
        tags=["Manager Targets"], summary="One target of any profile",
        description="""
One target by `target_id` (from `targets/branches` or the operator's
`profiles`), in the same shape as `targets/branch`. A one-day target
(`card: true`) has no `days` and no `today`: just target, achieved,
`pending` (still to achieve), `status` and `met`.
""",
        request=envelope_request("ManagerTargetEnvelope", ManagerTargetRequest),
        responses=envelope_responses(
            (200, "ok", "Target.", {"period": {"from_date": "2026-12-02", "to_date": "2026-12-02",
                                               "label": "2 Dec 2026"},
                                    "kind": "sales", "target_id": 518, "profile": _PROFILE_SAMPLE, "card": True,
                                    "branch": {"branch_id": 27, "code": "CPG1", "name": "CREEK PARK GATE 1"},
                                    "target": "2000.00", "achieved": "2150.00", "pending": "0.00",
                                    "percent": "107.5", "status": "met", "met": True,
                                    "business_date": "2026-12-02", "days_left": 1, "today": None, "days": []}),
            (404, "no_target", "No such target.", {}),
            *_COMMON,
        ),
    )
    def post(self, request, app):
        values, companies, refused = self.begin(request, app)
        if refused:
            return refused
        target = (Target.objects.filter(company_id__in=companies, pk=values["target_id"])
                  .select_related("company", "branch", "profile").first())
        if target is None:
            return envelope("no_target", "No such target.", http_status=404)
        return envelope("ok", "Target.", manager_branch(target, _now()))
