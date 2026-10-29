"""The manager app's masters and reports -- views only translate HTTP; the
queries are apps/rental/reports.py.

POST /api/v1/{app}/states                        states the company has stations in
POST /api/v1/{app}/branches                      its active stations, optionally by state
POST /api/v1/{app}/reports/collections/states    settled sales per state for a date range
POST /api/v1/{app}/reports/collections/branches  ... per station of one state
POST /api/v1/{app}/reports/orders                one station's orders booked in a date range
POST /api/v1/{app}/credit-notes/list|approve|reject          credit-note requests (approvals.py)
POST /api/v1/{app}/card-discounts/claims/list|approve|reject card-discount requests
POST /api/v1/{app}/requests/pending-counts                   badge counts of everything waiting

Manager app only. Any signed-in manager may call them: the manager's token is
the permission (owner, 9 Oct 2026). Every figure is the manager's own
company's.
"""

from drf_spectacular.utils import extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.company.scoping import companies_for
from apps.discount import services as discount_services
from apps.discount.models import CardDiscountClaim
from apps.portal.authentication import AppJWTAuthentication
from apps.rental import approvals, credit_notes, reports
from apps.rental.models import DecisionChannel
from apps.rental.serializers import (
    BranchOrdersRequest,
    CollectionsByBranchRequest,
    CollectionsByStateRequest,
    ManagerApprovalListRequest,
    ManagerBranchesRequest,
    ManagerClaimDecisionRequest,
    ManagerCreditNoteApproveRequest,
    ManagerCreditNoteRejectRequest,
    ManagerPendingCountsRequest,
    ManagerStatesRequest,
    ManagerVehiclesRequest,
    ManagerVehicleTypesRequest,
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
_VEHICLE_TYPES_SAMPLE = {"vehicle_types": [
    {"vehicle_type_id": 4, "name": "Monaco", "category_id": 1, "category": "BYKY"},
]}
_VEHICLES_SAMPLE = {
    "vehicles": [
        {"vehicle_id": 41, "vehicle_name": "MO 41", "identifier": "VB0041", "vehicle_type_id": 4,
         "vehicle_type": "Monaco"},
    ],
    "page": 1, "pages": 1, "total_vehicles": 1,
}
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


class VehicleTypesView(_ManagerView):
    """POST /api/v1/{app}/vehicle-types -- manager app only."""

    form_class = ManagerVehicleTypesRequest

    @extend_schema(
        tags=["Manager Masters"],
        summary="The company's vehicle types",
        description="Every **active, approved** vehicle type of your company, by name -- the master, not "
                    "tied to a station. Send `category_id` for one category's. `vehicle_type_id` is what "
                    "`reports/orders` and `vehicles/list` take. An unknown `category_id` answers an empty "
                    "list. `request_data` may be empty.",
        request=envelope_request("ManagerVehicleTypesEnvelope", ManagerVehicleTypesRequest,
                                 request_data_required=False),
        responses=envelope_responses(
            (200, "ok", "Vehicle types.", _VEHICLE_TYPES_SAMPLE),
            (400, "invalid_request", "category_id must be a whole number.",
             {"errors": {"category_id": "must be a whole number"}}),
            *_COMMON,
        ),
    )
    def post(self, request, app):
        values, companies, _, refused = self.values(request, app)
        if refused:
            return refused
        return envelope("ok", "Vehicle types.",
                        {"vehicle_types": reports.vehicle_types(companies, values.get("category_id"))})


class VehicleListView(_ManagerView):
    """POST /api/v1/{app}/vehicles/list -- manager app only. (Not `vehicles`:
    that is the operator app's vehicles at its station.)"""

    form_class = ManagerVehiclesRequest

    @extend_schema(
        tags=["Manager Masters"],
        summary="The company's vehicles",
        description="Every **active, approved** vehicle of your company -- the master: no station, no "
                    "rental state. By name, 50 a page (`page`, from 1). Send `vehicle_type_id` for one "
                    "type's, `search` (up to 50 characters) to match the name, code or identifier "
                    "(`VB0041`), ignoring case. `vehicle_id` is what `reports/orders` takes. An unknown "
                    "`vehicle_type_id` answers an empty list. `request_data` may be empty.",
        request=envelope_request("ManagerVehiclesEnvelope", ManagerVehiclesRequest, request_data_required=False),
        responses=envelope_responses(
            (200, "ok", "Vehicles.", _VEHICLES_SAMPLE),
            (400, "invalid_request", "vehicle_type_id must be a whole number.",
             {"errors": {"vehicle_type_id": "must be a whole number"}}),
            *_COMMON,
        ),
    )
    def post(self, request, app):
        values, companies, _, refused = self.values(request, app)
        if refused:
            return refused
        rows, page = reports.vehicles(companies, values.get("vehicle_type_id"),
                                      (values.get("search") or "").strip(), values["page"])
        return envelope("ok", "Vehicles.", {"vehicles": rows, "page": page.number,
                                            "pages": page.paginator.num_pages,
                                            "total_vehicles": page.paginator.count})


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
         "credit_notes": "0.000", "card_payment": "2200.000", "cash_payment": "3120.000"},
    ],
    "total": {"orders": 412, "net_amount": "18450.500", "credit_notes": "70.000",
              "total_card_payment": "7350.250", "total_cash_payment": "11100.250"},
}
_BY_STATE_AND_BRANCH_SAMPLE = {
    "from_date": "2026-10-01", "to_date": "2026-10-08", "state_id": None,
    "states": [
        {"state_id": 2, "code": "DXB", "name": "Dubai",
         "branches": _BY_BRANCH_SAMPLE["branches"], "summary": _BY_BRANCH_SAMPLE["total"]},
        {"state_id": 1, "code": "AUH", "name": "Abu Dhabi",
         "branches": [
             {"branch_id": 3, "code": "ADC1", "name": "Abu Dhabi Corniche 1", "orders": 96,
              "net_amount": "4100.000", "credit_notes": "50.000", "card_payment": "1500.000",
              "cash_payment": "2600.000"},
         ],
         "summary": {"orders": 96, "net_amount": "4100.000", "credit_notes": "50.000",
                     "total_card_payment": "1500.000", "total_cash_payment": "2600.000"}},
    ],
    "total": {"orders": 508, "net_amount": "22550.500", "credit_notes": "120.000",
              "total_card_payment": "8850.250", "total_cash_payment": "13700.250"},
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
        summary="Collections per station (one state, or all grouped by state)",
        description="Settled sales per station, highest first. `state_id` is optional, and decides the "
                    "shape:\n"
                    "- **sent** -- that state's stations as a flat `branches` list, and their `total` "
                    "(unchanged);\n"
                    "- **left out** -- **every** station of your company, grouped under its state: "
                    "`states[]`, each with `state_id`, `code`, `name`, its `branches` (highest first) and "
                    "its `summary`; states highest first; then the grand `total`. `state_id` comes back "
                    "`null`. A state's `summary` has the same keys as `total`.\n\n"
                    "Every active station is listed (0 when it sold nothing); a closed station only when it "
                    "sold in the range; a state only when it has a listed station. A state with no stations "
                    "of yours answers an empty list.\n\n"
                    "**`card_payment` / `cash_payment`** -- what the station's settled orders in the range "
                    "were paid by card and by cash: advance and settlement, less refunds. A payment mode "
                    "is matched by its name containing \"card\" / \"cash\"; other modes (e.g. cheque) are "
                    "in neither. `total_card_payment` / `total_cash_payment` add them up." + _COLLECTIONS,
        request=envelope_request("CollectionsByBranchEnvelope", CollectionsByBranchRequest),
        responses=envelope_responses(
            (200, "ok", "Collections.", _BY_BRANCH_SAMPLE, "ok (state_id sent)"),
            (200, "ok", "Collections.", _BY_STATE_AND_BRANCH_SAMPLE, "ok (no state_id: grouped by state)"),
            (400, "invalid_request", "state_id must be a whole number.",
             {"errors": {"state_id": "must be a whole number"}}),
            _DATES_INVALID, *_COMMON,
        ),
    )
    def post(self, request, app):
        values, companies, zone, refused = self.values(request, app)
        if refused:
            return refused
        state_id = values.get("state_id")
        dates = {"from_date": values["from_date"].isoformat(), "to_date": values["to_date"].isoformat(),
                 "state_id": state_id}
        if state_id:
            rows, total = reports.collections_by_branch(companies, state_id, values["from_date"],
                                                        values["to_date"], zone)
            return envelope("ok", "Collections.", {**dates, "branches": rows, "total": total})
        states, total = reports.collections_by_state_and_branch(companies, values["from_date"],
                                                                values["to_date"], zone)
        return envelope("ok", "Collections.", {**dates, "states": states, "total": total})


