"""The operator app's customer and order APIs -- views only translate HTTP;
the rules are in apps/rental/services.py (customer_by_phone, create_customer,
create_rental_order).

POST /api/v1/{app}/customers/lookup: given a phone, the matching customer's
data -- so the operator app can prefill a rental for a returning customer
instead of retyping their details.

POST /api/v1/{app}/customers/create: register a new customer from the till,
online or from an offline queue.

POST /api/v1/{app}/orders: book one rental -- the order, its vehicle lines
and any advance payments, in one call.

POST /api/v1/{app}/orders/detail: one order, by sync_id or order number.
"""

from django.utils import timezone
from drf_spectacular.utils import OpenApiExample, extend_schema
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.company.scoping import companies_for
from apps.devices import services as devices_services
from apps.devices.models import BillKind
from apps.portal.authentication import AppJWTAuthentication
from apps.portal.services import has_permission
from apps.rental import credit_notes, services
from apps.rental import requests as order_requests
from apps.rental.models import DecisionChannel, Invoice, OrderItemStatus
from apps.rental.serializers import (
    CreditNoteCancelRequest,
    CreditNoteRequest,
    CreditNoteStatusRequest,
    CustomerCreateRequest,
    CustomerLookupRequest,
    ManagerRequestApproveRequest,
    ManagerRequestDecisionRequest,
    ManagerRequestListRequest,
    OrderAddRequest,
    OrderCreateRequest,
    OrderDetailRequest,
    OrderPaymentsRequest,
    OrderRemoveRequest,
    OrderReplaceRequest,
    OrderRequestCreateRequest,
    OrderRequestStatusRequest,
    OrderRequestWithdrawRequest,
    OrderReturnRequest,
    OrderSettleRequest,
)
from core.api import envelope, request_parts, session_station
from core.enums import Channel
from core.schema import SERVER_ERROR, envelope_request, envelope_responses
from core.timezones import zone_for

_CUSTOMER_SAMPLE = {
    "customer_id": 4021, "customer_code": "CU014", "sync_id": None,
    "first_name": "Ahmed", "last_name": "Al Mansoori",
    "gender": "male", "date_of_birth": "1990-05-14", "nationality": "United Arab Emirates",
    "id_type": "emirates_id", "id_no": "784-1990-1234567-1",
    "mobile_country_code": "+971", "mobile_no": "501234567", "mobile_full": "971501234567",
    "email": "ahmed.almansoori@example.com",
    "address": "Dubai, UAE", "is_blocked": False, "block_reason": "",
}

_CUSTOMER_BLOCKED_SAMPLE = {
    **_CUSTOMER_SAMPLE, "customer_id": 4022, "customer_code": "CU015", "first_name": "Fatima",
    "last_name": "Hassan", "mobile_no": "501234568", "mobile_full": "971501234568", "is_blocked": True,
    "block_reason": "Repeated late returns",
}

_CREATED_SAMPLE = {
    **_CUSTOMER_SAMPLE, "customer_id": 4023, "customer_code": "CU016",
    "sync_id": "01923f8e-5b2a-7c3d-9e4f-a1b2c3d4e5f6",
}

_PHONE_RULE = """
**Phone** -- all three required: `mobile_country_code` (`+971`), `mobile_no`
(`501234567`) and `full_number` -- the country code's digits then the number's,
digits only (`971501234567`). The three must agree; the match is on
`full_number`.
"""

_LOOKUP_DESCRIPTION = """
The customer matching a phone -- so the operator app can prefill a rental for a
returning customer instead of retyping their details.
""" + _PHONE_RULE + """
**Sent:** the customer's full record, **including a blocked one** -- `is_blocked`
and `block_reason` tell the app the state, but the lookup itself is never
refused for a blocked customer. It is scoped to this device's own company even
though a phone is globally unique, so one company's device can never reach
another company's customer by guessing a phone number.

An unknown phone is `unknown_customer`.
"""

_CREATE_DESCRIPTION = """
Register a new customer from the till. The company is this device's own; the
customer code (`CU…`) is made by the server.

**sync_id** -- a **UUIDv7** made on the device for this customer and resent
**unchanged** on every retry. A second call with the same sync_id is answered
`duplicate` with the stored customer and writes nothing, so an offline queue
can resend safely.

**Required:** `sync_id`, `first_name`, and the phone. Everything else is
optional: `last_name`, `gender` (male, female, others), `date_of_birth`
(`YYYY-MM-DD`), `nationality`, `id_type` (emirates_id, passport,
driving_license, others), `id_no`, `email`, `address`, `remarks`.
""" + _PHONE_RULE + """
**A phone already registered** is `customer_exists`: to this company, with
that customer (block state included) so the app can use them; to another
company, with no details.
"""


def _customer_json(customer):
    return {
        "customer_id": customer.pk, "customer_code": customer.customer_code,
        "sync_id": str(customer.sync_id) if customer.sync_id else None,
        "first_name": customer.first_name, "last_name": customer.last_name,
        "gender": customer.gender, "date_of_birth": customer.date_of_birth.isoformat()
        if customer.date_of_birth else None,
        "nationality": customer.nationality, "id_type": customer.id_type, "id_no": customer.id_no,
        "mobile_country_code": customer.mobile_country_code, "mobile_no": customer.mobile_no,
        "mobile_full": customer.mobile_full,
        "email": customer.email, "address": customer.address,
        "is_blocked": customer.is_blocked, "block_reason": customer.block_reason,
    }


class _OperatorView(APIView):
    """Operator app only, signed in on a device mapped to an open station."""

    authentication_classes = [AppJWTAuthentication]
    permission_classes = [IsAuthenticated]

    def station(self, request, app):
        """(branch, None) or (None, the reply to send)."""
        if app != Channel.OPERATOR:
            return None, envelope("wrong_channel", "Not allowed on this app.", http_status=403)
        return session_station(request)


_COMMON = (
    (401, "not_authenticated", "Sign in first.", {}),
    (403, "wrong_channel", "Not allowed on this app.", {}),
    (409, "device_not_mapped", "This device has no station.", {}),
    (409, "branch_inactive", "This station is closed.", {}),
    SERVER_ERROR,
)

_MISMATCH = (400, "invalid_request", "full_number does not match mobile_country_code and mobile_no.",
             {"errors": {"full_number": "does not match mobile_country_code and mobile_no"}}, "invalid_request (phone)")


class CustomerLookupView(_OperatorView):
    """POST /api/v1/{app}/customers/lookup -- operator app only."""

    @extend_schema(
        tags=["Operator Customers"],
        summary="The customer matching a phone",
        description=_LOOKUP_DESCRIPTION,
        request=envelope_request("CustomerLookupEnvelope", CustomerLookupRequest),
        responses=envelope_responses(
            (200, "ok", "Customer.", _CUSTOMER_SAMPLE),
            (200, "ok", "Customer.", _CUSTOMER_BLOCKED_SAMPLE, "ok (blocked)"),
            (400, "invalid_request", "full_number is required.", {"errors": {"full_number": "is required"}}),
            _MISMATCH,
            (404, "unknown_customer", "No customer with that phone number.", {}),
            *_COMMON,
        ),
    )
    def post(self, request, app):
        branch, refused = self.station(request, app)
        if refused:
            return refused

        _, request_data = request_parts(request)
        form = CustomerLookupRequest(data=request_data)
        form.is_valid(raise_exception=True)

        try:
            customer = services.customer_by_phone(branch.company, form.validated_data["full_number"])
        except services.UnknownCustomer:
            return envelope("unknown_customer", "No customer with that phone number.", http_status=404)
        return envelope("ok", "Customer.", _customer_json(customer))


class CustomerCreateView(_OperatorView):
    """POST /api/v1/{app}/customers/create -- operator app only."""

    @extend_schema(
        tags=["Operator Customers"],
        summary="Register a new customer",
        description=_CREATE_DESCRIPTION,
        request=envelope_request("CustomerCreateEnvelope", CustomerCreateRequest),
        responses=envelope_responses(
            (200, "ok", "Customer created.", _CREATED_SAMPLE),
            (200, "duplicate", "Already created.", _CREATED_SAMPLE),
            (400, "invalid_request", "sync_id must be a UUIDv7.", {"errors": {"sync_id": "must be a UUIDv7"}}),
            _MISMATCH,
            (409, "customer_exists", "A customer with this phone number already exists.",
             _CUSTOMER_BLOCKED_SAMPLE, "customer_exists (this company)"),
            (409, "customer_exists", "A customer with this phone number already exists.", {},
             "customer_exists (another company)"),
            (409, "sync_id_conflict", "That sync_id is already used.", {}),
            *_COMMON,
        ),
    )
    def post(self, request, app):
        branch, refused = self.station(request, app)
        if refused:
            return refused

        _, request_data = request_parts(request)
        form = CustomerCreateRequest(data=request_data)
        form.is_valid(raise_exception=True)

        try:
            customer, created = services.create_customer(request.user, branch.company, form.validated_data)
        except services.CustomerExists as exists:
            data = _customer_json(exists.customer) if exists.customer else {}
            return envelope("customer_exists", "A customer with this phone number already exists.", data,
                            http_status=409)
        except services.SyncIdConflict:
            return envelope("sync_id_conflict", "That sync_id is already used.", http_status=409)

        if not created:
            return envelope("duplicate", "Already created.", _customer_json(customer))
        return envelope("ok", "Customer created.", _customer_json(customer))


TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def _local(moment, zone):
    return timezone.localtime(moment, zone).strftime(TIME_FORMAT) if moment else None


def _money(value):
    return str(value) if value is not None else None


def _discount_json(order):
    """The discount the settle applied: a card discount (its approved request,
    or none for an automatic one) or a manager's approved discount -- null
    without one."""
    if order.discount_claim_id:
        claim = order.discount_claim
        source, request_id = "card", str(claim.pk) if claim.requires_approval else None
    else:
        applied = order.requests.filter(applied_at__isnull=False).first()
        if applied is None:
            return None
        source, request_id = "manager", str(applied.pk)
    return {
        "source": source, "request_sync_id": request_id,
        "discount_percentage": _money(order.discount_percentage), "discount_amount": _money(order.discount_amount),
    }


def order_json(order, *, zone, next_order_number):
    """The one shape every order call answers with (order_lifecycle_design.md
    3): the order, its customer, its totals, every item and every payment.
    Times are company time, `YYYY-MM-DD HH:MM:SS`, as the tablet sends them.
    `next_order_number` is the asking tablet's receipt counter after the call."""
    items = list(order.items.select_related("vehicle").order_by("start_time", "created_on"))
    payments = list(order.payments.select_related("mode").order_by("paid_at", "created_on"))
    invoice = Invoice.objects.filter(order_id=order.pk).first()
    return {
        "sync_id": str(order.pk), "order_no": order.order_no,
        "status": order.status, "payment_status": order.payment_status,
        "booked_at": _local(order.booked_at, zone), "start_time": _local(order.start_time, zone),
        "completed_at": _local(order.completed_at, zone), "cancelled_at": _local(order.cancelled_at, zone),
        "is_direct_bill": order.is_direct_bill, "is_hotel_order": order.is_hotel_order,
        "hotel_commission": _money(order.hotel_commission),
        "customer": {"id": order.customer_id, "name": order.customer_name, "mobile": order.customer_mobile},
        # The bill: null until settle.
        "subtotal": _money(order.subtotal),
        "discount": _discount_json(order),
        "tax_percentage": _money(order.tax_percentage), "tax_amount": _money(order.tax_amount),
        "rounding_adjustment": _money(order.rounding_adjustment), "net_amount": _money(order.net_amount),
        # Money collected, from booking on; balance_due null until settle.
        "amount_received": _money(order.amount_received), "amount_refunded": _money(order.amount_refunded),
        "paid_amount": _money(order.paid_amount), "balance_due": _money(order.balance_due),
        "items_out": sum(1 for item in items if item.status == OrderItemStatus.ACTIVE),
        "items": [
            {
                "sync_id": str(item.pk), "status": item.status,
                "vehicle": {"id": item.vehicle_id, "name": item.vehicle.vehicle_name,
                            "identifier": item.vehicle.identifier},
                "fare_id": item.fare_id, "offer_id": item.offer_id, "package_minutes": item.package_minutes,
                "run_minutes": item.run_minutes,                       # null until returned
                "start_time": _local(item.start_time, zone),
                "expected_end_time": _local(item.expected_end_time, zone),
                "end_time": _local(item.end_time, zone),
                "base_fare": _money(item.base_fare),
                "overtime_amount": _money(item.overtime_amount),       # null until returned
                "total_amount": _money(item.total_amount),             # null until returned
                "replaced_item_id": str(item.replaced_item_id) if item.replaced_item_id else None,
                "reason": item.reason,
            }
            for item in items
        ],
        "payments": [
            {
                "sync_id": str(payment.pk), "kind": payment.kind,
                "payment_mode": {"id": payment.mode_id, "name": payment.mode.name},
                "amount": _money(payment.amount), "reference_no": payment.reference_no,
                "reference_date": payment.reference_date.isoformat() if payment.reference_date else None,
                "paid_at": _local(payment.paid_at, zone),
            }
            for payment in payments
        ],
        "invoice": {
            "invoice_no": invoice.invoice_no, "issued_at": _local(invoice.issued_at, zone),
            "net_amount": _money(invoice.net_amount),
        } if invoice else None,
        "next_order_number": next_order_number,
    }


def _refused(refusal):
    return envelope(refusal.code, refusal.message, {"retry": refusal.retry, **refusal.data},
                    http_status=refusal.status)


def _error_rows(*codes):
    """Swagger rows for these ORDER_ERRORS codes -- status, message and the
    retry flag exactly as sent."""
    return tuple(
        (status, code, message, {"retry": retry})
        for code in codes
        for status, message, retry in [services.ORDER_ERRORS[code]]
    )


def _error_table(*codes):
    rows = "\n".join(
        f"| `{code}` | {services.ORDER_ERRORS[code][0]} | "
        f"{'retry' if services.ORDER_ERRORS[code][2] else 'final'} | {services.ORDER_ERRORS[code][1]} |"
        for code in codes
    )
    return "| Code | HTTP | Retry or final | Meaning |\n|---|---|---|---|\n" + rows


def _counter_number(session):
    return devices_services.counter_for(session.device, session.branch, BillKind.ORDER).next_number


_ORDER_SAMPLE = {
    "sync_id": "01923e1c-0a11-7b22-8c33-d4e5f6a7b8c9", "order_no": "DUBPP60182000231",
    "status": "active", "payment_status": "pending",
    "booked_at": "2026-10-02 16:00:05", "start_time": "2026-10-02 16:00:00",
    "completed_at": None, "cancelled_at": None,
    "is_direct_bill": False, "is_hotel_order": False, "hotel_commission": "0.00",
    "customer": {"id": 5512, "name": "Ahmed Al Mansoori", "mobile": "971501234567"},
    "subtotal": None, "discount": None, "tax_percentage": None, "tax_amount": None,
    "rounding_adjustment": None, "net_amount": None,
    "amount_received": "100.00", "amount_refunded": "0.00", "paid_amount": "100.00", "balance_due": None,
    "items_out": 2,
    "items": [
        {"sync_id": "01923e1c-0a12-7b22-8c33-d4e5f6a7b8c9", "status": "active",
         "vehicle": {"id": 1041, "name": "MO 41", "identifier": "VB1241"},
         "fare_id": 88, "offer_id": None, "package_minutes": 60, "run_minutes": None,
         "start_time": "2026-10-02 16:00:00", "expected_end_time": "2026-10-02 17:00:00", "end_time": None,
         "base_fare": "50.00", "overtime_amount": None, "total_amount": None,
         "replaced_item_id": None, "reason": ""},
        {"sync_id": "01923e1c-0a13-7b22-8c33-d4e5f6a7b8c9", "status": "active",
         "vehicle": {"id": 3102, "name": "DC 02", "identifier": "VB0874"},
         "fare_id": 88, "offer_id": None, "package_minutes": 60, "run_minutes": None,
         "start_time": "2026-10-02 16:00:00", "expected_end_time": "2026-10-02 17:00:00", "end_time": None,
         "base_fare": "50.00", "overtime_amount": None, "total_amount": None,
         "replaced_item_id": None, "reason": ""},
    ],
    "payments": [
        {"sync_id": "01923e1c-0a14-7b22-8c33-d4e5f6a7b8c9", "kind": "advance",
         "payment_mode": {"id": 1, "name": "Cash"}, "amount": "60.00", "reference_no": "",
         "reference_date": None, "paid_at": "2026-10-02 16:00:05"},
        {"sync_id": "01923e1c-0a15-7b22-8c33-d4e5f6a7b8c9", "kind": "advance",
         "payment_mode": {"id": 2, "name": "Card"}, "amount": "40.00", "reference_no": "448812",
         "reference_date": "2026-10-02", "paid_at": "2026-10-02 16:00:05"},
    ],
    "invoice": None,
    "next_order_number": 232,
}

# The same order once both bikes are back -- DC 02 on time, MO 41 twelve
# minutes late -- and the bill settled: 5% card discount, VAT included in the
# fares (4.98 of the 104.50), the balance by card (order_lifecycle_design.md
# 3.3, 3.4).
_RETURNED_ITEMS = [
    {**_ORDER_SAMPLE["items"][0], "status": "returned", "run_minutes": 72, "end_time": "2026-10-02 17:12:00",
     "overtime_amount": "10.00", "total_amount": "60.00"},
    {**_ORDER_SAMPLE["items"][1], "status": "returned", "run_minutes": 60, "end_time": "2026-10-02 17:00:00",
     "overtime_amount": "0.00", "total_amount": "50.00"},
]
_RETURNED_SAMPLE = {**_ORDER_SAMPLE, "items_out": 0, "items": _RETURNED_ITEMS}
_SETTLED_SAMPLE = {
    **_RETURNED_SAMPLE, "status": "completed", "payment_status": "paid", "completed_at": "2026-10-02 17:13:30",
    "subtotal": "110.00",
    "discount": {"source": "card", "request_sync_id": "01923e2a-11aa-7b22-8c33-d4e5f6a7b8c9",
                 "discount_percentage": "5.00", "discount_amount": "5.50"},
    "tax_percentage": "5.00", "tax_amount": "4.98", "rounding_adjustment": "0.00", "net_amount": "104.50",
    "amount_received": "104.50", "paid_amount": "104.50", "balance_due": "0.00",
    "payments": [
        *_ORDER_SAMPLE["payments"],
        {"sync_id": "01923e1c-0a16-7b22-8c33-d4e5f6a7b8c9", "kind": "settlement",
         "payment_mode": {"id": 2, "name": "Card"}, "amount": "4.50", "reference_no": "4421",
         "reference_date": "2026-10-02", "paid_at": "2026-10-02 17:13:30"},
    ],
    "invoice": {"invoice_no": "DUBPP60182000231", "issued_at": "2026-10-02 17:13:30", "net_amount": "104.50"},
}

