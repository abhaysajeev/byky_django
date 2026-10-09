"""The manager app's masters and reports -- views only translate HTTP; the
queries are apps/rental/reports.py.

POST /api/v1/{app}/states                        states the company has stations in
POST /api/v1/{app}/branches                      its active stations, optionally by state
POST /api/v1/{app}/reports/collections/states    settled sales per state for a date range
POST /api/v1/{app}/reports/collections/branches  ... per station of one state
POST /api/v1/{app}/reports/orders                one station's orders booked in a date range

Manager app only. Any signed-in manager may call them: the manager's token is
the permission (owner, 9 Oct 2026). Every figure is the manager's own
company's.
"""

from drf_spectacular.utils import extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.company.scoping import companies_for
from apps.portal.authentication import AppJWTAuthentication
from apps.rental import reports
from apps.rental.serializers import (
    BranchOrdersRequest,
    CollectionsByBranchRequest,
    CollectionsByStateRequest,
    ManagerBranchesRequest,
    ManagerStatesRequest,
)
from core.api import envelope, manager_only, request_parts
from core.schema import SERVER_ERROR, envelope_request, envelope_responses
from core.timezones import zone_for

_COMMON = (
    (401, "not_authenticated", "Sign in first.", {}),
    (403, "wrong_channel", "Not allowed on this app.", {}),
    SERVER_ERROR,
)
_DATES_INVALID = (400, "invalid_request", "to_date must not be before from_date.",
                  {"errors": {"to_date": "must not be before from_date"}})

_DATES = """
**Dates:** `from_date` and `to_date`, both required, `YYYY-MM-DD`, company
time, **both days included**; `to_date` must not be before `from_date`. No
limit on the range.
"""

_COLLECTIONS = """
**Collections are settled orders:** each order counts once, with its
invoice's `net_amount`, on the day it was **settled**. Running and cancelled
orders are not collections. `credit_notes` is the total of approved credit
notes on those orders -- shown beside the net, **not** subtracted. Money is
3-decimal strings.
""" + _DATES


class _ManagerView(APIView):
    authentication_classes = [AppJWTAuthentication]
    permission_classes = [IsAuthenticated]
    form_class = None

    def values(self, request, app):
        """(values, company_ids, zone, None) or (None, None, None, the refusal)."""
        refused = manager_only(request, app)
        if refused:
            return None, None, None, refused
        _, request_data = request_parts(request)
        form = self.form_class(data=request_data)
        form.is_valid(raise_exception=True)
        companies = list(companies_for(request.user).values_list("id", flat=True))
        return form.validated_data, companies, zone_for(getattr(request.user, "company", None)), None


# -- Masters ------------------------------------------------------------------------

_STATES_SAMPLE = {"states": [
    {"state_id": 2, "code": "DXB", "name": "Dubai", "branch_count": 6},
    {"state_id": 1, "code": "AUH", "name": "Abu Dhabi", "branch_count": 5},
]}
_BRANCHES_SAMPLE = {"branches": [
    {"branch_id": 3, "code": "ADC1", "name": "Abu Dhabi Corniche 1", "state_id": 1, "state": "Abu Dhabi",
     "location": "Corniche"},
]}


class StatesView(_ManagerView):
    """POST /api/v1/{app}/states -- manager app only."""

    form_class = ManagerStatesRequest

    @extend_schema(
        tags=["Manager Masters"],
        summary="States the company has stations in",
        description="Every state (emirate) with at least one **active** station of your company, by name, "
                    "with how many. `request_data` may be empty.",
        request=envelope_request("ManagerStatesEnvelope", ManagerStatesRequest, request_data_required=False),
        responses=envelope_responses((200, "ok", "States.", _STATES_SAMPLE), *_COMMON),
    )
    def post(self, request, app):
        _, companies, _, refused = self.values(request, app)
        if refused:
            return refused
        return envelope("ok", "States.", {"states": reports.states(companies)})