# -- Order details ------------------------------------------------------------------------

_ORDER_SAMPLE = {
    "order_id": "01923e1c-0a11-7b22-8c33-d4e5f6a7b8c9", "order_no": "ABCO1105700000006",
    "station_name": "Creek Park Gate 1", "customer_name": "Ahmed Al Mansoori", "customer_mobile": "971501234567",
    "booked_at": "2026-10-08 08:58:10", "start_time": "2026-10-08 09:00:00", "end_time": "2026-10-08 11:00:00",
    "duration_minutes": 120, "order_status": "partially_received", "amount": "250.000",
    "advance_paid": "100.000", "invoice_no": None,
    "credit_note": {"credit_note_no": "ABCO1105700000004CN", "status": "approved", "net_amount": "20.000"},
    "vehicles": [
        {"vehicle_id": 104, "vehicle_name": "QUAD-04", "vehicle_type_id": 6, "vehicle_type": "Quad",
         "vehicle_status": "running",
         "start_time": "2026-10-08 09:00:00", "end_time": None, "minutes": 95},
        {"vehicle_id": 112, "vehicle_name": "BUGGY-12", "vehicle_type_id": 7, "vehicle_type": "Buggy",
         "vehicle_status": "returned",
         "start_time": "2026-10-08 09:00:00", "end_time": "2026-10-08 10:05:00", "minutes": 65},
    ],
    "payments": [
        {"mode": "Cash", "kind": "advance", "amount": "100.000", "reference_no": "",
         "paid_at": "2026-10-08 08:58:10"},
    ],
}
_ORDERS_SAMPLE = {
    "from_date": "2026-10-01", "to_date": "2026-10-08",
    "state_id": None, "branch_id": 7, "branch_name": "Creek Park Gate 1", "vehicle_type_id": None, "vehicle_id": None,
    "order_status": "all",
    "summary": {"all": 15, "running": 4, "partially_received": 2, "awaiting_settlement": 1,
                "fully_received": 8, "cancelled": 0, "replaced": 1},
    "orders": [_ORDER_SAMPLE], "page": 1, "pages": 1, "total_orders": 15,
}

