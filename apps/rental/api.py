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
from drf_spectacular.utils import extend_schema
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.devices import services as devices_services
from apps.devices.models import BillKind
from apps.portal.authentication import AppJWTAuthentication
from apps.rental import services
from apps.rental.models import OrderItemStatus
from apps.rental.serializers import (
    CustomerCreateRequest,
    CustomerLookupRequest,
    OrderCreateRequest,
    OrderDetailRequest,
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


def order_json(order, *, zone, next_order_number):
    """The one shape every order call answers with (order_lifecycle_design.md
    3): the order, its customer, its totals, every item and every payment.
    Times are company time, `YYYY-MM-DD HH:MM:SS`, as the tablet sends them.
    `next_order_number` is the asking tablet's receipt counter after the call."""
    items = list(order.items.select_related("vehicle").order_by("start_time", "created_on"))
    payments = list(order.payments.select_related("mode").order_by("paid_at", "created_on"))
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
        "discount": {
            "claim_id": str(order.discount_claim_id), "percentage": _money(order.discount_percentage),
            "amount": _money(order.discount_amount),
        } if order.discount_claim_id else None,
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
        "next_order_number": next_order_number,
    }


def _refused(refusal):
    return envelope(refusal.code, refusal.message, {"retry": refusal.retry}, http_status=refusal.status)


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
         "fare_id": 88, "offer_id": None, "package_minutes": 60,
         "start_time": "2026-10-02 16:00:00", "expected_end_time": "2026-10-02 17:00:00", "end_time": None,
         "base_fare": "50.00", "overtime_amount": None, "total_amount": None,
         "replaced_item_id": None, "reason": ""},
        {"sync_id": "01923e1c-0a13-7b22-8c33-d4e5f6a7b8c9", "status": "active",
         "vehicle": {"id": 3102, "name": "DC 02", "identifier": "VB0874"},
         "fare_id": 88, "offer_id": None, "package_minutes": 60,
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
    "next_order_number": 232,
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
| item `overtime_amount`, `total_amount` (= base fare + overtime) | that vehicle is returned |
| order `subtotal`, `discount`, `tax_percentage`, `tax_amount`, `rounding_adjustment`, `net_amount`, `balance_due` | the order is settled |
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