class BranchesView(_ManagerView):
    """POST /api/v1/{app}/branches -- manager app only."""

    form_class = ManagerBranchesRequest

    @extend_schema(
        tags=["Manager Masters"],
        summary="The company's active stations",
        description="Every **active** station of your company, by name -- send `state_id` for one state's. "
                    "`branch_id` is what the reports and `requests/list` take.",
        request=envelope_request("ManagerBranchesEnvelope", ManagerBranchesRequest, request_data_required=False),
        responses=envelope_responses(
            (200, "ok", "Branches.", _BRANCHES_SAMPLE),
            (400, "invalid_request", "state_id must be a whole number.",
             {"errors": {"state_id": "must be a whole number"}}),
            *_COMMON,
        ),
    )
    def post(self, request, app):
        values, companies, _, refused = self.values(request, app)
        if refused:
            return refused
        return envelope("ok", "Branches.", {"branches": reports.branches(companies, values.get("state_id"))})


# -- Collections ----------------------------------------------------------------------

_TOTAL = {"orders": 1033, "net_amount": "45210.250", "credit_notes": "120.000"}
_BY_STATE_SAMPLE = {
    "from_date": "2026-10-01", "to_date": "2026-10-08",
    "states": [
        {"state_id": 2, "code": "DXB", "name": "Dubai", "orders": 412, "net_amount": "18450.500",
         "credit_notes": "70.000"},
        {"state_id": 9, "code": "FUJ", "name": "Fujairah", "orders": 0, "net_amount": "0.000",
         "credit_notes": "0.000"},
    ],
    "total": _TOTAL,
}
_BY_BRANCH_SAMPLE = {
    "from_date": "2026-10-01", "to_date": "2026-10-08", "state_id": 2,
    "branches": [
        {"branch_id": 7, "code": "CPG1", "name": "Creek Park Gate 1", "orders": 128, "net_amount": "5320.000",
         "credit_notes": "0.000"},
    ],
    "total": {"orders": 412, "net_amount": "18450.500", "credit_notes": "70.000"},
}


class CollectionsByStateView(_ManagerView):
    """POST /api/v1/{app}/reports/collections/states -- manager app only."""

    form_class = CollectionsByStateRequest

    @extend_schema(
        tags=["Manager Reports"],
        summary="Collections per state",
        description="Settled sales per state, highest first, and the grand `total`. Every state your company "
                    "has stations in is listed -- 0 when it sold nothing." + _COLLECTIONS,
        request=envelope_request("CollectionsByStateEnvelope", CollectionsByStateRequest),
        responses=envelope_responses((200, "ok", "Collections.", _BY_STATE_SAMPLE), _DATES_INVALID, *_COMMON),
    )
    def post(self, request, app):
        values, companies, zone, refused = self.values(request, app)
        if refused:
            return refused
        rows, total = reports.collections_by_state(companies, values["from_date"], values["to_date"], zone)
        return envelope("ok", "Collections.", {
            "from_date": values["from_date"].isoformat(), "to_date": values["to_date"].isoformat(),
            "states": rows, "total": total})


class CollectionsByBranchView(_ManagerView):
    """POST /api/v1/{app}/reports/collections/branches -- manager app only."""

    form_class = CollectionsByBranchRequest

    @extend_schema(
        tags=["Manager Reports"],
        summary="Collections per station of one state",
        description="Settled sales per station of the state `state_id`, highest first, and the state's "
                    "`total`. Every active station is listed (0 when it sold nothing); a closed station only "
                    "when it sold in the range. A state with no stations of yours answers an empty list."
                    + _COLLECTIONS,
        request=envelope_request("CollectionsByBranchEnvelope", CollectionsByBranchRequest),
        responses=envelope_responses(
            (200, "ok", "Collections.", _BY_BRANCH_SAMPLE),
            (400, "invalid_request", "state_id is required.", {"errors": {"state_id": "is required"}}),
            _DATES_INVALID, *_COMMON,
        ),
    )
    def post(self, request, app):
        values, companies, zone, refused = self.values(request, app)
        if refused:
            return refused
        rows, total = reports.collections_by_branch(companies, values["state_id"], values["from_date"],
                                                    values["to_date"], zone)
        return envelope("ok", "Collections.", {
            "from_date": values["from_date"].isoformat(), "to_date": values["to_date"].isoformat(),
            "state_id": values["state_id"], "branches": rows, "total": total})


# -- Order details ------------------------------------------------------------------------