_ORDERS_DESCRIPTION = """
The orders **booked** in the date range -- running, settled and cancelled --
newest first, 50 a page (`page`, from 1), with a `summary` of the whole range
(not just this page).

**Filters, all optional** -- send none for every station of your company:
- `state_id` -- one state's stations (`states`);
- `branch_id` -- one station (`branches`); `branch_name` comes back with it,
  both `null` when not sent;
- `vehicle_type_id` -- orders with a vehicle of that type (`vehicle-types`);
- `vehicle_id` -- orders with that vehicle (`vehicles/list`).

A vehicle filter matches an order with any line on that vehicle -- replaced,
removed and cancelled lines too. The `summary` follows **every filter and the
dates**; `order_status` narrows the list only. A pair that does not match (a
station outside the state, a vehicle of another type) answers an empty list
and a zero summary. A station that is not yours is `unknown_branch`. The
filters are echoed back as sent (`null` when not).

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
`returned`, `replaced`, `removed`, `cancelled`), with `minutes` run so far,
and its `vehicle_id` and `vehicle_type_id`. `station_name` is the order's own
station.
`payments` are as taken: `advance`, `settlement`, `refund`.
""" + _DATES


class BranchOrdersView(_ManagerView):
    """POST /api/v1/{app}/reports/orders -- manager app only."""

    form_class = BranchOrdersRequest

    @extend_schema(
        tags=["Manager Reports"],
        summary="Orders by state / station / vehicle type / vehicle, with a status summary",
        description=_ORDERS_DESCRIPTION,
        request=envelope_request("BranchOrdersEnvelope", BranchOrdersRequest),
        responses=envelope_responses(
            (200, "ok", "Orders.", _ORDERS_SAMPLE),
            (400, "invalid_request", "branch_id must be a whole number.",
             {"errors": {"branch_id": "must be a whole number"}}),
            _DATES_INVALID,
            (404, "unknown_branch", "No station with that id.", {}),
            *_COMMON,
        ),
    )
    def post(self, request, app):
        values, companies, zone, refused = self.values(request, app)
        if refused:
            return refused
        filters = {key: values.get(key) for key in ("state_id", "branch_id", "vehicle_type_id", "vehicle_id")}
        found = reports.orders_report(companies, values["from_date"], values["to_date"], zone, **filters,
                                      status=values["order_status"], page=values["page"])
        if found is None:
            return envelope("unknown_branch", "No station with that id.", http_status=404)
        branch, summary, rows, page = found
        return envelope("ok", "Orders.", {
            "from_date": values["from_date"].isoformat(), "to_date": values["to_date"].isoformat(),
            "state_id": filters["state_id"], "branch_id": branch.pk if branch else None,
            "branch_name": branch.name if branch else None,
            "vehicle_type_id": filters["vehicle_type_id"], "vehicle_id": filters["vehicle_id"],
            "order_status": values["order_status"], "summary": summary, "orders": rows,
            "page": page.number, "pages": page.paginator.num_pages, "total_orders": page.paginator.count,
        })