_BOOK_ERRORS = (
    "sync_id_conflict", "order_no_used", "vehicle_already_rented", "vehicle_repeated", "item_id_used",
    "payment_id_used", "unknown_customer", "unknown_payment_mode", "unknown_vehicle",
    "vehicle_not_at_station", "unknown_fare", "unknown_offer",
)

_ORDER_DESCRIPTION = """
One rental booking -- the order, its vehicles and any advance payments, in one
call (order_lifecycle_design.md 3.1).

**Ids -- all UUIDv7, all made on the tablet, all resent unchanged on a retry:**
- `sync_id` -- the order's id, and this call's;
- `items[].sync_id` -- each vehicle line's id, used later to return or replace it;
- `payments[].sync_id` -- each payment's id.

**Resending is safe.** The same call again (same `sync_id`, same body) is
answered `duplicate` with the reply first given, and writes nothing. The same
`sync_id` with a *different* body is `sync_id_conflict` -- a bug in the app,
never silently one version or the other. Keys may come in any order.

**`customer_id`, `vehicle_id`, `fare_id`, `offer_id`, `payment_mode_id`** are
ids the tablet already holds from `/customers/lookup`, `/vehicles`, `/fares`
and `/payment-modes`.

**`order_no`** is the printed receipt number, unique in the company, built
from the top-level `order_no_prefix` that login and `/device/settings` give.
The server reads the running number back out of it (this tablet's prefix and
registration id, then the digits) and raises the tablet's receipt counter to
it -- **even when the booking is refused**, because the receipt was already
printed. `next_order_number` in the reply is the counter after that.

**`payments`** (may be empty or left out) -- the money taken at booking,
one entry per payment mode (cash and card are two entries). Only `advance`
at booking. `amount` more than 0; `reference_no` / `reference_date` for a
card slip or cheque.

**No bill yet.** Each item carries only its `package_minutes` and the
`base_fare` agreed for it. The rest is filled later, never guessed:

| Amount | Filled when |
|---|---|
| item `base_fare` | booking |
| item `run_minutes`, `overtime_amount`, `total_amount` (= base fare + overtime) | that vehicle is returned |
| order `subtotal`, `discount`, `rounding_adjustment`, `net_amount`, `balance_due`; `tax_percentage`, `tax_amount` (the VAT included -- fares include VAT) | the order is settled |
| order `amount_received`, `amount_refunded`, `paid_amount` | each payment, from booking on |

Until then they are `null`. `payment_status` is `pending` while the order is
active and `paid` once it is settled (blank for a cancelled order).

**Times** are `YYYY-MM-DD HH:MM:SS` in company time, no offset: `booked_at`
is when the booking happened on the tablet (earlier if it was offline),
`start_time` the rental's own start.

**Errors** carry `data.retry`: `true` -- keep the call queued and send it
again; `false` -- stop and show the operator.

""" + _error_table(*_BOOK_ERRORS)


class OrderCreateView(_OperatorView):
    """POST /api/v1/{app}/orders -- operator app only."""

    @extend_schema(
        tags=["Operator Orders"],
        summary="Book a rental",
        description=_ORDER_DESCRIPTION,
        request=envelope_request("OrderCreateEnvelope", OrderCreateRequest),
        responses=envelope_responses(
            (200, "ok", "Order created.", _ORDER_SAMPLE),
            (200, "duplicate", "Already recorded.", _ORDER_SAMPLE),
            (400, "invalid_request", "sync_id must be a UUIDv7.", {"errors": {"sync_id": "must be a UUIDv7"}}),
            *_error_rows(*_BOOK_ERRORS),
            *_COMMON,
        ),
    )
    def post(self, request, app):
        branch, refused = self.station(request, app)
        if refused:
            return refused

        _, request_data = request_parts(request)
        form = OrderCreateRequest(data=request_data)
        if not form.is_valid():
            # Refused, but the receipt was printed: its number still counts.
            services.count_receipt(request.auth, request_data.get("order_no"))
            raise ValidationError(form.errors)
        values = form.validated_data

        zone = zone_for(branch.company)
        for key in ("booked_at", "start_time"):
            values[key] = timezone.make_aware(values[key], zone)
        for item in values["items"]:
            for key in ("start_time", "expected_end_time"):
                item[key] = timezone.make_aware(item[key], zone)
        for payment in values.get("payments") or []:
            payment["paid_at"] = timezone.make_aware(payment["paid_at"], zone)

        session = request.auth
        try:
            data, created = services.create_rental_order(
                session, values, request_data,
                lambda order: order_json(order, zone=zone, next_order_number=_counter_number(session)),
            )
        except services.OrderRefused as refusal:
            return _refused(refusal)

        if not created:
            return envelope("duplicate", "Already recorded.", data)
        return envelope("ok", "Order created.", data)


_DETAIL_DESCRIPTION = """
One order, whole -- in the same shape every order call answers with. Look it
up by `sync_id` **or** `order_no`, not both.

Any tablet at the order's station can read it, so a rental is never stuck on
a dead tablet. An order at another station is `unknown_order`, the same as
one that does not exist.
"""


class OrderDetailView(_OperatorView):
    """POST /api/v1/{app}/orders/detail -- operator app only."""

    @extend_schema(
        tags=["Operator Orders"],
        summary="One order, with its items and payments",
        description=_DETAIL_DESCRIPTION,
        request=envelope_request("OrderDetailEnvelope", OrderDetailRequest),
        responses=envelope_responses(
            (200, "ok", "Order.", _ORDER_SAMPLE),
            (400, "invalid_request", "sync_id or order_no is required.",
             {"errors": {"sync_id": "or order_no is required"}}),
            *_error_rows("unknown_order"),
            *_COMMON,
        ),
    )
    def post(self, request, app):
        branch, refused = self.station(request, app)
        if refused:
            return refused

        _, request_data = request_parts(request)
        form = OrderDetailRequest(data=request_data)
        form.is_valid(raise_exception=True)

        try:
            order = services.order_at_station(
                branch, sync_id=form.validated_data.get("sync_id"), order_no=form.validated_data.get("order_no"),
            )
        except services.OrderRefused as refusal:
            return _refused(refusal)
        data = order_json(order, zone=zone_for(branch.company), next_order_number=_counter_number(request.auth))
        return envelope("ok", "Order.", data)


_FLOW = """
**Every rental, direct ones too:** book → return each vehicle → settle. A direct
rental is returned like any other (overtime 0, so total = base fare), and its
payment may be taken at settle instead of up front.
"""

_RETURN_ERRORS = (
    "order_not_synced", "sync_id_conflict", "order_closed", "unknown_item", "item_not_active",
    "invalid_return_time", "amount_mismatch",
)

_RETURN_DESCRIPTION = """
One vehicle back (order_lifecycle_design.md 3.3). The line closes as `returned`
and the vehicle can be rented again at once.

**`sync_id`** is this call's own id (UUIDv7, made on the tablet, resent
unchanged); `order_id` and `item_id` are the order's and the line's.

**The tablet works out the amounts:** `run_minutes` (how long it ran),
`overtime_amount` (0 if on time) and `total_amount`. The server stores them as
sent, checking only that `total_amount` = the line's `base_fare` +
`overtime_amount` (`amount_mismatch`).

Any tablet at the order's station can return it. A return that reaches the
server before its booking is `order_not_synced` -- keep it queued and retry.
""" + _FLOW + """
""" + _error_table(*_RETURN_ERRORS)


