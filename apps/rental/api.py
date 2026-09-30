"""The operator app's customer APIs -- views only translate HTTP; the rules
are in apps/rental/services.py (customer_by_phone, create_customer).

POST /api/v1/{app}/customers/lookup: given a phone, the matching customer's
data -- so the operator app can prefill a rental for a returning customer
instead of retyping their details.

POST /api/v1/{app}/customers/create: register a new customer from the till,
online or from an offline queue.
"""

from drf_spectacular.utils import extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.portal.authentication import AppJWTAuthentication
from apps.rental import services
from apps.rental.serializers import CustomerCreateRequest, CustomerLookupRequest
from core.api import envelope, request_parts, session_station
from core.enums import Channel
from core.schema import SERVER_ERROR, envelope_request, envelope_responses

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