# -- Credit notes and card-discount claims (apps/rental/approvals.py) ---------------
#
# The web's own decisions, the manager as the decider: credit_notes.approve /
# reject and discount.services.decide_claim, with channel `manager`. Whoever
# decides first -- here or on the web -- wins; the other is told it is decided.

_APPROVALS = """
**Manager app.** From **every station** of your company. Any signed-in manager
may list and decide -- the manager-app login is the permission. The decision
is the web's own, recorded against you with `decided_channel` `manager`. One
already decided -- here or on the web -- is refused.
"""

_APPROVAL_LIST = _APPROVALS + """
**This call** lists them newest first, 50 a page (`page`, from 1). Send
`"pending": true` for only those waiting, `state_id` for one state's stations,
`branch_id` for one station, and `from_date` / `to_date` (`YYYY-MM-DD`, company
time, both days included, either may be left out) for when they were asked.
"""

_CREDIT_NOTE_ROW = {
    "credit_note_id": "01a1013d-fa97-7179-93ae-3ae25842f1ce", "status": "pending", "source": "device",
    "credit_note_no": None,
    "order": {"order_id": "01923e1c-0a11-7b22-8c33-d4e5f6a7b8c9", "order_no": "ADC1019000231",
              "customer_name": "Ahmed Al Mansoori", "customer_mobile": "971501234567", "net_amount": "100.000"},
    "invoice": {"invoice_no": "ADC1019000231", "net_amount": "100.000"},
    "branch_id": 3, "station": "Abu Dhabi Corniche 1",
    "requested_by": "Rashed", "requested_at": "2026-10-08 17:05:00", "reason": "Bike broke down after 10 min",
    "net_amount": None, "tax_amount": None, "taxable_amount": None,
    "decided_by": None, "decided_channel": None, "decided_at": None, "decision_remark": "",
}
_CREDIT_NOTE_ISSUED = {
    **_CREDIT_NOTE_ROW, "status": "approved", "credit_note_no": "ADC1019000231CN",
    "net_amount": "40.000", "tax_amount": "1.905", "taxable_amount": "38.095",
    "decided_by": "Sara K", "decided_channel": "manager", "decided_at": "2026-10-08 17:20:00",
    "decision_remark": "Refund for the broken bike",
}
_CLAIM_ROW = {
    "claim_id": "01a1013d-fa97-7179-93ae-3ae25842f1cf", "status": "pending",
    "order": {"order_id": "01923e1c-0a11-7b22-8c33-d4e5f6a7b8c9", "order_no": "ADC1019000231"},
    "customer_name": "Ahmed Al Mansoori", "customer_mobile": "971501234567",
    "card_type": "Esaad", "card_grade": "Gold", "card_number": "ES-448812",
    "card_photo": None, "card_photo_id": "01a14a3c-77d2-7f1e-9a10-3f7c2b9d4e51",
    "card_photo_url": "/api/v1/manager/photos/01a14a3c-77d2-7f1e-9a10-3f7c2b9d4e51?size=display",
    "card_photo_thumb_url": "/api/v1/manager/photos/01a14a3c-77d2-7f1e-9a10-3f7c2b9d4e51?size=thumb",
    "discount_percent": "20.00", "fare_basis": "base",
    "bill_amount": "100.000", "discount_amount": "20.000", "net_amount": "80.000",
    "branch_id": 3, "station": "Abu Dhabi Corniche 1",
    "requested_by": "Rashed", "requested_at": "2026-10-08 16:50:00",
    "decided_by": None, "decided_channel": None, "decided_at": None, "remarks": "",
}
_CLAIM_DECIDED = {**_CLAIM_ROW, "status": "approved", "decided_by": "Sara K", "decided_channel": "manager",
                  "decided_at": "2026-10-08 16:52:00"}