class OrderReturnView(_OperatorView):
    """POST /api/v1/{app}/orders/return -- operator app only."""

    @extend_schema(
        tags=["Operator Orders"],
        summary="Return one vehicle",
        description=_RETURN_DESCRIPTION,
        request=envelope_request("OrderReturnEnvelope", OrderReturnRequest),
        responses=envelope_responses(
            (200, "ok", "Vehicle returned.", _RETURNED_SAMPLE),
            (200, "duplicate", "Already recorded.", _RETURNED_SAMPLE),
            (400, "invalid_request", "run_minutes is required.", {"errors": {"run_minutes": "is required"}}),
            *_error_rows(*_RETURN_ERRORS),
            *_COMMON,
        ),
    )
    def post(self, request, app):
        branch, refused = self.station(request, app)
        if refused:
            return refused

        _, request_data = request_parts(request)
        form = OrderReturnRequest(data=request_data)
        form.is_valid(raise_exception=True)
        values = form.validated_data
        zone = zone_for(branch.company)
        values["returned_at"] = timezone.make_aware(values["returned_at"], zone)

        session = request.auth
        try:
            data, done = services.return_item(
                session, values, request_data,
                lambda order: order_json(order, zone=zone, next_order_number=_counter_number(session)),
            )
        except services.OrderRefused as refusal:
            return _refused(refusal)
        return envelope("ok", "Vehicle returned.", data) if done else envelope("duplicate", "Already recorded.", data)


_SETTLE_ERRORS = (
    "order_not_synced", "sync_id_conflict", "order_closed", "items_still_out", "amount_mismatch",
    "balance_not_settled", "unknown_card_claim", "card_claim_not_approved", "unknown_card_discount",
    "unknown_discount_request", "discount_request_not_approved", "discount_mismatch",
    "approved_discount_not_applied", "unknown_payment_mode", "payment_id_used",
)

_SETTLE_DESCRIPTION = """
Close the bill (order_lifecycle_design.md 3.4). Every vehicle must already be
returned (`items_still_out`). In one step: the bill is stored, the card
discount redeemed, the payments recorded, the order **completed** (payment
status `paid`) and the **invoice** issued -- numbered by the order number.

**The tablet works out the whole bill and sends it:** `subtotal`, the discount,
`rounding_adjustment` (may be negative), `net_amount`, and the VAT figures.
The server only checks that the figures agree (`amount_mismatch`):
- `subtotal` = the returned vehicles' `total_amount`s (replaced or removed
  vehicles are not billed);
- `net_amount` = `subtotal` − `discount_amount` + `rounding_adjustment`.

**VAT is included in the fares** -- nothing is added for it.
`tax_percentage` and `tax_amount` are the VAT *contained* in `net_amount`
(`net_amount` × rate / (100 + rate), to 2 places: 104.50 at 5% → 4.98); they
are stored as sent and printed on the tax invoice, never part of the sum.

**The discount** (optional) -- **at most one** of two blocks; both is
`invalid_request`. `discount_amount` is what the net check above uses.

**`card_discount`** -- a card discount, one of:
- `{request_sync_id, discount_percentage, discount_amount}` -- an approved
  card-discount request (`request_sync_id` = the `sync_id` you sent to
  `card-discounts/approval`): it becomes redeemed;
- `{card_discount_id, card_number, discount_percentage, discount_amount}` -- an
  automatic discount: recorded as a new redemption.

Every other card-discount request on the order still pending, or approved but
not applied, is cancelled.

**`manager_discount`** -- a manager's approved discount
(`orders/requests`, kind `discount`):
`{request_sync_id, discount_percentage, discount_amount}` --
`request_sync_id` = the `sync_id` you sent to `orders/requests`. It must be
this order's request and **approved** (`unknown_discount_request`,
`discount_request_not_approved` -- a revoked one too). It must match the
approval (`discount_mismatch`, the expected figures in `data`):
- a **percent** approval: `discount_percentage` = the approved %;
  `discount_amount` is yours, worked out on the bill;
- an **amount** approval: leave `discount_percentage` out; `discount_amount` =
  the approved AED, or the `subtotal` if the bill is smaller (net 0).

An order **with an approved manager discount must apply it**: settling without
`manager_discount` (or with a card discount instead) is
`approved_discount_not_applied`, with `request_sync_id`, `discount_type` and
`discount_value` in `data`. A request still pending at settle is closed.

**`payments`** -- `settlement` for the balance, `refund` for money handed back
when the customer paid more than the bill. After them the bill must be paid
in full: what was received minus refunded = `net_amount`
(`balance_not_settled`; nothing is written). A bill of 0 settles too.
""" + _FLOW + """
""" + _error_table(*_SETTLE_ERRORS)


def _settle_example(name, discount):
    body = {
        "sync_id": "01923e1c-0a15-7b22-8c33-d4e5f6a7b8c9", "order_id": "01923e1c-0a11-7b22-8c33-d4e5f6a7b8c9",
        "settled_at": "2026-10-02 17:13:30", "subtotal": "110.000", **discount,
        "tax_percentage": "5.00", "tax_amount": "4.976", "rounding_adjustment": "0.000",
        "net_amount": "104.500",
        "payments": [{"sync_id": "01923e1c-0a16-7b22-8c33-d4e5f6a7b8c9", "kind": "settlement",
                      "payment_mode_id": 2, "amount": "4.500", "reference_no": "4421",
                      "reference_date": "2026-10-02", "paid_at": "2026-10-02 17:13:30"}],
    }
    return OpenApiExample(name, request_only=True, value={"credentials": {}, "request_data": body})


_SETTLE_EXAMPLES = [
    _settle_example("manager discount, approved as percent", {"manager_discount": {
        "request_sync_id": "01923e2b-77aa-7b22-8c33-d4e5f6a7b8c9", "discount_percentage": "5.00",
        "discount_amount": "5.500"}}),
    _settle_example("manager discount, approved as AED amount", {"manager_discount": {
        "request_sync_id": "01923e2b-77aa-7b22-8c33-d4e5f6a7b8c9", "discount_amount": "5.500"}}),
    _settle_example("approved card-discount request", {"card_discount": {
        "request_sync_id": "01923e2a-11aa-7b22-8c33-d4e5f6a7b8c9", "discount_percentage": "5.00",
        "discount_amount": "5.500"}}),
    _settle_example("automatic card discount", {"card_discount": {
        "card_discount_id": 7, "card_number": "4111 22** **** 3344", "discount_percentage": "5.00",
        "discount_amount": "5.500"}}),
]


class OrderSettleView(_OperatorView):
    """POST /api/v1/{app}/orders/settle -- operator app only."""

    @extend_schema(
        tags=["Operator Orders"],
        summary="Settle an order and issue its invoice",
        description=_SETTLE_DESCRIPTION,
        request=envelope_request("OrderSettleEnvelope", OrderSettleRequest),
        examples=_SETTLE_EXAMPLES,
        responses=envelope_responses(
            (200, "ok", "Order settled.", _SETTLED_SAMPLE),
            (200, "duplicate", "Already recorded.", _SETTLED_SAMPLE),
            (400, "invalid_request", "net_amount is required.", {"errors": {"net_amount": "is required"}}),
            *_error_rows(*_SETTLE_ERRORS),
            *_COMMON,
        ),
    )
    def post(self, request, app):
        branch, refused = self.station(request, app)
        if refused:
            return refused

        _, request_data = request_parts(request)
        form = OrderSettleRequest(data=request_data)
        form.is_valid(raise_exception=True)
        values = form.validated_data
        zone = zone_for(branch.company)
        values["settled_at"] = timezone.make_aware(values["settled_at"], zone)
        for payment in values.get("payments") or []:
            payment["paid_at"] = timezone.make_aware(payment["paid_at"], zone)

        session = request.auth
        try:
            data, done = services.settle_order(
                session, values, request_data,
                lambda order: order_json(order, zone=zone, next_order_number=_counter_number(session)),
            )
        except services.OrderRefused as refusal:
            return _refused(refusal)
        return envelope("ok", "Order settled.", data) if done else envelope("duplicate", "Already recorded.", data)


# -- Add, replace, remove a vehicle ---------------------------------------------------

_ADDED_ITEM = {
    **_ORDER_SAMPLE["items"][1], "sync_id": "01923e1c-0a17-7b22-8c33-d4e5f6a7b8c9",
    "vehicle": {"id": 1050, "name": "MO 50", "identifier": "VB1250"}, "package_minutes": 30,
    "start_time": "2026-10-02 16:30:00", "expected_end_time": "2026-10-02 17:00:00", "base_fare": "30.00",
}
_ADDED_SAMPLE = {
    **_ORDER_SAMPLE, "items_out": 3, "items": [*_ORDER_SAMPLE["items"], _ADDED_ITEM],
    "amount_received": "130.00", "paid_amount": "130.00",
    "payments": [
        *_ORDER_SAMPLE["payments"],
        {"sync_id": "01923e1c-0a18-7b22-8c33-d4e5f6a7b8c9", "kind": "advance",
         "payment_mode": {"id": 1, "name": "Cash"}, "amount": "30.00", "reference_no": "",
         "reference_date": None, "paid_at": "2026-10-02 16:30:00"},
    ],
}
_REPLACED_SAMPLE = {
    **_ORDER_SAMPLE,
    "items": [
        {**_ORDER_SAMPLE["items"][0], "status": "replaced", "end_time": "2026-10-02 16:20:00",
         "reason": "Chain broken"},
        _ORDER_SAMPLE["items"][1],
        {**_ORDER_SAMPLE["items"][0], "sync_id": "01923e1c-0a19-7b22-8c33-d4e5f6a7b8c9",
         "vehicle": {"id": 1042, "name": "MO 42", "identifier": "VB1242"},
         "start_time": "2026-10-02 16:20:00",
         "replaced_item_id": _ORDER_SAMPLE["items"][0]["sync_id"]},
    ],
}
_REMOVED_SAMPLE = {
    **_ORDER_SAMPLE, "items_out": 1,
    "items": [
        _ORDER_SAMPLE["items"][0],
        {**_ORDER_SAMPLE["items"][1], "status": "removed", "end_time": "2026-10-02 16:05:00",
         "reason": "Booked by mistake"},
    ],
}