_ORDER_SAMPLE = {
    "order_id": "01923e1c-0a11-7b22-8c33-d4e5f6a7b8c9", "order_no": "ABCO1105700000006",
    "station_name": "Creek Park Gate 1", "customer_name": "Ahmed Al Mansoori", "customer_mobile": "971501234567",
    "booked_at": "2026-10-08 08:58:10", "start_time": "2026-10-08 09:00:00", "end_time": "2026-10-08 11:00:00",
    "duration_minutes": 120, "order_status": "partially_received", "amount": "250.000",
    "advance_paid": "100.000", "invoice_no": None,
    "credit_note": {"credit_note_no": "ABCO1105700000004CN", "status": "approved", "net_amount": "20.000"},
    "vehicles": [
        {"vehicle_name": "QUAD-04", "vehicle_type": "Quad", "vehicle_status": "running",
         "start_time": "2026-10-08 09:00:00", "end_time": None, "minutes": 95},
        {"vehicle_name": "BUGGY-12", "vehicle_type": "Buggy", "vehicle_status": "returned",
         "start_time": "2026-10-08 09:00:00", "end_time": "2026-10-08 10:05:00", "minutes": 65},
    ],
    "payments": [
        {"mode": "Cash", "kind": "advance", "amount": "100.000", "reference_no": "",
         "paid_at": "2026-10-08 08:58:10"},
    ],
}
_ORDERS_SAMPLE = {
    "branch_id": 7, "branch_name": "Creek Park Gate 1", "from_date": "2026-10-01", "to_date": "2026-10-08",
    "order_status": "all",
    "summary": {"all": 15, "running": 4, "partially_received": 2, "awaiting_settlement": 1,
                "fully_received": 8, "cancelled": 0, "replaced": 1},
    "orders": [_ORDER_SAMPLE], "page": 1, "pages": 1, "total_orders": 15,
}

_ORDERS_DESCRIPTION = """
One station's orders **booked** in the date range -- running, settled and
cancelled -- newest first, 50 a page (`page`, from 1), with a `summary` of the
whole range (not just this page).

**`order_status`** (each order; also the filter, default `all`):
- `running` -- no vehicle back yet;
- `partially_received` -- some vehicles back, some still out;
- `awaiting_settlement` -- every vehicle back, the bill not settled yet;
- `fully_received` -- settled;
- `cancelled`.

`summary.replaced` counts orders with a replaced vehicle -- it overlaps the
others. Per order: `end_time` is the last vehicle back once settled, else the
latest expected end; `amount` is the settled net, or what the lines come to so
far (back at their total, out at their package price); `advance_paid` is the
money held now; `credit_note` its waiting or issued credit note. `vehicles`
lists every line, replaced and removed ones too (`vehicle_status` `running`,
`returned`, `replaced`, `removed`, `cancelled`), with `minutes` run so far.
`payments` are as taken: `advance`, `settlement`, `refund`.
""" + _DATES


class BranchOrdersView(_ManagerView):
    """POST /api/v1/{app}/reports/orders -- manager app only."""

    form_class = BranchOrdersRequest

    @extend_schema(
        tags=["Manager Reports"],
        summary="One station's orders, with a status summary",
        description=_ORDERS_DESCRIPTION,
        request=envelope_request("BranchOrdersEnvelope", BranchOrdersRequest),
        responses=envelope_responses(
            (200, "ok", "Orders.", _ORDERS_SAMPLE),
            (400, "invalid_request", "branch_id is required.", {"errors": {"branch_id": "is required"}}),
            _DATES_INVALID,
            (404, "unknown_branch", "No station with that id.", {}),
            *_COMMON,
        ),
    )
    def post(self, request, app):
        values, companies, zone, refused = self.values(request, app)
        if refused:
            return refused
        found = reports.branch_orders(companies, values["branch_id"], values["from_date"], values["to_date"],
                                      zone, status=values["order_status"], page=values["page"])
        if found is None:
            return envelope("unknown_branch", "No station with that id.", http_status=404)
        branch, summary, rows, page = found
        return envelope("ok", "Orders.", {
            "branch_id": branch.pk, "branch_name": branch.name,
            "from_date": values["from_date"].isoformat(), "to_date": values["to_date"].isoformat(),
            "order_status": values["order_status"], "summary": summary, "orders": rows,
            "page": page.number, "pages": page.paginator.num_pages, "total_orders": page.paginator.count,
        })