_PAGED = {"page": 1, "pages": 1, "total": 1}
_LIST_INVALID = (400, "invalid_request", "page must be 1 or more.", {"errors": {"page": "must be 1 or more"}})


def _list_reply(rows, page, key):
    return envelope("ok", "List.", {key: rows, "page": page.number, "pages": page.paginator.num_pages,
                                    "total": page.paginator.count})


class CreditNoteListView(_ManagerView):
    """POST /api/v1/{app}/credit-notes/list -- manager app only."""

    form_class = ManagerApprovalListRequest

    @extend_schema(
        tags=["Manager Credit Notes"],
        summary="Credit-note requests from every station, newest first",
        description=_APPROVAL_LIST + "\nA credit note issued straight from the web is listed too, already "
                                     "`approved`; it has no `requested_at`.",
        request=envelope_request("ManagerCreditNoteListEnvelope", ManagerApprovalListRequest),
        responses=envelope_responses(
            (200, "ok", "List.", {"credit_notes": [_CREDIT_NOTE_ROW], **_PAGED}),
            _LIST_INVALID, *_COMMON,
        ),
    )
    def post(self, request, app):
        values, companies, zone, refused = self.values(request, app)
        if refused:
            return refused
        rows, page = approvals.credit_note_list(
            companies, pending_only=values["pending"], state_id=values.get("state_id"),
            branch_id=values.get("branch_id"), from_date=values.get("from_date"), to_date=values.get("to_date"),
            zone=zone, page=values["page"],
        )
        return _list_reply(rows, page, "credit_notes")


_CREDIT_NOTE_ERRORS = (
    (404, "unknown_credit_note", "No such credit note request.", {}),
    (409, "credit_note_decided", "This request is already approved.", {}),
)


class _CreditNoteDecisionView(_ManagerView):
    def decide(self, request, app, act, message):
        values, companies, _, refused = self.values(request, app)
        if refused:
            return refused
        try:
            note = act(values, companies)
        except credit_notes.CreditNoteRefused as refusal:
            return envelope(refusal.code, refusal.message, http_status=refusal.status)
        return envelope("ok", message, approvals.credit_note_row(note.pk))