_CHANGE_ERRORS = ("order_not_synced", "sync_id_conflict", "order_closed")
_NEW_LINE_ERRORS = (
    "vehicle_already_rented", "vehicle_not_at_station", "unknown_vehicle", "unknown_fare", "unknown_offer",
    "item_id_used",
)
_ADD_ERRORS = (*_CHANGE_ERRORS, *_NEW_LINE_ERRORS, "unknown_payment_mode", "payment_id_used")
_REPLACE_ERRORS = (*_CHANGE_ERRORS, "unknown_item", "item_not_active", "invalid_time", "vehicle_repeated",
                   *_NEW_LINE_ERRORS)
_REMOVE_ERRORS = (*_CHANGE_ERRORS, "unknown_item", "item_not_active", "invalid_time")

_CHANGE_RULES = """
**`sync_id`** is this call's own id (UUIDv7, made on the tablet, resent
unchanged); a new line has its own `sync_id` too. Any tablet at the order's
station can make the change, while the order is still active (`order_closed`
once it is settled or cancelled). A call that reaches the server before its
booking is `order_not_synced` -- keep it queued and retry.
"""

_ADD_DESCRIPTION = """
Another vehicle joins an active order. The new `item` is checked as at
booking: this company's, at this station, not already out. It carries only
its package and agreed `base_fare`; its final amount comes at return.

**`payments`** (optional, `advance` only) -- money taken for the added vehicle,
recorded in this same call. Send each payment once: here, or alone through
the payments call -- never both.
""" + _CHANGE_RULES + """
""" + _error_table(*_ADD_ERRORS)

_REPLACE_DESCRIPTION = """
A vehicle swapped for another (order_lifecycle_design.md 3.2), in one step:
- the old line (`old_item_id`) closes as **`replaced`** at `replaced_at`, with
  the `reason` (required) -- it is **never billed**, and its vehicle is free
  at once;
- the `new_item` starts, with `replaced_item_id` pointing back at the old line,
  carrying the package's `base_fare` (the customer pays for one package).

The replacement must be another vehicle (`vehicle_repeated`), checked as at
booking; `replaced_at` cannot be before the old line's start (`invalid_time`).
""" + _CHANGE_RULES + """
""" + _error_table(*_REPLACE_ERRORS)

_REMOVE_DESCRIPTION = """
A vehicle taken off the order with no replacement. The line stays on the
order as **`removed`**, with `removed_at` and the `reason` (required) -- it is
**never billed** nor invoiced, `items_out` no longer counts it, and the
vehicle is free at once. A vehicle the customer rode is **returned**, not
removed.
""" + _CHANGE_RULES + """
""" + _error_table(*_REMOVE_ERRORS)


class _ChangeView(_OperatorView):
    """One change to an active order: validate, make the times company-aware,
    run the service, answer ok / duplicate / the refusal."""

    form_class = None
    times = ()
    item_keys = ()
    done_message = ""

    def change(self, session, values, request_data, reply_for):
        raise NotImplementedError

    def post(self, request, app):
        branch, refused = self.station(request, app)
        if refused:
            return refused

        _, request_data = request_parts(request)
        form = self.form_class(data=request_data)
        form.is_valid(raise_exception=True)
        values = form.validated_data
        zone = zone_for(branch.company)
        for key in self.times:
            values[key] = timezone.make_aware(values[key], zone)
        for key in self.item_keys:
            for time_key in ("start_time", "expected_end_time"):
                values[key][time_key] = timezone.make_aware(values[key][time_key], zone)
        for payment in values.get("payments") or []:
            payment["paid_at"] = timezone.make_aware(payment["paid_at"], zone)

        session = request.auth
        try:
            data, done = self.change(
                session, values, request_data,
                lambda order: order_json(order, zone=zone, next_order_number=_counter_number(session)),
            )
        except services.OrderRefused as refusal:
            return _refused(refusal)
        return envelope("ok", self.done_message, data) if done else envelope("duplicate", "Already recorded.", data)


class OrderAddView(_ChangeView):
    """POST /api/v1/{app}/orders/add -- operator app only."""

    form_class, times, item_keys, done_message = OrderAddRequest, ("added_at",), ("item",), "Vehicle added."

    @extend_schema(
        tags=["Operator Orders"],
        summary="Add a vehicle to an order",
        description=_ADD_DESCRIPTION,
        request=envelope_request("OrderAddEnvelope", OrderAddRequest),
        responses=envelope_responses(
            (200, "ok", "Vehicle added.", _ADDED_SAMPLE),
            (200, "duplicate", "Already recorded.", _ADDED_SAMPLE),
            (400, "invalid_request", "item is required.", {"errors": {"item": "is required"}}),
            *_error_rows(*_ADD_ERRORS),
            *_COMMON,
        ),
    )
    def post(self, request, app):
        return super().post(request, app)

    def change(self, *args):
        return services.add_item(*args)


class OrderReplaceView(_ChangeView):
    """POST /api/v1/{app}/orders/replace -- operator app only."""

    form_class, times, item_keys = OrderReplaceRequest, ("replaced_at",), ("new_item",)
    done_message = "Vehicle replaced."

    @extend_schema(
        tags=["Operator Orders"],
        summary="Replace a vehicle on an order",
        description=_REPLACE_DESCRIPTION,
        request=envelope_request("OrderReplaceEnvelope", OrderReplaceRequest),
        responses=envelope_responses(
            (200, "ok", "Vehicle replaced.", _REPLACED_SAMPLE),
            (200, "duplicate", "Already recorded.", _REPLACED_SAMPLE),
            (400, "invalid_request", "reason is required.", {"errors": {"reason": "is required"}}),
            *_error_rows(*_REPLACE_ERRORS),
            *_COMMON,
        ),
    )
    def post(self, request, app):
        return super().post(request, app)

    def change(self, *args):
        return services.replace_item(*args)


class OrderRemoveView(_ChangeView):
    """POST /api/v1/{app}/orders/remove -- operator app only."""

    form_class, times, done_message = OrderRemoveRequest, ("removed_at",), "Vehicle removed."

    @extend_schema(
        tags=["Operator Orders"],
        summary="Remove a vehicle from an order",
        description=_REMOVE_DESCRIPTION,
        request=envelope_request("OrderRemoveEnvelope", OrderRemoveRequest),
        responses=envelope_responses(
            (200, "ok", "Vehicle removed.", _REMOVED_SAMPLE),
            (200, "duplicate", "Already recorded.", _REMOVED_SAMPLE),
            (400, "invalid_request", "reason is required.", {"errors": {"reason": "is required"}}),
            *_error_rows(*_REMOVE_ERRORS),
            *_COMMON,
        ),
    )
    def post(self, request, app):
        return super().post(request, app)

    def change(self, *args):
        return services.remove_item(*args)


# -- Standalone payments ----------------------------------------------------------------

_ADVANCE_ENTRIES = [
    {"sync_id": "01923e1c-0a20-7b22-8c33-d4e5f6a7b8c9", "kind": "advance",
     "payment_mode": {"id": 1, "name": "Cash"}, "amount": "50.00", "reference_no": "",
     "reference_date": None, "paid_at": "2026-10-02 16:40:00"},
    {"sync_id": "01923e1c-0a21-7b22-8c33-d4e5f6a7b8c9", "kind": "advance",
     "payment_mode": {"id": 2, "name": "Card"}, "amount": "20.00", "reference_no": "448901",
     "reference_date": "2026-10-02", "paid_at": "2026-10-02 16:40:00"},
]
_ADVANCE_SAMPLE = {
    **_ORDER_SAMPLE, "amount_received": "170.00", "paid_amount": "170.00",
    "payments": [*_ORDER_SAMPLE["payments"], *_ADVANCE_ENTRIES],
}
_REFUND_SAMPLE = {
    **_ORDER_SAMPLE, "amount_refunded": "40.00", "paid_amount": "60.00",
    "payments": [
        *_ORDER_SAMPLE["payments"],
        {"sync_id": "01923e1c-0a22-7b22-8c33-d4e5f6a7b8c9", "kind": "refund",
         "payment_mode": {"id": 1, "name": "Cash"}, "amount": "40.00", "reference_no": "R-17",
         "reference_date": "2026-10-02", "paid_at": "2026-10-02 16:45:00"},
    ],
}

_PAYMENTS_ERRORS = ("order_not_synced", "sync_id_conflict", "order_closed", "unknown_payment_mode",
                    "payment_id_used")

_PAYMENTS_DESCRIPTION = """
Money moving on its own: an **extra advance** mid-rental, or a **refund** --
part of an advance handed back, or money returned after a cancel is approved.

**One `kind` for the whole call** -- `advance` or `refund` -- and a list of
`payments` (cash and card together are two entries, recorded together or not
at all). Each entry has its own `sync_id` (UUIDv7); `amount` is more than 0;
`reference_no` / `reference_date` for a card slip or cheque.

| Order is | `advance` | `refund` |
|---|---|---|
| active | yes | yes |
| cancelled | no | yes |
| completed | no | no -- the bill is invoiced |

Anything else is `order_closed`.

**Send each payment once.** Money that belongs to a booking, an added vehicle
or a settlement goes in that call's own `payments` -- never again here; the
same payment `sync_id` twice is `payment_id_used`.
""" + _CHANGE_RULES + """
""" + _error_table(*_PAYMENTS_ERRORS)


class OrderPaymentsView(_ChangeView):
    """POST /api/v1/{app}/orders/payments -- operator app only."""

    form_class, done_message = OrderPaymentsRequest, "Payment recorded."

    @extend_schema(
        tags=["Operator Orders"],
        summary="Take an extra advance, or record a refund",
        description=_PAYMENTS_DESCRIPTION,
        request=envelope_request("OrderPaymentsEnvelope", OrderPaymentsRequest),
        responses=envelope_responses(
            (200, "ok", "Payment recorded.", _ADVANCE_SAMPLE, "ok (advance)"),
            (200, "ok", "Payment recorded.", _REFUND_SAMPLE, "ok (refund)"),
            (200, "duplicate", "Already recorded.", _ADVANCE_SAMPLE),
            (400, "invalid_request", "kind must be advance or refund.",
             {"errors": {"kind": "must be advance or refund"}}),
            *_error_rows(*_PAYMENTS_ERRORS),
            *_COMMON,
        ),
    )
    def post(self, request, app):
        return super().post(request, app)

    def change(self, *args):
        return services.record_payments(*args)


# -- Credit notes ---------------------------------------------------------------------

_CREDIT_NOTE_PENDING_SAMPLE = {
    "credit_note_id": "01a1013d-fa97-7179-93ae-3ae25842f1ce", "order_id": "01a0fbf0-cd1e-7002-a9e6-6c4c162129a7",
    "order_no": "CF0011017000003", "source": "device", "status": "pending", "credit_note_no": None,
    "net_amount": None, "tax_amount": None, "taxable_amount": None, "reason": "Bike chain broke after 10 minutes",
    "decision_remark": "", "decided_at": None,
}
_CREDIT_NOTE_APPROVED_SAMPLE = {
    **_CREDIT_NOTE_PENDING_SAMPLE, "status": "approved", "credit_note_no": "CF0011017000003CN",
    "net_amount": "21.00", "tax_amount": "1.00", "taxable_amount": "20.00",
    "decision_remark": "Half refund agreed", "decided_at": "2026-10-06T10:42:00+00:00",
}
_CREDIT_NOTE_WEB_SAMPLE = {
    **_CREDIT_NOTE_APPROVED_SAMPLE, "credit_note_id": "01a1020a-1b2c-7d3e-8f40-5a6b7c8d9e0f",
    "order_id": "01a0fbf1-0000-7000-8000-000000000001", "order_no": "CF0011017000004",
    "credit_note_no": "CF0011017000004CN", "source": "web", "reason": "Complaint by phone",
    "decision_remark": "",
}

_CREDIT_NOTE_REQUEST_ERRORS = (
    "order_not_synced", "sync_id_conflict", "order_not_settled", "credit_note_pending", "credit_note_issued",
)
_CREDIT_NOTE_CANCEL_ERRORS = ("sync_id_conflict", "unknown_credit_note", "credit_note_closed")

_CREDIT_NOTE_RULES = """
**A credit note** is a partial refund on a **settled** order, issued against its
invoice. The tablet only **asks**: the back office sets the amount when it
approves, or rejects. The back office can also issue one directly on the web
(`source: web`) -- the tablet sees those through `credit-notes/status`.
Nothing here changes the order, its invoice or its payments.

`status`: `pending` (waiting) · `approved` (issued: `credit_note_no` and the
amounts are filled -- `net_amount` is given back, VAT included, `tax_amount` the
VAT inside it) · `rejected` (see `decision_remark`) · `cancelled` (the tablet
withdrew it). After a rejection or a cancel the order may be asked again; while
one is pending, or once one is approved, it may not.
"""

_CREDIT_NOTE_REQUEST_DESCRIPTION = _CREDIT_NOTE_RULES + """
**This call** asks for one. `sync_id` is a UUIDv7 made on the tablet and resent
unchanged on retry -- it **becomes the credit note's id** (`credit_note_id`).
`order_id` is the settled order's sync_id; `requested_at` the tablet's time;
`reason` optional free text. Any tablet of the company may ask, for any order.
""" + _error_table(*_CREDIT_NOTE_REQUEST_ERRORS)

_CREDIT_NOTE_CANCEL_DESCRIPTION = _CREDIT_NOTE_RULES + """
**This call** withdraws a request that is still `pending`. `sync_id` is this
call's own UUIDv7; `credit_note_id` the request's. Cancelling one already
cancelled answers the same; an approved or rejected one is `credit_note_closed`.
""" + _error_table(*_CREDIT_NOTE_CANCEL_ERRORS)

_CREDIT_NOTE_STATUS_DESCRIPTION = _CREDIT_NOTE_RULES + """
**This call** reads where they stand: send the `order_ids` (1-100) and get every
credit note on those orders, newest first -- the tablet's own requests and those
issued on the web. Orders with none are simply absent. Read-only.
"""


class CreditNoteRequestView(_OperatorView):
    """POST /api/v1/{app}/orders/credit-notes -- operator app only."""

    @extend_schema(
        tags=["Operator Credit Notes"],
        summary="Ask for a credit note on a settled order",
        description=_CREDIT_NOTE_REQUEST_DESCRIPTION,
        request=envelope_request("CreditNoteRequestEnvelope", CreditNoteRequest),
        responses=envelope_responses(
            (200, "ok", "Credit note requested.", _CREDIT_NOTE_PENDING_SAMPLE),
            (200, "duplicate", "Already recorded.", _CREDIT_NOTE_PENDING_SAMPLE),
            (400, "invalid_request", "order_id is required.", {"errors": {"order_id": "is required"}}),
            *_error_rows(*_CREDIT_NOTE_REQUEST_ERRORS),
            *_COMMON,
        ),
    )
    def post(self, request, app):
        branch, refused = self.station(request, app)
        if refused:
            return refused
        _, request_data = request_parts(request)
        form = CreditNoteRequest(data=request_data)
        form.is_valid(raise_exception=True)
        values = form.validated_data
        values["requested_at"] = timezone.make_aware(values["requested_at"], zone_for(branch.company))
        try:
            data, done = credit_notes.request_credit_note(request.auth, values, request_data)
        except services.OrderRefused as refusal:
            return _refused(refusal)
        return (envelope("ok", "Credit note requested.", data) if done
                else envelope("duplicate", "Already recorded.", data))


class CreditNoteCancelView(_OperatorView):
    """POST /api/v1/{app}/orders/credit-notes/cancel -- operator app only."""

    @extend_schema(
        tags=["Operator Credit Notes"],
        summary="Withdraw a credit note request still waiting",
        description=_CREDIT_NOTE_CANCEL_DESCRIPTION,
        request=envelope_request("CreditNoteCancelEnvelope", CreditNoteCancelRequest),
        responses=envelope_responses(
            (200, "ok", "Credit note request cancelled.", {**_CREDIT_NOTE_PENDING_SAMPLE, "status": "cancelled",
                                                          "decided_at": "2026-10-06T09:15:00+00:00"}),
            (200, "duplicate", "Already recorded.", {**_CREDIT_NOTE_PENDING_SAMPLE, "status": "cancelled"}),
            (400, "invalid_request", "credit_note_id is required.", {"errors": {"credit_note_id": "is required"}}),
            *_error_rows(*_CREDIT_NOTE_CANCEL_ERRORS),
            *_COMMON,
        ),
    )
    def post(self, request, app):
        branch, refused = self.station(request, app)
        if refused:
            return refused
        _, request_data = request_parts(request)
        form = CreditNoteCancelRequest(data=request_data)
        form.is_valid(raise_exception=True)
        values = form.validated_data
        values["cancelled_at"] = timezone.make_aware(values["cancelled_at"], zone_for(branch.company))
        try:
            data, done = credit_notes.cancel_credit_note(request.auth, values, request_data)
        except services.OrderRefused as refusal:
            return _refused(refusal)
        return (envelope("ok", "Credit note request cancelled.", data) if done
                else envelope("duplicate", "Already recorded.", data))