class CreditNoteApproveView(_CreditNoteDecisionView):
    """POST /api/v1/{app}/credit-notes/approve -- manager app only."""

    form_class = ManagerCreditNoteApproveRequest

    @extend_schema(
        tags=["Manager Credit Notes"],
        summary="Issue a credit note request with the amount you set",
        description=_APPROVALS + """
**This call** issues a pending request with `net_amount` (AED, VAT included,
above 0 and at most the order's net -- `invalid_amount`), as the web does. The
note takes its number (order number + `CN`) and its VAT share at the invoice's
rate. `remarks` optional; the tablet sees it as `decision_remark`.
""",
        request=envelope_request("ManagerCreditNoteApproveEnvelope", ManagerCreditNoteApproveRequest),
        responses=envelope_responses(
            (200, "ok", "Credit note issued.", _CREDIT_NOTE_ISSUED),
            (400, "invalid_request", "net_amount is required.", {"errors": {"net_amount": "is required"}}),
            (400, "invalid_amount", "The credit note cannot be more than the order's net amount, AED 100.000.", {}),
            *_CREDIT_NOTE_ERRORS, *_COMMON,
        ),
    )
    def post(self, request, app):
        return self.decide(request, app, lambda v, companies: credit_notes.approve(
            request.user, v["credit_note_id"], companies, v["net_amount"], v.get("remarks", ""),
            channel=DecisionChannel.MANAGER), "Credit note issued.")


class CreditNoteRejectView(_CreditNoteDecisionView):
    """POST /api/v1/{app}/credit-notes/reject -- manager app only."""

    form_class = ManagerCreditNoteRejectRequest

    @extend_schema(
        tags=["Manager Credit Notes"],
        summary="Reject a credit note request",
        description=_APPROVALS + "\n**This call** rejects a pending request. `remarks` optional; the tablet "
                                 "sees it as `decision_remark`.",
        request=envelope_request("ManagerCreditNoteRejectEnvelope", ManagerCreditNoteRejectRequest),
        responses=envelope_responses(
            (200, "ok", "Credit note request rejected.", {**_CREDIT_NOTE_ROW, "status": "rejected",
                                                          "decided_by": "Sara K", "decided_channel": "manager",
                                                          "decided_at": "2026-10-08 17:20:00"}),
            (400, "invalid_request", "credit_note_id is required.", {"errors": {"credit_note_id": "is required"}}),
            *_CREDIT_NOTE_ERRORS, *_COMMON,
        ),
    )
    def post(self, request, app):
        return self.decide(request, app, lambda v, companies: credit_notes.reject(
            request.user, v["credit_note_id"], companies, v.get("remarks", ""),
            channel=DecisionChannel.MANAGER), "Credit note request rejected.")


class ClaimListView(_ManagerView):
    """POST /api/v1/{app}/card-discounts/claims/list -- manager app only."""

    form_class = ManagerApprovalListRequest

    @extend_schema(
        tags=["Manager Card Discounts"],
        summary="Card-discount approval requests from every station, newest first",
        description=_APPROVAL_LIST + "\nThe card photo -- show it before deciding: `card_photo_thumb_url` "
                                     "(320 px, for the list) and `card_photo_url` (1280 px; `?size=original` "
                                     "for zooming), paths on this server that need the access token "
                                     "(`GET /api/v1/manager/photos/<id>`). Older requests may carry only "
                                     "`card_photo`, a plain link the tablet sent. A claim still pending when "
                                     "its order was billed is `cancelled`.",
        request=envelope_request("ManagerClaimListEnvelope", ManagerApprovalListRequest),
        responses=envelope_responses(
            (200, "ok", "List.", {"claims": [_CLAIM_ROW], **_PAGED}),
            _LIST_INVALID, *_COMMON,
        ),
    )
    def post(self, request, app):
        values, companies, zone, refused = self.values(request, app)
        if refused:
            return refused
        rows, page = approvals.claim_list(
            companies, pending_only=values["pending"], state_id=values.get("state_id"),
            branch_id=values.get("branch_id"), from_date=values.get("from_date"), to_date=values.get("to_date"),
            zone=zone, page=values["page"],
        )
        return _list_reply(rows, page, "claims")