class CreditNoteStatusView(_OperatorView):
    """POST /api/v1/{app}/orders/credit-notes/status -- operator app only."""

    @extend_schema(
        tags=["Operator Credit Notes"],
        summary="Where credit notes on these orders stand",
        description=_CREDIT_NOTE_STATUS_DESCRIPTION,
        request=envelope_request("CreditNoteStatusEnvelope", CreditNoteStatusRequest),
        responses=envelope_responses(
            (200, "ok", "Credit notes.", {"credit_notes": [_CREDIT_NOTE_APPROVED_SAMPLE, _CREDIT_NOTE_WEB_SAMPLE]}),
            (400, "invalid_request", "send at least one order_id.",
             {"errors": {"order_ids": "send at least one order_id"}}),
            *_COMMON,
        ),
    )
    def post(self, request, app):
        branch, refused = self.station(request, app)
        if refused:
            return refused
        _, request_data = request_parts(request)
        form = CreditNoteStatusRequest(data=request_data)
        form.is_valid(raise_exception=True)
        notes = credit_notes.credit_notes_for_orders(branch.company, form.validated_data["order_ids"])
        return envelope("ok", "Credit notes.", {"credit_notes": [credit_notes.note_json(note) for note in notes]})


# -- Requests: the operator asks, a manager decides (apps/rental/requests.py) ------------

_REQUEST_PENDING_SAMPLE = {
    "request_sync_id": "01923e2b-77aa-7b22-8c33-d4e5f6a7b8c9", "order_id": "01923e1c-0a11-7b22-8c33-d4e5f6a7b8c9",
    "order_no": "DUBPP60182000231", "kind": "discount", "status": "pending", "reason": "Regular customer",
    "requested_at": "2026-10-08 16:40:00", "discount_type": None, "discount_value": None,
    "decided_by": None, "decided_channel": None, "decided_at": None, "decision_note": "",
    "revoked_by": None, "revoked_channel": None, "revoked_at": None, "revoke_note": "",
    "applied_amount": None, "applied_at": None,
}
_REQUEST_APPROVED_SAMPLE = {
    **_REQUEST_PENDING_SAMPLE, "status": "approved", "discount_type": "percent", "discount_value": "10.00",
    "decided_by": "Sara K", "decided_channel": "manager", "decided_at": "2026-10-08 16:43:12",
}
_REQUEST_AMOUNT_SAMPLE = {
    **_REQUEST_APPROVED_SAMPLE, "request_sync_id": "01923e2b-88bb-7b22-8c33-d4e5f6a7b8c9",
    "order_id": "01923e1c-0b22-7b22-8c33-d4e5f6a7b8c9", "order_no": "DUBPP60182000232",
    "discount_type": "amount", "discount_value": "15.000", "decided_channel": "web", "reason": "",
}

_REQUEST_RULES = """
**Requests** -- the operator asks a manager about a **running** order; the
manager decides on the web or in the manager app. Only `kind: discount` so far.

A **discount** request carries no amount: the bill is not made yet. The manager
approves a **rule** -- `discount_type` `percent` (`discount_value` e.g.
`"10.00"`) or `amount` (AED, e.g. `"15.000"`) -- or rejects it. An approval can
be **revoked** while the order runs. The tablet applies an approved discount
itself at settle, in `manager_discount` (see `orders/settle`); the server checks
it against the approval, and an order with an approved discount **must** apply
it.

`status`: `pending` · `approved` · `rejected` (see `decision_note`) ·
`withdrawn` (the tablet took it back) · `closed` (still pending when the order
settled) · `revoked` (approved, then taken back -- see `revoke_note`).
`decided_channel` / `revoked_channel`: `web` or `manager`. After rejected,
withdrawn or revoked the order may be asked again; while one is pending or
approved it may not. An order takes a manager discount **or** a card discount,
never both (`card_discount_requested`). Times are company time.
"""

_REQUEST_CREATE_ERRORS = (
    "order_not_synced", "sync_id_conflict", "order_closed", "discount_request_pending", "discount_approved",
    "card_discount_requested",
)
_REQUEST_WITHDRAW_ERRORS = ("order_not_synced", "sync_id_conflict", "unknown_request", "request_closed")

_REQUEST_CREATE_DESCRIPTION = _REQUEST_RULES + """
**This call** asks. `sync_id` is a UUIDv7 made on the tablet and resent
unchanged on retry -- it **becomes the request's id** (`request_sync_id`).
`order_id` is the running order's sync_id; `kind` is `discount`; `reason`
optional free text; `requested_at` the tablet's time. Any tablet at the order's
station may ask. A settled or cancelled order is `order_closed`.
""" + _error_table(*_REQUEST_CREATE_ERRORS)

_REQUEST_WITHDRAW_DESCRIPTION = _REQUEST_RULES + """
**This call** takes back a request still `pending`. `sync_id` is this call's own
UUIDv7; `request_sync_id` the request's. Withdrawing one already withdrawn
answers the same; a decided or closed one is `request_closed`. Any tablet at the
order's station may withdraw.
""" + _error_table(*_REQUEST_WITHDRAW_ERRORS)

_REQUEST_STATUS_DESCRIPTION = _REQUEST_RULES + """
**This call** reads where they stand: send the `order_ids` (1-100) and get every
request on those orders, newest first, with the approved rule and who decided
where. Orders with none are simply absent. Read-only.
"""


class OrderRequestView(_OperatorView):
    """POST /api/v1/{app}/orders/requests -- operator app only."""

    @extend_schema(
        tags=["Operator Requests"],
        summary="Ask a manager about a running order (discount)",
        description=_REQUEST_CREATE_DESCRIPTION,
        request=envelope_request("OrderRequestEnvelope", OrderRequestCreateRequest),
        responses=envelope_responses(
            (200, "ok", "Request sent.", _REQUEST_PENDING_SAMPLE),
            (200, "duplicate", "Already recorded.", _REQUEST_PENDING_SAMPLE),
            (400, "invalid_request", "kind must be one of: discount.",
             {"errors": {"kind": "must be one of: discount"}}),
            *_error_rows(*_REQUEST_CREATE_ERRORS),
            *_COMMON,
        ),
    )
    def post(self, request, app):
        branch, refused = self.station(request, app)
        if refused:
            return refused
        _, request_data = request_parts(request)
        form = OrderRequestCreateRequest(data=request_data)
        form.is_valid(raise_exception=True)
        values = form.validated_data
        values["requested_at"] = timezone.make_aware(values["requested_at"], zone_for(branch.company))
        try:
            data, done = order_requests.request_discount(request.auth, values, request_data)
        except services.OrderRefused as refusal:
            return _refused(refusal)
        return envelope("ok", "Request sent.", data) if done else envelope("duplicate", "Already recorded.", data)


class OrderRequestWithdrawView(_OperatorView):
    """POST /api/v1/{app}/orders/requests/withdraw -- operator app only."""

    @extend_schema(
        tags=["Operator Requests"],
        summary="Take back a request still waiting",
        description=_REQUEST_WITHDRAW_DESCRIPTION,
        request=envelope_request("OrderRequestWithdrawEnvelope", OrderRequestWithdrawRequest),
        responses=envelope_responses(
            (200, "ok", "Request withdrawn.", {**_REQUEST_PENDING_SAMPLE, "status": "withdrawn"}),
            (200, "duplicate", "Already recorded.", {**_REQUEST_PENDING_SAMPLE, "status": "withdrawn"}),
            (400, "invalid_request", "request_sync_id is required.",
             {"errors": {"request_sync_id": "is required"}}),
            *_error_rows(*_REQUEST_WITHDRAW_ERRORS),
            *_COMMON,
        ),
    )
    def post(self, request, app):
        branch, refused = self.station(request, app)
        if refused:
            return refused
        _, request_data = request_parts(request)
        form = OrderRequestWithdrawRequest(data=request_data)
        form.is_valid(raise_exception=True)
        values = form.validated_data
        values["withdrawn_at"] = timezone.make_aware(values["withdrawn_at"], zone_for(branch.company))
        try:
            data, done = order_requests.withdraw(request.auth, values, request_data)
        except services.OrderRefused as refusal:
            return _refused(refusal)
        return (envelope("ok", "Request withdrawn.", data) if done
                else envelope("duplicate", "Already recorded.", data))


class OrderRequestStatusView(_OperatorView):
    """POST /api/v1/{app}/orders/requests/status -- operator app only."""

    @extend_schema(
        tags=["Operator Requests"],
        summary="Where requests on these orders stand",
        description=_REQUEST_STATUS_DESCRIPTION,
        request=envelope_request("OrderRequestStatusEnvelope", OrderRequestStatusRequest),
        responses=envelope_responses(
            (200, "ok", "Requests.", {"requests": [_REQUEST_APPROVED_SAMPLE, _REQUEST_AMOUNT_SAMPLE]}),
            (400, "invalid_request", "send at least one order_id.",
             {"errors": {"order_ids": "send at least one order_id"}}),
            *_COMMON,
        ),
    )
    def post(self, request, app):
        branch, refused = self.station(request, app)
        if refused:
            return refused
        _, request_data = request_parts(request)
        form = OrderRequestStatusRequest(data=request_data)
        form.is_valid(raise_exception=True)
        zone = zone_for(branch.company)
        rows = order_requests.requests_for_orders(branch.company, form.validated_data["order_ids"])
        return envelope("ok", "Requests.", {"requests": [order_requests.request_json(r, zone) for r in rows]})


# -- Manager app --------------------------------------------------------------------

REQUEST_PAGE = "rental.request"

_MANAGER_SAMPLE = {
    **_REQUEST_PENDING_SAMPLE, "branch_id": 3, "station": "Creek Park 1", "requested_by": "Rashed K",
    "order": {
        "order_no": "DUBPP60182000231", "status": "active", "customer_name": "Ahmed Al Mansoori",
        "customer_mobile": "971501234567", "booked_at": "2026-10-08 16:00:00", "advance_paid": "100.000",
        "vehicles": [
            {"vehicle": "MO 41", "identifier": "VB1241", "vehicle_type": "Monaco", "status": "active",
             "start_time": "2026-10-08 16:00:00", "expected_end_time": "2026-10-08 17:00:00",
             "end_time": None, "minutes_run": 40},
            {"vehicle": "MO 42", "identifier": "VB1242", "vehicle_type": "Monaco", "status": "returned",
             "start_time": "2026-10-08 16:00:00", "expected_end_time": "2026-10-08 16:30:00",
             "end_time": "2026-10-08 16:31:00", "minutes_run": 31},
        ],
    },
}
_MANAGER_DECIDED = {
    "decided_by": "Sara K", "decided_channel": "manager", "decided_at": "2026-10-08 16:43:12",
}

_MANAGER_COMMON = (
    (401, "not_authenticated", "Sign in first.", {}),
    (403, "wrong_channel", "Not allowed on this app.", {}),
    (403, "forbidden", "You do not have permission to do that.", {}),
    SERVER_ERROR,
)

_MANAGER_RULES = _REQUEST_RULES + """
**Manager app.** Requests from **every station** of your company. Deciding needs
the **Approve** right on the Requests page (listing needs **Read**); the
decision is recorded against you, with `decided_channel` / `revoked_channel`
`manager`. A request already decided -- here or on the web -- answers
`request_decided`.
"""

_MANAGER_LIST_DESCRIPTION = _MANAGER_RULES + """
**This call** lists requests, newest first, 50 a page (`page`, from 1). Send
`"pending": true` for only those waiting, and `branch_id` for one station. Each
row carries the order: customer, advance paid so far, and every vehicle out or
back with the minutes run so far (`minutes_run`; for one still out, up to now).
"""

_MANAGER_APPROVE_DESCRIPTION = _MANAGER_RULES + """
**This call** approves a pending discount request as a rule: `discount_type`
`percent` (`discount_value` above 0, at most 100, 2 decimals) or `amount` (AED
above 0, 3 decimals). `note` optional.
"""

_MANAGER_REJECT_DESCRIPTION = _MANAGER_RULES + """
**This call** rejects a pending request. `note` optional -- the tablet sees it
as `decision_note`.
"""

_MANAGER_REVOKE_DESCRIPTION = _MANAGER_RULES + """
**This call** takes back an **approved** request while its order still runs
(`request_not_approved`, `order_closed`). The tablet sees `revoked`; the settle
then refuses that discount. `note` optional (`revoke_note`).
"""


class _ManagerView(APIView):
    """Manager app only, with the Requests page right; scoped to the
    companies the user may see."""

    authentication_classes = [AppJWTAuthentication]
    permission_classes = [IsAuthenticated]
    action = "approve"

    def refusal(self, request, app):
        # The session too, not only the URL: an operator's token cannot decide.
        if app != Channel.MANAGER or request.auth.channel != Channel.MANAGER:
            return envelope("wrong_channel", "Not allowed on this app.", http_status=403)
        if not has_permission(request.user, REQUEST_PAGE, self.action):
            return envelope("forbidden", "You do not have permission to do that.", http_status=403)
        return None

    def company_ids(self, request):
        return list(companies_for(request.user).values_list("id", flat=True))

    def decide(self, request, app, form_class, act, message):
        refused = self.refusal(request, app)
        if refused:
            return refused
        _, request_data = request_parts(request)
        form = form_class(data=request_data)
        form.is_valid(raise_exception=True)
        try:
            req = act(form.validated_data, self.company_ids(request))
        except order_requests.RequestRefused as refusal:
            return envelope(refusal.code, refusal.message, http_status=refusal.status)
        row = order_requests.manager_row(req.pk)
        return envelope("ok", message, order_requests.manager_json(row, zone_for(row.company), timezone.now()))


_DECISION_ERRORS = (
    (400, "invalid_request", "request_sync_id is required.", {"errors": {"request_sync_id": "is required"}}),
    (404, "unknown_request", "No request with that id.", {}),
    (409, "request_decided", "This request is already approved.", {}),
)
_ORDER_CLOSED = (409, "order_closed", "This order is already completed or cancelled.", {})


class ManagerRequestListView(_ManagerView):
    """POST /api/v1/{app}/requests/list -- manager app only."""

    action = "read"

    @extend_schema(
        tags=["Manager Requests"],
        summary="Requests from every station, newest first",
        description=_MANAGER_LIST_DESCRIPTION,
        request=envelope_request("ManagerRequestListEnvelope", ManagerRequestListRequest),
        responses=envelope_responses(
            (200, "ok", "Requests.", {"requests": [_MANAGER_SAMPLE], "page": 1, "pages": 1, "total": 1}),
            (400, "invalid_request", "page must be 1 or more.", {"errors": {"page": "must be 1 or more"}}),
            *_MANAGER_COMMON,
        ),
    )
    def post(self, request, app):
        refused = self.refusal(request, app)
        if refused:
            return refused
        _, request_data = request_parts(request)
        form = ManagerRequestListRequest(data=request_data)
        form.is_valid(raise_exception=True)
        values = form.validated_data
        rows, page = order_requests.manager_list(
            self.company_ids(request), pending_only=values["pending"], branch_id=values.get("branch_id"),
            page=values["page"],
        )
        return envelope("ok", "Requests.", {"requests": rows, "page": page.number,
                                             "pages": page.paginator.num_pages, "total": page.paginator.count})


class ManagerRequestApproveView(_ManagerView):
    """POST /api/v1/{app}/requests/approve -- manager app only."""

    @extend_schema(
        tags=["Manager Requests"],
        summary="Approve a discount request as a % or an AED amount",
        description=_MANAGER_APPROVE_DESCRIPTION,
        request=envelope_request("ManagerRequestApproveEnvelope", ManagerRequestApproveRequest),
        responses=envelope_responses(
            (200, "ok", "Request approved.", {**_MANAGER_SAMPLE, **_MANAGER_DECIDED, "status": "approved",
                                               "discount_type": "percent", "discount_value": "10.00"}),
            *_DECISION_ERRORS, _ORDER_CLOSED, *_MANAGER_COMMON,
        ),
    )
    def post(self, request, app):
        return self.decide(request, app, ManagerRequestApproveRequest, lambda v, companies: order_requests.approve(
            request.user, v["request_sync_id"], companies, DecisionChannel.MANAGER,
            v["discount_type"], v["discount_value"], v.get("note", "")), "Request approved.")


class ManagerRequestRejectView(_ManagerView):
    """POST /api/v1/{app}/requests/reject -- manager app only."""

    @extend_schema(
        tags=["Manager Requests"],
        summary="Reject a request",
        description=_MANAGER_REJECT_DESCRIPTION,
        request=envelope_request("ManagerRequestRejectEnvelope", ManagerRequestDecisionRequest),
        responses=envelope_responses(
            (200, "ok", "Request rejected.", {**_MANAGER_SAMPLE, **_MANAGER_DECIDED, "status": "rejected"}),
            *_DECISION_ERRORS, *_MANAGER_COMMON,
        ),
    )
    def post(self, request, app):
        return self.decide(request, app, ManagerRequestDecisionRequest, lambda v, companies: order_requests.reject(
            request.user, v["request_sync_id"], companies, DecisionChannel.MANAGER, v.get("note", "")),
            "Request rejected.")


class ManagerRequestRevokeView(_ManagerView):
    """POST /api/v1/{app}/requests/revoke -- manager app only."""

    @extend_schema(
        tags=["Manager Requests"],
        summary="Revoke an approved request while the order runs",
        description=_MANAGER_REVOKE_DESCRIPTION,
        request=envelope_request("ManagerRequestRevokeEnvelope", ManagerRequestDecisionRequest),
        responses=envelope_responses(
            (200, "ok", "Approval revoked.", {
                **_MANAGER_SAMPLE, **_MANAGER_DECIDED, "status": "revoked", "discount_type": "percent",
                "discount_value": "10.00", "revoked_by": "Sara K", "revoked_channel": "manager",
                "revoked_at": "2026-10-08 16:50:00"}),
            (400, "invalid_request", "request_sync_id is required.",
             {"errors": {"request_sync_id": "is required"}}),
            (404, "unknown_request", "No request with that id.", {}),
            (409, "request_not_approved", "Only an approved request can be revoked.", {}),
            _ORDER_CLOSED, *_MANAGER_COMMON,
        ),
    )
    def post(self, request, app):
        return self.decide(request, app, ManagerRequestDecisionRequest, lambda v, companies: order_requests.revoke(
            request.user, v["request_sync_id"], companies, DecisionChannel.MANAGER, v.get("note", "")),
            "Approval revoked.")