_CLAIM_ERRORS = (
    (400, "invalid_request", "claim_id is required.", {"errors": {"claim_id": "is required"}}),
    (404, "unknown_claim", "No such card discount request.", {}),
    (409, "claim_decided", "This request is already decided.", {}),
)


class _ClaimDecisionView(_ManagerView):
    form_class = ManagerClaimDecisionRequest
    approve = True
    message = ""

    def decide(self, request, app):
        values, companies, _, refused = self.values(request, app)
        if refused:
            return refused
        claim = CardDiscountClaim.objects.filter(pk=values["claim_id"], company_id__in=companies).first()
        if claim is None:
            return envelope("unknown_claim", "No such card discount request.", http_status=404)
        try:
            discount_services.decide_claim(request.user, claim, self.approve, values.get("remarks", ""),
                                           channel=DecisionChannel.MANAGER)
        except discount_services.NotPending:
            return envelope("claim_decided", "This request is already decided.", http_status=409)
        return envelope("ok", self.message, approvals.claim_row(claim.pk))


class ClaimApproveView(_ClaimDecisionView):
    """POST /api/v1/{app}/card-discounts/claims/approve -- manager app only."""

    approve, message = True, "Request approved."

    @extend_schema(
        tags=["Manager Card Discounts"],
        summary="Approve a card-discount request",
        description=_APPROVALS + "\n**This call** approves a pending claim; the tablet can then apply the "
                                 "card discount at settle. `remarks` optional.",
        request=envelope_request("ManagerClaimApproveEnvelope", ManagerClaimDecisionRequest),
        responses=envelope_responses((200, "ok", "Request approved.", _CLAIM_DECIDED), *_CLAIM_ERRORS, *_COMMON),
    )
    def post(self, request, app):
        return self.decide(request, app)


class ClaimRejectView(_ClaimDecisionView):
    """POST /api/v1/{app}/card-discounts/claims/reject -- manager app only."""

    approve, message = False, "Request rejected."

    @extend_schema(
        tags=["Manager Card Discounts"],
        summary="Reject a card-discount request",
        description=_APPROVALS + "\n**This call** rejects a pending claim. `remarks` optional.",
        request=envelope_request("ManagerClaimRejectEnvelope", ManagerClaimDecisionRequest),
        responses=envelope_responses((200, "ok", "Request rejected.", {**_CLAIM_DECIDED, "status": "rejected"}),
                                     *_CLAIM_ERRORS, *_COMMON),
    )
    def post(self, request, app):
        return self.decide(request, app)


class PendingCountsView(_ManagerView):
    """POST /api/v1/{app}/requests/pending-counts -- manager app only."""

    form_class = ManagerPendingCountsRequest

    @extend_schema(
        tags=["Manager Requests"],
        summary="How many of each kind wait for a decision",
        description="For the app's badges: pending order requests by kind (`discount`, `complimentary`, "
                    "`reprint`, `order_reprint`, `cancel`), credit-note requests, card-discount requests, leave "
                    "requests (`leave`, by the stations they are decided for), service requests (`service`), "
                    "and their `total` "
                    "-- across your company, or one state (`state_id`) or station (`branch_id`).",
        request=envelope_request("ManagerPendingCountsEnvelope", ManagerPendingCountsRequest,
                                 request_data_required=False),
        responses=envelope_responses(
            (200, "ok", "Pending.", {"discount": 2, "complimentary": 0, "reprint": 1, "order_reprint": 0,
                                     "cancel": 1, "credit_note": 1, "card_discount": 3, "leave": 2, "service": 1,
                                     "total": 11}),
            (400, "invalid_request", "state_id must be a whole number.",
             {"errors": {"state_id": "must be a whole number"}}),
            *_COMMON,
        ),
    )
    def post(self, request, app):
        values, companies, _, refused = self.values(request, app)
        if refused:
            return refused
        return envelope("ok", "Pending.", approvals.pending_counts(
            companies, state_id=values.get("state_id"), branch_id=values.get("branch_id")))
